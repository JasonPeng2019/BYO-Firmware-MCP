"""Source-backed factory-register research, server capture, and same-unit replay.

Research uses the existing cooperative setup trust boundary: the caller identifies
documented facts; the server pins the source and compares real observations. This
does not infer package identity from a device-group code or interpret PDF prose.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.setup_flow.datasheet_evidence import (
    PARSER_VERSION,
    capture_datasheet_evidence,
    replay_datasheet_evidence,
)


def _fields(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{label} requires exactly {sorted(fields)}")
    return dict(value)


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must name the documented source fact")
    return value.strip()


def _pages(value: object) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ValueError("source_pages must contain positive PDF page numbers")
    return [_integer(item, "source page", 1) for item in value]


def normalize_factory_research(value: object) -> dict[str, Any]:
    research = _fields(
        value,
        {"source_url", "source_pages", "source_note", "identity_coverage", "registers"},
        "factory identity research",
    )
    url = urlparse(_text(research["source_url"], "source_url"))
    if url.scheme != "https" or not url.netloc:
        raise ValueError("source_url must identify the official HTTPS document source")
    research["source_pages"] = _pages(research["source_pages"])
    for name in ("source_note", "identity_coverage"):
        research[name] = _text(research[name], name)
    rows = research["registers"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("factory identity requires documented device, flash_size and unique reads")
    registers = []
    intervals: list[tuple[int, int]] = []
    for row in rows:
        register = _fields(
            row,
            {
                "address",
                "width_bits",
                "role",
                "mask",
                "expected",
                "unit_bytes",
                "source_pages",
                "source_note",
                "read_only",
                "factory",
            },
            "factory register",
        )
        address = _integer(register["address"], "factory register address")
        width = _integer(register["width_bits"], "factory register width")
        if width not in {8, 16, 32} or address % (width // 8) or address + width // 8 > 2**32:
            raise ValueError("factory register must be an aligned 8/16/32-bit target read")
        interval = (address, address + width // 8)
        # Arm core identity/status is not a factory unit identifier. Vendor
        # DBGMCU registers outside System Control Space remain eligible.
        if interval[0] < 0xE0010000 and 0xE000E000 < interval[1]:
            raise ValueError("Arm System Control Space/CPUID cannot prove factory unit identity")
        if any(start < interval[1] and interval[0] < end for start, end in intervals):
            raise ValueError("factory identity register reads must not overlap")
        intervals.append(interval)
        role = register["role"]
        if not isinstance(role, str) or role not in {"device", "flash_size", "unique"}:
            raise ValueError("factory register role must be device, flash_size or unique")
        limit = (1 << width) - 1
        mask = _integer(register["mask"], "factory register mask", 1)
        if mask > limit:
            raise ValueError("factory register mask exceeds its width")
        if register["read_only"] is not True or register["factory"] is not True:
            raise ValueError(
                "documented factory registers must be read-only and factory-programmed"
            )
        if role == "device":
            expected = _integer(register["expected"], "documented device/group expectation")
            if expected > limit or register["unit_bytes"] is not None:
                raise ValueError("device/group expectation or units are invalid")
        else:
            if register["expected"] is not None or mask != limit:
                raise ValueError(
                    "UID/flash-size expectations are server-derived with full-width masks"
                )
            if role == "flash_size":
                _integer(register["unit_bytes"], "documented flash-size unit_bytes", 1)
            elif register["unit_bytes"] is not None:
                raise ValueError("unique identifier has no size unit")
        register["source_pages"] = _pages(register["source_pages"])
        register["source_note"] = _text(register["source_note"], "factory register section")
        registers.append(register)
    if {row["role"] for row in registers} != {"device", "flash_size", "unique"}:
        raise ValueError(
            "exact unit proof requires device/group, flash-size and unique factory facts"
        )
    research["registers"] = registers
    return research


def normalize_bootloader_request(value: object) -> dict[str, Any]:
    request = _fields(
        value,
        {
            "start",
            "end",
            "source_path",
            "source_url",
            "source_pages",
            "source_note",
            "identity_coverage",
            "registers",
        },
        "user_bootloader",
    )
    start, end = (_integer(request[name], name) for name in ("start", "end"))
    if not start < end <= 2**32:
        raise ValueError("bootloader bounds must satisfy 0 <= start < end <= 2**32")
    source_path = _text(request["source_path"], "source_path")
    research = normalize_factory_research(
        {key: item for key, item in request.items() if key not in {"start", "end", "source_path"}}
    )
    return dict(start=start, end=end, source_path=source_path, **research)


def validate_factory_record(
    value: object,
    *,
    flash_size: int,
    memory_ranges: Sequence[tuple[int, int]],
) -> dict[str, Any]:
    """Validate persisted structure and recorded observations without live authority."""
    record = _fields(
        value, {"source", "research", "probe_identity", "observations"}, "factory proof"
    )
    source = _fields(record["source"], {"sha256", "reference", "parser_version"}, "factory source")
    if source["parser_version"] != PARSER_VERSION:
        raise ValueError("factory source parser version is unavailable")
    for name in ("sha256", "reference"):
        _text(source[name], f"factory source {name}")
    _text(record["probe_identity"], "captured probe identity")
    research = normalize_factory_research(record["research"])
    observations = record["observations"]
    if not isinstance(observations, list) or len(observations) != len(research["registers"]):
        raise ValueError("factory proof requires each server-captured observation")
    unique: list[tuple[int, int]] = []
    for register, observed in zip(research["registers"], observations):
        width = register["width_bits"]
        limit = (1 << width) - 1
        actual = _integer(observed, "server-captured factory observation")
        if actual > limit:
            raise ValueError("factory observation exceeds its register width")
        start, end = register["address"], register["address"] + width // 8
        if any(left < end and start < right for left, right in memory_ranges):
            raise ValueError("RAM or programmable flash cannot supply factory unit identity")
        role = register["role"]
        if (
            role == "device"
            and actual & register["mask"] != register["expected"] & register["mask"]
        ):
            raise ValueError("live device/group does not match the documented supported device")
        if role == "flash_size" and actual * register["unit_bytes"] != flash_size:
            raise ValueError(
                "live flash size does not match independently verified programmable geometry"
            )
        if role == "unique":
            unique.append((actual, limit))
    if all(value == 0 for value, _ in unique) or all(value == limit for value, limit in unique):
        raise ValueError("factory unique identifier is blank/unavailable")
    return dict(record, source=source, research=research)


def capture_factory_identity(
    store: FirmStore,
    request: Mapping[str, Any],
    *,
    probe_identity: str,
    reader: Callable[[int, int], int],
    flash_size: int,
    memory_ranges: Sequence[tuple[int, int]],
) -> dict[str, Any]:
    research = normalize_factory_research(
        {key: value for key, value in request.items() if key not in {"start", "end", "source_path"}}
    )
    # Reject mutable memory locations before any target read.
    for register in research["registers"]:
        start = register["address"]
        end = start + register["width_bits"] // 8
        if any(left < end and start < right for left, right in memory_ranges):
            raise ValueError("RAM or programmable flash cannot supply factory unit identity")
    path = Path(request["source_path"]).expanduser()
    if not path.is_absolute():
        path = store.layout.project_root / path
    source = capture_datasheet_evidence(store, path)
    record = dict(
        source=source.source_document(),
        research=research,
        probe_identity=probe_identity,
        observations=[reader(row["address"], row["width_bits"]) for row in research["registers"]],
    )
    record = validate_factory_record(record, flash_size=flash_size, memory_ranges=memory_ranges)
    replay_factory_identity(
        store,
        record,
        flash_size=flash_size,
        memory_ranges=memory_ranges,
        probe_identity=probe_identity,
        reader=reader,
    )
    return record


def replay_factory_identity(
    store: FirmStore,
    value: object,
    *,
    flash_size: int,
    memory_ranges: Sequence[tuple[int, int]],
    probe_identity: str | None = None,
    reader: Callable[[int, int], int] | None = None,
) -> dict[str, Any]:
    record = validate_factory_record(value, flash_size=flash_size, memory_ranges=memory_ranges)
    source = record["source"]
    replayed = replay_datasheet_evidence(store, source["reference"], source["sha256"])
    if replayed.source_document() != source:
        raise ValueError("factory source no longer replays exactly")
    if probe_identity is not None and probe_identity != record["probe_identity"]:
        raise ValueError("factory proof belongs to a different probe binding")
    if reader is not None:
        for row, captured in zip(record["research"]["registers"], record["observations"]):
            actual = reader(row["address"], row["width_bits"])
            if actual & row["mask"] != captured & row["mask"]:
                raise ValueError(
                    "factory identity changed: device/group, flash size or unique unit differs"
                )
    return record
