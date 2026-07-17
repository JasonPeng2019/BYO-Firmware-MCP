"""Bounded fail-closed cleanup of owned-process markers from an earlier run."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from pyocd_debug_mcp.kernel.processes import (
    DEFAULT_MARKER_ROOT,
    ProcessMarker,
    identity_matches,
    terminate_marked_group,
)


@dataclass(frozen=True, slots=True)
class HygieneResult:
    inspected: int
    terminated: int
    stale_removed: int
    refused: int


def cleanup_stale_owned_processes(
    root: Path = DEFAULT_MARKER_ROOT,
    *,
    max_markers: int = 128,
    timeout_seconds: float = 2.0,
) -> HygieneResult:
    root = root.resolve()
    if not root.exists():
        return HygieneResult(0, 0, 0, 0)
    deadline = time.monotonic() + timeout_seconds
    inspected = terminated = stale = refused = 0
    for path in sorted(root.glob("*.json"))[:max_markers]:
        if time.monotonic() >= deadline:
            break
        inspected += 1
        try:
            resolved = path.resolve()
            if resolved.parent != root:
                raise ValueError("marker escaped owned root")
            raw = json.loads(path.read_text(encoding="utf-8"))
            marker = ProcessMarker(**raw)
            if marker.schema_version != 1 or marker.pid <= 0 or not marker.start_token:
                raise ValueError("invalid marker identity")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            refused += 1
            continue
        is_lock_marker = path.name.endswith(".lock.json")
        if not identity_matches(marker.pid, marker.start_token):
            path.unlink(missing_ok=True)
            stale += 1
            continue
        if is_lock_marker:
            # A live identity still owns this lock; startup must not steal it.
            refused += 1
            continue
        if terminate_marked_group(marker.pid, marker.start_token):
            path.unlink(missing_ok=True)
            terminated += 1
        else:
            refused += 1
    return HygieneResult(inspected, terminated, stale, refused)
