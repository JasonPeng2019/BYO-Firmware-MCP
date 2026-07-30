"""Shared builders for discovery-hook tests.

Not in the implementation guide's file list: it exists only so the eight hook test
modules do not each re-implement manifest construction. Every helper here builds a
*real* manifest on disk and a *real* child-process hook, so nothing below mocks the
execution path under test.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Iterable, Sequence

from pyocd_debug_mcp import discovery_hooks

FAKE_HOOK = Path(__file__).resolve().parent / "fake_discovery_hook.py"
ALL_PLATFORMS = ["windows", "macos", "linux"]


def hook_entry(
    hook_id: str,
    kind: str,
    *,
    argv: Sequence[str] = (),
    entrypoint: str = "hook.py",
    platforms: Iterable[str] | None = None,
    runner: str = "server-python",
    timeout_seconds: float | None = 10.0,
) -> dict[str, Any]:
    """Build one manifest entry."""

    entry: dict[str, Any] = {
        "hook_id": hook_id,
        "kind": kind,
        "platforms": list(ALL_PLATFORMS if platforms is None else platforms),
        "runner": runner,
        "entrypoint": entrypoint,
        "argv": list(argv),
    }
    if timeout_seconds is not None:
        entry["timeout_seconds"] = timeout_seconds
    return entry


def write_manifest(
    root: Path,
    entries: Sequence[dict[str, Any]],
    *,
    schema_version: int | None = discovery_hooks.HOOK_SCHEMA_VERSION,
    filename: str = discovery_hooks.MANIFEST_FILENAME,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write a manifest document, copying the fake hook script beside it."""

    root.mkdir(parents=True, exist_ok=True)
    if not (root / "hook.py").exists():
        shutil.copy(FAKE_HOOK, root / "hook.py")
    document: dict[str, Any] = {}
    if schema_version is not None:
        document["schema_version"] = schema_version
    document["hooks"] = list(entries)
    if extra:
        document.update(extra)
    path = root / filename
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def write_raw_manifest(root: Path, text: str, *, filename: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "hook.py").exists():
        shutil.copy(FAKE_HOOK, root / "hook.py")
    path = root / (filename or discovery_hooks.MANIFEST_FILENAME)
    path.write_text(text, encoding="utf-8")
    return path


def snapshot_for(
    root: Path,
    entries: Sequence[dict[str, Any]],
    *,
    environ: dict[str, str] | None = None,
) -> discovery_hooks.DiscoveryHookSnapshot:
    write_manifest(root, entries)
    return discovery_hooks.load_hook_snapshot(root, environ=environ or {})


def single_spec(
    root: Path,
    kind: str,
    argv: Sequence[str],
    *,
    timeout_seconds: float = 10.0,
    hook_id: str = "fake",
) -> discovery_hooks.DiscoveryHookSpec:
    """Build a one-hook snapshot and return its resolved spec."""

    snapshot = snapshot_for(
        root,
        [hook_entry(hook_id, kind, argv=argv, timeout_seconds=timeout_seconds)],
    )
    assert len(snapshot.hooks) == 1
    return snapshot.hooks[0]


def open_handle_count() -> int:
    """Current process descriptor/handle count, for leak assertions."""

    import psutil

    process = psutil.Process()
    if os.name == "nt":
        return int(process.num_handles())
    return int(process.num_fds())
