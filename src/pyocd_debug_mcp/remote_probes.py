"""Persistent registry of pyOCD probe-server endpoints (`remote:<host>:<port>`).

This is the delivery mechanism for the one route that survives a local USB/driver
stack pyOCD cannot see through at all: `pyocd server` owns a probe on one machine and
serves it over TCP, and a client addresses it with `TCPClientProbe` via the
`remote:<host>:<port>` selector. Registering an endpoint here makes it appear as a
normal probe row in every future inventory snapshot and persists it across restarts, so
the gap closes permanently rather than needing to be rediscovered every run.

Nothing here imports pyOCD or opens a session. `check_endpoint` is a plain bounded TCP
connect -- the honest, cheap reachability signal; anything heavier (constructing a real
probe, opening a session) belongs at connect time, not at registration or snapshot time.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

DEFAULT_CHECK_TIMEOUT_SECONDS = 3.0

# Serializes threads before they acquire the authoritative per-registry OS lock.
# Multiple MCP server processes can share one project, so a process-local lock alone
# is insufficient for the load -> modify -> save transaction.
_registry_lock = threading.Lock()
_endpoint_check_slots = threading.BoundedSemaphore(8)


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(attributes & marker) or bool(getattr(metadata, "st_reparse_tag", 0))


def _ensure_durable_directory(directory: Path) -> None:
    """Create missing components and persist each new directory entry."""

    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for component in reversed(missing):
        component.mkdir(exist_ok=True)
        _fsync_directory(component.parent)


@contextmanager
def _registry_file_lock(path: Path) -> Iterator[None]:
    """Serialize one registry's read/modify/write transaction across processes."""

    _ensure_durable_directory(path.parent)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    locked = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError(f"remote-probe registry lock is not a private regular file: {lock_path}")
        path_metadata = lock_path.lstat()
        if stat.S_ISLNK(path_metadata.st_mode) or _is_reparse_point(path_metadata):
            raise OSError(f"remote-probe registry lock must not be a link: {lock_path}")
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or path_metadata.st_nlink != 1
            or (path_metadata.st_dev, path_metadata.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise OSError(f"remote-probe registry lock changed while opening: {lock_path}")
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            try:
                fchmod(descriptor, 0o600)
            except OSError:
                pass
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
        elif os.name == "nt":  # pragma: no cover - native Windows CI
            import msvcrt

            # Windows byte-range locks may extend beyond EOF. Locking first
            # avoids a first-use race where two processes both try to write the
            # sentinel byte before either owns the range.
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            locked = True
        else:  # pragma: no cover - supported runtime platforms are POSIX/Windows
            raise OSError(f"unsupported registry-lock platform: {os.name}")
        current = lock_path.lstat()
        if (
            stat.S_ISLNK(current.st_mode)
            or _is_reparse_point(current)
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise OSError(f"remote-probe registry lock changed before acquisition: {lock_path}")
        yield
    finally:
        if locked and os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        elif locked and os.name == "nt":  # pragma: no cover - native Windows CI
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


class RemoteProbeError(RuntimeError):
    """A host or port value failed correctness validation as a network address."""


class RemoteProbeDurabilityError(OSError):
    """The registry was replaced but its directory entry may not be durable."""


class _DuplicateRegistryKey(ValueError):
    """Internal signal for a duplicate key in mutation-sensitive JSON."""


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateRegistryKey(key)
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class RemoteProbeEntry:
    """One registered probe-server endpoint."""

    host: str
    port: int
    description: str
    registered_at: str
    """ISO-8601 UTC timestamp of the most recent register call for this endpoint."""

    @property
    def selector(self) -> str:
        """The exact string to hand pyOCD as `unique_id`, prefix included.

        `TCPClientProbe.get_probe_with_id` returns `None` unless `is_explicit`, and
        `is_explicit` is only set when the `remote:` prefix is present -- so this
        property, not `host`/`port` alone, is what every consumer must use.
        """

        return f"remote:{self.host}:{self.port}"


def normalize_host(host: str) -> str:
    """Return one selector-compatible DNS/IPv4 host without an embedded port."""

    if not isinstance(host, str):
        raise RemoteProbeError(f"host must be a string, got {host!r}")
    normalized = host.strip()
    if not normalized:
        raise RemoteProbeError("host must not be empty")
    if ":" in normalized:
        raise RemoteProbeError(
            "host must not contain ':' or an embedded port; pyOCD remote selectors do not "
            "support IPv6 literals"
        )
    try:
        ascii_host = normalized.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise RemoteProbeError(f"host is not a valid DNS/IPv4 name: {host!r}") from exc
    validation_host = ascii_host[:-1] if ascii_host.endswith(".") else ascii_host
    labels = validation_host.split(".")
    if (
        len(ascii_host) > 254
        or not labels
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(not (character.isalnum() or character == "-") for character in label)
            for label in labels
        )
    ):
        raise RemoteProbeError(f"host is not a valid DNS/IPv4 name: {host!r}")
    return ascii_host


def normalize_port(port: object) -> int:
    """Accept an integer/decimal string in the real TCP port range."""

    if isinstance(port, bool):
        raise RemoteProbeError(f"port must be an integer, got {port!r}")
    if isinstance(port, int):
        value = port
    elif isinstance(port, str) and port.strip().isascii() and port.strip().isdigit():
        try:
            value = int(port.strip())
        except ValueError as exc:
            raise RemoteProbeError(f"port must be an integer, got {port!r}") from exc
    else:
        raise RemoteProbeError(f"port must be an integer, got {port!r}")
    if not 1 <= value <= 65535:
        raise RemoteProbeError(f"port must be between 1 and 65535, got {value}")
    return value


def load_remote_probes(path: Path) -> tuple[RemoteProbeEntry, ...]:
    """Load the registry. A missing or unreadable file is an empty tuple, never an error.

    A malformed file must not crash discovery: any entry that fails to parse is skipped
    rather than aborting the whole load, and a file that is not valid JSON at all yields
    the empty tuple. Discovery failing closed because a registry file got corrupted would
    be a worse bug than the one this registry exists to fix.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ()
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return ()
    if not isinstance(document, dict):
        return ()
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        return ()

    entries: list[RemoteProbeEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        try:
            host = normalize_host(raw.get("host", ""))
            port = normalize_port(raw.get("port"))
        except RemoteProbeError:
            continue
        description = raw.get("description")
        if not isinstance(description, str):
            description = ""
        registered_at = raw.get("registered_at")
        if not isinstance(registered_at, str):
            registered_at = ""
        entries.append(
            RemoteProbeEntry(
                host=host, port=port, description=description, registered_at=registered_at
            )
        )
    return tuple(entries)


def _load_remote_probes_for_mutation(path: Path) -> tuple[RemoteProbeEntry, ...]:
    """Load a registry without discarding bytes a mutation would overwrite.

    Discovery deliberately treats a damaged registry as empty/partial so one bad
    optional file cannot take down inventory.  A write has a different contract:
    silently converting malformed or future-schema input to the subset understood by
    this version would destroy registrations while reporting success.  Mutations
    therefore accept only the exact schema emitted by :func:`save_remote_probes`.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError) as exc:
        raise RemoteProbeError(
            f"cannot read remote-probe registry for mutation; existing bytes were preserved: {exc}"
        ) from exc
    try:
        document = json.loads(text, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise RemoteProbeError(
            "remote-probe registry is malformed; refusing to overwrite existing bytes"
        ) from exc
    if not isinstance(document, dict) or set(document) != {"schema_version", "entries"}:
        raise RemoteProbeError(
            "remote-probe registry has an unsupported shape; refusing to overwrite it"
        )
    schema_version = document.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
        or not isinstance(document.get("entries"), list)
    ):
        raise RemoteProbeError(
            "remote-probe registry has an unsupported schema; refusing to overwrite it"
        )

    entries: list[RemoteProbeEntry] = []
    identities: set[tuple[str, int]] = set()
    for raw in document["entries"]:
        if not isinstance(raw, dict) or set(raw) != {
            "host",
            "port",
            "description",
            "registered_at",
        }:
            raise RemoteProbeError(
                "remote-probe registry contains an invalid entry; refusing to overwrite it"
            )
        host = raw.get("host")
        description = raw.get("description")
        registered_at = raw.get("registered_at")
        port = raw.get("port")
        if (
            not isinstance(host, str)
            or not isinstance(description, str)
            or not isinstance(registered_at, str)
            or not isinstance(port, int)
            or isinstance(port, bool)
        ):
            raise RemoteProbeError(
                "remote-probe registry contains an invalid entry; refusing to overwrite it"
            )
        try:
            normalized_host = normalize_host(host)
            normalized_port = normalize_port(port)
        except RemoteProbeError as exc:
            raise RemoteProbeError(
                "remote-probe registry contains an invalid entry; refusing to overwrite it"
            ) from exc
        if normalized_host != host:
            raise RemoteProbeError(
                "remote-probe registry contains a non-canonical host; refusing to overwrite it"
            )
        identity = (normalized_host.casefold(), normalized_port)
        if identity in identities:
            raise RemoteProbeError(
                "remote-probe registry contains duplicate entries; refusing to overwrite it"
            )
        identities.add(identity)
        entries.append(
            RemoteProbeEntry(
                host=normalized_host,
                port=normalized_port,
                description=description,
                registered_at=registered_at,
            )
        )
    return tuple(entries)


def _fsync_directory(directory: Path) -> None:
    """Persist a replaced registry directory entry on POSIX."""

    if os.name != "posix":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_remote_probes(path: Path, entries: Sequence[RemoteProbeEntry]) -> None:
    """Atomically replace the registry file with `entries`, creating parent dirs."""

    _ensure_durable_directory(path.parent)
    document = {
        "schema_version": 1,
        "entries": [
            {
                "host": entry.host,
                "port": entry.port,
                "description": entry.description,
                "registered_at": entry.registered_at,
            }
            for entry in entries
        ],
    }
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        try:
            _fsync_directory(path.parent)
        except OSError as exc:
            raise RemoteProbeDurabilityError(
                "remote-probe registry was updated, but directory durability is uncertain"
            ) from exc
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def upsert_remote_probe(
    entries: Sequence[RemoteProbeEntry],
    host: str,
    port: int,
    description: str,
) -> tuple[RemoteProbeEntry, ...]:
    """Add a new endpoint, or update an existing one's description and timestamp.

    Deduplicated on `(host.casefold(), port)`: re-registering an existing endpoint
    updates its row rather than creating a duplicate.
    """

    normalized_host = normalize_host(host)
    normalized_port = normalize_port(port)
    key = (normalized_host.casefold(), normalized_port)
    new_entry = RemoteProbeEntry(
        host=normalized_host,
        port=normalized_port,
        description=(description or "").strip(),
        registered_at=_now_iso(),
    )
    updated: list[RemoteProbeEntry] = []
    replaced = False
    for entry in entries:
        if (entry.host.casefold(), entry.port) == key:
            updated.append(new_entry)
            replaced = True
        else:
            updated.append(entry)
    if not replaced:
        updated.append(new_entry)
    return tuple(updated)


def remove_remote_probe(
    entries: Sequence[RemoteProbeEntry],
    host: str,
    port: int,
) -> tuple[tuple[RemoteProbeEntry, ...], bool]:
    """Return `entries` without the `(host, port)` match, and whether one was removed."""

    normalized_host = normalize_host(host)
    normalized_port = normalize_port(port)
    key = (normalized_host.casefold(), normalized_port)
    kept = tuple(entry for entry in entries if (entry.host.casefold(), entry.port) != key)
    return kept, len(kept) != len(entries)


def register_entry(
    path: Path, host: str, port: int, description: str
) -> tuple[RemoteProbeEntry, ...]:
    """Atomically load, upsert, and persist under thread and process locks.

    The whole read-modify-write cycle runs as one critical section, so a second
    concurrent `register_entry`/`unregister_entry` call against the same file always
    either fully precedes or fully follows this one -- never interleaves with it and
    overwrites its write with a stale snapshot. Callers must resolve reachability with
    `check_endpoint` *before* calling this: it does not touch the network itself.
    """

    with _registry_lock, _registry_file_lock(path):
        entries = _load_remote_probes_for_mutation(path)
        updated = upsert_remote_probe(entries, host, port, description)
        save_remote_probes(path, updated)
        return updated


def unregister_entry(path: Path, host: str, port: int) -> tuple[tuple[RemoteProbeEntry, ...], bool]:
    """Atomically load, remove, and persist under thread and process locks.

    See `register_entry` for why the whole cycle must be one critical section.
    """

    with _registry_lock, _registry_file_lock(path):
        entries = _load_remote_probes_for_mutation(path)
        remaining, removed = remove_remote_probe(entries, host, port)
        if removed:
            save_remote_probes(path, remaining)
        return remaining, removed


def check_endpoint(
    host: str, port: int, timeout_seconds: float = DEFAULT_CHECK_TIMEOUT_SECONDS
) -> bool:
    """A plain bounded TCP accept check. No pyOCD import, no session, no retries.

    This is the honest, cheap reachability signal a registration call reports back to
    the agent. Anything heavier -- constructing a real probe, opening a session -- is
    connect-time behavior and does not belong here.
    """

    timeout = max(0.0, float(timeout_seconds))
    if not _endpoint_check_slots.acquire(blocking=False):
        return False
    finished = threading.Event()
    reachable = False

    def attempt() -> None:
        nonlocal reachable
        try:
            with socket.create_connection((host, port), timeout=timeout):
                reachable = True
        except OSError:
            pass
        finally:
            finished.set()
            _endpoint_check_slots.release()

    try:
        thread = threading.Thread(target=attempt, name="byo-remote-probe-check", daemon=True)
        thread.start()
    except (OSError, RuntimeError):
        _endpoint_check_slots.release()
        return False
    finished.wait(timeout)
    return reachable if finished.is_set() else False


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
