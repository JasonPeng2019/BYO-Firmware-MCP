"""Monitoring store location and workspace identity.

Two independent questions live here, and conflating them is the trap this module
exists to prevent:

* **Where do we write?** The per-user application-data directory, with the
  operator root as a last-resort fallback and an in-memory buffer if neither is
  writable. Logging must never silently no-op.
* **Which workspace is this?** Derived from the path the agent supplies on the
  initialization handshake. That path is an *identity input only* -- monitoring
  output is never written inside the workspace project directory.

Workspace identity is deliberately split in two. The local directory name is a
salted digest of the workspace path: its purpose is auditable anonymization, not
secrecy from the owner, so that no plaintext project path is ever written to disk
or carried in anything that could be listed, shipped, or screen-shared. The
identifier that is *delivered* is a separate opaque random token carrying zero
path information, so it cannot be brute-forced back to a path under any salt
policy. Never derive the delivered identifier from the path, even hashed: a
workspace path is a small, guessable input.
"""

from __future__ import annotations

import os
import secrets
import threading
from dataclasses import dataclass
from enum import Enum
from hashlib import blake2b
from pathlib import Path

APP_DIR_NAME = "BYO"
SERVER_DATA = "server_data"
SIMULATED_REMOTE = "simulated_remote"
OPERATOR_SUBDIR = ".byo-monitor"
SALT_FILE = "fingerprint.salt"
TOKEN_FILE = "workspace.token"
UNBOUND_WORKSPACE = "unbound"

_SALT_BYTES = 32
_TOKEN_BYTES = 16


class StoreState(str, Enum):
    """Where the monitoring store resolved to, reported by the health check."""

    APP_DATA = "app_data"
    OPERATOR_ROOT = "operator_root"
    BUFFERING = "buffering"


@dataclass(frozen=True, slots=True)
class StoreRoot:
    """A resolved monitoring store, or the buffering state when none is writable."""

    state: StoreState
    root: Path | None

    @property
    def available(self) -> bool:
        return self.root is not None

    @property
    def server_data(self) -> Path | None:
        return None if self.root is None else self.root / SERVER_DATA

    @property
    def simulated_remote(self) -> Path | None:
        return None if self.root is None else self.root / SIMULATED_REMOTE


_guard = threading.Lock()
_cached_root: StoreRoot | None = None
_cached_salt: bytes | None = None
_override_root: Path | None = None


def _usable(candidate: Path) -> bool:
    """Return whether a candidate root exists and accepts a write."""

    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _app_data_candidate() -> Path | None:
    try:
        from platformdirs import user_data_dir

        return Path(user_data_dir(APP_DIR_NAME, appauthor=False, roaming=False))
    except Exception:  # noqa: BLE001 - a missing or broken app-dirs backend must not raise
        return None


def _operator_candidate() -> Path | None:
    configured = os.environ.get("BYO_MCP_ARTIFACT_ROOT", "").strip()
    if not configured:
        return None
    try:
        return Path(configured).expanduser().resolve() / OPERATOR_SUBDIR
    except OSError:
        return None


def resolve_store_root() -> StoreRoot:
    """Resolve the monitoring store once and cache it for the process.

    The agent-supplied workspace path is deliberately absent from this chain: it
    identifies a workspace, it is never a write target.
    """

    global _cached_root
    with _guard:
        if _cached_root is not None:
            return _cached_root
        if _override_root is not None:
            candidates: list[tuple[StoreState, Path]] = [
                (StoreState.APP_DATA, _override_root)
            ]
        else:
            candidates = []
            app_data = _app_data_candidate()
            if app_data is not None:
                candidates.append((StoreState.APP_DATA, app_data))
            operator = _operator_candidate()
            if operator is not None:
                candidates.append((StoreState.OPERATOR_ROOT, operator))
        resolved = StoreRoot(StoreState.BUFFERING, None)
        for state, candidate in candidates:
            if _usable(candidate):
                for sub in (SERVER_DATA, SIMULATED_REMOTE):
                    try:
                        (candidate / sub).mkdir(parents=True, exist_ok=True)
                    except OSError:
                        continue
                resolved = StoreRoot(state, candidate)
                break
        _cached_root = resolved
        return resolved


def deployment_salt() -> bytes:
    """Return the per-deployment privacy salt, creating it on first use.

    The salt lives with the store because workspace identity needs it too; putting
    it in the redaction module would create an import cycle. It is a *privacy*
    secret, not an integrity one -- it defends against readers of delivered
    reports, not against the machine's owner.
    """

    # Resolve the store first rather than reading whatever happens to be cached.
    # Depending on call order here would silently hand back a process-local salt
    # when the salt had not yet been loaded, so the same workspace would hash
    # differently in two processes sharing one store.
    store_root = resolve_store_root()
    global _cached_salt
    with _guard:
        if _cached_salt is not None:
            return _cached_salt
        salt = secrets.token_bytes(_SALT_BYTES)
        store = store_root
        if store is not None and store.root is not None:
            path = store.root / SALT_FILE
            try:
                if path.exists():
                    existing = path.read_bytes()
                    if len(existing) == _SALT_BYTES:
                        _cached_salt = existing
                        return existing
                path.write_bytes(salt)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            except OSError:
                pass  # a process-local salt is acceptable; never crash for this
        _cached_salt = salt
        return salt


def workspace_id(path: Path | None) -> str:
    """Return the anonymized directory name for a workspace path."""

    if path is None:
        return UNBOUND_WORKSPACE
    try:
        text = str(path.resolve())
    except OSError:
        text = str(path)
    digest = blake2b(
        deployment_salt() + text.casefold().encode("utf-8", "replace"),
        digest_size=8,
    )
    return digest.hexdigest()


def _workspace_dir(store: StoreRoot, wid: str) -> Path | None:
    server_data = store.server_data
    return None if server_data is None else server_data / wid


def read_workspace_token(store: StoreRoot, wid: str) -> str | None:
    """Return the delivered workspace token without creating anything.

    Readers such as the health check must use this: the health check is required
    to be side-effect free, and lazily creating a token file is a side effect.
    """

    directory = _workspace_dir(store, wid)
    if directory is None:
        return None
    try:
        return (directory / TOKEN_FILE).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def workspace_token(store: StoreRoot, wid: str) -> str:
    """Return the workspace's delivered identifier, creating it on first bind.

    The token is random and carries no path information, so the owner can see
    exactly what will represent the repository off-box. Nothing carrying the
    plaintext path may ever be written into this directory.
    """

    existing = read_workspace_token(store, wid)
    if existing is not None:
        return existing
    token = secrets.token_hex(_TOKEN_BYTES)
    directory = _workspace_dir(store, wid)
    if directory is None:
        return token
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / TOKEN_FILE).write_text(token, encoding="utf-8")
    except OSError:
        pass  # an unpersisted token still identifies this run honestly
    return token


def _reset_cache(override: Path | None = None) -> None:
    """Test-only hook: clear the cached store and optionally pin a root.

    Tests must use this rather than an environment variable. ``resolve_store_root``
    tries the application-data directory first, so a test that only set
    ``BYO_MCP_ARTIFACT_ROOT`` would still write into the developer's real store.
    """

    global _cached_root, _cached_salt, _override_root
    with _guard:
        _cached_root = None
        _cached_salt = None
        _override_root = override


__all__ = [
    "APP_DIR_NAME",
    "SERVER_DATA",
    "SIMULATED_REMOTE",
    "UNBOUND_WORKSPACE",
    "StoreRoot",
    "StoreState",
    "deployment_salt",
    "read_workspace_token",
    "resolve_store_root",
    "workspace_id",
    "workspace_token",
]
