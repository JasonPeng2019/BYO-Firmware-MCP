"""Content-based firmware format recognition shared by planning, safety, and backends."""

from __future__ import annotations

from enum import Enum
from pathlib import Path


class FirmwareFormat(str, Enum):
    ELF = "elf"
    INTEL_HEX = "hex"
    RAW_BINARY = "bin"
    MOTOROLA_S_RECORD = "srec"
    UF2 = "uf2"
    TI_TXT = "ti-txt"
    UNKNOWN = "unknown"


def detect_firmware_format(path: Path) -> FirmwareFormat:
    """Identify formats from bytes; extensions are never authority."""

    selected = path.expanduser().resolve()
    try:
        with selected.open("rb") as stream:
            prefix = stream.read(4096)
    except OSError:
        return FirmwareFormat.UNKNOWN
    if prefix.startswith(b"\x7fELF"):
        return FirmwareFormat.ELF
    stripped = prefix.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if stripped.startswith(b":"):
        first = stripped.splitlines()[0].strip()
        try:
            bytes.fromhex(first[1:].decode("ascii"))
        except (UnicodeError, ValueError):
            pass
        else:
            return FirmwareFormat.INTEL_HEX
    first_line = stripped.splitlines()[0].strip() if stripped else b""
    if len(first_line) >= 4 and first_line[:1] == b"S" and first_line[1:2] in b"0123456789":
        try:
            bytes.fromhex(first_line[2:].decode("ascii"))
        except (UnicodeError, ValueError):
            pass
        else:
            return FirmwareFormat.MOTOROLA_S_RECORD
    if (
        len(prefix) >= 512
        and prefix[:4] == b"UF2\n"
        and prefix[4:8] == b"WQ]\x9e"
        and prefix[508:512] == b"0o\xb1\n"
    ):
        return FirmwareFormat.UF2
    if first_line.startswith(b"@"):
        try:
            int(first_line[1:].decode("ascii"), 16)
        except (UnicodeError, ValueError):
            pass
        else:
            return FirmwareFormat.TI_TXT
    return FirmwareFormat.UNKNOWN


def matching_elf_companion(path: Path) -> Path | None:
    """Find a same-build ELF companion without assuming one vendor's suffix."""

    selected = path.expanduser().resolve()
    candidates = (
        selected.with_suffix(".elf"),
        selected.with_suffix(".axf"),
        selected.with_suffix(".out"),
        selected.parent / "firmware.elf",
    )
    for candidate in dict.fromkeys(candidates):
        if candidate != selected and candidate.is_file():
            if detect_firmware_format(candidate) is FirmwareFormat.ELF:
                return candidate
    return None
