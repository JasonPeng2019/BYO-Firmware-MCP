"""Canonical, provider-neutral semantic memory-map authority.

The document is deliberately evidence, not a board catalogue or an ownership
policy.  It records only facts supplied by the current provider and explicit
project evidence, and is rechecked against the live session before a map-bound
operation starts.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from firmware_mcp.adapters.debug_interface import (
    PhysicalMemoryRegion,
    RecoveryCapability,
    TargetSessionHandle,
)
from firmware_mcp.firmstore.profiles import BoardProfile, ProfileRepository
from firmware_mcp.firmstore.store import FirmStore
from firmware_mcp.safety.linker import file_backed_elf_ranges
from firmware_mcp.setup_flow.device_support import PackProvisionError, access_facts
from firmware_mcp.services.physical_memory import (
    PhysicalMemoryAccessError,
    require_live_physical_access,
)
from firmware_mcp.services.live_identity import (
    LiveIdentityContradiction,
    LiveIdentityObservationError,
    observe_live_identity,
)

ROLES = frozenset(
    {
        "application",
        "bootloader",
        "ordinary_ram",
        "peripheral",
        "option",
        "otp",
        "security",
        "lifecycle",
        "sensitive",
        "rom",
        "unknown",
    }
)


class SafetyAuthorityError(PhysicalMemoryAccessError):
    """A canonical map is missing, malformed, stale, or semantically unsuitable."""


def _canonical(document: Mapping[str, Any]) -> bytes:
    value = dict(document)
    value.pop("digest", None)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def map_digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(document)).hexdigest()


def _int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SafetyAuthorityError(f"Safety map {label} must be a non-negative integer.")
    return value


_FACT_KEYS = frozenset({"physical", "readable", "writable", "executable", "role"})


def _source_ref(identifier: str) -> str:
    return f"source:{identifier}"


def _check_regions(regions: object, source_kinds: Mapping[str, str]) -> list[dict[str, Any]]:
    if not isinstance(regions, list) or not regions:
        raise SafetyAuthorityError("Safety map requires a non-empty normalized regions list.")
    result: list[dict[str, Any]] = []
    previous = -1
    for index, raw in enumerate(regions):
        if not isinstance(raw, dict):
            raise SafetyAuthorityError(f"Safety map region {index} is not an object.")
        if set(raw) != {
            "start",
            "end",
            "kind",
            "role",
            "readable",
            "writable",
            "executable",
            "name",
            "provenance",
        }:
            raise SafetyAuthorityError(
                "Safety map region has unexpected or missing canonical fields."
            )
        start, end = _int(raw.get("start"), "region start"), _int(raw.get("end"), "region end")
        if end <= start or end > 1 << 64 or start < previous:
            raise SafetyAuthorityError(
                "Safety map regions must be sorted, non-overlapping half-open ranges."
            )
        role = raw.get("role")
        if role not in ROLES:
            raise SafetyAuthorityError(
                f"Safety map region 0x{start:X} has invalid semantic role {role!r}."
            )
        if not isinstance(raw.get("kind"), str) or not raw["kind"]:
            raise SafetyAuthorityError("Safety map region physical kind is required.")
        if not isinstance(raw.get("name"), str):
            raise SafetyAuthorityError("Safety map region physical name is required.")
        if not all(
            isinstance(raw.get(flag), bool) for flag in ("readable", "writable", "executable")
        ):
            raise SafetyAuthorityError("Safety map region access flags must be boolean facts.")
        provenance = raw.get("provenance")
        if not isinstance(provenance, dict) or set(provenance) != _FACT_KEYS:
            raise SafetyAuthorityError(
                "Safety map region requires separate canonical provenance for every fact."
            )
        allowed_source_kinds = {
            "physical": {"provider-region", "support-geometry"},
            "readable": {"provider-region", "support-geometry"},
            "writable": {"provider-region", "support-geometry"},
            "executable": {"provider-region", "support-geometry"},
            "role": {"layout-source", "elf", "derived-role"},
        }
        for fact, refs in provenance.items():
            if (
                not isinstance(refs, list)
                or not refs
                or refs != sorted(set(refs))
                or not all(isinstance(ref, str) and ref in source_kinds for ref in refs)
            ):
                raise SafetyAuthorityError(
                    f"Safety map region {fact} provenance does not reference canonical source records."
                )
            if any(source_kinds[ref] not in allowed_source_kinds[fact] for ref in refs):
                raise SafetyAuthorityError(
                    f"Safety map region {fact} provenance cites an inappropriate source kind."
                )
        result.append(dict(raw))
        previous = end
    return result


def validate_document(document: object, *, board_id: str | None = None) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise SafetyAuthorityError("Safety map is not a JSON object.")
    required = {
        "schema_version",
        "board_id",
        "identity",
        "sources",
        "regions",
        "erase_geometry",
        "digest",
    }
    if set(document) != required or document.get("schema_version") != 2:
        raise SafetyAuthorityError(
            "Safety map has an unsupported or incomplete canonical schema; create a fresh "
            "refresh_safety_map plan to publish the current capability-aware map."
        )
    if not isinstance(document.get("board_id"), str) or not document["board_id"]:
        raise SafetyAuthorityError("Safety map board_id is required.")
    if board_id is not None and document["board_id"] != board_id:
        raise SafetyAuthorityError(
            "Safety map belongs to another logical board; refresh_safety_map for this board."
        )
    identity = document.get("identity")
    identity_keys = {
        "configured_part_number",
        "provider_id",
        "target",
        "provider_support_identity",
        "capability",
        "comparison_status",
        "exact_live_part_number",
        "evidence",
    }
    if not isinstance(identity, dict) or set(identity) != identity_keys:
        raise SafetyAuthorityError("Safety map capability-aware identity evidence is incomplete.")
    if not all(
        isinstance(identity.get(key), str) and identity[key]
        for key in (
            "configured_part_number",
            "provider_id",
            "target",
            "provider_support_identity",
        )
    ) or identity.get("capability") not in {"exact", "compatible", "unavailable"}:
        raise SafetyAuthorityError("Safety map capability-aware identity route is malformed.")
    capability = identity["capability"]
    status = identity.get("comparison_status")
    exact_part = identity.get("exact_live_part_number")
    evidence = identity.get("evidence")
    if not isinstance(evidence, dict):
        raise SafetyAuthorityError("Safety map identity evidence is malformed.")
    evidence_kind = evidence.get("kind")
    if evidence_kind == "register":
        required_register = {
            "kind",
            "address",
            "expected",
            "mask",
            "width_bits",
            "label",
            "provenance",
            "masked_observed",
        }
        if set(evidence) != required_register:
            raise SafetyAuthorityError("Safety map register identity evidence is incomplete.")
        for field in {"address", "expected", "mask", "width_bits", "masked_observed"}:
            _int(evidence.get(field), f"identity evidence {field}")
        if not all(
            isinstance(evidence.get(field), str) and evidence[field]
            for field in {"label", "provenance"}
        ):
            raise SafetyAuthorityError("Safety map register identity provenance is malformed.")
    elif evidence_kind == "provider":
        if set(evidence) != {"kind", "provenance", "support_identity", "evidence"}:
            raise SafetyAuthorityError("Safety map provider identity evidence is incomplete.")
        if not all(
            isinstance(evidence.get(field), str) and evidence[field]
            for field in {"provenance", "support_identity"}
        ) or not isinstance(evidence.get("evidence"), dict):
            raise SafetyAuthorityError("Safety map provider identity evidence is malformed.")
    elif evidence_kind == "unavailable":
        if (
            set(evidence) != {"kind", "reason"}
            or not isinstance(evidence.get("reason"), str)
            or not evidence["reason"]
        ):
            raise SafetyAuthorityError(
                "Safety map unavailable identity evidence requires a reason."
            )
    else:
        raise SafetyAuthorityError("Safety map identity evidence kind is malformed.")
    if capability == "exact":
        if (
            status != "matched"
            or not isinstance(exact_part, str)
            or not exact_part
            or evidence_kind not in {"register", "provider"}
        ):
            raise SafetyAuthorityError("Safety map exact identity evidence is incomplete.")
    elif capability == "compatible":
        if (
            status != "compatible"
            or exact_part is not None
            or evidence_kind not in {"register", "provider"}
        ):
            raise SafetyAuthorityError(
                "Safety map compatible identity must not claim an exact part."
            )
    elif status != "unavailable" or exact_part is not None or evidence_kind != "unavailable":
        raise SafetyAuthorityError("Safety map unavailable identity evidence is malformed.")
    sources = document.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SafetyAuthorityError("Safety map sources are malformed.")
    source_kinds: dict[str, str] = {}
    source_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise SafetyAuthorityError("Safety map sources are malformed.")
        kind = source.get("kind")
        if not isinstance(kind, str):
            raise SafetyAuthorityError("Safety map source kind is malformed.")
        expected = {
            "provider": {"kind", "identifier", "detail"},
            "provider-region": {
                "kind",
                "identifier",
                "detail",
                "order",
                "start",
                "end",
                "physical_kind",
                "readable",
                "writable",
                "executable",
                "name",
            },
            "layout": {"kind", "identifier", "detail", "sha256"},
            "layout-source": {"kind", "identifier", "detail", "sha256"},
            "elf": {"kind", "identifier", "detail", "sha256"},
            "support-geometry": {"kind", "identifier", "detail", "sha256"},
            "derived-role": {"kind", "identifier", "detail", "sources"},
        }.get(kind)
        if expected is None or set(source) != expected:
            raise SafetyAuthorityError(
                "Safety map source has unexpected or missing canonical fields."
            )
        identifier, detail = source.get("identifier"), source.get("detail")
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(detail, str)
            or not detail
        ):
            raise SafetyAuthorityError("Safety map source identifier and detail are required.")
        if identifier in source_ids:
            raise SafetyAuthorityError("Safety map source identifiers must be unique.")
        source_ids.add(identifier)
        if kind in {"layout", "layout-source", "elf", "support-geometry"} and (
            not isinstance(source.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
        ):
            raise SafetyAuthorityError("Safety map source SHA-256 is malformed.")
        if kind == "provider-region":
            if (
                isinstance(source.get("order"), bool)
                or not isinstance(source.get("order"), int)
                or _int(source.get("start"), "provider-region start")
                >= _int(source.get("end"), "provider-region end")
                or not isinstance(source.get("physical_kind"), str)
                or not source["physical_kind"]
                or not isinstance(source.get("name"), str)
                or not all(
                    isinstance(source.get(key), bool)
                    for key in ("readable", "writable", "executable")
                )
            ):
                raise SafetyAuthorityError("Safety map provider-native source is malformed.")
        if kind == "derived-role" and (
            not isinstance(source.get("sources"), list)
            or not source["sources"]
            or source["sources"] != sorted(set(source["sources"]))
            or not all(isinstance(ref, str) and ref for ref in source["sources"])
        ):
            raise SafetyAuthorityError("Safety map derived role source is malformed.")
        source_kinds[_source_ref(identifier)] = kind
    for source in sources:
        if source["kind"] == "derived-role" and not set(source["sources"]).issubset(source_kinds):
            raise SafetyAuthorityError("Safety map derived role does not cite its physical source.")
        if source["kind"] == "derived-role" and any(
            source_kinds[ref] not in {"provider-region", "support-geometry"}
            for ref in source["sources"]
        ):
            raise SafetyAuthorityError(
                "Safety map derived role must cite explicit physical source evidence."
            )
    provider_sources = [source for source in sources if source["kind"] == "provider"]
    if (
        len(provider_sources) != 1
        or provider_sources[0]["identifier"] != identity["provider_support_identity"]
    ):
        raise SafetyAuthorityError(
            "Safety map provider support source is incomplete or mismatched."
        )
    native_sources = [source for source in sources if source["kind"] == "provider-region"]
    if [source["order"] for source in native_sources] != list(range(len(native_sources))):
        raise SafetyAuthorityError(
            "Safety map provider-native records must retain their exact ordered boundaries."
        )
    checked = dict(document)
    checked["regions"] = _check_regions(document["regions"], source_kinds)
    erase = document.get("erase_geometry")
    if not isinstance(erase, dict) or set(erase) not in (
        {"available", "ranges"},
        {"available", "reason"},
    ):
        raise SafetyAuthorityError(
            "Safety map erase geometry must state exact ranges or explicit unavailability."
        )
    if erase.get("available") is True:
        ranges = erase.get("ranges")
        if not isinstance(ranges, list) or not ranges:
            raise SafetyAuthorityError("Safety map erase ranges are malformed.")
        previous_end = -1
        for index, raw_range in enumerate(ranges):
            if not isinstance(raw_range, dict) or set(raw_range) != {
                "start",
                "end",
                "provenance",
            }:
                raise SafetyAuthorityError(
                    "Safety map erase ranges require start, end, and provenance facts."
                )
            start = _int(raw_range.get("start"), "erase range start")
            end = _int(raw_range.get("end"), "erase range end")
            refs = raw_range.get("provenance")
            if (
                end <= start
                or end > 1 << 64
                or start < previous_end
                or not isinstance(refs, list)
                or not refs
                or refs != sorted(set(refs))
                or not all(isinstance(ref, str) and ref for ref in refs)
            ):
                raise SafetyAuthorityError(
                    "Safety map erase ranges must be sorted, non-overlapping half-open evidence ranges."
                )
            if not set(refs).issubset(source_kinds):
                raise SafetyAuthorityError(
                    "Safety map erase provenance does not reference a canonical source record."
                )
            if any(
                source_kinds[ref] not in {"provider-region", "support-geometry"} for ref in refs
            ):
                raise SafetyAuthorityError(
                    "Safety map erase provenance cites an inappropriate source kind."
                )
            cursor = start
            for region in checked["regions"]:
                if region["end"] <= cursor:
                    continue
                if region["start"] > cursor:
                    break
                physical_kind = str(region["kind"]).removeprefix("physical_").casefold()
                if physical_kind != "flash":
                    raise SafetyAuthorityError(
                        "Safety map erase range is not contained in persisted flash evidence."
                    )
                cursor = min(end, region["end"])
                if cursor == end:
                    break
            if cursor != end:
                raise SafetyAuthorityError(
                    f"Safety map erase range {index} is not contained in persisted physical evidence."
                )
            previous_end = end
    elif erase.get("available") is False:
        if not isinstance(erase.get("reason"), str) or not erase["reason"]:
            raise SafetyAuthorityError("Safety map unavailable erase geometry requires a reason.")
    else:
        raise SafetyAuthorityError("Safety map erase geometry availability is malformed.")
    if not isinstance(document.get("digest"), str) or document["digest"] != map_digest(document):
        raise SafetyAuthorityError(
            "Safety map digest does not match its canonical semantic content."
        )
    return checked


def _physical_regions(
    handle: TargetSessionHandle, regions_for: Any
) -> tuple[PhysicalMemoryRegion, ...]:
    raw = regions_for(handle)
    regions = tuple(sorted(tuple(raw), key=lambda item: (item.start, item.end, item.name)))
    metadata = handle.metadata
    if metadata is None:
        raise SafetyAuthorityError(
            "Live provider session evidence is unavailable; reconnect and validate."
        )
    previous = -1
    checked: list[PhysicalMemoryRegion] = []
    for item in regions:
        try:
            record = PhysicalMemoryRegion.from_record(item.to_record())
        except ValueError as exc:
            raise SafetyAuthorityError(
                f"Live provider physical record is malformed: {exc}"
            ) from exc
        if record.session_token != metadata.runtime_token or record.start < previous:
            raise SafetyAuthorityError(
                "Live provider physical records are stale or overlap ambiguously; reconnect and validate."
            )
        previous = record.end
        checked.append(record)
    return tuple(checked)


def _identity(
    handle: TargetSessionHandle,
    read_memory: Any | None = None,
    configured_part_number: str | None = None,
) -> dict[str, Any]:
    return observe_live_identity(
        handle, read_memory=read_memory, configured_part_number=configured_part_number
    ).to_record()


def _role_for(kind: str) -> str:
    """Translate only explicit provider physical kinds, never addresses/names."""

    return {
        "ram": "ordinary_ram",
        "physical_ram": "ordinary_ram",
        "rom": "rom",
        "physical_rom": "rom",
        "peripheral": "peripheral",
        "physical_peripheral": "peripheral",
    }.get(kind.casefold(), "unknown")


def build_document(
    *,
    board_id: str,
    handle: TargetSessionHandle,
    regions_for: Any,
    layout: Mapping[str, Any] | None = None,
    layout_source_payloads: Mapping[str, bytes] | None = None,
    application_elf: tuple[Path, bytes] | None = None,
    read_memory: Any | None = None,
    support_geometry: Mapping[str, Any] | None = None,
    support_identity: str | None = None,
    configured_part_number: str | None = None,
) -> dict[str, Any]:
    """Build one candidate from current provider facts and exact selected bytes."""
    identity = _identity(handle, read_memory, configured_part_number)
    physical = _physical_regions(handle, regions_for)
    if not physical:
        raise SafetyAuthorityError(
            "Current provider exposed no physical memory regions; reconnect and validate before refreshing the safety map."
        )
    sources: list[dict[str, Any]] = [
        {
            "kind": "provider",
            "identifier": identity["provider_support_identity"],
            "detail": "current provider support identity",
        }
    ]
    # Each replayed support range owns a canonical source record.  That keeps
    # compatible overlaps auditable and lets a conflict name both exact facts.
    support_facts: list[dict[str, Any]] = []
    for order, region in enumerate(physical):
        sources.append(
            {
                "kind": "provider-region",
                "identifier": f"provider-region-{order}",
                "detail": "current provider-native physical memory record",
                "order": order,
                "start": region.start,
                "end": region.end,
                "physical_kind": region.kind,
                "readable": region.readable,
                "writable": region.writable,
                "executable": region.executable,
                "name": region.name,
            }
        )
    erase_geometry: dict[str, Any] = {
        "available": False,
        "reason": "The replayed exact support source exposes no erase geometry.",
    }
    if support_geometry is not None:
        if support_identity != identity["provider_support_identity"]:
            raise SafetyAuthorityError(
                "Replayed support geometry is not bound to this exact provider support identity."
            )
        encoded_geometry = json.dumps(
            dict(support_geometry), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        geometry_id = f"support-geometry-{hashlib.sha256(encoded_geometry).hexdigest()}"
        sources.append(
            {
                "kind": "support-geometry",
                "identifier": geometry_id,
                "detail": "replayed exact device-support geometry; SVD capability "
                + str(
                    support_geometry.get("svd_register_capability", {}).get("status", "unavailable")
                    if isinstance(support_geometry.get("svd_register_capability"), Mapping)
                    else "unavailable"
                ),
                "sha256": hashlib.sha256(encoded_geometry).hexdigest(),
            }
        )
        for group, expected_kind in (
            ("flash_regions", "flash"),
            ("ram_regions", "ram"),
            ("rom_regions", "rom"),
            ("peripheral_regions", "peripheral"),
            ("cpu_system_regions", "cpu_system"),
        ):
            entries = support_geometry.get(group, [])
            if not isinstance(entries, list):
                raise SafetyAuthorityError("Replayed support geometry regions are malformed.")
            for index, fact in enumerate(entries):
                if not isinstance(fact, Mapping):
                    raise SafetyAuthorityError("Replayed support geometry region is malformed.")
                start, end = (
                    _int(fact.get("start"), "support start"),
                    _int(fact.get("end"), "support end"),
                )
                if end <= start:
                    raise SafetyAuthorityError("Replayed support geometry range is malformed.")
                access = fact.get("access", "r")
                if not isinstance(access, str):
                    raise SafetyAuthorityError("Replayed support geometry access is malformed.")
                try:
                    readable, writable, executable = access_facts(access)
                except PackProvisionError as exc:
                    raise SafetyAuthorityError(
                        f"Replayed support geometry access is malformed: {exc}"
                    ) from exc
                identifier = f"{geometry_id}-{group}-{index}"
                sources.append(
                    {
                        "kind": "support-geometry",
                        "identifier": identifier,
                        "detail": f"replayed {group}[{index}] exact physical range",
                        "sha256": hashlib.sha256(
                            json.dumps(
                                dict(fact),
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ).encode("utf-8")
                        ).hexdigest(),
                    }
                )
                support_facts.append(
                    {
                        "start": start,
                        "end": end,
                        "kind": expected_kind,
                        "name": str(fact.get("name", f"{group}[{index}]")),
                        "readable": readable,
                        "writable": writable,
                        "executable": executable,
                        "source": _source_ref(identifier),
                    }
                )
        sectors = support_geometry.get("erase_sectors", [])
        if not isinstance(sectors, list):
            raise SafetyAuthorityError("Replayed support erase geometry is malformed.")
        if sectors:
            ranges: list[dict[str, Any]] = []
            previous = -1
            for entry in sectors:
                if not isinstance(entry, Mapping):
                    raise SafetyAuthorityError("Replayed support erase sector is malformed.")
                start, end = (
                    _int(entry.get("start"), "erase sector start"),
                    _int(entry.get("end"), "erase sector end"),
                )
                if end <= start or start < previous:
                    raise SafetyAuthorityError(
                        "Replayed support erase sectors overlap or are malformed."
                    )
                previous = end
                ranges.append(
                    {"start": start, "end": end, "provenance": [_source_ref(geometry_id)]}
                )
            erase_geometry = {"available": True, "ranges": ranges}
    semantic: list[tuple[int, int, str, str]] = []
    if layout is not None:
        if (
            layout.get("schema_version") != 1
            or layout.get("board_id") != board_id
            or not isinstance(layout.get("regions"), list)
        ):
            raise SafetyAuthorityError(
                "Layout must have schema_version=1, this board_id, and regions."
            )
        for index, fact in enumerate(layout["regions"]):
            if not isinstance(fact, dict):
                raise SafetyAuthorityError(f"Layout region {index} is malformed.")
            role, start, end = (
                fact.get("role"),
                _int(fact.get("start"), "layout start"),
                _int(fact.get("end"), "layout end"),
            )
            if (
                role not in ROLES - {"unknown"}
                or end <= start
                or not isinstance(fact.get("source_locator"), str)
                or not fact["source_locator"]
            ):
                raise SafetyAuthorityError(
                    "Layout facts require a known role, half-open range, and source_locator."
                )
            if not isinstance(fact.get("source_path"), str) or not fact["source_path"]:
                raise SafetyAuthorityError(
                    "Layout facts require an existing source_path and source_locator."
                )
            source_path = Path(fact["source_path"])
            identifier = str(source_path.expanduser().resolve())
            if layout_source_payloads is not None:
                try:
                    raw = layout_source_payloads[identifier]
                except KeyError as exc:
                    raise SafetyAuthorityError(
                        f"Guarded layout evidence snapshot is unavailable for {identifier}; create a new refresh plan."
                    ) from exc
            else:
                try:
                    raw = source_path.read_bytes()
                except OSError as exc:
                    raise SafetyAuthorityError(
                        f"Layout evidence file is unavailable: {exc}"
                    ) from exc
            if not any(source["identifier"] == identifier for source in sources):
                sources.append(
                    {
                        "kind": "layout-source",
                        "identifier": identifier,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "detail": fact["source_locator"],
                    }
                )
            semantic.append((start, end, str(role), identifier))
    if application_elf is not None:
        path, payload = application_elf
        digest = hashlib.sha256(payload).hexdigest()
        identifier = str(path.expanduser().resolve())
        sources.append(
            {
                "kind": "elf",
                "identifier": identifier,
                "sha256": digest,
                "detail": "file-backed PT_LOAD ranges",
            }
        )
        for start, end in file_backed_elf_ranges(path, payload):
            semantic.append((start, end, "application", identifier))

    def normalized_kind(kind: str) -> str:
        return kind.casefold().removeprefix("physical_")

    # A semantic claim cannot disappear merely because it falls outside live
    # evidence.  Replayed support geometry is still physical evidence for map
    # publication, but it is deliberately not a replacement for live access at
    # operation time (``SafetyAuthority.require`` proves that separately).
    for left, right, role, _reference in semantic:
        cursor = left
        while cursor < right:
            live = next((item for item in physical if item.start <= cursor < item.end), None)
            support = next(
                (item for item in support_facts if int(item["start"]) <= cursor < int(item["end"])),
                None,
            )
            if live is None and support is None:
                raise SafetyAuthorityError(
                    f"Semantic role {role} is outside explicit physical evidence at 0x{cursor:X}-0x{right:X}."
                )
            if live is not None:
                kind = normalized_kind(live.kind)
            else:
                assert support is not None
                kind = str(support["kind"])
            if role == "ordinary_ram" and kind != "ram":
                raise SafetyAuthorityError(
                    f"Semantic ordinary_ram conflicts with {kind} at 0x{cursor:X}."
                )
            if role == "peripheral" and kind != "peripheral":
                raise SafetyAuthorityError(
                    f"Semantic peripheral conflicts with {kind} at 0x{cursor:X}."
                )
            if role == "rom" and kind != "rom":
                raise SafetyAuthorityError(f"Semantic rom conflicts with {kind} at 0x{cursor:X}.")
            cursor = min(
                right,
                live.end if live is not None else right,
                int(support["end"]) if support is not None else right,
            )
    boundaries = (
        {point for region in physical for point in (region.start, region.end)}
        | {point for fact in support_facts for point in (int(fact["start"]), int(fact["end"]))}
        | {point for start, end, _, _ in semantic for point in (start, end)}
    )
    ordered = sorted(boundaries)
    output: list[dict[str, Any]] = []
    for start, end in zip(ordered, ordered[1:]):
        enclosing = [region for region in physical if region.start <= start and end <= region.end]
        supporting = [
            fact
            for fact in support_facts
            if int(fact["start"]) <= start and end <= int(fact["end"])
        ]
        if not enclosing and not supporting:
            continue
        if len(enclosing) > 1:
            raise SafetyAuthorityError(
                f"Conflicting provider physical facts at 0x{start:X}-0x{end:X}; reconnect and validate before refreshing."
            )
        if supporting:
            support_signature = (
                str(supporting[0]["kind"]),
                bool(supporting[0]["readable"]),
                bool(supporting[0]["writable"]),
                bool(supporting[0]["executable"]),
            )
            for fact in supporting[1:]:
                signature = (
                    str(fact["kind"]),
                    bool(fact["readable"]),
                    bool(fact["writable"]),
                    bool(fact["executable"]),
                )
                if signature != support_signature:
                    raise SafetyAuthorityError(
                        "Conflicting replayed support physical facts at "
                        f"0x{start:X}-0x{end:X}: {supporting[0]['source']} and {fact['source']}."
                    )
        region = enclosing[0] if enclosing else None
        native_refs: list[str] = []
        if region is not None:
            native_order = physical.index(region)
            native_refs.append(_source_ref(f"provider-region-{native_order}"))
        support_refs = sorted({str(fact["source"]) for fact in supporting})
        if region is not None and supporting:
            expected = (
                normalized_kind(region.kind),
                region.readable,
                region.writable,
                region.executable,
            )
            observed = (
                str(supporting[0]["kind"]),
                bool(supporting[0]["readable"]),
                bool(supporting[0]["writable"]),
                bool(supporting[0]["executable"]),
            )
            if observed != expected:
                raise SafetyAuthorityError(
                    "Replayed support physical facts conflict with live provider physical facts at "
                    f"0x{start:X}-0x{end:X}: {supporting[0]['source']} versus {native_refs[0]}."
                )
        if region is not None:
            kind, name = region.kind, region.name
            readable, writable, executable = (
                region.readable,
                region.writable,
                region.executable,
            )
        else:
            kind = f"physical_{supporting[0]['kind']}"
            name = str(supporting[0]["name"])
            readable, writable, executable = (
                bool(supporting[0]["readable"]),
                bool(supporting[0]["writable"]),
                bool(supporting[0]["executable"]),
            )
        physical_refs = sorted({*native_refs, *support_refs})
        # Exact live/support compatibility establishes every access fact,
        # including explicitly false values. Provenance must not suggest
        # that a compatible support source observed only granted access.
        fact_provenance: dict[str, list[str]] = {
            "physical": physical_refs,
            "readable": physical_refs,
            "writable": physical_refs,
            "executable": physical_refs,
        }
        facts = [
            (role, reference)
            for left, right, role, reference in semantic
            if left <= start and end <= right
        ]
        roles = {role for role, _ in facts}
        if len(roles) > 1:
            raise SafetyAuthorityError(
                f"Conflicting semantic roles at 0x{start:X}-0x{end:X}: {sorted(roles)}."
            )
        role = next(iter(roles), _role_for(kind))
        if facts:
            role_refs = sorted({_source_ref(reference) for _, reference in facts})
        else:
            derived_id = (
                "derived-role-"
                + hashlib.sha256("|".join(physical_refs).encode("utf-8")).hexdigest()[:16]
            )
            if not any(source["identifier"] == derived_id for source in sources):
                sources.append(
                    {
                        "kind": "derived-role",
                        "identifier": derived_id,
                        "detail": f"{role} derives from explicit physical kind {kind}",
                        "sources": physical_refs,
                    }
                )
            role_refs = [_source_ref(derived_id)]
        output.append(
            {
                "start": start,
                "end": end,
                "kind": kind,
                "role": role,
                "readable": readable,
                "writable": writable,
                "executable": executable,
                "name": name,
                "provenance": {
                    **fact_provenance,
                    "role": role_refs,
                },
            }
        )
    document: dict[str, Any] = {
        "schema_version": 2,
        "board_id": board_id,
        "identity": identity,
        "sources": sources,
        "regions": output,
        "erase_geometry": erase_geometry,
    }
    document["digest"] = map_digest(document)
    return validate_document(document, board_id=board_id)


class SafetyAuthority:
    def __init__(
        self, store: FirmStore, profiles: ProfileRepository, regions_for: Any, read_memory: Any
    ) -> None:
        self.store, self.profiles, self.regions_for, self.read_memory = (
            store,
            profiles,
            regions_for,
            read_memory,
        )

    def path_for(self, board_id: str) -> Path:
        return self.store.layout.safety_board(board_id) / "memory-map.json"

    def load(self, board_id: str, profile: BoardProfile | None = None) -> dict[str, Any]:
        profile = profile or self.profiles.load(board_id)
        expected = self.path_for(board_id).relative_to(self.store.layout.project_root).as_posix()
        if profile.safety_ref != expected:
            raise SafetyAuthorityError(
                "Safety map is missing or not associated with this current profile; create a refresh_safety_map plan."
            )
        try:
            raw = json.loads(self.path_for(board_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SafetyAuthorityError(f"Safety map cannot be loaded: {exc}") from exc
        return validate_document(raw, board_id=board_id)

    def binding(self, board_id: str, handle: TargetSessionHandle) -> dict[str, Any]:
        profile = self.profiles.load(board_id)
        document = self.load(board_id, profile)
        configured_part_number = getattr(profile, "mcu_part_number", None)
        if configured_part_number is None:
            configured_part_number = getattr(handle.board, "silicon_id_bound_part_number", None)
        try:
            identity = _identity(handle, self.read_memory, configured_part_number)
        except LiveIdentityContradiction as exc:
            raise SafetyAuthorityError(
                "Safety map identity contradicts the current live target; reconnect or inspect "
                "the configured board before refreshing the map."
            ) from exc
        except LiveIdentityObservationError as exc:
            raise SafetyAuthorityError(
                "Safety map identity observation failed before comparison; reconnect and retry "
                "the live identity read before refreshing the map."
            ) from exc
        if document["identity"] != identity:
            raise SafetyAuthorityError(
                "Safety map identity is stale for this live target; refresh_safety_map after validating the board."
            )
        persisted = tuple(
            (
                source["order"],
                source["start"],
                source["end"],
                source["physical_kind"],
                source["readable"],
                source["writable"],
                source["executable"],
                source["name"],
            )
            for source in document["sources"]
            if source["kind"] == "provider-region"
        )
        live = _physical_regions(handle, self.regions_for)
        current = tuple(
            (
                order,
                item.start,
                item.end,
                item.kind,
                item.readable,
                item.writable,
                item.executable,
                item.name,
            )
            for order, item in enumerate(live)
        )
        if persisted != current:
            raise SafetyAuthorityError(
                "Safety map provider-native physical-region facts are stale or conflicted; refresh_safety_map before this operation."
            )
        return {
            "safety_map_path": str(self.path_for(board_id).resolve()),
            "safety_map_digest": str(document["digest"]),
            "live_identity": document["identity"],
        }

    def current_document(self, board_id: str, handle: TargetSessionHandle) -> dict[str, Any]:
        """Return the one current, identity- and region-fresh map document."""

        self.binding(board_id, handle)
        return self.load(board_id)

    def describe(
        self, board_id: str, handle: TargetSessionHandle, start: int, length: int, access: str
    ) -> dict[str, Any]:
        """Describe complete live/map-covered bytes without a role policy decision."""

        detail = self.require(board_id, handle, start, length, access, allow_unknown_read=True)
        document = self.load(board_id)
        end, cursor, covered = start + length, start, []
        for region in document["regions"]:
            if region["end"] <= cursor:
                continue
            if region["start"] > cursor:
                break
            covered.append(
                {
                    key: region[key]
                    for key in (
                        "start",
                        "end",
                        "kind",
                        "name",
                        "role",
                        "readable",
                        "writable",
                        "executable",
                        "provenance",
                    )
                }
            )
            cursor = min(end, region["end"])
            if cursor == end:
                break
        return {**detail, "regions": covered, "start": start, "end": end}

    def classify_write(
        self, board_id: str, handle: TargetSessionHandle, start: int, length: int
    ) -> dict[str, Any]:
        """Classify one fully writable span from current semantic evidence."""

        detail = self.describe(board_id, handle, start, length, "write")
        roles = set(detail["roles"])
        if "unknown" in roles:
            raise SafetyAuthorityError(
                "Safety-map role evidence is unknown; refresh support/layout evidence before writing."
            )
        special = {
            "application",
            "bootloader",
            "option",
            "otp",
            "security",
            "lifecycle",
            "sensitive",
        }
        return {**detail, "risk": "destructive" if roles & special else "routine"}

    def touched_erase_sectors(
        self, board_id: str, handle: TargetSessionHandle, ranges: list[tuple[int, int]]
    ) -> list[dict[str, Any]]:
        document = self.current_document(board_id, handle)
        erase = document["erase_geometry"]
        if erase.get("available") is not True:
            raise SafetyAuthorityError(
                "Exact erase geometry is unavailable; refresh support/layout evidence before flashing."
            )
        sectors: list[dict[str, Any]] = []
        for sector in erase["ranges"]:
            if any(start < sector["end"] and end > sector["start"] for start, end in ranges):
                self.describe(
                    board_id, handle, sector["start"], sector["end"] - sector["start"], "write"
                )
                sectors.append(dict(sector))
        if not sectors:
            raise SafetyAuthorityError("No exact erase sector covers the parsed flash image.")
        return sectors

    def validate_flash_role(
        self,
        board_id: str,
        handle: TargetSessionHandle,
        ranges: list[tuple[int, int]],
        flash_role: str,
    ) -> dict[str, Any]:
        allowed = {
            "application": {"application"},
            "bootloader": {"bootloader"},
            "sensitive": {"option", "otp", "security", "lifecycle", "sensitive"},
            "full-device": None,
        }
        if flash_role not in allowed:
            raise SafetyAuthorityError("flash_role is invalid.")
        effects = [
            self.describe(board_id, handle, start, end - start, "write") for start, end in ranges
        ]
        sectors = self.touched_erase_sectors(board_id, handle, ranges)
        for sector in sectors:
            detail = self.describe(
                board_id, handle, sector["start"], sector["end"] - sector["start"], "write"
            )
            effects.append(detail)
        if any(
            str(region["kind"]).casefold().removeprefix("physical_") != "flash"
            for item in effects
            for region in item["regions"]
        ):
            raise SafetyAuthorityError(
                "Parsed image or touched erase sector is not complete writable physical flash; refresh live provider evidence."
            )
        roles = {role for item in effects for role in item["roles"]}
        if "unknown" in roles or (
            allowed[flash_role] is not None and not roles.issubset(allowed[flash_role])
        ):
            raise SafetyAuthorityError(
                "Parsed image or touched erase sector is outside the declared flash_role; refresh the map or select the exact role."
            )
        return {
            "roles": sorted(roles),
            "effects": effects,
            "sectors": sectors,
            "map_digest": self.binding(board_id, handle)["safety_map_digest"],
        }

    def resolve_recovery(
        self,
        board_id: str,
        handle: TargetSessionHandle,
        capability: RecoveryCapability,
    ) -> list[dict[str, Any]]:
        """Resolve one typed provider capability to current map-covered effects.

        The capability itself never grants access: both variants are reduced to
        complete current provider and semantic-map write coverage here, under
        the same identity/region freshness binding used by all map operations.
        """

        document = self.current_document(board_id, handle)
        coverage = capability.coverage
        kind = coverage["kind"]
        ranges: list[tuple[int, int]]
        if kind == "all_matching":
            raw_kinds = coverage.get("physical_kinds")
            if not isinstance(raw_kinds, list) or not all(
                isinstance(item, str) for item in raw_kinds
            ):
                raise SafetyAuthorityError("Recovery capability physical kinds are malformed.")
            physical_kinds = set(raw_kinds)
            ranges = [
                (int(region["start"]), int(region["end"]))
                for region in document["regions"]
                if (
                    str(region["kind"]) in physical_kinds
                    or f"physical_{str(region['kind']).casefold().removeprefix('physical_')}"
                    in physical_kinds
                )
            ]
        elif kind == "exact_ranges":
            raw_ranges = coverage.get("ranges")
            if not isinstance(raw_ranges, list) or not all(
                isinstance(item, dict) for item in raw_ranges
            ):
                raise SafetyAuthorityError("Recovery capability exact ranges are malformed.")
            ranges = [(int(item["start"]), int(item["end"])) for item in raw_ranges]
        else:  # RecoveryCapability.from_record makes this unreachable.
            raise SafetyAuthorityError("Recovery capability coverage is malformed.")
        if not ranges:
            raise SafetyAuthorityError(
                "Recovery capability resolves to no current provider regions; reconnect and refresh_safety_map."
            )
        effects = [
            self.describe(board_id, handle, start, end - start, "write") for start, end in ranges
        ]
        if any("unknown" in effect["roles"] for effect in effects):
            raise SafetyAuthorityError(
                "Recovery capability reaches an unknown semantic role; refresh support/layout evidence first."
            )
        # Describe() validates exact provider containment for exact_ranges and
        # all current map partitions for all_matching. Preserve the resolved
        # half-open union, not a provider descriptor shorthand, in disclosure.
        return effects

    def require(
        self,
        board_id: str,
        handle: TargetSessionHandle,
        start: int,
        length: int,
        access: str,
        *,
        roles: set[str] | None = None,
        allow_unknown_read: bool = False,
    ) -> dict[str, Any]:
        require_live_physical_access(
            handle,
            start,
            length,
            access,
            regions_for=self.regions_for,
            read_memory=self.read_memory,
        )
        # A semantic map is never a cache replacement: every operation first
        # proves the profile association, exact identity, and provider regions
        # are still the evidence from which this digest was published.
        binding = self.binding(board_id, handle)
        document = self.load(board_id)
        cursor, end = start, start + length
        matching: list[dict[str, Any]] = []
        for region in document["regions"]:
            if region["end"] <= cursor:
                continue
            if region["start"] > cursor:
                break
            if not region[
                {"read": "readable", "write": "writable", "execute": "executable"}[access]
            ]:
                raise SafetyAuthorityError(
                    f"Safety map {document['digest']} denies {access} at 0x{cursor:X}; refresh_safety_map or select the correct address/tool."
                )
            if roles is not None and region["role"] not in roles:
                if not (access == "read" and allow_unknown_read and region["role"] == "unknown"):
                    raise SafetyAuthorityError(
                        f"Safety map {document['digest']} records role {region['role']} at 0x{cursor:X}; refresh_safety_map or select the correct address/tool."
                    )
            matching.append(region)
            cursor = min(end, region["end"])
            if cursor == end:
                return {
                    "digest": binding["safety_map_digest"],
                    "roles": [item["role"] for item in matching],
                    "unknown": any(item["role"] == "unknown" for item in matching),
                }
        raise SafetyAuthorityError(
            f"Safety map {document['digest']} does not cover requested range 0x{cursor:X}-0x{end:X}; refresh_safety_map."
        )
