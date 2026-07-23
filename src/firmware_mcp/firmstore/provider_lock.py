"""Durable project-wide publication lock for the one provider-recipe document."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterator

from filelock import FileLock


_registry_lock = RLock()
_locks: dict[Path, tuple[RLock, FileLock]] = {}


def provider_recipe_lock_path(project_root: Path) -> Path:
    """Return the sole project-wide lock for ``.firm/providers.json``."""

    return project_root.resolve() / ".firm" / "providers.publication.lock"


@contextmanager
def provider_recipe_publication_lock(project_root: Path) -> Iterator[None]:
    """Serialize every recipe snapshot, publication, and rollback exactly once."""

    path = provider_recipe_lock_path(project_root)
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
