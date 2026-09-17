"""Conservative setup-lite confirmation parsing and persisted evidence shape."""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pyocd_debug_mcp.kernel.processes import run_owned
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionError,
    RegionKind,
    RecoveryEraseDisclosure,
    SafetyRegion,
    SourceAuthority,
    build_recovery_erase_disclosure,
)


_CONFIRMATION_KEYS = frozenset({"decision", "regions", "flash", "recovery"})
_REGION_KEYS = frozenset(
    {
        "name",
        "kind",
        "start",
        "end",
        "readable",
        "writable",
        "executable",
        "source_pages",
        "source_note",
    }
)
_FLASH_KEYS = frozenset({"backend_target", "erase_sectors"})
_RECOVERY_KEYS = frozenset({"mechanism", "source_pages", "source_note"})
_EXTRACTION_TIMEOUT_SECONDS = 10.0
_MAX_EXTRACTION_OUTPUT_BYTES = 1_000_000
_LITE_KINDS = frozenset(
    {
        RegionKind.RAM.value,
        RegionKind.PHYSICAL_RAM.value,
        RegionKind.PHYSICAL_FLASH.value,
        RegionKind.APPLICATION_FLASH.value,
        RegionKind.BOOTLOADER_FLASH.value,
        "bootloader",  # frozen operator-facing spelling; normalize internally below
        RegionKind.ROM.value,
        RegionKind.PERIPHERAL.value,
        RegionKind.PERIPHERAL_READ_ONLY.value,
        RegionKind.PERIPHERAL_WRITE_ONLY.value,
        RegionKind.CPU_SYSTEM.value,
        RegionKind.PROHIBITED.value,
    }
)


class LiteConfirmationError(ValueError):
    """The operator response did not establish an explicit partial capability."""


@dataclass(frozen=True, slots=True)
class LiteConfirmation:
    """Normalized containment map plus verbatim operator confirmation evidence."""

    map_snapshot: dict[str, object]
    evidence: dict[str, object]

    @property
    def region_count(self) -> int:
        regions = self.map_snapshot.get("regions")
        return len(regions) if isinstance(regions, list) else 0


def _address(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise LiteConfirmationError(f"{field} must be a non-boolean integer or hexadecimal string")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value, 0)
        except ValueError as exc:
            raise LiteConfirmationError(
                f"{field} must be an integer or hexadecimal string"
            ) from exc
    else:
        raise LiteConfirmationError(f"{field} must be an integer or hexadecimal string")
    if not 0 <= result < 1 << 64:
        raise LiteConfirmationError(f"{field} must be in the unsigned 64-bit address range")
    return result


def _validate_native_recovery_disclosure(
    regions: list[dict[str, object]], sectors: list[object]
) -> RecoveryEraseDisclosure:
    """Require map-derived complete loss disclosure before native lite recovery exists."""

    try:
        mapped = [
            SafetyRegion(
                str(row["name"]),
                RegionKind(str(row["kind"])),
                AddressRange(
                    _address(row["start"], "native recovery region start"),
                    _address(row["end"], "native recovery region end"),
                ),
                (
                    Provenance(
                        SourceAuthority.OFFICIAL_DOCUMENT,
                        "setup-lite-confirmation",
                        str(row["source_note"]),
                    ),
                ),
                executable=bool(row["executable"]),
            )
            for row in regions
        ]
        return build_recovery_erase_disclosure(mapped, {"sectors": sectors}, mass_erase=True)
    except (KeyError, TypeError, ValueError, RegionError) as exc:
        raise LiteConfirmationError(
            "native recovery requires confirmed physical-flash spans and complete erase sectors: "
            f"{exc}"
        ) from exc


def normalize_lite_confirmation(board_id: str, response: Mapping[str, object]) -> LiteConfirmation:
    """Validate the frozen confirmation schema without inventing missing facts.

    The active map contains numeric endpoints for the normal safety primitives;
    evidence deliberately retains the submitted rows (including wording and
    source page references) so refresh can replay corrections rather than
    reparsing a PDF over the operator's work.
    """

    if set(response) != _CONFIRMATION_KEYS:
        raise LiteConfirmationError(
            "lite confirmation must contain exactly decision, regions, flash, and recovery"
        )
    if response.get("decision") != "confirm-or-correct":
        raise LiteConfirmationError("decision must be exactly confirm-or-correct")
    regions = response.get("regions")
    if not isinstance(regions, list) or not regions:
        raise LiteConfirmationError(
            "lite confirmation requires at least one confirmed map-dependent region"
        )
    flash = response.get("flash")
    if not isinstance(flash, Mapping) or set(flash) != _FLASH_KEYS:
        raise LiteConfirmationError("flash must contain exactly backend_target and erase_sectors")
    target = flash.get("backend_target")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise LiteConfirmationError("flash.backend_target must be non-empty text or null")
    sectors = flash.get("erase_sectors")
    if not isinstance(sectors, list):
        raise LiteConfirmationError("flash.erase_sectors must be a list")
    recovery = response.get("recovery")
    if recovery is not None:
        if not isinstance(recovery, Mapping) or set(recovery) != _RECOVERY_KEYS:
            raise LiteConfirmationError(
                "recovery must be null or contain exactly mechanism, source_pages, and source_note"
            )
        # The target-control service deliberately exposes only this typed primitive.
        # Do not persist a datasheet label as though it were a callable backend API.
        if recovery.get("mechanism") != "backend_mass_erase":
            raise LiteConfirmationError(
                "recovery.mechanism must name the documented typed backend_mass_erase primitive"
            )
        pages = recovery.get("source_pages")
        if (
            not isinstance(pages, list)
            or not pages
            or any(
                isinstance(page, bool) or not isinstance(page, int) or page <= 0 for page in pages
            )
        ):
            raise LiteConfirmationError("recovery.source_pages must be non-empty positive integers")
        note = recovery.get("source_note")
        if not isinstance(note, str) or not note.strip():
            raise LiteConfirmationError("recovery.source_note must be non-empty text")

    normalized_regions: list[dict[str, object]] = []
    for index, item in enumerate(regions):
        if not isinstance(item, Mapping) or set(item) != _REGION_KEYS:
            raise LiteConfirmationError(f"regions[{index}] must use the exact lite region schema")
        name = item.get("name")
        kind = item.get("kind")
        note = item.get("source_note")
        pages = item.get("source_pages")
        if not isinstance(name, str) or not name.strip():
            raise LiteConfirmationError(f"regions[{index}].name must be non-empty text")
        if not isinstance(kind, str) or kind not in _LITE_KINDS:
            raise LiteConfirmationError(
                f"regions[{index}].kind is not a supported containment kind"
            )
        if not isinstance(note, str) or not note.strip():
            raise LiteConfirmationError(f"regions[{index}].source_note must be non-empty text")
        if (
            not isinstance(pages, list)
            or not pages
            or any(
                isinstance(page, bool) or not isinstance(page, int) or page <= 0 for page in pages
            )
        ):
            raise LiteConfirmationError(
                f"regions[{index}].source_pages must be non-empty positive integers"
            )
        if any(
            not isinstance(item.get(key), bool) for key in ("readable", "writable", "executable")
        ):
            raise LiteConfirmationError(f"regions[{index}] permissions must be booleans")
        start = _address(item.get("start"), f"regions[{index}].start")
        end = _address(item.get("end"), f"regions[{index}].end")
        if start >= end:
            raise LiteConfirmationError(f"regions[{index}] must have start < end")
        normalized_kind = RegionKind.BOOTLOADER_FLASH.value if kind == "bootloader" else kind
        normalized_regions.append(
            {
                **dict(item),
                "kind": normalized_kind,
                "start": start,
                "end": end,
            }
        )

    if recovery is not None:
        _validate_native_recovery_disclosure(normalized_regions, sectors)

    return LiteConfirmation(
        map_snapshot={
            "board_id": board_id,
            "regions": normalized_regions,
            "flash": copy.deepcopy(dict(flash)),
            "recovery": copy.deepcopy(dict(recovery)) if isinstance(recovery, Mapping) else None,
        },
        evidence={
            "confirmation_schema": 1,
            "decision": "confirm-or-correct",
            "confirmed_regions": copy.deepcopy(regions),
            "flash": copy.deepcopy(dict(flash)),
            "recovery": copy.deepcopy(dict(recovery)) if isinstance(recovery, Mapping) else None,
        },
    )


def native_lite_recovery_disclosure(board_id: str, map_snapshot: Mapping[str, object]):
    """Revalidate and derive the exact native recovery disclosure from a committed map."""

    confirmation = normalize_lite_confirmation(
        board_id,
        {
            "decision": "confirm-or-correct",
            "regions": map_snapshot.get("regions"),
            "flash": map_snapshot.get("flash"),
            "recovery": map_snapshot.get("recovery"),
        },
    )
    recovery = confirmation.map_snapshot.get("recovery")
    if recovery is None:
        raise LiteConfirmationError("setup-lite has no documented native recovery mechanism")
    regions = confirmation.map_snapshot["regions"]
    flash = confirmation.map_snapshot["flash"]
    assert isinstance(regions, list) and isinstance(flash, Mapping)
    sectors = flash.get("erase_sectors")
    assert isinstance(sectors, list)
    return _validate_native_recovery_disclosure(regions, sectors)


def conservative_lite_proposal(datasheet_path: Path) -> dict[str, object]:
    """Describe a deliberately non-authoritative extraction attempt.

    We only detect a local extractor here. Its presence is useful operator
    context, but an extractor's text is never geometry authority: unknown
    layouts produce the empty proposal and require the same confirmation.
    """

    path = Path(datasheet_path).expanduser().resolve()
    extractor = shutil.which("pdftotext")
    proposal: dict[str, object] = {
        "datasheet_path": str(path),
        "extractor": "pdftotext" if extractor else None,
        "regions": [],
        "flash": {"backend_target": None, "erase_sectors": []},
        "recovery": None,
        "uncertain": True,
    }
    if extractor is None:
        proposal["extractor_status"] = "unavailable"
        proposal["diagnostic"] = (
            "pdftotext is not installed; supply or correct every lite confirmation fact."
        )
        return proposal
    try:
        # Bounded local extraction only.  It is operator context, never an
        # authority, and deliberately reads no more than the first 32 pages.
        completed = run_owned(
            [extractor, "-f", "1", "-l", "32", "-layout", str(path), "-"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_EXTRACTION_TIMEOUT_SECONDS,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        proposal["extractor_status"] = "failed"
        proposal["diagnostic"] = f"local pdftotext failed: {exc}"
        return proposal
    if completed.returncode != 0:
        proposal["extractor_status"] = "failed"
        proposal["diagnostic"] = (
            completed.stderr or "pdftotext returned a non-zero exit status"
        ).strip()
        return proposal

    output = completed.stdout if isinstance(completed.stdout, str) else ""
    if len(output.encode("utf-8", errors="replace")) > _MAX_EXTRACTION_OUTPUT_BYTES:
        proposal["extractor_status"] = "failed"
        proposal["diagnostic"] = (
            "local pdftotext output exceeded the bounded extraction limit; "
            "supply or correct every lite confirmation fact."
        )
        return proposal

    candidates: list[dict[str, object]] = []
    # This intentionally recognizes only an unambiguous same-line range plus
    # a classification word.  Any prose/table ambiguity leaves the proposal
    # empty instead of inventing a region or permission.
    pattern = re.compile(
        r"(?P<label>[A-Za-z][A-Za-z0-9 _/-]{0,48})\s+"
        r"(?P<start>0x[0-9a-fA-F]+)\s*(?:-|–|to)\s*(?P<end>0x[0-9a-fA-F]+)",
        re.IGNORECASE,
    )
    for page, page_text in enumerate(output.split("\f"), start=1):
        for line in page_text.splitlines():
            match = pattern.search(line)
            if match is None:
                continue
            label = match.group("label").strip()
            lowered = label.casefold()
            if "ram" in lowered:
                kind = RegionKind.RAM.value
            elif "boot" in lowered and "flash" in lowered:
                kind = RegionKind.BOOTLOADER_FLASH.value
            elif "flash" in lowered:
                kind = RegionKind.APPLICATION_FLASH.value
            elif "rom" in lowered:
                kind = RegionKind.ROM.value
            else:
                continue
            start = int(match.group("start"), 0)
            # Datasheets usually print inclusive end addresses; preserve the
            # candidate as an exclusive range only when it is non-overflowing.
            end = int(match.group("end"), 0) + 1
            if end <= start:
                continue
            candidates.append(
                {
                    "name": label,
                    "kind": kind,
                    "start": start,
                    "end": end,
                    "readable": False,
                    "writable": False,
                    "executable": False,
                    "source_pages": [page],
                    "source_note": "unverified local pdftotext candidate; operator must correct/confirm",
                }
            )
    proposal["extractor_status"] = "ok"
    proposal["regions"] = candidates
    proposal["diagnostic"] = (
        "Local extraction found conservative candidates only; confirm every range, permission, "
        "backend target, erase sector, and recovery fact exactly."
    )
    return proposal


__all__ = [
    "LiteConfirmation",
    "LiteConfirmationError",
    "conservative_lite_proposal",
    "normalize_lite_confirmation",
]
