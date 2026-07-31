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
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

DEFAULT_CHECK_TIMEOUT_SECONDS = 3.0

# Serializes the load -> modify -> save cycle below so two concurrent register/
# unregister calls (an agent commonly issues several tool calls in one turn, and
# nothing upstream serializes a sync tool call that carries no board_id -- see
# kernel/operations.py's worker_lock(None)) can never interleave and silently lose one
# call's write while its own tool response already reported success. One server
# process owns this file; there is no cross-process writer to coordinate with, so a
# plain in-process lock is the whole fix. It must never be held across a network call
# -- `register_entry`/`unregister_entry` below only ever wrap the on-disk mutation,
# never `check_endpoint`, or two unrelated registrations would serialize behind each
# other's multi-second TCP timeout.
_registry_lock = threading.Lock()


class RemoteProbeError(RuntimeError):
    """A host or port value failed correctness validation as a network address."""


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
    """Strip and reject an empty host. A correctness check on a network address."""

    normalized = str(host or "").strip()
    if not normalized:
        raise RemoteProbeError("host must not be empty")
    return normalized


def normalize_port(port: object) -> int:
    """Coerce to `int` and require it fall inside the real TCP port range."""

    try:
        value = int(port)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RemoteProbeError(f"port must be an integer, got {port!r}") from exc
    if isinstance(port, bool):
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
    except OSError:
        return ()
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
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


def save_remote_probes(path: Path, entries: Sequence[RemoteProbeEntry]) -> None:
    """Atomically replace the registry file with `entries`, creating parent dirs."""

    path.parent.mkdir(parents=True, exist_ok=True)
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
    except BaseException:
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
    """Atomically load, upsert, and persist one endpoint under `_registry_lock`.

    The whole read-modify-write cycle runs as one critical section, so a second
    concurrent `register_entry`/`unregister_entry` call against the same file always
    either fully precedes or fully follows this one -- never interleaves with it and
    overwrites its write with a stale snapshot. Callers must resolve reachability with
    `check_endpoint` *before* calling this: it does not touch the network itself.
    """

    with _registry_lock:
        entries = load_remote_probes(path)
        updated = upsert_remote_probe(entries, host, port, description)
        save_remote_probes(path, updated)
        return updated


def unregister_entry(
    path: Path, host: str, port: int
) -> tuple[tuple[RemoteProbeEntry, ...], bool]:
    """Atomically load, remove, and (if found) persist under `_registry_lock`.

    See `register_entry` for why the whole cycle must be one critical section.
    """

    with _registry_lock:
        entries = load_remote_probes(path)
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

    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
