"""Independently derived flash images and the readback oracle for flash success.

A flash succeeds only after every byte the artifact addresses has been read back
from the target and matched. The expected bytes are derived here, without pyOCD:
ELF/AXF ``PT_LOAD`` file data at physical load addresses, Intel HEX data records,
or a raw ``.bin`` at an explicit, never inferred, base address. Holes between
addressed bytes are neither padded nor read, and the final reset state is reported
separately from this programming proof. Derivation, support preflight, programming
and readback all consume one private snapshot of the artifact bytes, so a rebuild
of the original path mid-flash can never program bytes that were not verified.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pyocd_debug_mcp.safety.linker import (
    LinkerEvidenceError,
    read_elf_load_runs,
    read_hex_load_runs,
)
from pyocd_debug_mcp.target_errors import (
    FlashArtifactChangedError,
    FlashVerificationError,
    UnsupportedArtifactError,
)

ADDRESSED_FLASH_SUFFIXES: Final = frozenset({".axf", ".elf", ".hex"})
BIN_FLASH_SUFFIX: Final = ".bin"
FLASH_ARTIFACT_SUFFIXES: Final = ADDRESSED_FLASH_SUFFIXES | {BIN_FLASH_SUFFIX}
ADDRESS_LIMIT: Final = 1 << 32
VERIFY_CHUNK_BYTES: Final = 4096
TARGET_STATES: Final = frozenset({"running", "halted", "reset_state_unconfirmed"})
_RESULT_KEYS: Final = frozenset(
    {"programming", "verified_bytes", "verified_ranges", "target_state"}
)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class FlashImage:
    """The bytes an artifact addresses, as sorted, disjoint ``(start, data)`` runs."""

    runs: tuple[tuple[int, bytes], ...]

    def __post_init__(self) -> None:
        if not self.runs:
            raise UnsupportedArtifactError(
                "Flash artifact addresses no bytes; there is nothing to program or verify"
            )
        previous_end = 0
        for start, data in self.runs:
            if not _is_integer(start) or not isinstance(data, bytes) or not data:
                raise ValueError("flash image runs must be (integer start, non-empty bytes)")
            if start < previous_end:
                raise ValueError("flash image runs must be sorted and disjoint")
            previous_end = start + len(data)
            if start < 0 or previous_end > ADDRESS_LIMIT:
                raise UnsupportedArtifactError(
                    f"Flash artifact addresses 0x{start:X}-0x{previous_end - 1:X}, outside "
                    "the 32-bit target address space"
                )

    @property
    def ranges(self) -> tuple[tuple[int, int], ...]:
        """Half-open ``[start, end)`` address ranges of the addressed bytes."""

        return tuple((start, start + len(data)) for start, data in self.runs)

    @property
    def byte_count(self) -> int:
        return sum(len(data) for _start, data in self.runs)


def check_flash_request(firmware: Path, base_address: int | None) -> None:
    """Refuse an artifact type or address combination the flash path cannot program.

    ELF/AXF/HEX artifacts carry their own load addresses and reject ``base_address``.
    A ``.bin`` carries none, so it requires an explicit ``base_address``; the boot
    memory fallback pyOCD would otherwise use is never relied on.
    """

    suffix = firmware.suffix.lower()
    if suffix not in FLASH_ARTIFACT_SUFFIXES:
        raise UnsupportedArtifactError(
            f"Unsupported artifact type '{firmware.suffix}' - use one of: "
            + ", ".join(sorted(FLASH_ARTIFACT_SUFFIXES))
        )
    if suffix != BIN_FLASH_SUFFIX:
        if base_address is not None:
            raise UnsupportedArtifactError(
                f"base_address applies only to .bin artifacts; a {suffix} artifact "
                "carries its own load addresses"
            )
        return
    if base_address is None:
        raise UnsupportedArtifactError(
            "A .bin artifact carries no addresses; pass an explicit base_address "
            "(the server never infers one)"
        )
    if not _is_integer(base_address) or not 0 <= base_address < ADDRESS_LIMIT:
        raise UnsupportedArtifactError("base_address must be an integer in [0, 0x100000000)")


@contextmanager
def snapshot_flash_artifact(
    firmware: Path, *, expected_sha256: str | None = None
) -> Iterator[Path]:
    """Yield a private copy of the artifact bytes, read once, for one flash.

    The copy keeps the artifact's file name, so its format is unchanged. Deriving
    the image from it and programming it means the bytes programmed are exactly the
    bytes checked and read back, even if ``firmware`` is rebuilt meanwhile. With
    ``expected_sha256`` those bytes must also be the approved content; otherwise
    the flash is refused before any target effect.
    """

    try:
        data = firmware.read_bytes()
    except OSError as exc:
        raise UnsupportedArtifactError(f"Cannot read flash artifact: {exc}") from exc
    if expected_sha256 is not None:
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha256:
            raise FlashArtifactChangedError(
                f"Flash artifact {firmware} changed after it was approved (sha256 "
                f"{actual}, approved {expected_sha256}); nothing was reset, erased or "
                "programmed. Let the build finish, then flash the artifact again."
            )
    # A cleanup failure after a verified flash must not report it as failed.
    with tempfile.TemporaryDirectory(prefix="byo-flash-", ignore_cleanup_errors=True) as directory:
        snapshot = Path(directory) / firmware.name
        snapshot.write_bytes(data)
        yield snapshot


def derive_flash_image(firmware: Path, *, base_address: int | None = None) -> FlashImage:
    """Derive the addressed bytes an artifact programs, independently of pyOCD."""

    check_flash_request(firmware, base_address)
    suffix = firmware.suffix.lower()
    if suffix == BIN_FLASH_SUFFIX:
        assert base_address is not None
        try:
            data = firmware.read_bytes()
        except OSError as exc:
            raise UnsupportedArtifactError(f"Cannot read flash artifact: {exc}") from exc
        return FlashImage(((base_address, data),) if data else ())
    reader = read_hex_load_runs if suffix == ".hex" else read_elf_load_runs
    try:
        runs = reader(firmware)
    except LinkerEvidenceError as exc:
        raise UnsupportedArtifactError(
            f"Flash artifact cannot be parsed [{exc.code}]: {exc}"
        ) from exc
    return FlashImage(runs)


def verify_flash_image(
    image: FlashImage,
    read_block: Callable[[int, int], Iterable[int]],
    *,
    chunk_bytes: int = VERIFY_CHUNK_BYTES,
) -> None:
    """Compare bounded readback chunks with every addressed artifact byte.

    ``read_block(address, length)`` must return that many byte values. Any read
    failure, short read, or differing byte raises ``FlashVerificationError``.
    """

    if not _is_integer(chunk_bytes) or chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be a positive integer")
    for start, run in image.runs:
        for offset in range(0, len(run), chunk_bytes):
            address = start + offset
            expected = run[offset : offset + chunk_bytes]
            try:
                actual = bytes(read_block(address, len(expected)))
            except Exception as exc:
                raise FlashVerificationError(
                    f"Programmed flash could not be read back at 0x{address:08X} "
                    f"({type(exc).__name__}: {exc}); programming is not verified"
                ) from exc
            if len(actual) != len(expected):
                raise FlashVerificationError(
                    f"Flash readback at 0x{address:08X} returned {len(actual)} of "
                    f"{len(expected)} bytes; programming is not verified"
                )
            if actual != expected:
                differing = [i for i, (a, b) in enumerate(zip(actual, expected)) if a != b]
                first = differing[0]
                raise FlashVerificationError(
                    f"Flash readback does not match the artifact at 0x{address + first:08X}: "
                    f"expected 0x{expected[first]:02X}, read 0x{actual[first]:02X} "
                    f"({len(differing)} of {len(expected)} bytes differ in this chunk); "
                    "programming is not verified"
                )


@dataclass(frozen=True, slots=True)
class FlashResult:
    """Programming proven by readback, reported separately from the final reset state.

    ``target_state`` describes only the post-programming reset. A value of
    ``reset_state_unconfirmed`` leaves execution unconfirmed; it never weakens the
    verified bytes, and no reset or backend return value establishes them.
    """

    verified_ranges: tuple[tuple[int, int], ...]
    target_state: str

    def __post_init__(self) -> None:
        if self.target_state not in TARGET_STATES:
            raise ValueError("flash target_state is invalid")
        if not self.verified_ranges:
            raise ValueError("a flash result requires verified ranges")
        previous_end = 0
        for item in self.verified_ranges:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(_is_integer(bound) for bound in item)
                or not previous_end <= item[0] < item[1] <= ADDRESS_LIMIT
            ):
                raise ValueError("flash verified ranges must be sorted, disjoint and in range")
            previous_end = item[1]

    @classmethod
    def for_image(cls, image: FlashImage, target_state: str) -> FlashResult:
        return cls(image.ranges, target_state)

    @property
    def verified_bytes(self) -> int:
        return sum(end - start for start, end in self.verified_ranges)

    def to_document(self) -> dict[str, object]:
        return {
            "programming": "verified",
            "verified_bytes": self.verified_bytes,
            "verified_ranges": [[start, end] for start, end in self.verified_ranges],
            "target_state": self.target_state,
        }

    @classmethod
    def from_document(cls, value: object) -> FlashResult:
        if not isinstance(value, dict) or set(value) != _RESULT_KEYS:
            raise ValueError("flash result document has unexpected fields")
        if value["programming"] != "verified":
            raise ValueError("flash result does not prove verified programming")
        ranges = value["verified_ranges"]
        if not isinstance(ranges, list) or not all(
            isinstance(item, list) and len(item) == 2 for item in ranges
        ):
            raise ValueError("flash verified ranges are malformed")
        target_state = value["target_state"]
        if not isinstance(target_state, str):
            raise ValueError("flash target_state is invalid")
        result = cls(tuple((item[0], item[1]) for item in ranges), target_state)
        verified_bytes = value["verified_bytes"]
        if not _is_integer(verified_bytes) or verified_bytes != result.verified_bytes:
            raise ValueError("flash verified byte count does not match its ranges")
        return result

    def programming_summary(self) -> dict[str, object]:
        """Public programming proof, kept apart from ``target_state``."""

        return {
            "status": "verified",
            "verified_bytes": self.verified_bytes,
            "ranges": [
                {"start": f"0x{start:08X}", "end": f"0x{end:08X}"}
                for start, end in self.verified_ranges
            ],
        }
