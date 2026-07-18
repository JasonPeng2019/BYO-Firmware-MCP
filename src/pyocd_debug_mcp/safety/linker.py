"""Build-owned partition and loadable-segment extraction from ELF/linker maps."""

from __future__ import annotations

import re
from hashlib import sha256
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Final

from elftools.common.exceptions import ELFError  # type: ignore[import-untyped]
from elftools.elf.elffile import ELFFile  # type: ignore[import-untyped]
from elftools.elf.sections import SymbolTableSection  # type: ignore[import-untyped]

from pyocd_debug_mcp.safety.regions import AddressRange, RegionError

_CONFIGURATION_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_MAP_ASSIGNMENT: Final = re.compile(
    r"^\s*(?P<name>[A-Za-z_.$][A-Za-z0-9_.$]*)\s*=\s*"
    r"(?P<value>0[xX][0-9A-Fa-f]+|[0-9]+)\s*;?\s*$"
)
_MAP_ADDRESS_NAME: Final = re.compile(
    r"^\s*(?P<value>0[xX][0-9A-Fa-f]+)\s+"
    r"(?P<name>[A-Za-z_.$][A-Za-z0-9_.$]*)\s*$"
)
_GNU_MAP_EVALUATED_SYMBOL: Final = re.compile(
    r"^\s*(?P<value>0[xX][0-9A-Fa-f]+)\s+"
    r"(?:(?:PROVIDE|PROVIDE_HIDDEN)\s*\(\s*)?"
    r"(?P<name>[A-Za-z_.$][A-Za-z0-9_.$]*)"
    r"(?:\s*=\s*.+?)?\s*\)?\s*$"
)
_GNU_MAP_LITERAL_PROVIDE: Final = re.compile(
    r"^\s*(?:PROVIDE|PROVIDE_HIDDEN)\s*\(\s*"
    r"(?P<name>[A-Za-z_.$][A-Za-z0-9_.$]*)\s*=\s*"
    r"(?P<value>0[xX][0-9A-Fa-f]+|[0-9]+)\s*\)\s*;?\s*$"
)

_FLASH_SYMBOLS: Final = {
    "application": (
        ("__app_partition_start", "__app_partition_end"),
        ("__application_start", "__application_end"),
        ("__rom_region_start", "__rom_region_end"),
    ),
    "bootloader": (
        ("__bootloader_partition_start", "__bootloader_partition_end"),
        ("__bootloader_start", "__bootloader_end"),
        ("__rom_region_start", "__rom_region_end"),
    ),
}
_RAM_SYMBOLS: Final = (
    ("_image_ram_start", "_image_ram_end"),
    ("__ram_region_start", "__ram_region_end"),
    ("__kernel_ram_start", "__kernel_ram_end"),
)
_VECTOR_SYMBOLS: Final = (
    "_vector_start",       # Zephyr
    "_vector_table",
    "__vector_table",
    "__Vectors",          # CMSIS / Arm toolchains
    "__Vectors_Start",
    "g_pfnVectors",       # STM32Cube GCC
    "__isr_vector",
    "_vectors",
)
_INTERESTING_SYMBOLS: Final = frozenset(
    {name for pairs in (*_FLASH_SYMBOLS.values(), _RAM_SYMBOLS) for pair in pairs for name in pair}
    | {"__rom_region_start", "__rom_region_size"}
    | set(_VECTOR_SYMBOLS)
)


class LinkerEvidenceError(ValueError):
    """Build evidence is absent, malformed, ambiguous, or internally conflicting."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BuildRole(str, Enum):
    APPLICATION = "application"
    BOOTLOADER = "bootloader"


def elf_requires_vector_table(path: Path) -> bool:
    """Return the bundled ELF provider's architecture-specific startup requirement."""

    try:
        with path.expanduser().resolve().open("rb") as stream:
            architecture = ELFFile(stream).get_machine_arch()
    except (OSError, ELFError) as exc:
        raise LinkerEvidenceError(
            "safety/linker-malformed-elf", f"ELF could not be parsed: {path}"
        ) from exc
    return architecture in {"ARM", "AArch64"}


@dataclass(frozen=True, slots=True)
class BuildArtifactSelection:
    configuration_id: str
    role: BuildRole
    elf_path: Path
    linker_map_path: Path | None = None
    hex_path: Path | None = None

    def __post_init__(self) -> None:
        if _CONFIGURATION_ID.fullmatch(self.configuration_id) is None:
            raise LinkerEvidenceError(
                "build/invalid-configuration-id",
                "configuration_id must be a stable 1-128 character identifier",
            )
        if (
            not isinstance(self.elf_path, Path)
            or (self.linker_map_path is not None and not isinstance(self.linker_map_path, Path))
            or (self.hex_path is not None and not isinstance(self.hex_path, Path))
        ):
            raise LinkerEvidenceError(
                "build/path-type", "ELF, HEX, and linker-map paths must be pathlib.Path values"
            )


@dataclass(frozen=True, slots=True)
class LoadableSegment:
    index: int
    load_range: AddressRange | None
    runtime_range: AddressRange
    file_size: int
    memory_size: int
    readable: bool
    writable: bool
    executable: bool


@dataclass(frozen=True, slots=True)
class BuildProvenance:
    """A content-addressed build artifact used to derive the evidence."""

    artifact_kind: str
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class BuildEvidence:
    configuration_id: str | None
    role: BuildRole | None
    artifact_present: bool
    flash_available: bool
    flash_partition: AddressRange | None
    ram_partitions: tuple[AddressRange, ...]
    loadable_segments: tuple[LoadableSegment, ...]
    hex_ranges: tuple[AddressRange, ...]
    entry_point: int | None
    vector_table: int | None
    elf_path: Path | None
    linker_map_path: Path | None
    hex_path: Path | None
    provenance: tuple[BuildProvenance, ...]
    reason: str | None = None

    @classmethod
    def absent(cls) -> BuildEvidence:
        return cls(
            None,
            None,
            False,
            False,
            None,
            (),
            (),
            (),
            None,
            None,
            None,
            None,
            None,
            (),
            (
                "No selected build/link artifacts are present. Non-flash safety evidence may "
                "continue, but application and bootloader flashing remain unavailable."
            ),
        )


def select_build_configuration(
    configurations: tuple[BuildArtifactSelection, ...] | list[BuildArtifactSelection],
    configuration_id: str | None,
) -> BuildArtifactSelection | None:
    """Select exactly one build without accepting partition ranges from the caller."""

    candidates = tuple(configurations)
    if not candidates:
        return None
    identifiers = [item.configuration_id for item in candidates]
    if len(set(identifiers)) != len(identifiers):
        raise LinkerEvidenceError(
            "build/duplicate-configuration", "build configuration identifiers must be unique"
        )
    if configuration_id is not None:
        selected = next(
            (item for item in candidates if item.configuration_id == configuration_id), None
        )
        if selected is None:
            raise LinkerEvidenceError(
                "build/unknown-configuration",
                f"Unknown build configuration '{configuration_id}'",
            )
        return selected
    if len(candidates) == 1:
        return candidates[0]
    raise LinkerEvidenceError(
        "build/selection-required",
        "More than one build configuration is present; select one by its identifier.",
    )


def _parse_map(path: Path) -> dict[str, int]:
    if not path.is_file():
        raise LinkerEvidenceError("build/map-missing", f"Linker map does not exist: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise LinkerEvidenceError("build/map-unreadable", f"Cannot read linker map: {exc}") from exc
    values: dict[str, int] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        # GNU ld map files (including Zephyr's) print the evaluated value first,
        # followed by the symbol and often a non-literal linker expression.  The
        # leading value is the linker's resolved result and is the value we need;
        # requiring the expression itself to be a numeric literal rejected normal
        # Zephyr rows such as ``0x10000 __rom_region_end = (...)``.
        match = (
            _MAP_ASSIGNMENT.fullmatch(line)
            or _MAP_ADDRESS_NAME.fullmatch(line)
            or _GNU_MAP_EVALUATED_SYMBOL.fullmatch(line)
            or _GNU_MAP_LITERAL_PROVIDE.fullmatch(line)
        )
        if match is None:
            if _looks_like_malformed_safety_symbol(line):
                raise LinkerEvidenceError(
                    "build/map-malformed",
                    f"Malformed safety symbol at {path.name}:{line_number}",
                )
            continue
        name = match.group("name")
        if name not in _INTERESTING_SYMBOLS:
            continue
        value = int(match.group("value"), 0)
        prior = values.get(name)
        if prior is not None and prior != value:
            raise LinkerEvidenceError(
                "build/map-symbol-conflict", f"Linker map gives conflicting values for {name}"
            )
        values[name] = value
    return values


def _looks_like_malformed_safety_symbol(line: str) -> bool:
    """Distinguish a broken definition from harmless section-name references."""

    for name in _INTERESTING_SYMBOLS:
        escaped = re.escape(name)
        if re.match(rf"^\s*{escaped}(?:\s|=|\?\?\?|$)", line):
            return True
        if re.match(
            rf"^\s*0[xX][0-9A-Fa-f]+\s+"
            rf"(?:(?:PROVIDE|PROVIDE_HIDDEN)\s*\(\s*)?{escaped}(?:\s|=|\)|$)",
            line,
        ):
            return True
    return False


def _read_elf(
    path: Path,
) -> tuple[dict[str, int], tuple[LoadableSegment, ...], int, dict[int, int]]:
    if not path.is_file():
        raise LinkerEvidenceError("build/elf-missing", f"ELF artifact does not exist: {path}")
    try:
        handle: BinaryIO
        with path.open("rb") as handle:
            elf = ELFFile(handle)
            if elf.elfclass not in {32, 64}:
                raise LinkerEvidenceError("build/elf-class", "ELF class must be 32 or 64 bit")
            symbols: dict[str, int] = {}
            for section in elf.iter_sections():
                if not isinstance(section, SymbolTableSection):
                    continue
                for symbol in section.iter_symbols():
                    name = symbol.name
                    if name not in _INTERESTING_SYMBOLS:
                        continue
                    value = int(symbol.entry.st_value)
                    prior = symbols.get(name)
                    if prior is not None and prior != value:
                        raise LinkerEvidenceError(
                            "build/elf-symbol-conflict",
                            f"ELF gives conflicting values for {name}",
                        )
                    symbols[name] = value

            segments: list[LoadableSegment] = []
            image: dict[int, int] = {}
            for index, segment in enumerate(elf.iter_segments()):
                if segment.header.p_type != "PT_LOAD":
                    continue
                file_size = int(segment.header.p_filesz)
                memory_size = int(segment.header.p_memsz)
                if memory_size <= 0 or file_size < 0 or file_size > memory_size:
                    raise LinkerEvidenceError(
                        "build/segment-size", f"Malformed PT_LOAD segment {index}"
                    )
                try:
                    runtime = AddressRange.from_start_size(int(segment.header.p_vaddr), memory_size)
                    load = (
                        AddressRange.from_start_size(int(segment.header.p_paddr), file_size)
                        if file_size
                        else None
                    )
                except RegionError as exc:
                    raise LinkerEvidenceError(
                        "build/segment-range", f"Invalid PT_LOAD segment {index}: {exc}"
                    ) from exc
                flags = int(segment.header.p_flags)
                load_address = int(segment.header.p_paddr)
                data = bytes(segment.data())
                if len(data) != file_size:
                    raise LinkerEvidenceError(
                        "build/segment-data", f"PT_LOAD segment {index} has incomplete file data"
                    )
                for offset, value in enumerate(data):
                    address = load_address + offset
                    prior = image.get(address)
                    if prior is not None and prior != value:
                        raise LinkerEvidenceError(
                            "build/segment-overlap",
                            f"PT_LOAD segments disagree at address 0x{address:x}",
                        )
                    image[address] = value
                segments.append(
                    LoadableSegment(
                        index,
                        load,
                        runtime,
                        file_size,
                        memory_size,
                        bool(flags & 4),
                        bool(flags & 2),
                        bool(flags & 1),
                    )
                )
            if not segments:
                raise LinkerEvidenceError(
                    "build/no-loadable-segments", "ELF contains no PT_LOAD segments"
                )
            entry_point = int(elf.header.e_entry)
    except LinkerEvidenceError:
        raise
    except (OSError, ELFError, ValueError, TypeError, KeyError) as exc:
        raise LinkerEvidenceError("build/elf-malformed", f"Cannot parse ELF: {exc}") from exc
    return symbols, tuple(segments), entry_point, image


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


def _read_hex(path: Path) -> tuple[dict[int, int], tuple[AddressRange, ...]]:
    """Strictly parse data records from an Intel HEX build companion."""

    if not path.is_file():
        raise LinkerEvidenceError("build/hex-missing", f"HEX artifact does not exist: {path}")
    try:
        lines = path.read_text(encoding="ascii").splitlines()
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
                if address in image:
                    raise LinkerEvidenceError(
                        "build/hex-overlap", f"Intel HEX repeats address 0x{address:x}"
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


def _artifact_provenance(kind: str, path: Path) -> BuildProvenance:
    try:
        digest = sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise LinkerEvidenceError(
            f"build/{kind}-unreadable", f"Cannot fingerprint {kind} artifact: {exc}"
        ) from exc
    return BuildProvenance(kind, path, digest)


def _merge_symbols(elf: dict[str, int], linker_map: dict[str, int]) -> dict[str, int]:
    merged = dict(elf)
    for name, value in linker_map.items():
        prior = merged.get(name)
        if prior is not None and prior != value:
            raise LinkerEvidenceError(
                "build/artifact-conflict",
                f"ELF and linker map disagree for safety symbol {name}",
            )
        merged[name] = value
    return merged


def _symbol_range(
    symbols: dict[str, int], pairs: tuple[tuple[str, str], ...]
) -> AddressRange | None:
    for start_name, end_name in pairs:
        has_start = start_name in symbols
        has_end = end_name in symbols
        if has_start != has_end:
            raise LinkerEvidenceError(
                "build/partition-incomplete",
                f"Partition requires both {start_name} and {end_name}",
            )
        if has_start:
            try:
                return AddressRange(symbols[start_name], symbols[end_name])
            except RegionError as exc:
                raise LinkerEvidenceError(
                    "build/partition-range", f"Invalid partition symbols: {exc}"
                ) from exc
    return None


def extract_build_evidence(
    selection: BuildArtifactSelection | None,
    *,
    require_flash_partition: bool = True,
    require_ram_partition: bool = True,
    require_vector_table: bool = True,
) -> BuildEvidence:
    """Extract build facts without synthesizing missing evidence.

    Map construction keeps the strict defaults because it is deriving build-owned partitions.
    Runtime flash and breakpoint containment may disable partition requirements: their authority is
    the stable server-owned map, so a vendor ELF need only provide the facts that action consumes.
    """

    if selection is None:
        return BuildEvidence.absent()
    elf_path = selection.elf_path.expanduser().resolve()
    map_path = (
        selection.linker_map_path.expanduser().resolve()
        if selection.linker_map_path is not None
        else None
    )
    hex_path = selection.hex_path.expanduser().resolve() if selection.hex_path is not None else None
    elf_symbols, segments, entry_point, elf_image = _read_elf(elf_path)
    map_symbols = _parse_map(map_path) if map_path is not None else {}
    symbols = _merge_symbols(elf_symbols, map_symbols)
    flash_partition = _symbol_range(symbols, _FLASH_SYMBOLS[selection.role.value])
    if flash_partition is None and {
        "__rom_region_start",
        "__rom_region_size",
    }.issubset(symbols):
        try:
            flash_partition = AddressRange.from_start_size(
                symbols["__rom_region_start"], symbols["__rom_region_size"]
            )
        except RegionError as exc:
            raise LinkerEvidenceError(
                "build/partition-range", f"Invalid ROM partition symbols: {exc}"
            ) from exc
    if flash_partition is None and require_flash_partition:
        raise LinkerEvidenceError(
            "build/flash-partition-missing",
            "Selected ELF/map does not define a complete build-owned flash partition",
        )

    ram_partition = _symbol_range(symbols, _RAM_SYMBOLS)
    if ram_partition is None and require_ram_partition:
        raise LinkerEvidenceError(
            "build/ram-partition-missing",
            "Selected ELF/map does not define a complete build-owned RAM allocation",
        )
    vector_table = next((symbols[name] for name in _VECTOR_SYMBOLS if name in symbols), None)
    if vector_table is None and require_vector_table:
        raise LinkerEvidenceError(
            "build/vector-table-missing", "Selected ELF/map does not define a vector table"
        )

    load_ranges = [segment.load_range for segment in segments if segment.load_range is not None]
    if not load_ranges:
        raise LinkerEvidenceError(
            "build/segment-outside-partition",
            "The ELF has no loadable flash segment",
        )
    if flash_partition is not None and any(
        not flash_partition.contains(load_range) for load_range in load_ranges
    ):
        raise LinkerEvidenceError(
            "build/segment-outside-partition",
            "A loadable ELF segment lies outside the build-owned flash partition",
        )
    if flash_partition is not None and not flash_partition.contains_address(entry_point):
        raise LinkerEvidenceError(
            "build/entry-outside-partition",
            "ELF entry point lies outside the build-owned flash partition",
        )
    if (
        flash_partition is not None
        and vector_table is not None
        and not flash_partition.contains_address(vector_table)
    ):
        raise LinkerEvidenceError(
            "build/vector-outside-partition",
            "Vector table lies outside the build-owned flash partition",
        )

    hex_ranges: tuple[AddressRange, ...] = ()
    if hex_path is not None:
        hex_image, hex_ranges = _read_hex(hex_path)
        outside = sorted(set(hex_image) - set(elf_image))
        if outside:
            raise LinkerEvidenceError(
                "build/hex-outside-elf",
                f"HEX contains address 0x{outside[0]:x} absent from ELF load data",
            )
        mismatch = sorted(
            address for address, value in hex_image.items() if elf_image[address] != value
        )
        if mismatch:
            raise LinkerEvidenceError(
                "build/hex-content-conflict",
                f"HEX and ELF disagree at address 0x{mismatch[0]:x}",
            )
        missing_meaningful = sorted(
            address
            for address, value in elf_image.items()
            if address not in hex_image and value not in {0x00, 0xFF}
        )
        if missing_meaningful:
            raise LinkerEvidenceError(
                "build/hex-incomplete",
                f"HEX omits meaningful ELF data at address 0x{missing_meaningful[0]:x}",
            )
        if flash_partition is not None and any(
            not flash_partition.contains(item) for item in hex_ranges
        ):
            raise LinkerEvidenceError(
                "build/hex-outside-partition",
                "HEX data lies outside the build-owned flash partition",
            )

    artifact_paths = [("elf", elf_path)]
    if map_path is not None:
        artifact_paths.append(("linker_map", map_path))
    if hex_path is not None:
        artifact_paths.append(("hex", hex_path))
    provenance = tuple(_artifact_provenance(kind, path) for kind, path in sorted(artifact_paths))

    return BuildEvidence(
        configuration_id=selection.configuration_id,
        role=selection.role,
        artifact_present=True,
        flash_available=True,
        flash_partition=flash_partition,
        ram_partitions=((ram_partition,) if ram_partition is not None else ()),
        loadable_segments=segments,
        hex_ranges=hex_ranges,
        entry_point=entry_point,
        vector_table=vector_table,
        elf_path=elf_path,
        linker_map_path=map_path,
        hex_path=hex_path,
        provenance=provenance,
    )
