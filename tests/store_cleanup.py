"""Leave the developer's real monitoring store exactly as it was found.

Tests that launch the real server cannot redirect the store: it resolves through
the platform's application-data API, which no environment variable overrides. So
these tests genuinely write to the per-user store, and the honest way to stay
polite is to snapshot what was there beforehand and remove only what the run
added.

Per-test cleanup is not enough on its own -- a server subprocess can still be
flushing when a test tears down, and sessions that never bound a workspace land
in the shared ``unbound`` folder -- so this runs once at module teardown.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path


def store_root() -> Path:
    from platformdirs import user_data_dir

    return Path(user_data_dir("BYO", appauthor=False, roaming=False))


def snapshot() -> set[str] | None:
    """Record what the store held before the module ran.

    ``None`` means the store did not exist at all, in which case teardown removes
    it outright.
    """

    root = store_root()
    if not root.exists():
        return None
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def restore(before: set[str] | None) -> None:
    """Remove everything this module's tests added, and nothing else."""

    # A server subprocess can still be flushing when the last test ends, so
    # settle briefly rather than leaving its final write behind.
    time.sleep(1.0)
    root = store_root()
    if not root.exists():
        return
    if before is None:
        shutil.rmtree(root, ignore_errors=True)
        return
    current = sorted(
        (path for path in root.rglob("*")),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in current:
        relative = str(path.relative_to(root))
        if relative in before:
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            continue


__all__ = ["restore", "snapshot", "store_root"]
