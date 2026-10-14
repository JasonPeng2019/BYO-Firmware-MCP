#!/usr/bin/env python3
"""Build and verify clean base/CAN installs for the release candidate.

Build a fresh pair and verify it with:

    uv run --locked python scripts/verify_release_artifacts.py

Build, verify, and transactionally publish the exact handoff pair with rollback with:

    uv run --locked python scripts/verify_release_artifacts.py --keep-dist dist

Or verify the exact handoff pair already built in ``dist/`` with:

    uv run --locked python scripts/verify_release_artifacts.py dist
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import os
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.8rc1"
CAN_TOOLS = {
    "get_can_status",
    "connect_can",
    "disconnect_can",
    "read_can-plan",
    "read_can",
    "send_can-plan",
    "send_can",
}
COMMAND_TIMEOUT_SECONDS = 300.0
MAX_GENERATED_MEMBER_BYTES = 2 * 1024 * 1024
MAX_RELEASE_ARTIFACT_BYTES = 128 * 1024 * 1024
PUBLICATION_LOCK_TIMEOUT_SECONDS = 30.0
WHEEL_METADATA = (
    "Wheel-Version: 1.0\nGenerator: hatchling 1.32.4\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
).encode()
ENTRY_POINTS = (
    "[console_scripts]\n"
    "byo-mcp-sidecar = pyocd_debug_mcp.sidecar:main\n"
    "pyocd-collect-artifacts = pyocd_debug_mcp.artifact_collector:main\n"
    "pyocd-debug-mcp = pyocd_debug_mcp.cli:main\n"
    "pyocd-native-build = pyocd_debug_mcp.native_build:main\n"
    "pyocd-pack-repair = pyocd_debug_mcp.pack_index_repair:main\n"
).encode()


def _run(
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
    expect_success: bool = True,
) -> subprocess.CompletedProcess[str]:
    print("+", shlex.join(arguments), flush=True)
    process = subprocess.Popen(
        arguments,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = process.communicate(timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _retire_process_group(process.pid)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            arguments,
            COMMAND_TIMEOUT_SECONDS,
            output=stdout,
            stderr=stderr,
        ) from exc
    finally:
        # A verifier child must not outlive the command that launched it. In
        # particular, a spawned CAN worker retaining an imported __pycache__ on
        # NFS used to race temporary-directory cleanup after a green check.
        _retire_process_group(process.pid)
    result = subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
    if bool(result.returncode == 0) != expect_success:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        expectation = "succeed" if expect_success else "fail"
        raise RuntimeError(f"command was expected to {expectation}: {shlex.join(arguments)}")
    return result


def _retire_process_group(group_id: int) -> None:
    """Retire POSIX descendants left in a verifier command's private group."""

    if os.name != "posix":
        return
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise RuntimeError(f"verifier process group {group_id} did not retire")


@contextmanager
def _verification_workspace() -> Iterator[Path]:
    """Create and reliably remove a private verifier workspace."""

    # macOS exposes its default temporary directory through the /var symlink.
    # Use the canonical directory we just created so secure profile loading
    # can continue to reject symlinks in user-supplied paths.
    workspace = Path(tempfile.mkdtemp(prefix="byo-release-verify-")).resolve()
    try:
        yield workspace
    finally:
        last_error: OSError | None = None
        cleaned = False
        for _ in range(100):
            try:
                shutil.rmtree(workspace)
            except FileNotFoundError:
                cleaned = True
                break
            except OSError as exc:
                last_error = exc
                time.sleep(0.05)
            else:
                cleaned = True
                break
        if not cleaned and last_error is not None:
            raise last_error


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


BASE_CHECK = f"""
import asyncio
import importlib.metadata
import importlib.resources
import importlib.util
import json
import os

import pyocd_debug_mcp

assert importlib.metadata.version("pyocd-debug-mcp") == {VERSION!r}
assert pyocd_debug_mcp.__version__ == {VERSION!r}
root = importlib.resources.files("pyocd_debug_mcp")
for relative in (
    "examples/can/virtual_rx_only.yaml",
    "licenses/Apache-2.0.txt",
    "licenses/THIRD_PARTY_NOTICES.md",
    "docs/SERVER_GUIDE.md",
    "docs/client-contract.md",
    "docs/server-reference.md",
    "docs/RELEASE_STATUS.md",
):
    assert root.joinpath(relative).is_file(), relative
assert importlib.util.find_spec("can") is None

import pyocd_debug_mcp.server as server
assert server.mcp._mcp_server.version == {VERSION!r}
names = set(server.mcp._tool_manager._tools)
assert {CAN_TOOLS!r}.issubset(names), sorted({CAN_TOOLS!r} - names)
status = asyncio.run(server.mcp.call_tool("get_can_status", {{"board_id": "board"}}))
text = status[0].text if isinstance(status, tuple) else status[0].text
payload = json.loads(text)
assert payload["ok"] is True and payload["can"]["configured"] is False
server._can_manager.shutdown_sync()
server._monitor.closeout("release-artifact-verification")
"""


MISSING_EXTRA_CHECK = """
import importlib.resources
import os

fixture = importlib.resources.files("pyocd_debug_mcp").joinpath(
    "examples/can/virtual_rx_only.yaml"
)
os.environ["PYOCD_CAN_MODE"] = "available"
os.environ["PYOCD_CAN_CONFIG"] = str(fixture)
import pyocd_debug_mcp.server  # noqa: F401
"""


CAN_SMOKE_CHECK = """
# Match the generated console wrapper's only top-level import. A spawn child
# re-executes this file, so importing this module must not initialize the server.
from pyocd_debug_mcp.cli import main as _console_main


def main():
    import importlib.resources
    from pathlib import Path

    from pyocd_debug_mcp.can_config import load_can_fixture_config
    from pyocd_debug_mcp.services.can_worker import CanWorkerClient

    fixture = Path(
        str(
            importlib.resources.files("pyocd_debug_mcp").joinpath(
                "examples/can/virtual_rx_only.yaml"
            )
        )
    )
    worker = CanWorkerClient(load_can_fixture_config(fixture))
    handle = worker.open(timeout_seconds=2.0)
    assert handle.interface == "virtual"
    assert worker.state(timeout_seconds=1.0) == "active"
    worker.close(timeout_seconds=2.0)


if __name__ == "__main__":
    main()
"""


def _create_venv(root: Path, name: str) -> Path:
    venv = root / name
    _run(["uv", "venv", "--python", sys.executable, str(venv)], cwd=root)
    return _venv_python(venv)


def _verify_base_install(root: Path, artifact: Path, label: str) -> None:
    python = _create_venv(root, f"base-{label}")
    _run(["uv", "pip", "install", "--python", str(python), str(artifact)], cwd=root)
    runtime = root / f"runtime-{label}"
    runtime.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PYOCD_CAN_")
        and key
        not in {
            "PYOCD_FIXTURE_EVIDENCE_CONTRACT",
            "PYOCD_FIXTURE_EVIDENCE_CONTRACT_SHA256",
        }
    }
    environment.update(
        {
            "BYO_MCP_ARTIFACT_ROOT": str(runtime / "artifacts"),
            "BYO_MCP_MONITOR_ROOT": str(runtime / "monitor"),
            "PYOCD_CAN_MODE": "off",
        }
    )
    _run([str(python), "-c", BASE_CHECK], cwd=runtime, environment=environment)

    missing = _run(
        [str(python), "-c", MISSING_EXTRA_CHECK],
        cwd=runtime,
        environment=environment,
        expect_success=False,
    )
    diagnostic = (missing.stdout + missing.stderr).lower()
    assert "pyocd-debug-mcp[can]" in diagnostic, diagnostic


def _verify_can_extra(root: Path, artifact: Path, label: str) -> None:
    python = _create_venv(root, f"can-extra-{label}")
    _run(
        ["uv", "pip", "install", "--python", str(python), f"{artifact}[can]"],
        cwd=root,
    )
    smoke = root / f"can-console-smoke-{label}.py"
    smoke.write_text(CAN_SMOKE_CHECK, encoding="utf-8")
    _run([str(python), str(smoke)], cwd=root)


def _release_artifacts(directory: Path) -> tuple[Path, Path]:
    """Return the one exact candidate pair, rejecting stale or ambiguous archives."""

    directory = Path(os.path.abspath(directory))
    directory_metadata = directory.lstat()
    if stat.S_ISLNK(directory_metadata.st_mode) or not stat.S_ISDIR(directory_metadata.st_mode):
        raise RuntimeError(f"artifact directory must be a non-symlink directory: {directory}")
    expected_wheel = directory / f"pyocd_debug_mcp-{VERSION}-py3-none-any.whl"
    expected_sdist = directory / f"pyocd_debug_mcp-{VERSION}.tar.gz"
    actual = sorted(
        path.name
        for pattern in ("*.whl", "*.tar.gz")
        for path in directory.glob(pattern)
        if path.is_file()
    )
    expected = sorted((expected_wheel.name, expected_sdist.name))
    if actual != expected:
        raise RuntimeError(
            f"artifact directory must contain exactly {expected}; found {actual}: {directory}"
        )
    for artifact in (expected_wheel, expected_sdist):
        metadata = artifact.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError(f"release artifact must be a singly linked regular file: {artifact}")
    return expected_wheel, expected_sdist


def _copy_artifact_to_descriptor(source: Path, destination_descriptor: int) -> None:
    """Copy one stable, singly linked artifact into an already-open destination."""

    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:  # pragma: no cover - Windows fail-closed portability guard
        raise RuntimeError("release artifact snapshots require O_NOFOLLOW support")
    source_flags |= nofollow
    source_descriptor = os.open(source, source_flags)
    try:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError(f"release artifact must be a singly linked regular file: {source}")
        if before.st_size > MAX_RELEASE_ARTIFACT_BYTES:
            raise RuntimeError(f"release artifact exceeds size bound: {source}")
        copied = 0
        while True:
            chunk = os.read(source_descriptor, 64 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > MAX_RELEASE_ARTIFACT_BYTES:
                raise RuntimeError(f"release artifact exceeds size bound: {source}")
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise OSError("short write while snapshotting release artifact")
                view = view[written:]
        os.fsync(destination_descriptor)
        after = os.fstat(source_descriptor)

        def identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if copied != before.st_size or identity(before) != identity(after):
            raise RuntimeError(f"release artifact changed while being snapshotted: {source}")
    finally:
        os.close(source_descriptor)


def _snapshot_artifact(source: Path, destination: Path) -> Path:
    """Copy one untrusted candidate into a private immutable verification input."""

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination_descriptor: int | None = None
    try:
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        _copy_artifact_to_descriptor(source, destination_descriptor)
    except BaseException:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
            destination_descriptor = None
        destination.unlink(missing_ok=True)
        raise
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
    return destination


def _snapshot_release_pair(work: Path, wheel: Path, sdist: Path) -> tuple[Path, Path]:
    snapshot = work / "candidate-snapshot"
    snapshot.mkdir(mode=0o700)
    return (
        _snapshot_artifact(wheel, snapshot / wheel.name),
        _snapshot_artifact(sdist, snapshot / sdist.name),
    )


def _artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_pair_digest_matches(
    expected: tuple[Path, Path], observed: tuple[Path, Path], *, label: str
) -> None:
    expected_digests = tuple(_artifact_sha256(path) for path in expected)
    observed_digests = tuple(_artifact_sha256(path) for path in observed)
    if observed_digests != expected_digests:
        raise RuntimeError(f"{label} artifact pair changed after its verified snapshot")


def _open_release_directory(destination: Path) -> tuple[int, int, str, os.stat_result]:
    """Open and pin the publication parent/destination without following final links."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory_flag is None:
        raise RuntimeError("release publication requires no-follow directory descriptors")
    destination = Path(os.path.abspath(destination))
    flags = os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = os.open(destination.anchor, flags)
    try:
        for component in destination.parent.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=parent_descriptor)
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor
    except BaseException:
        os.close(parent_descriptor)
        raise
    try:
        try:
            os.mkdir(destination.name, mode=0o755, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        directory_descriptor = os.open(
            destination.name,
            os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
        pinned = os.fstat(directory_descriptor)
        named = os.stat(destination.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(pinned.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (pinned.st_dev, pinned.st_ino) != (named.st_dev, named.st_ino)
        ):
            os.close(directory_descriptor)
            raise RuntimeError(
                f"release destination must be a stable non-symlink directory: {destination}"
            )
        return parent_descriptor, directory_descriptor, destination.name, pinned
    except BaseException:
        os.close(parent_descriptor)
        raise


def _entry_sha256(directory_descriptor: int, name: str) -> str:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:  # pragma: no cover - fail-closed portability guard
        raise RuntimeError("release publication requires O_NOFOLLOW support")
    descriptor = os.open(
        name,
        os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
        dir_fd=directory_descriptor,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError(f"published artifact is not a singly linked regular file: {name}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    finally:
        os.close(descriptor)


def _snapshot_artifact_at(source: Path, directory_descriptor: int, name: str) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        _copy_artifact_to_descriptor(source, descriptor)
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        try:
            os.unlink(name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _acquire_publication_lock(
    parent_descriptor: int, destination_name: str
) -> tuple[str, int, os.stat_result]:
    """Serialize publishers with an atomic sibling directory under the pinned parent."""

    lock_name = f".{destination_name}.release-publication.lock"
    deadline = time.monotonic() + PUBLICATION_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            os.mkdir(lock_name, mode=0o700, dir_fd=parent_descriptor)
            break
        except FileExistsError:
            try:
                metadata = os.stat(lock_name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                # The incumbent can normally release the lock between our
                # failed mkdir and inspection. Retry acquisition rather than
                # turning successful serialization into a spurious failure.
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(f"release publication lock is not a directory: {lock_name}")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "another release publication is still active; remove a stale lock only "
                    f"after confirming no publisher is running: {lock_name}"
                ) from None
            time.sleep(0.01)

    directory_flag = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if directory_flag is None or nofollow is None:
        try:
            os.rmdir(lock_name, dir_fd=parent_descriptor)
        except OSError:
            pass
        raise RuntimeError("release publication lock requires no-follow directory descriptors")
    descriptor = os.open(
        lock_name,
        os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_descriptor,
    )
    pinned = os.fstat(descriptor)
    named = os.stat(lock_name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not stat.S_ISDIR(pinned.st_mode) or (pinned.st_dev, pinned.st_ino) != (
        named.st_dev,
        named.st_ino,
    ):
        os.close(descriptor)
        raise RuntimeError("release publication lock changed during acquisition")
    return lock_name, descriptor, pinned


def _assert_publication_lock(
    parent_descriptor: int,
    lock_name: str,
    lock_descriptor: int,
    pinned: os.stat_result,
) -> None:
    current = os.fstat(lock_descriptor)
    named = os.stat(lock_name, dir_fd=parent_descriptor, follow_symlinks=False)
    expected = (pinned.st_dev, pinned.st_ino)
    if (
        not stat.S_ISDIR(current.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or (current.st_dev, current.st_ino) != expected
        or (named.st_dev, named.st_ino) != expected
    ):
        raise RuntimeError("release publication lock changed during publication")


def _release_publication_lock(
    parent_descriptor: int,
    lock_name: str,
    lock_descriptor: int,
    pinned: os.stat_result,
) -> None:
    try:
        _assert_publication_lock(parent_descriptor, lock_name, lock_descriptor, pinned)
    finally:
        os.close(lock_descriptor)
    os.rmdir(lock_name, dir_fd=parent_descriptor)


def _publish_verified_pair(destination: Path, wheel: Path, sdist: Path) -> tuple[Path, Path]:
    """Transactionally publish a pair through one pinned directory descriptor.

    A destination-name-specific interprocess lock serializes inventory, staging,
    commit, and rollback. Both new archives are staged and digest-checked before
    either public name is touched. A failure while replacing publisher-owned names
    restores the complete previous pair (or the original empty directory).
    Unexpected external entries fail the transaction but are deliberately left
    untouched. Publication is intentionally described as a rollback transaction
    rather than a pair-atomic operation: portable filesystems do not provide a
    two-name atomic replacement primitive.
    """

    destination = Path(os.path.abspath(destination))
    parent_descriptor, directory_descriptor, destination_name, pinned = _open_release_directory(
        destination
    )
    expected_names = (wheel.name, sdist.name)
    nonce = secrets.token_hex(16)
    staged = {
        wheel.name: f".{wheel.name}.{nonce}.new",
        sdist.name: f".{sdist.name}.{nonce}.new",
    }
    backups = {
        wheel.name: f".{wheel.name}.{nonce}.old",
        sdist.name: f".{sdist.name}.{nonce}.old",
    }
    installed: list[str] = []
    moved_old: list[str] = []
    publication_lock: tuple[str, int, os.stat_result] | None = None
    try:
        publication_lock = _acquire_publication_lock(parent_descriptor, destination_name)
        initial_entries = set(os.listdir(directory_descriptor))
        if initial_entries not in (set(), set(expected_names)):
            raise RuntimeError(
                "release destination must be empty or contain only the exact previous pair: "
                f"{destination}"
            )
        for name in initial_entries:
            metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeError(
                    f"previous release artifact must be a singly linked regular file: {name}"
                )

        for source in (wheel, sdist):
            _snapshot_artifact_at(source, directory_descriptor, staged[source.name])
        expected_digests = {
            wheel.name: _artifact_sha256(wheel),
            sdist.name: _artifact_sha256(sdist),
        }
        staged_digests = {
            name: _entry_sha256(directory_descriptor, staged[name]) for name in expected_names
        }
        if staged_digests != expected_digests:
            raise RuntimeError("staged publication pair differs from its verified snapshot")
        os.fsync(directory_descriptor)

        try:
            _assert_publication_lock(
                parent_descriptor,
                publication_lock[0],
                publication_lock[1],
                publication_lock[2],
            )
            for name in expected_names:
                if name in initial_entries:
                    os.replace(
                        name,
                        backups[name],
                        src_dir_fd=directory_descriptor,
                        dst_dir_fd=directory_descriptor,
                    )
                    moved_old.append(name)
            for name in expected_names:
                os.replace(
                    staged[name],
                    name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
                installed.append(name)

            named = os.stat(
                destination_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (
                pinned.st_dev,
                pinned.st_ino,
            ):
                raise RuntimeError("release destination changed during publication")
            published_digests = {
                name: _entry_sha256(directory_descriptor, name) for name in expected_names
            }
            if published_digests != expected_digests:
                raise RuntimeError("published artifact pair differs from its verified snapshot")
            expected_committed_entries = set(expected_names) | {backups[name] for name in moved_old}
            committed_entries = set(os.listdir(directory_descriptor))
            if committed_entries != expected_committed_entries:
                raise RuntimeError(
                    "release destination inventory changed during publication: "
                    f"expected {sorted(expected_committed_entries)}, "
                    f"found {sorted(committed_entries)}"
                )
            _assert_publication_lock(
                parent_descriptor,
                publication_lock[0],
                publication_lock[1],
                publication_lock[2],
            )
            os.fsync(directory_descriptor)
        except BaseException:
            for name in reversed(installed):
                try:
                    os.unlink(name, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass
            for name in reversed(moved_old):
                os.replace(
                    backups[name],
                    name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
            os.fsync(directory_descriptor)
            raise

        for name in moved_old:
            os.unlink(backups[name], dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
        os.fsync(parent_descriptor)
    finally:
        # A failed rollback deliberately leaves any surviving ``.old`` file in
        # place for operator recovery instead of deleting the last good bytes.
        for name in staged.values():
            try:
                os.unlink(name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
        try:
            if publication_lock is not None:
                _release_publication_lock(
                    parent_descriptor,
                    publication_lock[0],
                    publication_lock[1],
                    publication_lock[2],
                )
        finally:
            os.close(directory_descriptor)
            os.close(parent_descriptor)
    return destination / wheel.name, destination / sdist.name


def _source_files(directory: Path) -> list[Path]:
    """Return packageable source files without local interpreter by-products."""

    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    )


def _source_archive_mappings() -> tuple[dict[str, Path], dict[str, Path]]:
    """Map archive member names to the source file that must supply their bytes."""

    wheel: dict[str, Path] = {}
    sdist: dict[str, Path] = {}
    sdist_prefix = f"pyocd_debug_mcp-{VERSION}"

    package_root = ROOT / "src/pyocd_debug_mcp"
    for source in _source_files(package_root):
        relative = source.relative_to(package_root).as_posix()
        wheel[f"pyocd_debug_mcp/{relative}"] = source
        sdist[f"{sdist_prefix}/src/pyocd_debug_mcp/{relative}"] = source

    for directory_name in ("docs", "client-workspace", "scripts", "skills"):
        directory = ROOT / directory_name
        for source in _source_files(directory):
            relative = source.relative_to(directory).as_posix()
            sdist[f"{sdist_prefix}/{directory_name}/{relative}"] = source
            if directory_name == "docs":
                wheel[f"pyocd_debug_mcp/docs/{relative}"] = source

    individual_files = {
        ".gitignore": ROOT / ".gitignore",
        "README.md": ROOT / "README.md",
        "SERVER_GUIDE.md": ROOT / "SERVER_GUIDE.md",
        "THIRD_PARTY_NOTICES.md": ROOT / "THIRD_PARTY_NOTICES.md",
        "LICENSES/Apache-2.0.txt": ROOT / "LICENSES/Apache-2.0.txt",
        "fixtures/can/virtual_rx_only.yaml": ROOT / "fixtures/can/virtual_rx_only.yaml",
        "pyproject.toml": ROOT / "pyproject.toml",
        "uv.lock": ROOT / "uv.lock",
    }
    for relative, source in individual_files.items():
        sdist[f"{sdist_prefix}/{relative}"] = source

    wheel.update(
        {
            "pyocd_debug_mcp/docs/SERVER_GUIDE.md": ROOT / "SERVER_GUIDE.md",
            "pyocd_debug_mcp/licenses/THIRD_PARTY_NOTICES.md": (ROOT / "THIRD_PARTY_NOTICES.md"),
            "pyocd_debug_mcp/licenses/Apache-2.0.txt": ROOT / "LICENSES/Apache-2.0.txt",
            "pyocd_debug_mcp/examples/can/virtual_rx_only.yaml": (
                ROOT / "fixtures/can/virtual_rx_only.yaml"
            ),
        }
    )
    return wheel, sdist


def _assert_current_archive_member(
    *, artifact: Path, member: str, archived: bytes, source: Path
) -> None:
    expected = source.read_bytes()
    if archived != expected:
        raise RuntimeError(
            f"{artifact.name}:{member} does not match current source file "
            f"{source.relative_to(ROOT)}; rebuild the release artifacts"
        )


def _verify_wheel_record(
    archive: zipfile.ZipFile, names: set[str], record_name: str, artifact: Path
) -> None:
    rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
    if any(len(row) != 3 for row in rows):
        raise RuntimeError(f"{artifact.name}:{record_name} contains a malformed row")
    records = {row[0]: (row[1], row[2]) for row in rows}
    if len(records) != len(rows) or set(records) != names:
        raise RuntimeError(f"{artifact.name}:{record_name} does not match wheel inventory")
    for name in sorted(names - {record_name}):
        digest_text, size_text = records[name]
        try:
            algorithm, encoded = digest_text.split("=", 1)
            size = int(size_text)
        except ValueError as exc:
            raise RuntimeError(
                f"{artifact.name}:{record_name} has invalid metadata for {name}"
            ) from exc
        if algorithm != "sha256":
            raise RuntimeError(f"{artifact.name}:{record_name} has unsupported hash for {name}")
        content = archive.read(name)
        actual = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        if encoded != actual or size != len(content):
            raise RuntimeError(f"{artifact.name}:{record_name} has stale metadata for {name}")
    if records[record_name] != ("", ""):
        raise RuntimeError(f"{artifact.name}:{record_name} must leave its own hash and size empty")


def _verify_source_alignment(wheel: Path, sdist: Path) -> None:
    """Reject archives whose sources, inventory, or generated metadata are stale."""

    wheel_sources, sdist_sources = _source_archive_mappings()
    with zipfile.ZipFile(wheel) as archive:
        infos = archive.infolist()
        ordered_names = [info.filename for info in infos]
        names = set(ordered_names)
        if len(names) != len(ordered_names):
            raise RuntimeError(f"{wheel.name} contains duplicate archive member names")
        dist_info_prefix = f"pyocd_debug_mcp-{VERSION}.dist-info/"
        metadata_name = dist_info_prefix + "METADATA"
        wheel_name = dist_info_prefix + "WHEEL"
        entry_points_name = dist_info_prefix + "entry_points.txt"
        record_name = dist_info_prefix + "RECORD"
        expected_names = set(wheel_sources) | {
            metadata_name,
            wheel_name,
            entry_points_name,
            record_name,
        }
        if names != expected_names:
            raise RuntimeError(
                f"{wheel.name} inventory differs from the current package: "
                f"missing={sorted(expected_names - names)}, extra={sorted(names - expected_names)}"
            )
        for info in infos:
            mode = info.external_attr >> 16
            expected_mode = stat.S_IFREG | 0o644 if info.filename in wheel_sources else 0o644
            if info.is_dir() or info.create_system != 3 or mode != expected_mode:
                raise RuntimeError(
                    f"{wheel.name}:{info.filename} has unexpected archive type metadata"
                )
            expected_size = (
                wheel_sources[info.filename].stat().st_size
                if info.filename in wheel_sources
                else MAX_GENERATED_MEMBER_BYTES
            )
            if info.file_size > expected_size:
                raise RuntimeError(f"{wheel.name}:{info.filename} exceeds its declared size bound")
        wheel_core_metadata = archive.read(metadata_name)
        if archive.read(wheel_name) != WHEEL_METADATA:
            raise RuntimeError(f"{wheel.name}:{wheel_name} does not match the pinned build backend")
        if archive.read(entry_points_name) != ENTRY_POINTS:
            raise RuntimeError(
                f"{wheel.name}:{entry_points_name} does not match current console scripts"
            )
        _verify_wheel_record(archive, names, record_name, wheel)
        for member, source in wheel_sources.items():
            _assert_current_archive_member(
                artifact=wheel,
                member=member,
                archived=archive.read(member),
                source=source,
            )

    with tarfile.open(sdist, "r:gz") as archive:
        sdist_prefix = f"pyocd_debug_mcp-{VERSION}"
        members: dict[str, tarfile.TarInfo] = {}
        for member in archive:
            parsed = PurePosixPath(member.name)
            if (
                member.name.startswith("/")
                or "\\" in member.name
                or parsed.parts[:1] != (sdist_prefix,)
                or any(part in {"", ".", ".."} for part in parsed.parts)
                or parsed.as_posix() != member.name
            ):
                raise RuntimeError(f"{sdist.name} contains an unsafe archive path: {member.name}")
            if not member.isfile():
                raise RuntimeError(
                    f"{sdist.name} contains a non-regular archive member: {member.name}"
                )
            expected_source = sdist_sources.get(member.name)
            expected_size = (
                expected_source.stat().st_size
                if expected_source is not None
                else MAX_GENERATED_MEMBER_BYTES
            )
            if member.size > expected_size:
                raise RuntimeError(f"{sdist.name}:{member.name} exceeds its declared size bound")
            expected_mode = (
                0o755
                if expected_source is not None
                and expected_source.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                else 0o644
            )
            if stat.S_IMODE(member.mode) != expected_mode:
                raise RuntimeError(
                    f"{sdist.name}:{member.name} must have regular-file mode {expected_mode:04o}"
                )
            if member.name in members:
                raise RuntimeError(f"{sdist.name} contains duplicate archive member names")
            members[member.name] = member
        package_info_name = f"pyocd_debug_mcp-{VERSION}/PKG-INFO"
        expected_names = set(sdist_sources) | {package_info_name}
        if set(members) != expected_names:
            raise RuntimeError(
                f"{sdist.name} inventory differs from the current package: "
                f"missing={sorted(expected_names - members.keys())}, "
                f"extra={sorted(members.keys() - expected_names)}"
            )
        for member_name, source in sdist_sources.items():
            extracted = archive.extractfile(members[member_name])
            if extracted is None:
                raise RuntimeError(f"could not read {sdist.name}:{member_name}")
            _assert_current_archive_member(
                artifact=sdist,
                member=member_name,
                archived=extracted.read(),
                source=source,
            )
        package_info = archive.extractfile(members[package_info_name])
        if package_info is None or package_info.read() != wheel_core_metadata:
            raise RuntimeError(f"{wheel.name} METADATA and {sdist.name} PKG-INFO differ")


def _core_metadata(wheel: Path, sdist: Path) -> tuple[bytes, bytes]:
    with zipfile.ZipFile(wheel) as wheel_archive:
        wheel_metadata = wheel_archive.read(f"pyocd_debug_mcp-{VERSION}.dist-info/METADATA")
    with tarfile.open(sdist, "r:gz") as sdist_archive:
        member = sdist_archive.getmember(f"pyocd_debug_mcp-{VERSION}/PKG-INFO")
        extracted = sdist_archive.extractfile(member)
        if extracted is None:
            raise RuntimeError(f"could not read {sdist.name}:{member.name}")
        sdist_metadata = extracted.read()
    return wheel_metadata, sdist_metadata


def _archive_member_metadata(wheel: Path, sdist: Path) -> tuple[object, object]:
    """Return install-relevant archive metadata for exact reference comparison."""

    with zipfile.ZipFile(wheel) as archive:
        wheel_metadata = (
            archive.comment,
            {
                info.filename: (
                    info.create_system,
                    info.create_version,
                    info.extract_version,
                    info.flag_bits,
                    info.compress_type,
                    info.date_time,
                    info.external_attr,
                    info.internal_attr,
                    info.extra,
                    info.comment,
                )
                for info in archive.infolist()
            },
        )
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_metadata = (
            dict(archive.pax_headers),
            {
                member.name: (
                    member.mode,
                    member.uid,
                    member.gid,
                    member.size,
                    member.mtime,
                    member.type,
                    member.linkname,
                    member.uname,
                    member.gname,
                    member.devmajor,
                    member.devminor,
                    dict(member.pax_headers),
                )
                for member in archive
            },
        )
    return wheel_metadata, sdist_metadata


def _verify_generated_metadata_alignment(
    wheel: Path,
    sdist: Path,
    reference_wheel: Path,
    reference_sdist: Path,
) -> None:
    """Compare generated core metadata with a fresh trusted build from this tree."""

    candidate = _core_metadata(wheel, sdist)
    reference = _core_metadata(reference_wheel, reference_sdist)
    if reference[0] != reference[1]:
        raise RuntimeError("fresh reference wheel METADATA and sdist PKG-INFO differ")
    if candidate != reference:
        raise RuntimeError("release artifact core metadata differs from a fresh trusted build")
    if _archive_member_metadata(wheel, sdist) != _archive_member_metadata(
        reference_wheel, reference_sdist
    ):
        raise RuntimeError("release artifact member metadata differs from a fresh trusted build")


def _verify_pair(
    work: Path,
    wheel: Path,
    sdist: Path,
    *,
    reference_wheel: Path,
    reference_sdist: Path,
) -> None:
    _verify_source_alignment(wheel, sdist)
    _verify_generated_metadata_alignment(
        wheel,
        sdist,
        reference_wheel,
        reference_sdist,
    )
    _verify_base_install(work, wheel, "wheel")
    _verify_base_install(work, sdist, "sdist")
    _verify_can_extra(work, wheel, "wheel")
    _verify_can_extra(work, sdist, "sdist")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifact_dir",
        nargs="?",
        type=Path,
        help="verify this exact wheel/sdist pair instead of building a temporary pair",
    )
    parser.add_argument(
        "--keep-dist",
        type=Path,
        help="also copy/build artifacts into this directory after verification",
    )
    arguments = parser.parse_args()
    if arguments.artifact_dir is not None and arguments.keep_dist is not None:
        parser.error("artifact_dir and --keep-dist are mutually exclusive")

    with _verification_workspace() as work:
        if arguments.artifact_dir is None:
            dist = work / "dist"
            _run(["uv", "build", "--out-dir", str(dist)], cwd=ROOT)
        else:
            dist = arguments.artifact_dir
        source_wheel, source_sdist = _release_artifacts(dist)
        wheel, sdist = _snapshot_release_pair(work, source_wheel, source_sdist)
        if arguments.artifact_dir is None:
            reference_wheel, reference_sdist = wheel, sdist
        else:
            reference_dist = work / "reference-dist"
            _run(["uv", "build", "--out-dir", str(reference_dist)], cwd=ROOT)
            reference_wheel, reference_sdist = _release_artifacts(reference_dist)
            _verify_source_alignment(reference_wheel, reference_sdist)
            # Reject any byte that was not emitted by the pinned deterministic
            # build before asking archive parsers to inspect an untrusted pair.
            # This covers otherwise-ignored ZIP/tar trailers and also bounds tar
            # extension-header decompression by avoiding it for non-identical input.
            _assert_pair_digest_matches(
                (reference_wheel, reference_sdist),
                (wheel, sdist),
                label="candidate",
            )
        _verify_pair(
            work,
            wheel,
            sdist,
            reference_wheel=reference_wheel,
            reference_sdist=reference_sdist,
        )

        verified_output = (wheel, sdist)
        if arguments.artifact_dir is not None:
            recheck_root = work / "source-recheck"
            recheck_root.mkdir(mode=0o700)
            current_source = _release_artifacts(dist)
            rechecked = _snapshot_release_pair(recheck_root, *current_source)
            _assert_pair_digest_matches((wheel, sdist), rechecked, label="source")

        if arguments.keep_dist is not None:
            verified_output = _publish_verified_pair(arguments.keep_dist, wheel, sdist)

        for artifact in verified_output:
            print(f"sha256 {artifact.name} {_artifact_sha256(artifact)}")

    print(f"release artifacts verified for {VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
