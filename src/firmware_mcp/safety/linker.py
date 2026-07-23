"""Exact file-backed flash image parsing and canonical verification evidence."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from elftools.common.exceptions import ELFError  # type: ignore[import-untyped]
from elftools.elf.elffile import ELFFile  # type: ignore[import-untyped]

from firmware_mcp.services.physical_types import AddressRange, RegionError


class LinkerEvidenceError(ValueError):
    """A flash artifact is malformed or lacks exact file-backed bytes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FlashImage:
    """Exact programmed bytes from an ELF/AXF PT_LOAD or Intel HEX artifact."""

    path: Path
    bytes_by_address: dict[int, int]
    ranges: tuple[tuple[int, int], ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class ElfLoadSegment:
    """One structurally validated ``PT_LOAD`` segment from immutable ELF bytes."""

    index: int
    address: int
    file_size: int
    memory_size: int
    flags: int
    data: bytes


def _image_ranges(image: dict[int, int]) -> tuple[tuple[int, int], ...]:
    addresses = sorted(image)
    if not addresses:
        return ()
    ranges: list[tuple[int, int]] = []
    start = previous = addresses[0]
    for address in addresses[1:]:
        if address != previous + 1:
            ranges.append((start, previous + 1))
            start = address
        previous = address
    ranges.append((start, previous + 1))
    return tuple(ranges)


def canonical_image_digest(bytes_by_address: dict[int, int]) -> str:
    """Hash sorted 64-bit physical-address/byte pairs for readback evidence."""

    digest = sha256()
    for address in sorted(bytes_by_address):
        value = bytes_by_address[address]
        if address < 0 or address >= 1 << 64 or not 0 <= value <= 0xFF:
            raise LinkerEvidenceError("build/image-range", "Image has an invalid address or byte")
        digest.update(address.to_bytes(8, "big", signed=False))
        digest.update(bytes((value,)))
    return digest.hexdigest()


def parse_flash_image(path: Path) -> FlashImage:
    """Parse exact file-backed bytes; sparse holes are deliberately absent."""

    resolved = path.expanduser().resolve()
    return parse_flash_image_bytes(resolved, resolved.read_bytes())


def parse_flash_image_bytes(path: Path, payload: bytes) -> FlashImage:
    """Parse one already captured artifact snapshot with its original suffix label."""

    resolved = path.expanduser().resolve()
    suffix = resolved.suffix.casefold()
    if suffix in {".elf", ".axf"}:
        image = _read_elf(resolved, payload)
    elif suffix == ".hex":
        image, _ = _read_hex(resolved, payload)
    else:
        raise LinkerEvidenceError(
            "build/unsupported-image", "Flash image must be an ELF, AXF, or Intel HEX artifact"
        )
    if not image:
        raise LinkerEvidenceError("build/flash-content-missing", "Flash image contains no bytes")
    return FlashImage(resolved, image, _image_ranges(image), canonical_image_digest(image))


def _elf_load_segments(path: Path, payload: bytes | None = None) -> tuple[ElfLoadSegment, ...]:
    """Parse and validate all ``PT_LOAD`` records from one immutable snapshot.

    A zero-file-byte segment contributes no file-backed authority, but its
    file offset and complete physical ``p_memsz`` reservation still have to be
    structurally valid.  Every ELF consumer uses this one walk so a malformed
    header cannot be accepted by one authority and rejected by another.
    """

    if payload is None and not path.is_file():
        raise LinkerEvidenceError("build/elf-missing", f"ELF artifact does not exist: {path}")

    try:
        raw = path.read_bytes() if payload is None else bytes(payload)
        elf = ELFFile(BytesIO(raw))
        if elf.elfclass not in {32, 64}:
            raise LinkerEvidenceError("build/elf-class", "ELF class must be 32 or 64 bit")
        segments: list[ElfLoadSegment] = []
        for index, segment in enumerate(elf.iter_segments()):
            if segment.header.p_type != "PT_LOAD":
                continue
            raw_fields = {
                "p_offset": segment.header.p_offset,
                "p_filesz": segment.header.p_filesz,
                "p_memsz": segment.header.p_memsz,
                "p_paddr": segment.header.p_paddr,
                "p_flags": segment.header.p_flags,
            }
            if any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value >= 1 << 64
                for value in raw_fields.values()
            ):
                raise LinkerEvidenceError(
                    "build/segment-field", f"PT_LOAD segment {index} has invalid header fields"
                )
            offset = raw_fields["p_offset"]
            filesz = raw_fields["p_filesz"]
            memsz = raw_fields["p_memsz"]
            address = raw_fields["p_paddr"]
            flags = raw_fields["p_flags"]
            if filesz > memsz:
                raise LinkerEvidenceError(
                    "build/segment-size", f"Malformed PT_LOAD segment {index}"
                )
            file_end = offset + filesz
            if file_end > 1 << 64 or file_end > len(raw):
                raise LinkerEvidenceError(
                    "build/segment-offset",
                    f"PT_LOAD segment {index} file bytes extend beyond the artifact immutable snapshot",
                )
            if address + memsz > 1 << 64:
                raise LinkerEvidenceError(
                    "build/segment-range",
                    f"PT_LOAD segment {index} exceeds the unsigned 64-bit address space",
                )
            if memsz:
                try:
                    AddressRange.from_start_size(address, memsz)
                except RegionError as exc:
                    raise LinkerEvidenceError(
                        "build/segment-range", f"Invalid PT_LOAD segment {index}: {exc}"
                    ) from exc
            data = raw[offset:file_end]
            if len(data) != filesz:
                raise LinkerEvidenceError(
                    "build/segment-data",
                    f"PT_LOAD segment {index} has incomplete immutable file data",
                )
            segments.append(ElfLoadSegment(index, address, filesz, memsz, flags, data))
        if not segments:
            raise LinkerEvidenceError(
                "build/no-loadable-segments", "ELF contains no PT_LOAD segments"
            )
        return tuple(segments)
    except LinkerEvidenceError:
        raise
    except (OSError, ELFError, ValueError, TypeError, KeyError) as exc:
        raise LinkerEvidenceError(
            "build/elf-malformed", f"Cannot parse ELF load segments: {exc}"
        ) from exc


def file_backed_elf_ranges(path: Path, payload: bytes | None = None) -> tuple[tuple[int, int], ...]:
    """Return every nonempty file-backed ``PT_LOAD`` physical range."""

    return tuple(
        (segment.address, segment.address + segment.file_size)
        for segment in _elf_load_segments(path, payload)
        if segment.file_size
    )


def executable_elf_ranges(path: Path, payload: bytes | None = None) -> tuple[tuple[int, int], ...]:
    """Return only exact file-backed PF_X physical load ranges.

    ``payload`` lets guarded callers parse the immutable byte snapshot they
    hash-checked, rather than reopening a mutable selected path.
    """

    return tuple(
        (segment.address, segment.address + segment.file_size)
        for segment in _elf_load_segments(path, payload)
        if segment.file_size and segment.flags & 1
    )


def _read_elf(path: Path, payload: bytes | None = None) -> dict[int, int]:
    try:
        image: dict[int, int] = {}
        for segment in _elf_load_segments(path, payload):
            for offset, value in enumerate(segment.data):
                address = segment.address + offset
                prior = image.get(address)
                if prior is not None and prior != value:
                    raise LinkerEvidenceError(
                        "build/segment-overlap",
                        f"PT_LOAD segments disagree at address 0x{address:x}",
                    )
                image[address] = value
    except LinkerEvidenceError:
        raise
    except (OSError, ELFError, ValueError, TypeError, KeyError) as exc:
        raise LinkerEvidenceError("build/elf-malformed", f"Cannot parse ELF: {exc}") from exc
    return image


def _hex_record(line: str, *, path: Path, line_number: int) -> bytes:
    if not line.startswith(":") or len(line) < 11 or (len(line) - 1) % 2:
        raise LinkerEvidenceError(
            "build/hex-malformed", f"Malformed Intel HEX record at {path.name}:{line_number}"
        )
    try:
        record = bytes.fromhex(line[1:])
    except ValueError as exc:
        raise LinkerEvidenceError(
            "build/hex-malformed", f"Malformed Intel HEX record at {path.name}:{line_number}"
        ) from exc
    if len(record) != record[0] + 5 or sum(record) & 0xFF:
        raise LinkerEvidenceError(
            "build/hex-checksum", f"Invalid Intel HEX length/checksum at {path.name}:{line_number}"
        )
    return record


def _address_ranges(addresses: list[int]) -> tuple[AddressRange, ...]:
    if not addresses:
        return ()
    ranges: list[AddressRange] = []
    start = previous = addresses[0]
    for address in addresses[1:]:
        if address != previous + 1:
            ranges.append(AddressRange(start, previous + 1))
            start = address
        previous = address
    ranges.append(AddressRange(start, previous + 1))
    return tuple(ranges)


def _read_hex(
    path: Path, payload: bytes | None = None
) -> tuple[dict[int, int], tuple[AddressRange, ...]]:
    """Strictly parse data records from an Intel HEX artifact."""

    if not path.is_file():
        raise LinkerEvidenceError("build/hex-missing", f"HEX artifact does not exist: {path}")
    try:
        lines = (
            path.read_text(encoding="ascii") if payload is None else bytes(payload).decode("ascii")
        ).splitlines()
    except (OSError, UnicodeError) as exc:
        raise LinkerEvidenceError(
            "build/hex-unreadable", f"Cannot read HEX artifact: {exc}"
        ) from exc
    image: dict[int, int] = {}
    base = 0
    eof_seen = False
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line:
            continue
        if eof_seen:
            raise LinkerEvidenceError("build/hex-after-eof", "Intel HEX contains data after EOF")
        record = _hex_record(line, path=path, line_number=line_number)
        length = record[0]
        offset = int.from_bytes(record[1:3], "big")
        record_type = record[3]
        data = record[4 : 4 + length]
        if record_type == 0:
            start = base + offset
            try:
                AddressRange.from_start_size(start, length)
            except RegionError as exc:
                raise LinkerEvidenceError(
                    "build/hex-range", f"Invalid HEX data range: {exc}"
                ) from exc
            for index, value in enumerate(data):
                address = start + index
                prior = image.get(address)
                if prior is not None and prior != value:
                    raise LinkerEvidenceError(
                        "build/hex-overlap", f"Intel HEX records disagree at address 0x{address:x}"
                    )
                image[address] = value
        elif record_type == 1:
            if length != 0 or offset != 0:
                raise LinkerEvidenceError("build/hex-malformed", "Malformed Intel HEX EOF record")
            eof_seen = True
        elif record_type == 2:
            if length != 2 or offset != 0:
                raise LinkerEvidenceError(
                    "build/hex-malformed", "Malformed Intel HEX segment-address record"
                )
            base = int.from_bytes(data, "big") << 4
        elif record_type == 4:
            if length != 2 or offset != 0:
                raise LinkerEvidenceError(
                    "build/hex-malformed", "Malformed Intel HEX linear-address record"
                )
            base = int.from_bytes(data, "big") << 16
        elif record_type in {3, 5}:
            if length != 4 or offset != 0:
                raise LinkerEvidenceError(
                    "build/hex-malformed", "Malformed Intel HEX start-address record"
                )
        else:
            raise LinkerEvidenceError(
                "build/hex-record-type", f"Unsupported Intel HEX record type {record_type}"
            )
    if not eof_seen or not image:
        raise LinkerEvidenceError(
            "build/hex-incomplete", "Intel HEX requires an EOF record and non-empty image data"
        )
    addresses = sorted(image)
    return image, _address_ranges(addresses)
