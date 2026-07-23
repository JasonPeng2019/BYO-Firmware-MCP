"""One durable per-project/per-board publication lock for safety authority.

The lock has deliberately no lease, timeout, stale-owner cleanup, or retry
policy.  It serializes only publication and map-bound target work for one
logical board; different boards use different lock files.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterator

from filelock import FileLock


_registry_lock = RLock()
_locks: dict[Path, tuple[RLock, FileLock]] = {}


def safety_lock_path(project_root: Path, board_id: str) -> Path:
    """Return the one project-local lock path for ``board_id``."""

    return project_root.resolve() / ".firm" / "safety" / board_id / ".publication.lock"


@contextmanager
def safety_publication_lock(project_root: Path, board_id: str) -> Iterator[None]:
    """Hold the re-entrant local and cross-process board safety lock."""

    path = safety_lock_path(project_root, board_id)
    with _registry_lock:
        entry = _locks.get(path)
        if entry is None:
            entry = (RLock(), FileLock(str(path), timeout=-1))
            _locks[path] = entry
    local_lock, durable_lock = entry
    with local_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with durable_lock:
            yield
