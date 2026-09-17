"""Project-local, restart-invalidated manual permission grants.

The files are a coordination protocol with the shipped manual-only skills, not
proof of human origin.  Server-issued tokens remain memory-only and every
destructive/transition attempt is durably marked consumed before it runs.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


Action = Literal["downgrade", "mass-erase"]
_ACTIONS: tuple[Action, ...] = ("downgrade", "mass-erase")
_CLAIM_KEYS = frozenset(
    {"schema_version", "owner", "claim_id", "server_run_id", "action", "created_at"}
)
_CLAIM_ID = re.compile(r"[0-9a-f]{32}\Z")


class ManualPermissionError(RuntimeError):
    """Stable ``manual/*`` refusal surfaced by permission-facing tools."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"manual/{code}: {message}")


@dataclass(frozen=True, slots=True)
class ManualGrant:
    action: Action
    state: str
    server_run_id: str
    grant_id: str | None
    board_id: str | None
    policy_digest: str | None
    binding_digest: str | None
    claim_id: str | None


class ManualPermissionRepository:
    """Own the server side of the frozen manual permission file protocol."""

    def __init__(self, project_root: Path, server_run_id: str) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.server_run_id = server_run_id
        self.root = self.project_root / ".agent-workspace" / "runtime" / "manual-permissions"
        self._guard = threading.RLock()
        self._tokens: dict[str, tuple[str, str, str, str]] = {}
        self._consumed_tokens: set[str] = set()
        self._ready = False
        self._reset_error: Exception | None = None
        self._protocol_timeout_seconds = 5.0

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def reset_startup(self) -> None:
        """Invalidate every durable grant before any authorization is consumed."""

        with self._guard:
            try:
                with self._protocol_lock():
                    self._atomic_write(self.root / "epoch.json", self._epoch_document())
                    for action in _ACTIONS:
                        self._atomic_write(self._action_path(action), self._locked_document(action))
                    # A helper may have read this just-published epoch and be
                    # atomically promoting its grant.  Its current-epoch claim
                    # is real contention, not stale startup debris. Only a
                    # claim positively tied to another run is safe to clear.
                    for path in self.root.glob("*.claim"):
                        claim = self._load_claim(path, missing_is_none=True)
                        if claim is not None and claim["server_run_id"] != self.server_run_id:
                            self._unlink_claim_if_matches(path, claim)
            except Exception as exc:  # callers must leave unrelated raw work reachable
                self._ready = False
                self._reset_error = exc
                raise
            self._tokens.clear()
            self._consumed_tokens.clear()
            self._ready = True
            self._reset_error = None

    def status(self, action: Action) -> ManualGrant:
        with self._guard:
            document = self._load_action(action)
            return ManualGrant(
                action=action,
                state=self._field(document, "state", str),
                server_run_id=self._field(document, "server_run_id", str),
                grant_id=self._optional_text(document, "grant_id"),
                board_id=self._optional_text(document, "board_id"),
                policy_digest=self._optional_text(document, "policy_digest"),
                binding_digest=self._optional_text(document, "binding_digest"),
                claim_id=self._optional_text(document, "claim_id"),
            )

    @property
    def readiness(self) -> tuple[bool, str | None]:
        """Expose startup-reset state for non-authorizing capability discovery."""

        with self._guard:
            return self._ready, (str(self._reset_error) if self._reset_error is not None else None)

    def write_unlocked_grant(
        self,
        *,
        action: Action,
        grant_id: str,
        board_id: str,
        policy_digest: str,
        binding_digest: str | None = None,
    ) -> None:
        """Write an unlocked current-epoch grant (the workspace helper's protocol)."""

        with self._guard:
            self._require_ready()
            with self._protocol_lock():
                current = self._load_action(action)
                if current["state"] != "locked":
                    raise ManualPermissionError(
                        "already-consumed", "the action is not locked for a new grant"
                    )
                self._atomic_write(
                    self._action_path(action),
                    self._document(
                        action=action,
                        state="unlocked",
                        grant_id=grant_id,
                        board_id=board_id,
                        policy_digest=policy_digest,
                        binding_digest=binding_digest,
                        claim_id=None,
                    ),
                )

    def issue_downgrade_token(self, board_id: str, grant_id: str, policy_digest: str) -> str:
        """Reserve one matching downgrade grant and issue an opaque run-local token."""

        with self._guard:
            claim = self._reserve("downgrade", board_id, grant_id, policy_digest, None)
            token = secrets.token_urlsafe(24)
            self._tokens[token] = ("downgrade", board_id, policy_digest, claim)
            return token

    def consume_downgrade(self, board_id: str, token: str, policy_digest: str) -> None:
        """Consume a token before publishing the downgrade pointer change."""

        with self._guard:
            try:
                action, expected_board, expected_policy, claim = self._tokens[token]
            except KeyError as exc:
                if token in self._consumed_tokens:
                    raise ManualPermissionError(
                        "already-consumed", "the downgrade permission was already consumed"
                    ) from exc
                raise ManualPermissionError(
                    "stale-epoch", "the downgrade permission is absent or stale"
                ) from exc
            if action != "downgrade" or expected_board != board_id:
                raise ManualPermissionError(
                    "wrong-board", "the downgrade permission belongs to another board"
                )
            if expected_policy != policy_digest:
                self._abandon_downgrade(board_id, expected_policy, claim)
                self._tokens.pop(token, None)
                self._consumed_tokens.add(token)
                raise ManualPermissionError(
                    "stale-policy", "the board policy changed after permission was issued"
                )
            self._consume("downgrade", board_id, policy_digest, None, claim)
            self._tokens.pop(token, None)
            self._consumed_tokens.add(token)

    def reserve_mass_erase(
        self,
        board_id: str,
        grant_id: str,
        policy_digest: str,
        binding_digest: str,
    ) -> str:
        """Reserve a disclosure-bound mass-erase manual grant before approval."""

        with self._guard:
            return self._reserve("mass-erase", board_id, grant_id, policy_digest, binding_digest)

    def consume_mass_erase(
        self,
        board_id: str,
        policy_digest: str,
        binding_digest: str,
        claim_id: str,
    ) -> None:
        with self._guard:
            self._consume("mass-erase", board_id, policy_digest, binding_digest, claim_id)

    def abandon_mass_erase(
        self,
        board_id: str,
        claim_id: str,
    ) -> None:
        """Release one failed reservation back to a clean locked state.

        This is intentionally not a rollback to ``unlocked``: the previous
        human grant and its server claim are dead.  The helper may publish a
        fresh current-epoch grant in this same run, while the old claim can
        never be replayed.
        """

        with self._guard:
            self._require_ready()
            with self._protocol_lock():
                transient_path, transient_claim = self._acquire_claim("mass-erase")
                try:
                    document = self._load_action("mass-erase")
                    if (
                        document["state"] != "reserved"
                        or document.get("claim_id") != claim_id
                        or document.get("board_id") != board_id
                    ):
                        return
                    self._atomic_write(
                        self._action_path("mass-erase"),
                        self._locked_document("mass-erase"),
                    )
                finally:
                    self._unlink_claim_if_matches(transient_path, transient_claim)

    def _abandon_downgrade(self, board_id: str, policy_digest: str, claim_id: str) -> None:
        """Invalidate only this stale downgrade reservation under the shared lock."""

        self._require_ready()
        with self._protocol_lock():
            transient_path, transient_claim = self._acquire_claim("downgrade")
            try:
                document = self._load_action("downgrade")
                if document["state"] != "reserved" or document.get("claim_id") != claim_id:
                    return
                self._validate_binding(
                    document,
                    "downgrade",
                    board_id,
                    self._field(document, "grant_id", str),
                    policy_digest,
                    None,
                )
                self._atomic_write(
                    self._action_path("downgrade"), self._locked_document("downgrade")
                )
            finally:
                self._unlink_claim_if_matches(transient_path, transient_claim)

    def _reserve(
        self,
        action: Action,
        board_id: str,
        grant_id: str,
        policy_digest: str,
        binding_digest: str | None,
    ) -> str:
        self._require_ready()
        with self._protocol_lock():
            claim_path, transient_claim = self._acquire_claim(action)
            try:
                # Read only after the cross-process claim has been won: a workspace
                # helper may have been atomically publishing an unlocked grant.
                document = self._load_action(action)
                if document["state"] == "locked":
                    raise ManualPermissionError("locked", f"ask the human to invoke ${action}")
                if document["state"] == "reserved":
                    raise ManualPermissionError(
                        "already-reserved", "this manual grant is already claimed"
                    )
                if document["state"] == "consumed":
                    raise ManualPermissionError(
                        "already-consumed", "this manual grant was already consumed"
                    )
                if document["state"] != "unlocked":
                    raise ManualPermissionError(
                        "malformed", "manual grant has an unsupported state"
                    )
                self._validate_binding(
                    document, action, board_id, grant_id, policy_digest, binding_digest
                )
                claim = secrets.token_hex(16)
                self._atomic_write(
                    self._action_path(action),
                    self._document(
                        action=action,
                        state="reserved",
                        grant_id=grant_id,
                        board_id=board_id,
                        policy_digest=policy_digest,
                        binding_digest=binding_digest,
                        claim_id=claim,
                    ),
                )
                return claim
            finally:
                # The reserved durable state, not the transient claim, blocks later
                # callers. A crash before here leaves a current-epoch claim and
                # fails closed; the next run removes this positively old claim.
                self._unlink_claim_if_matches(claim_path, transient_claim)

    def _consume(
        self,
        action: Action,
        board_id: str,
        policy_digest: str,
        binding_digest: str | None,
        claim_id: str,
    ) -> None:
        self._require_ready()
        with self._protocol_lock():
            transient_path, transient_claim = self._acquire_claim(action)
            try:
                # Re-read after winning the cross-process claim. This closes the
                # window between plan acceptance/token issue and destructive use.
                document = self._load_action(action)
                if document["state"] == "consumed":
                    raise ManualPermissionError(
                        "already-consumed", "the manual grant was already consumed"
                    )
                if document["state"] != "reserved":
                    raise ManualPermissionError(
                        "locked", "the manual grant is not reserved for this attempt"
                    )
                if document.get("claim_id") != claim_id:
                    raise ManualPermissionError(
                        "already-reserved", "another attempt owns this manual grant"
                    )
                self._validate_binding(
                    document,
                    action,
                    board_id,
                    self._field(document, "grant_id", str),
                    policy_digest,
                    binding_digest,
                )
                self._atomic_write(
                    self._action_path(action),
                    self._document(
                        action=action,
                        state="consumed",
                        grant_id=self._field(document, "grant_id", str),
                        board_id=board_id,
                        policy_digest=policy_digest,
                        binding_digest=binding_digest,
                        claim_id=claim_id,
                    ),
                )
            finally:
                self._unlink_claim_if_matches(transient_path, transient_claim)

    def _validate_binding(
        self,
        document: dict[str, object],
        action: Action,
        board_id: str,
        grant_id: str,
        policy_digest: str,
        binding_digest: str | None,
    ) -> None:
        if document.get("action") != action:
            raise ManualPermissionError(
                "wrong-action", "manual grant action does not match this operation"
            )
        if document.get("server_run_id") != self.server_run_id:
            raise ManualPermissionError("stale-epoch", "manual grant is from a previous server run")
        if document.get("grant_id") != grant_id or document.get("board_id") != board_id:
            raise ManualPermissionError(
                "wrong-board", "manual grant does not match the requested board"
            )
        if document.get("policy_digest") != policy_digest:
            raise ManualPermissionError(
                "stale-policy", "manual grant does not match the current policy"
            )
        if document.get("binding_digest") != binding_digest:
            raise ManualPermissionError(
                "binding-mismatch", "manual grant does not match the current disclosure"
            )

    def _require_ready(self) -> None:
        if not self._ready:
            detail = f": {self._reset_error}" if self._reset_error is not None else ""
            raise ManualPermissionError(
                "reset-failed", f"manual permissions were not reset at startup{detail}"
            )

    def _load_action(self, action: Action) -> dict[str, object]:
        try:
            document = json.loads(self._action_path(action).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ManualPermissionError(
                "missing", "manual permission state is missing; restart the server"
            ) from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManualPermissionError(
                "malformed", "manual permission state is malformed"
            ) from exc
        if not isinstance(document, dict) or set(document) != {
            "schema_version",
            "action",
            "state",
            "server_run_id",
            "grant_id",
            "board_id",
            "policy_digest",
            "binding_digest",
            "claim_id",
            "created_at",
        }:
            raise ManualPermissionError(
                "malformed", "manual permission state has an invalid schema"
            )
        return document

    def _epoch_document(self) -> dict[str, object]:
        return {"schema_version": 1, "server_run_id": self.server_run_id, "created_at": self._now()}

    def _locked_document(self, action: Action) -> dict[str, object]:
        return self._document(
            action=action,
            state="locked",
            grant_id=None,
            board_id=None,
            policy_digest=None,
            binding_digest=None,
            claim_id=None,
        )

    def _document(
        self,
        *,
        action: Action,
        state: str,
        grant_id: str | None,
        board_id: str | None,
        policy_digest: str | None,
        binding_digest: str | None,
        claim_id: str | None,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "action": action,
            "state": state,
            "server_run_id": self.server_run_id,
            "grant_id": grant_id,
            "board_id": board_id,
            "policy_digest": policy_digest,
            "binding_digest": binding_digest,
            "claim_id": claim_id,
            "created_at": self._now(),
        }

    def _action_path(self, action: Action) -> Path:
        return self.root / f"{action}.json"

    @property
    def _protocol_lock_path(self) -> Path:
        return self.root / ".protocol.lock"

    @contextmanager
    def _protocol_lock(self):
        """Hold the S0 cross-process permission protocol lock for at most five seconds.

        The thread ``RLock`` prevents local re-entry races; this byte-range/
        advisory lock is the authoritative serialization point for the helper,
        this server process, and a concurrent server process.  The OS releases
        it when a process crashes.
        """

        self.root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._protocol_lock_path, os.O_CREAT | os.O_RDWR)
        deadline = time.monotonic() + self._protocol_timeout_seconds
        acquired = False
        try:
            while not acquired:
                try:
                    if os.name == "nt":
                        import msvcrt

                        if os.fstat(descriptor).st_size == 0:
                            os.write(descriptor, b"\0")
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ManualPermissionError(
                            "already-reserved",
                            "manual permission protocol is busy; retry after the active claim completes",
                        ) from exc
                    time.sleep(0.05)
            yield
        finally:
            if acquired:
                try:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    # Closing still releases an OS lock after a partial error.
                    pass
            os.close(descriptor)

    def _acquire_claim(self, action: Action) -> tuple[Path, dict[str, object]]:
        path = self.root / f"{action}.claim"
        claim = self._claim_document(action, owner="server")
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ManualPermissionError(
                "already-reserved", "manual permission is being changed by another process"
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(claim, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return path, claim

    def _claim_document(
        self, action: Action, *, owner: Literal["helper", "server"]
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "owner": owner,
            "claim_id": secrets.token_hex(16),
            "server_run_id": self.server_run_id,
            "action": action,
            "created_at": self._now(),
        }

    @staticmethod
    def _load_claim(path: Path, *, missing_is_none: bool = False) -> dict[str, object] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if missing_is_none:
                return None
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or set(value) != _CLAIM_KEYS:
            return None
        if value.get("schema_version") != 1:
            return None
        if value.get("owner") not in {"helper", "server"}:
            return None
        if (
            not isinstance(value.get("claim_id"), str)
            or _CLAIM_ID.fullmatch(value["claim_id"]) is None
        ):
            return None
        if not isinstance(value.get("server_run_id"), str) or not value["server_run_id"]:
            return None
        if value.get("action") not in _ACTIONS:
            return None
        if not isinstance(value.get("created_at"), str) or not value["created_at"]:
            return None
        return value

    @staticmethod
    def _unlink_claim_if_matches(path: Path, expected: dict[str, object]) -> None:
        """Delete only the unchanged claim owned by this cleanup path.

        The claim id is cryptographically unique, so matching owner/id/epoch/
        action prevents an old helper or server cleanup from deleting a claim
        that replaced it after a restart race.
        """

        current = ManualPermissionRepository._load_claim(path, missing_is_none=True)
        if current is None:
            return
        fields = ("owner", "claim_id", "server_run_id", "action")
        if all(current[field] == expected.get(field) for field in fields):
            try:
                path.unlink()
            except FileNotFoundError:
                return

    @staticmethod
    def _field(document: dict[str, object], name: str, expected: type[str]) -> str:
        value = document.get(name)
        if not isinstance(value, expected):
            raise ManualPermissionError("malformed", f"manual permission field {name} is invalid")
        return value

    @staticmethod
    def _optional_text(document: dict[str, object], name: str) -> str | None:
        value = document.get(name)
        return value if isinstance(value, str) else None

    @staticmethod
    def _atomic_write(path: Path, document: dict[str, object]) -> None:
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
