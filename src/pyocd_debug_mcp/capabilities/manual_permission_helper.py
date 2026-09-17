"""Packaged implementation of the manual-only grant unlock protocol.

The workspace guidance is compiled into an installed runtime, so its manual
permission transition must live beside the compiled MCP sidecar rather than in
the developer-only workspace source tree.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path

from pyocd_debug_mcp.capabilities.manual_permissions import (
    ManualPermissionError,
    ManualPermissionRepository,
)

_ACTIONS = frozenset(("downgrade", "mass-erase"))
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EPOCH_FIELDS = frozenset(("schema_version", "server_run_id", "created_at"))
_RECORD_FIELDS = frozenset(
    (
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
    )
)


def unlock_manual_grant(
    *,
    project_root: Path,
    action: str,
    board_id: str,
    policy_digest: str,
    binding_digest: str | None = None,
) -> dict[str, object]:
    """Unlock one locked current-epoch manual record without issuing a token."""

    root = Path(project_root).expanduser().resolve()
    if action not in _ACTIONS:
        return _refusal(
            root, action, "manual/wrong-action", "The requested manual action is not supported."
        )
    if not isinstance(board_id, str) or not board_id.strip() or not _is_digest(policy_digest):
        return _refusal(
            root,
            action,
            "manual/malformed",
            "Board and policy binding must come from the current server response.",
        )
    if action == "mass-erase" and not _is_digest(binding_digest):
        return _refusal(
            root,
            action,
            "manual/binding-mismatch",
            "Mass erase requires the current server disclosure binding digest.",
        )
    if action == "downgrade" and binding_digest is not None:
        return _refusal(
            root,
            action,
            "manual/binding-mismatch",
            "Downgrade grants do not accept a recovery disclosure binding.",
        )

    permissions_root = root / ".agent-workspace" / "runtime" / "manual-permissions"
    epoch_path = permissions_root / "epoch.json"
    action_path = permissions_root / f"{action}.json"
    if not root.is_dir() or not epoch_path.is_file() or not action_path.is_file():
        return _refusal(
            root,
            action,
            "manual/missing",
            "The current server manual-permission records are unavailable.",
        )

    try:
        observed_server_run_id = _current_epoch(epoch_path)
    except (OSError, ValueError, json.JSONDecodeError):
        observed_server_run_id = None
    if observed_server_run_id is None:
        return _refusal(
            root, action, "manual/malformed", "The current server epoch record is malformed."
        )

    repository = ManualPermissionRepository(root, observed_server_run_id)
    claim_path = permissions_root / f"{action}.claim"
    claim = repository._claim_document(action, owner="helper")
    try:
        with repository._protocol_lock():
            try:
                descriptor = _create_claim(claim_path, claim)
            except FileExistsError:
                return _refusal(
                    root,
                    action,
                    "manual/already-reserved",
                    "This manual grant is being used or has already been reserved.",
                )
            try:
                return _unlock_locked_record(
                    repository=repository,
                    root=root,
                    action=action,
                    board_id=board_id,
                    policy_digest=policy_digest,
                    binding_digest=binding_digest,
                    epoch_path=epoch_path,
                    action_path=action_path,
                    observed_server_run_id=observed_server_run_id,
                )
            finally:
                os.close(descriptor)
                ManualPermissionRepository._unlink_claim_if_matches(claim_path, claim)
    except ManualPermissionError as error:
        return _refusal(root, action, f"manual/{error.code}", str(error))
    except OSError:
        return _refusal(
            root,
            action,
            "manual/missing",
            "The current server manual-permission records are unavailable.",
        )


def _unlock_locked_record(
    *,
    repository: ManualPermissionRepository,
    root: Path,
    action: str,
    board_id: str,
    policy_digest: str,
    binding_digest: str | None,
    epoch_path: Path,
    action_path: Path,
    observed_server_run_id: str,
) -> dict[str, object]:
    try:
        server_run_id = _current_epoch(epoch_path)
        record = repository._load_action(action)  # type: ignore[arg-type]
    except (ManualPermissionError, OSError, ValueError, json.JSONDecodeError):
        return _refusal(
            root,
            action,
            "manual/malformed",
            "The current server manual-permission records are malformed.",
        )
    if server_run_id is None:
        return _refusal(
            root, action, "manual/malformed", "The current server epoch record is malformed."
        )
    if server_run_id != observed_server_run_id:
        return _refusal(
            root,
            action,
            "manual/stale-epoch",
            "The server restarted while this manual grant was being unlocked.",
        )

    state = _valid_action_state(record, action, observed_server_run_id)
    if state is None:
        return _refusal(
            root, action, "manual/malformed", "The current server action record is malformed."
        )
    if state == "consumed":
        return _refusal(
            root, action, "manual/already-consumed", "This manual grant was already consumed."
        )
    if state in {"unlocked", "reserved"}:
        return _refusal(
            root,
            action,
            "manual/already-reserved",
            "This manual grant is already unlocked or reserved.",
        )
    if state == "invalidated":
        return _refusal(
            root,
            action,
            "manual/stale-epoch",
            "This manual grant was invalidated by a server restart.",
        )

    if _current_epoch(epoch_path) != observed_server_run_id:
        return _refusal(
            root,
            action,
            "manual/stale-epoch",
            "The server restarted while this manual grant was being unlocked.",
        )
    grant_id = f"grant-{secrets.token_urlsafe(24)}"
    try:
        repository._atomic_write(
            action_path,
            repository._document(
                action=action,  # type: ignore[arg-type]
                state="unlocked",
                grant_id=grant_id,
                board_id=board_id,
                policy_digest=policy_digest,
                binding_digest=binding_digest,
                claim_id=None,
            ),
        )
    except OSError:
        return _refusal(
            root, action, "manual/missing", "The current server action record could not be updated."
        )
    return {
        "schema_version": 1,
        "status": "manual_grant_unlocked",
        "action": action,
        "board_id": board_id,
        "grant_id": grant_id,
        "server_run_id": observed_server_run_id,
        "policy_digest": policy_digest,
        "binding_digest": binding_digest,
        "project_root": str(root),
    }


def _create_claim(path: Path, claim: dict[str, object]) -> int:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        payload = (json.dumps(claim, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        ManualPermissionRepository._unlink_claim_if_matches(path, claim)
        raise
    return descriptor


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("unable to write manual-permission claim")
        offset += written


def _current_epoch(path: Path) -> str | None:
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or set(document) != _EPOCH_FIELDS
        or document.get("schema_version") != 1
        or not isinstance(document.get("server_run_id"), str)
        or not document["server_run_id"]
        or not isinstance(document.get("created_at"), str)
        or not document["created_at"]
    ):
        return None
    return str(document["server_run_id"])


def _valid_action_state(record: dict[str, object], action: str, server_run_id: str) -> str | None:
    state = record.get("state")
    if (
        set(record) != _RECORD_FIELDS
        or record.get("schema_version") != 1
        or record.get("action") != action
        or record.get("server_run_id") != server_run_id
        or state not in {"locked", "unlocked", "reserved", "consumed", "invalidated"}
        or not isinstance(record.get("created_at"), str)
        or not record["created_at"]
    ):
        return None
    if state == "locked" and any(
        record[field] is not None
        for field in ("grant_id", "board_id", "policy_digest", "binding_digest", "claim_id")
    ):
        return None
    return str(state)


def _is_digest(value: str | None) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _refusal(project_root: Path, action: str, code: str, message: str) -> dict[str, object]:
    skill = "$downgrade" if action == "downgrade" else "$mass-erase"
    return {
        "schema_version": 1,
        "status": "refused",
        "code": code,
        "action": action,
        "project_root": str(project_root),
        "message": message,
        "remedies": [f"Ask the human to manually invoke {skill} in this project."],
    }
