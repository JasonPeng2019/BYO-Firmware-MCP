"""Shared probe inventory and board-aware selection helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from pyocd.core.helpers import ConnectHelper  # type: ignore[import-untyped]
from pyocd.probe.aggregator import PROBE_CLASSES  # type: ignore[import-untyped]

from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.probe_families import (
    configured_probe_cli_commands,
    match_probe_family_text,
    provider_qualified_family,
)

RunCommand = Callable[[list[str]], tuple[int, str, str]]

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ROW_RE = re.compile(
    r"^\s*(?P<index>\d+)\s{2,}(?P<description>.+?)\s{2,}(?P<uid>\S+)(?:\s{2,}(?P<state>\S.*))?\s*$"
)


@dataclass(frozen=True)
class ProbeInfo:
    uid: str
    description: str
    raw: str
    state: str = ""
    family: str = "unknown"
    family_source: str = "unknown"

    @property
    def searchable_text(self) -> str:
        return f"{self.uid} {self.description} {self.raw}".lower()


@dataclass(frozen=True)
class ProbeResolution:
    probe: ProbeInfo | None
    note: str
    probes: tuple[ProbeInfo, ...]


def _normalize_text(value: object) -> str:
    return str(value or "").strip()


def _append_unique_text(parts: list[str], value: object) -> None:
    text = _normalize_text(value)
    if text and text not in parts:
        parts.append(text)


def probe_family_from_pyocd_probe(probe: Any) -> str:
    """Return pyOCD's registered provider key for a live probe object."""

    for provider_id, probe_class in PROBE_CLASSES.items():
        if isinstance(probe, probe_class):
            return str(provider_id).casefold()
    return "unknown"


def _probe_info_from_pyocd_probe(probe: Any) -> ProbeInfo | None:
    uid = _normalize_text(getattr(probe, "unique_id", ""))
    description_parts: list[str] = []
    raw_parts: list[str] = []

    _append_unique_text(description_parts, getattr(probe, "description", ""))
    _append_unique_text(raw_parts, getattr(probe, "description", ""))
    _append_unique_text(raw_parts, uid)

    board_info = getattr(probe, "associated_board_info", None)
    if board_info is not None:
        _append_unique_text(description_parts, getattr(board_info, "name", ""))
        _append_unique_text(raw_parts, getattr(board_info, "vendor", ""))
        _append_unique_text(raw_parts, getattr(board_info, "name", ""))
        _append_unique_text(raw_parts, getattr(board_info, "target", ""))

    description = " ".join(description_parts).strip()
    raw = " | ".join(raw_parts).strip()
    if not uid or not description:
        return None

    family = probe_family_from_pyocd_probe(probe)
    return ProbeInfo(
        uid=uid,
        description=description,
        raw=raw,
        family=family,
        family_source="pyocd_api" if family != "unknown" else "unknown",
    )


def _list_connected_probes_via_pyocd_api() -> list[ProbeInfo]:
    probes: list[ProbeInfo] = []
    for probe in ConnectHelper.get_all_connected_probes(
        blocking=False,
        print_wait_message=False,
    ):
        parsed = _probe_info_from_pyocd_probe(probe)
        if parsed is not None:
            probes.append(parsed)
    return probes


def parse_pyocd_probe_listing(output: str) -> list[ProbeInfo]:
    """Parse `pyocd list --probes` table output into structured rows."""

    probes: list[ProbeInfo] = []
    current_index: int | None = None

    for line in output.splitlines():
        raw = _ANSI_RE.sub("", line).rstrip()
        stripped = raw.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if (
            stripped.startswith("#")
            or lowered.startswith("probe/board")
            or lowered.startswith("no available debug probes")
            or re.fullmatch(r"-+", stripped)
        ):
            continue

        match = _ROW_RE.match(raw)
        if match:
            uid = match.group("uid").strip()
            family = provider_qualified_family(uid)
            source = "provider_qualified_uid" if family is not None else "legacy_text_fallback"
            if family is None:
                family = match_probe_family_text(raw)
                if family == "unknown":
                    source = "unknown"
            probe = ProbeInfo(
                uid=uid,
                description=match.group("description").strip(),
                state=(match.group("state") or "").strip(),
                raw=raw,
                family=family,
                family_source=source,
            )
            probes.append(probe)
            current_index = len(probes) - 1
            continue

        columns = re.split(r"\s{2,}", stripped)
        if len(columns) >= 3 and columns[0].isdigit():
            description = columns[1].strip()
            uid = columns[2].strip()
            state = columns[3].strip() if len(columns) >= 4 else ""
            if description and uid:
                family = provider_qualified_family(uid)
                source = (
                    "provider_qualified_uid" if family is not None else "legacy_text_fallback"
                )
                if family is None:
                    family = match_probe_family_text(raw)
                    if family == "unknown":
                        source = "unknown"
                probe = ProbeInfo(
                    uid=uid,
                    description=description,
                    state=state,
                    raw=raw,
                    family=family,
                    family_source=source,
                )
                probes.append(probe)
                current_index = len(probes) - 1
                continue

        if current_index is not None:
            current = probes[current_index]
            combined_description = f"{current.description} {stripped}".strip()
            combined_raw = f"{current.raw}\n{raw}"
            family = provider_qualified_family(current.uid)
            source = "provider_qualified_uid" if family is not None else "legacy_text_fallback"
            if family is None:
                family = match_probe_family_text(f"{combined_description} {combined_raw}")
                if family == "unknown":
                    source = "unknown"
            probes[current_index] = ProbeInfo(
                uid=current.uid,
                description=combined_description,
                state=current.state,
                raw=combined_raw,
                family=family,
                family_source=source,
            )

    return probes


def list_connected_probes(
    run_cmd: RunCommand,
    *,
    allow_subprocess_fallback: bool = True,
) -> list[ProbeInfo]:
    """Return the connected probes visible to pyOCD."""

    try:
        probes = _list_connected_probes_via_pyocd_api()
    except Exception:
        probes = []
    if probes:
        return probes
    if not allow_subprocess_fallback:
        return []

    for command in configured_probe_cli_commands():
        _rc, out, err = run_cmd(list(command))
        text = out if out.strip() else err
        if text.strip():
            probes = parse_pyocd_probe_listing(text)
            if probes:
                return probes
    return []


def _score_terms(text: str, terms: tuple[str, ...]) -> int:
    return sum(1 for term in terms if term in text)


def pick_probe_for_board(
    board: BoardConfig,
    probes: list[ProbeInfo],
    *,
    allow_single_fallback: bool,
) -> ProbeResolution:
    """Select one connected probe for a tracked board."""

    if not probes:
        return ProbeResolution(probe=None, note="no probes detected", probes=tuple())

    family_matches = [probe for probe in probes if probe.family == board.probe_family]
    if not family_matches:
        return ProbeResolution(
            probe=None,
            note="no connected probe has the reviewed board's expected provider family",
            probes=tuple(probes),
        )

    scored: list[tuple[int, ProbeInfo]] = []
    for probe in family_matches:
        score = _score_terms(probe.searchable_text, board.probe_hint_terms)
        if score > 0:
            scored.append((score, probe))

    if scored:
        best_score = max(score for score, _ in scored)
        best = [probe for score, probe in scored if score == best_score]
        if len(best) == 1:
            return ProbeResolution(probe=best[0], note="", probes=tuple(probes))
        return ProbeResolution(
            probe=None,
            note="multiple matching probes found; disconnect extras or refine probe_hint_terms",
            probes=tuple(probes),
        )

    if allow_single_fallback and len(family_matches) == 1:
        return ProbeResolution(
            probe=family_matches[0],
            note="single connected probe matched the reviewed provider family",
            probes=tuple(probes),
        )

    return ProbeResolution(
        probe=None,
        note="no matching probe found",
        probes=tuple(probes),
    )


def resolve_probe_for_board(
    board: BoardConfig,
    *,
    run_cmd: RunCommand,
    allow_single_fallback: bool,
    allow_subprocess_fallback: bool = True,
) -> ProbeResolution:
    """List probes and select the best match for the given board."""

    probes = list_connected_probes(
        run_cmd,
        allow_subprocess_fallback=allow_subprocess_fallback,
    )
    return pick_probe_for_board(
        board,
        probes,
        allow_single_fallback=allow_single_fallback,
    )
