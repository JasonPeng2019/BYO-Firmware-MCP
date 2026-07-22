"""Bounded fail-closed cleanup of owned-process markers from an earlier run."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from pyocd_debug_mcp.kernel.processes import (
    DEFAULT_MARKER_ROOT,
    ProcessMarker,
    ProcessIdentityUnavailable,
    _start_token,
    terminate_marked_group,
)


@dataclass(frozen=True, slots=True)
class HygieneResult:
    inspected: int
    terminated: int
    stale_removed: int
    live_owner_skipped: int
    unresolved: int

    @property
    def refused(self) -> int:
        return self.live_owner_skipped + self.unresolved


def cleanup_stale_owned_processes(
    root: Path = DEFAULT_MARKER_ROOT,
    *,
    timeout_seconds: float = 2.0,
) -> HygieneResult:
    root = root.resolve()
    if not root.exists():
        return HygieneResult(0, 0, 0, 0, 0)
    deadline = time.monotonic() + timeout_seconds
    inspected = terminated = stale = live_owner_skipped = unresolved = 0
    candidates = sorted(root.glob("*.json"))
    for path in candidates:
        if time.monotonic() >= deadline:
            break
        inspected += 1
        try:
            resolved = path.resolve()
            if resolved.parent != root:
                raise ValueError("marker escaped owned root")
            raw = json.loads(path.read_text(encoding="utf-8"))
            marker = ProcessMarker(**raw)
            if (
                marker.schema_version != 2
                or marker.owner_pid <= 0
                or not marker.owner_start_token
                or marker.pid <= 0
                or not marker.start_token
            ):
                raise ValueError("invalid marker identity")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            unresolved += 1
            continue
        is_lock_marker = path.name.endswith(".lock.json")
        try:
            owner_token = _start_token(marker.owner_pid)
        except ProcessIdentityUnavailable:
            unresolved += 1
            continue
        if owner_token == marker.owner_start_token:
            live_owner_skipped += 1
            continue
        try:
            current_token = _start_token(marker.pid)
        except ProcessIdentityUnavailable:
            unresolved += 1
            continue
        if current_token is not None and current_token != marker.start_token:
            path.unlink(missing_ok=True)
            stale += 1
            continue
        was_live = current_token == marker.start_token
        if is_lock_marker:
            # A live identity still owns this lock; startup must not steal it.
            if was_live:
                unresolved += 1
            else:
                path.unlink(missing_ok=True)
                stale += 1
            continue
        if terminate_marked_group(marker.pid, marker.start_token):
            path.unlink(missing_ok=True)
            if was_live:
                terminated += 1
            else:
                stale += 1
        else:
            unresolved += 1
    unresolved += max(0, len(candidates) - inspected)
    return HygieneResult(inspected, terminated, stale, live_owner_skipped, unresolved)


def require_clean_startup(root: Path = DEFAULT_MARKER_ROOT) -> HygieneResult:
    result = cleanup_stale_owned_processes(root)
    if result.unresolved:
        raise RuntimeError(
            f"Owned-process startup hygiene left {result.unresolved} unresolved marker(s) in "
            f"{root.resolve()}. Stop the owning process or inspect the retained markers before "
            "starting the MCP server."
        )
    return result
