"""Leave the developer's real monitoring store exactly as it was found.

Tests that launch the real server cannot redirect the store: it resolves through
the platform's application-data API, which no environment variable overrides. So
these tests genuinely write to the per-user store, and the honest way to stay
polite is to snapshot what was there beforehand, remove only what the run added,
and put back the exact prior bytes of anything the run changed or removed.

Per-test cleanup is not enough on its own -- a server subprocess can still be
flushing when a test tears down, and sessions that never bound a workspace land
in the shared ``unbound`` folder -- so this runs once at module teardown.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path


def store_root() -> Path:
    from platformdirs import user_data_dir

    return Path(user_data_dir("BYO", appauthor=False, roaming=False))


class StoreSnapshot(set[str]):
    """The relative names the store held, plus every prior file's exact bytes.

    It is still the set of names earlier callers compared against; the retained
    contents let teardown restore changed or deleted files rather than only
    removing additions.
    """

    def __init__(self, files: dict[str, tuple[bytes, int, int]], directories: set[str]):
        super().__init__(set(files) | directories)
        self.files = files
        self.directories = directories


def snapshot() -> set[str] | None:
    """Record what the store held before the module ran.

    ``None`` means the store did not exist at all, in which case teardown removes
    it outright.
    """

    root = store_root()
    if not root.exists():
        return None
    files: dict[str, tuple[bytes, int, int]] = {}
    directories: set[str] = set()
    for path in root.rglob("*"):
        relative = str(path.relative_to(root))
        if path.is_dir():
            directories.add(relative)
        else:
            status = path.stat()
            files[relative] = (path.read_bytes(), status.st_atime_ns, status.st_mtime_ns)
    return StoreSnapshot(files, directories)


def restore(before: set[str] | None) -> None:
    """Remove everything this module's tests added and restore prior bytes.

    Anything that cannot be put back is reported rather than silently left
    changed, so a run never claims it preserved a store it did not.
    """

    # A server subprocess can still be flushing when the last test ends, so
    # settle briefly rather than leaving its final write behind.
    time.sleep(1.0)
    root = store_root()
    if before is None:
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        if root.exists():
            raise RuntimeError(f"could not remove the store this module created: {root}")
        return
    if not isinstance(before, StoreSnapshot):
        raise TypeError("restore requires the full snapshot taken by snapshot()")
    failures: list[str] = []
    current = sorted(
        (path for path in root.rglob("*")) if root.exists() else (),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in current:
        relative = str(path.relative_to(root))
        if relative in before.directories and path.is_dir():
            continue
        if relative in before.files and path.is_file():
            continue
        if relative not in before:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            except OSError as exc:
                failures.append(f"{relative}: {exc}")
    for relative in sorted(before.directories, key=lambda name: len(Path(name).parts)):
        target = root / relative
        try:
            if target.exists() and not target.is_dir():
                target.unlink()
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            failures.append(f"{relative}: {exc}")
    for relative, (content, atime_ns, mtime_ns) in sorted(before.files.items()):
        target = root / relative
        try:
            if target.is_dir():
                shutil.rmtree(target)
            if not target.is_file() or target.read_bytes() != content:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                os.utime(target, ns=(atime_ns, mtime_ns))
        except OSError as exc:
            failures.append(f"{relative}: {exc}")
    if failures:
        raise RuntimeError("store was not restored exactly: " + "; ".join(failures))


__all__ = ["StoreSnapshot", "restore", "snapshot", "store_root"]
