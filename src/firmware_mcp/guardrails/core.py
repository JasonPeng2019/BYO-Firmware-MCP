"""Lock-protected, run-scoped cooperative-user permission and plan records.

Lock order for server-originated guarded work is board connection lock, this
in-process guard lock, then the project permission-store file lock.  The guard
and file locks are released before a wrapped hardware handler is called.
"""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import tempfile
from copy import deepcopy
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from contextvars import ContextVar
from typing import Literal, cast

from filelock import FileLock

from firmware_mcp.safety.linker import LinkerEvidenceError, parse_flash_image_bytes

RiskClass = Literal["routine", "destructive"]
LifecycleClass = Literal["inventory", "profile", "connected", "connected-and-safety"]


class GuardError(RuntimeError):
    """Stable guard rejection used by the public handler wrapper."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True, slots=True)
class ActionSpec:
    name: str
    risk: RiskClass
    lifecycle: LifecycleClass
    arguments: tuple[str, ...]
    artifact_bound: bool = False
    serial_bound: bool = False
    file_bindings: tuple[str, ...] = ()
    # Registry-owned policy/evidence seam.  GuardCore never switches on tool
    # names; it only compares reproducible canonical facts supplied here.
    classify: (
        Callable[[str, Mapping[str, object], Mapping[str, bytes] | None], dict[str, object]] | None
    ) = None


# The core owns hashing, comparison, and immutable execution snapshots.  A
# registry-provided resolver may name additional files referenced *inside* a
# selected structured artifact (for example, explicit layout evidence).  It
# receives execution snapshots when available, so it never needs to reopen the
# mutable structured artifact while an action is executing.
FileBindingResolver = Callable[
    [ActionSpec, Mapping[str, object], Mapping[str, bytes] | None], Mapping[str, Path]
]


@dataclass(slots=True)
class HardwareRequest:
    request_id: str
    run_id: str
    board_id: str
    scope: str
    requested_call_budget: int | None
    plan_id: str | None
    binding: dict[str, object]
    disclosure: dict[str, object] | None = None


@dataclass(slots=True)
class HardwareGrant:
    grant_id: str
    request_id: str
    run_id: str
    board_id: str
    binding: dict[str, object]
    initial_calls: int
    remaining_calls: int
    active: bool = True
    close_reason: str | None = None
    disclosure: dict[str, object] | None = None


@dataclass(slots=True)
class PlanAction:
    tool: str
    arguments: dict[str, object]
    max_calls: int
    remaining_calls: int
    serial_identity: dict[str, str] | None = None


@dataclass(slots=True)
class HardwarePlan:
    plan_id: str
    board_id: str
    objective: str
    expected_result: str
    actions: list[PlanAction]
    grant_id: str | None
    binding: dict[str, object]
    status: str
    close_reason: str | None = None
    attempts: list[dict[str, object]] = field(default_factory=list)
    disclosure: dict[str, object] | None = None


def _id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


def _positive(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GuardError(
            "guard/budget-invalid",
            "A user-selected call budget must be a positive integer (not true/false).",
        )
    return value


def _canonical(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise GuardError(
                "guard/arguments-invalid", "Action argument object keys must be strings."
            )
        return {key: _canonical(item) for key, item in value.items()}
    raise GuardError(
        "guard/arguments-invalid",
        f"Action arguments must be JSON values; got {type(value).__name__}.",
    )


def _disclosure_digest(disclosure: Mapping[str, object]) -> str:
    """Stable receipt binding for the exact JSON disclosure shown to the user."""

    encoded = json.dumps(_canonical(disclosure), sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


class GuardCore:
    """One server run's requests, grants, plans, and atomic attempt starts."""

    def __init__(
        self,
        *,
        project_root: Path,
        run_id: str,
        action_specs: Mapping[str, ActionSpec],
        evidence_for: Callable[[str], dict[str, object]],
        on_attempt: Callable[[str, dict[str, object]], None] | None = None,
        serial_identity_for: Callable[[str, str | None], dict[str, str]] | None = None,
        safety_binding_for: Callable[[str], dict[str, object]] | None = None,
        file_binding_resolver: FileBindingResolver | None = None,
    ) -> None:
        self.project_root, self.run_id = project_root, run_id
        self.action_specs = dict(action_specs)
        self._evidence_for, self._on_attempt = evidence_for, on_attempt
        self._serial_identity_for = serial_identity_for
        self._safety_binding_for = safety_binding_for
        self._file_binding_resolver = file_binding_resolver
        self._execution_files: ContextVar[dict[str, bytes]] = ContextVar(
            "guard_execution_files", default={}
        )
        self._lock = RLock()
        self._grants: dict[str, HardwareGrant] = {}
        self._plans: dict[str, HardwarePlan] = {}
        self._path = project_root / ".firm" / "hardware-permissions.json"
        # filelock supplies the cross-platform OS file-lock semantics; no
        # timeout/retry/stale-owner policy is invented by the guard core.
        self._store_lock = FileLock(str(self._path.with_suffix(".lock")), timeout=-1)

    @property
    def request_path(self) -> Path:
        return self._path

    def _file_paths(
        self,
        spec: ActionSpec,
        canonical: Mapping[str, object],
        snapshots: Mapping[str, bytes] | None = None,
    ) -> dict[str, Path]:
        """Return every selected file as a named immutable-input candidate."""

        paths: dict[str, Path] = {}
        for argument in spec.file_bindings or (("firmware_path",) if spec.artifact_bound else ()):
            value = canonical.get(argument)
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise GuardError(
                        "guard/file-invalid", f"{argument} must be a non-empty path string."
                    )
                paths[argument] = Path(value)
        # ``None`` means planning (the resolver may inspect the selected
        # project file); an empty mapping is the first execution pass, before
        # declared files have been captured; a non-empty mapping is the second
        # pass and must be parsed exclusively from those captured bytes.
        if self._file_binding_resolver is not None and (snapshots is None or snapshots):
            try:
                discovered = self._file_binding_resolver(spec, canonical, snapshots)
            except GuardError:
                raise
            except Exception as exc:  # noqa: BLE001 - selected evidence parse failures are actionable
                raise GuardError(
                    "guard/file-invalid",
                    f"Selected file evidence cannot be resolved: {type(exc).__name__}: {exc}",
                ) from exc
            if not isinstance(discovered, Mapping):
                raise GuardError(
                    "guard/file-invalid", "File-binding resolver returned malformed evidence."
                )
            for name, path in discovered.items():
                if not isinstance(name, str) or not name or not isinstance(path, Path):
                    raise GuardError(
                        "guard/file-invalid", "File-binding resolver returned malformed evidence."
                    )
                if name in paths and paths[name] != path:
                    raise GuardError(
                        "guard/file-invalid", f"File evidence name {name!r} is ambiguous."
                    )
                paths[name] = path
        return paths

    def _read(self) -> dict[str, object]:
        try:
            if not self._path.is_file():
                return {"version": 1, "requests": {}, "receipts": {}}
            result = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GuardError(
                "guard/permission-store-invalid", f"Permission store is malformed: {exc}"
            ) from exc
        except OSError as exc:
            raise self._store_unavailable(exc) from exc
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("requests"), dict)
            or not isinstance(result.get("receipts"), dict)
        ):
            raise GuardError(
                "guard/permission-store-invalid", "Permission store has an invalid record shape."
            )
        return result

    @staticmethod
    def _store_unavailable(error: BaseException) -> GuardError:
        """Describe an I/O failure without claiming its transaction committed."""

        return GuardError(
            "guard/permission-store-unavailable",
            "Permission persistence is unavailable or the transaction outcome is uncertain "
            f"({type(error).__name__}: {error}). Restore project .firm access, then inspect "
            "permission status/request again before retrying.",
        )

    def _write(self, record: dict[str, object]) -> None:
        temporary: str | None = None
        failure: BaseException | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix="hardware-permissions-", suffix=".tmp", dir=self._path.parent
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                output.write(json.dumps(record, sort_keys=True, indent=2) + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
        except OSError as exc:
            failure = exc
        finally:
            if temporary is not None:
                try:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                except OSError as exc:
                    if failure is None:
                        failure = exc
        if failure is not None:
            raise self._store_unavailable(failure) from failure

    @contextmanager
    def _permission_transaction(self, *, write: bool) -> Iterator[dict[str, object]]:
        """Hold the complete cross-process read/check/mutate/publication transaction."""

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise self._store_unavailable(exc) from exc
        with self._lock:
            acquired = False
            primary_error: BaseException | None = None
            release_failure: BaseException | None = None
            record: dict[str, object] | None = None
            try:
                try:
                    self._store_lock.acquire()
                    acquired = True
                except Exception as exc:
                    primary_error = self._store_unavailable(exc)
                if primary_error is None:
                    try:
                        record = self._read()
                    except BaseException as exc:
                        primary_error = exc
                if primary_error is None:
                    assert record is not None
                    original = deepcopy(record)
                    try:
                        yield record
                    except BaseException as exc:
                        primary_error = exc
                    # Some guard errors intentionally annotate a stale request
                    # before they are reported. Publish only normal or explicit
                    # guard-state mutations, never an arbitrary handler failure.
                    if (
                        write
                        and record != original
                        and (primary_error is None or isinstance(primary_error, GuardError))
                    ):
                        try:
                            self._write(record)
                        except BaseException as exc:
                            primary_error = exc
            finally:
                if acquired:
                    try:
                        self._store_lock.release()
                    except Exception as exc:
                        release_failure = exc
            if release_failure is not None:
                raise self._store_unavailable(release_failure) from release_failure
            if primary_error is not None:
                raise primary_error

    def request_permission(
        self,
        *,
        board_id: str,
        scope: str,
        requested_call_budget: int | None,
        plan_id: str | None,
    ) -> HardwareRequest:
        if scope not in {"routine-session", "destructive-once"}:
            raise GuardError(
                "guard/scope-invalid", "scope must be routine-session or destructive-once."
            )
        if requested_call_budget is not None:
            _positive(requested_call_budget)
        if scope == "routine-session" and plan_id is not None:
            raise GuardError(
                "guard/plan-shape", "routine-session permission requests require plan_id=null."
            )
        if scope == "destructive-once":
            if requested_call_budget not in {None, 1}:
                raise GuardError(
                    "guard/destructive-budget",
                    "destructive-once has exactly one attempt; requested_call_budget must be null or 1.",
                )
            with self._lock:
                plan = self._plans.get(plan_id or "")
                if (
                    plan is None
                    or plan.board_id != board_id
                    or plan.status != "disclosure-required"
                    or plan.disclosure is None
                ):
                    raise GuardError(
                        "guard/destructive-plan",
                        "destructive-once requires this board's disclosure-required destructive plan.",
                    )
                current_binding, current_digests, disclosure = self._reproduce_destructive(plan)
                plan_binding = {
                    key: value for key, value in plan.binding.items() if key != "artifact_digests"
                }
                if (
                    plan_binding != current_binding
                    or plan.binding.get("artifact_digests") != current_digests
                    or plan.disclosure != disclosure
                ):
                    plan.status, plan.close_reason = "invalidated", "destructive-disclosure-changed"
                    raise GuardError(
                        "guard/disclosure-stale",
                        "Destructive physical effects changed; create a new plan and exact permission request.",
                    )
                request = HardwareRequest(
                    _id("permission"),
                    self.run_id,
                    board_id,
                    scope,
                    1,
                    plan_id,
                    dict(plan.binding),
                    deepcopy(plan.disclosure),
                )
            with self._permission_transaction(write=True) as data:
                requests = data["requests"]
                assert isinstance(requests, dict)
                requests[request.request_id] = {"schema_version": 1, **asdict(request)}
            return request
        request = HardwareRequest(
            _id("permission"),
            self.run_id,
            board_id,
            scope,
            requested_call_budget,
            plan_id,
            self._evidence_for(board_id),
        )
        with self._permission_transaction(write=True) as data:
            requests = data["requests"]
            assert isinstance(requests, dict)
            requests[request.request_id] = {"schema_version": 1, **asdict(request)}
        return request

    def approve_request(
        self, request_id: str, *, approved: bool, call_budget: object
    ) -> dict[str, object]:
        """Record one direct-user receipt; used by elicitation and the local CLI."""
        receipt_result: dict[str, object]
        with self._permission_transaction(write=True) as data:
            requests, receipts = data["requests"], data["receipts"]
            assert isinstance(requests, dict) and isinstance(receipts, dict)
            if not isinstance(requests.get(request_id), dict):
                raise GuardError(
                    "guard/request-not-found", f"Unknown permission request '{request_id}'."
                )
            request = cast(dict[str, object], requests[request_id])
            if request.get("schema_version") != 1:
                raise GuardError(
                    "guard/permission-store-invalid",
                    "Permission request has an unsupported schema version.",
                )
            if request.get("invalidated_reason") is not None:
                raise GuardError(
                    "guard/request-stale",
                    f"Permission request was invalidated: {request['invalidated_reason']}.",
                )
            if request_id in receipts:
                raise GuardError(
                    "guard/receipt-exists", "This immutable request already has a receipt."
                )
            destructive = request.get("scope") == "destructive-once"
            if destructive and call_budget not in {None, 1}:
                raise GuardError(
                    "guard/destructive-budget",
                    "A destructive approval has exactly one call and no user-selected budget.",
                )
            disclosure = request.get("disclosure")
            if destructive and not isinstance(disclosure, dict):
                raise GuardError(
                    "guard/permission-store-invalid",
                    "Destructive permission request has no exact disclosure.",
                )
            receipts[request_id] = {
                "schema_version": 1,
                "request_id": request_id,
                "approved": bool(approved),
                "call_budget": 1
                if approved and destructive
                else (_positive(call_budget) if approved else None),
                "consumed_by": None,
                "scope": request.get("scope"),
                "plan_id": request.get("plan_id"),
                "disclosure": disclosure,
                "disclosure_sha256": _disclosure_digest(disclosure)
                if isinstance(disclosure, dict)
                else None,
            }
            receipt_result = dict(receipts[request_id])
        return receipt_result

    def permission_status(self, request_id: str) -> dict[str, object]:
        with self._permission_transaction(write=False) as data:
            requests, receipts = data["requests"], data["receipts"]
            assert isinstance(requests, dict) and isinstance(receipts, dict)
            request = requests.get(request_id)
            if not isinstance(request, dict):
                raise GuardError(
                    "guard/request-not-found", f"Unknown permission request '{request_id}'."
                )
            receipt = receipts.get(request_id)
            return {
                "request": dict(request),
                "receipt": dict(receipt) if isinstance(receipt, dict) else None,
            }

    def request_board_id(self, request_id: str) -> str:
        """Return immutable request routing data before taking that board's lock."""

        status = self.permission_status(request_id)
        request = status["request"]
        if not isinstance(request, dict):
            raise GuardError(
                "guard/permission-store-invalid", "Permission request has an invalid record shape."
            )
        board_id = request.get("board_id")
        if not isinstance(board_id, str) or not board_id:
            raise GuardError(
                "guard/permission-store-invalid", "Permission request has no board_id."
            )
        return board_id

    def get_permission(self, request_id: str) -> HardwareGrant:
        grant: HardwareGrant
        with self._permission_transaction(write=True) as data:
            requests, receipts = data["requests"], data["receipts"]
            assert isinstance(requests, dict) and isinstance(receipts, dict)
            request, receipt = requests.get(request_id), receipts.get(request_id)
            if not isinstance(request, dict):
                raise GuardError(
                    "guard/request-not-found", f"Unknown permission request '{request_id}'."
                )
            if request.get("schema_version") != 1:
                raise GuardError(
                    "guard/permission-store-invalid",
                    "Permission request has an unsupported schema version.",
                )
            if request.get("invalidated_reason") is not None:
                raise GuardError(
                    "guard/request-stale",
                    f"Permission request was invalidated: {request['invalidated_reason']}.",
                )
            if request.get("run_id") != self.run_id:
                raise GuardError(
                    "guard/request-stale", "Permission request belongs to a prior server run."
                )
            if not isinstance(receipt, dict):
                raise GuardError(
                    "guard/permission-pending",
                    "User approval is pending; relay elicitation or the approve-hardware command, then call get_hardware_permission.",
                )
            if not receipt.get("approved"):
                raise GuardError(
                    "guard/permission-declined",
                    "The user declined this hardware permission request.",
                )
            if receipt.get("consumed_by") is not None:
                raise GuardError(
                    "guard/receipt-consumed",
                    "This approval receipt was already consumed into a grant.",
                )
            if receipt.get("invalidated_reason") is not None:
                raise GuardError(
                    "guard/request-stale",
                    f"Permission receipt was invalidated: {receipt['invalidated_reason']}.",
                )
            board_id, binding = request.get("board_id"), request.get("binding")
            if not isinstance(board_id, str) or not isinstance(binding, dict):
                raise GuardError(
                    "guard/permission-store-invalid", "Permission request is malformed."
                )
            destructive = request.get("scope") == "destructive-once"
            disclosure = request.get("disclosure")
            if destructive:
                if receipt.get("schema_version") != 1:
                    raise GuardError(
                        "guard/permission-store-invalid",
                        "Destructive approval receipt has an unsupported schema version.",
                    )
                plan_id = request.get("plan_id")
                with self._lock:
                    plan = self._plans.get(plan_id if isinstance(plan_id, str) else "")
                    if (
                        plan is None
                        or plan.status != "disclosure-required"
                        or plan.board_id != board_id
                        or plan.binding != binding
                        or plan.disclosure != disclosure
                    ):
                        request["invalidated_reason"] = "destructive-plan-or-disclosure-changed"
                        raise GuardError(
                            "guard/binding-stale",
                            "Destructive plan or disclosure changed; create a new exact permission request.",
                        )
                    current_binding, current_digests, current_disclosure = (
                        self._reproduce_destructive(plan)
                    )
                plan_binding = {
                    key: value for key, value in binding.items() if key != "artifact_digests"
                }
                if (
                    current_binding != plan_binding
                    or binding.get("artifact_digests") != current_digests
                    or disclosure != current_disclosure
                ):
                    request["invalidated_reason"] = "destructive-disclosure-changed"
                    raise GuardError(
                        "guard/binding-stale",
                        "Destructive effects changed; create a new plan and permission request.",
                    )
                if (
                    receipt.get("scope") != "destructive-once"
                    or receipt.get("plan_id") != plan_id
                    or receipt.get("disclosure") != disclosure
                    or receipt.get("disclosure_sha256")
                    != _disclosure_digest(cast(dict[str, object], disclosure))
                    or receipt.get("call_budget") != 1
                ):
                    raise GuardError(
                        "guard/permission-store-invalid",
                        "Destructive approval receipt does not exactly match its request disclosure.",
                    )
                budget = 1
            else:
                if binding != self._evidence_for(board_id):
                    request["invalidated_reason"] = "binding-changed"
                    raise GuardError(
                        "guard/binding-stale",
                        "Board evidence changed; request a new permission grant.",
                    )
                budget = _positive(receipt.get("call_budget"))
            grant = HardwareGrant(
                _id("grant"),
                request_id,
                self.run_id,
                board_id,
                dict(binding),
                budget,
                budget,
                disclosure=deepcopy(disclosure) if isinstance(disclosure, dict) else None,
            )
            receipt["consumed_by"] = grant.grant_id
        # The grant becomes authority only after the consumed receipt has been
        # atomically published and the cross-process lock has been released.
        with self._lock:
            self._grants[grant.grant_id] = grant
            if destructive:
                assert isinstance(request.get("plan_id"), str)
                plan = self._plans[request["plan_id"]]
                plan.grant_id = grant.grant_id
                plan.status = "active"
        return grant

    def grant_record(self, grant: HardwareGrant) -> dict[str, object]:
        return {
            "grant_id": grant.grant_id,
            "board_id": grant.board_id,
            "binding": dict(grant.binding),
            "initial_calls": grant.initial_calls,
            "remaining_calls": grant.remaining_calls,
            "risk": "destructive" if grant.disclosure is not None else "routine",
            "disclosure": deepcopy(grant.disclosure),
            "invalidation_rules": (
                "disconnect, replacement, profile, assignment, session, identity, "
                "or explicit revocation"
            ),
            "next_call": (
                "create_hardware_plan(grant_id=<grant_id>, board_id=<board_id>, "
                "objective=<text>, expected_result=<text>, actions=[...])"
            ),
        }

    def revoke(self, grant_id: str) -> dict[str, object]:
        with self._lock:
            grant = self._grants.get(grant_id)
            invalidated: list[str] = []
            if grant is not None and grant.active:
                grant.active, grant.close_reason = False, "revoked"
                for plan in self._plans.values():
                    if plan.grant_id == grant_id and plan.status == "active":
                        plan.status, plan.close_reason = "invalidated", "grant-revoked"
                        invalidated.append(plan.plan_id)
            return {
                "grant_id": grant_id,
                "existed": grant is not None,
                "invalidated_plan_ids": invalidated,
            }

    def _action(self, board_id: str, raw: object) -> PlanAction:
        if not isinstance(raw, Mapping) or set(raw) != {"tool", "arguments", "max_calls"}:
            raise GuardError(
                "guard/action-shape",
                "Each action must contain exactly tool, arguments, and max_calls.",
            )
        tool, arguments, calls = raw["tool"], raw["arguments"], raw["max_calls"]
        if not isinstance(tool, str) or tool not in self.action_specs:
            raise GuardError("guard/action-tool", "Plan action names an unknown or direct tool.")
        if not isinstance(arguments, Mapping) or set(arguments) != set(
            self.action_specs[tool].arguments
        ):
            raise GuardError(
                "guard/action-arguments",
                f"{tool} arguments must exactly match its public schema excluding plan_id.",
            )
        canonical = _canonical(arguments)
        assert isinstance(canonical, dict)
        if canonical.get("board_id") != board_id:
            raise GuardError(
                "guard/action-board", "Every action board_id must equal the plan board_id."
            )
        if isinstance(calls, bool) or not isinstance(calls, int) or calls <= 0:
            raise GuardError(
                "guard/action-budget", "max_calls must be a positive integer, not true/false."
            )
        return PlanAction(tool, canonical, calls, calls)

    def _file_binding(
        self, actions: list[PlanAction]
    ) -> tuple[dict[str, str], list[dict[str, bytes]]]:
        """Capture selected files once for both planning facts and digest binding."""
        digests: dict[str, str] = {}
        captured: list[dict[str, bytes]] = []
        for action in actions:
            action_bytes: dict[str, bytes] = {}
            spec = self.action_specs[action.tool]
            for name, path in self._file_paths(spec, action.arguments).items():
                try:
                    payload = path.read_bytes()
                    if name == "firmware_path":
                        parse_flash_image_bytes(path, payload)
                except (LinkerEvidenceError, OSError) as exc:
                    raise GuardError(
                        "guard/file-binding-invalid", f"Cannot bind planned {name} bytes: {exc}"
                    ) from exc
                digests[f"{action.tool}:{name}"] = hashlib.sha256(payload).hexdigest()
                action_bytes[name] = payload
            captured.append(action_bytes)
        return digests, captured

    def execution_file(self, argument_name: str) -> bytes | None:
        """Return the exact checked execution snapshot for a bound file."""
        return self._execution_files.get().get(argument_name)

    def clear_execution_files(self) -> None:
        self._execution_files.set({})

    def _bind_serial_actions(self, board_id: str, actions: list[PlanAction]) -> None:
        """Snapshot one actually enumerated physical UART per canonical action."""

        for action in actions:
            if not self.action_specs[action.tool].serial_bound:
                continue
            if self._serial_identity_for is None:
                raise GuardError(
                    "guard/serial-identity-unavailable",
                    "Live serial identity is unavailable; re-detect the UART and create a new plan.",
                )
            port = action.arguments.get("port")
            if port is not None and not isinstance(port, str):
                raise GuardError(
                    "guard/serial-identity-unavailable", "Planned serial port is invalid."
                )
            observed = self._serial_identity_for(board_id, cast(str | None, port))
            if not all(
                isinstance(observed.get(key), str) and observed[key]
                for key in ("port", "kind", "value")
            ):
                raise GuardError(
                    "guard/serial-identity-unavailable",
                    "The resolved UART has no stable observed identity; re-detect it and replan.",
                )
            action.serial_identity = dict(observed)

    def _classification(
        self, board_id: str, action: PlanAction, snapshots: Mapping[str, bytes] | None = None
    ) -> dict[str, object]:
        spec = self.action_specs[action.tool]
        result = (
            spec.classify(board_id, action.arguments, snapshots)
            if spec.classify is not None
            else {"risk": spec.risk, "effects": {}}
        )
        if not isinstance(result, dict) or result.get("risk") not in {"routine", "destructive"}:
            raise GuardError(
                "guard/classification-invalid",
                "Current action safety classification is unavailable or malformed.",
            )
        return cast(dict[str, object], _canonical(result))

    def _current_binding(self, board_id: str, actions: list[PlanAction]) -> dict[str, object]:
        binding = self._evidence_for(board_id)
        if (
            any(
                self.action_specs[action.tool].lifecycle == "connected-and-safety"
                for action in actions
            )
            and self._safety_binding_for is not None
        ):
            binding = {**binding, **self._safety_binding_for(board_id)}
        return binding

    def _reproduce_destructive(
        self, plan: HardwarePlan
    ) -> tuple[dict[str, object], dict[str, str], dict[str, object]]:
        """Rebuild every reproducible fact before request or grant activation."""

        if len(plan.actions) != 1:
            raise GuardError(
                "guard/destructive-plan", "Destructive plans must contain exactly one action."
            )
        digests, snapshots = self._file_binding(plan.actions)
        binding = self._current_binding(plan.board_id, plan.actions)
        classification = self._classification(plan.board_id, plan.actions[0], snapshots[0])
        return (
            binding,
            digests,
            self._disclosure(plan.board_id, plan.actions[0], plan.binding, classification),
        )

    def _disclosure(
        self,
        board_id: str,
        action: PlanAction,
        binding: dict[str, object],
        classification: dict[str, object],
    ) -> dict[str, object]:
        if classification["risk"] != "destructive":
            raise GuardError(
                "guard/disclosure-invalid", "Routine actions have no destructive disclosure."
            )
        return cast(
            dict[str, object],
            _canonical(
                {
                    "schema_version": 1,
                    "tool": action.tool,
                    "arguments": action.arguments,
                    "board_id": board_id,
                    "run_id": self.run_id,
                    "binding": binding,
                    "risk": "destructive",
                    "scope": "destructive-once",
                    "effects": classification.get("effects", {}),
                }
            ),
        )

    def create_plan(
        self,
        *,
        grant_id: str | None,
        board_id: str,
        objective: str,
        expected_result: str,
        actions: object,
    ) -> HardwarePlan:
        if (
            not isinstance(objective, str)
            or not objective.strip()
            or not isinstance(expected_result, str)
            or not expected_result.strip()
        ):
            raise GuardError(
                "guard/plan-explanation", "objective and expected_result must be non-empty text."
            )
        if not isinstance(actions, list) or not actions:
            raise GuardError("guard/plan-actions", "actions must be a non-empty list.")
        validated = [self._action(board_id, item) for item in actions]
        with self._lock:
            artifact_digests, planning_snapshots = self._file_binding(validated)
            self._bind_serial_actions(board_id, validated)
            binding = self._current_binding(board_id, validated)
            if artifact_digests:
                binding = {**binding, "artifact_digests": artifact_digests}
            classifications = [
                self._classification(board_id, item, planning_snapshots[index])
                for index, item in enumerate(validated)
            ]
            destructive = any(item["risk"] == "destructive" for item in classifications)
            if destructive:
                if len(validated) != 1 or validated[0].max_calls != 1 or grant_id is not None:
                    raise GuardError(
                        "guard/destructive-plan",
                        "A destructive plan has one action, max_calls=1, and grant_id=null.",
                    )
                status = "disclosure-required"
            else:
                grant = self._grants.get(grant_id or "")
                if (
                    grant is None
                    or not grant.active
                    or grant.board_id != board_id
                    or grant.binding != self._evidence_for(board_id)
                ):
                    raise GuardError(
                        "guard/grant-inactive",
                        "Routine plans require one exact active current-board grant_id.",
                    )
                if sum(item.max_calls for item in validated) > grant.remaining_calls:
                    raise GuardError(
                        "guard/budget-exceeded",
                        "Plan calls exceed this grant's remaining user-approved budget.",
                    )
                status = "active"
            disclosure = (
                self._disclosure(board_id, validated[0], binding, classifications[0])
                if destructive
                else None
            )
            plan = HardwarePlan(
                _id("plan"),
                board_id,
                objective,
                expected_result,
                validated,
                grant_id,
                binding,
                status,
                disclosure=disclosure,
            )
            self._plans[plan.plan_id] = plan
            return plan

    def execute(
        self, *, tool: str, plan_id: object, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        if not isinstance(plan_id, str) or not plan_id:
            raise GuardError(
                "guard/plan-required",
                "Call request_hardware_permission, then create_hardware_plan, and supply its plan_id.",
            )
        canonical = _canonical(arguments)
        assert isinstance(canonical, dict)
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise GuardError(
                    "guard/plan-not-found", "No current-run hardware plan has that plan_id."
                )
            spec = self.action_specs.get(tool)
            if spec is None:
                raise GuardError("guard/action-tool", "Unknown guarded tool.")
            if plan.status not in {"active", "disclosure-required"}:
                raise GuardError(
                    "guard/plan-inactive",
                    f"Plan is {plan.status}: {plan.close_reason or 'no further attempts'}.",
                )
            current_binding = self._evidence_for(plan.board_id)
            if spec.lifecycle == "connected-and-safety" and self._safety_binding_for is not None:
                current_binding = {**current_binding, **self._safety_binding_for(plan.board_id)}
            plan_binding = {
                key: value for key, value in plan.binding.items() if key != "artifact_digests"
            }
            if canonical.get("board_id") != plan.board_id or plan_binding != current_binding:
                plan.status, plan.close_reason = "invalidated", "binding-changed"
                grant = self._grants.get(plan.grant_id or "")
                if grant is not None:
                    grant.active, grant.close_reason = False, "binding-changed"
                raise GuardError(
                    "guard/binding-stale",
                    "Board profile, assignment, session, or identity changed; create a new plan.",
                )
            bound_digests = plan.binding.get("artifact_digests")
            snapshots: dict[str, bytes] = {}
            if isinstance(bound_digests, dict):
                # First capture declared public paths.  An opaque resolver can
                # then derive nested evidence from those immutable bytes.
                initial = self._file_paths(spec, canonical, {})
                for argument, path in initial.items():
                    key = f"{tool}:{argument}"
                    expected = bound_digests.get(key)
                    try:
                        payload = path.read_bytes()
                    except OSError as exc:
                        plan.status, plan.close_reason = "invalidated", "file-unavailable"
                        raise GuardError(
                            "guard/file-stale", f"Planned {argument} is unavailable: {exc}"
                        ) from exc
                    if expected != hashlib.sha256(payload).hexdigest():
                        plan.status, plan.close_reason = (
                            "invalidated",
                            "artifact-changed" if argument == "firmware_path" else "file-changed",
                        )
                        raise GuardError(
                            "guard/file-stale",
                            "Artifact bytes changed after plan creation; create a new plan from the current bound file.",
                        )
                    snapshots[argument] = payload
                # A second resolver pass must use the captured structured
                # artifact bytes, never reopen that artifact while executing.
                for argument, path in self._file_paths(spec, canonical, snapshots).items():
                    if argument in snapshots:
                        continue
                    key = f"{tool}:{argument}"
                    expected = bound_digests.get(key)
                    try:
                        payload = path.read_bytes()
                    except OSError as exc:
                        plan.status, plan.close_reason = "invalidated", "file-unavailable"
                        raise GuardError(
                            "guard/file-stale", f"Planned {argument} is unavailable: {exc}"
                        ) from exc
                    if expected != hashlib.sha256(payload).hexdigest():
                        plan.status, plan.close_reason = "invalidated", "file-changed"
                        raise GuardError(
                            "guard/file-stale",
                            "Selected evidence bytes changed after plan creation; create a new plan from the current bound file.",
                        )
                    snapshots[argument] = payload
            if plan.status == "disclosure-required":
                raise GuardError(
                    "guard/destructive-permission-required",
                    "Request and activate one exact destructive permission before this action.",
                )
            action = next(
                (
                    item
                    for item in plan.actions
                    if item.tool == tool and item.arguments == canonical
                ),
                None,
            )
            if action is None:
                raise GuardError(
                    "guard/action-mismatch",
                    "Call arguments do not exactly match an action in this plan.",
                )
            classification = self._classification(plan.board_id, action, snapshots)
            if classification["risk"] == "destructive":
                reproduced = self._disclosure(plan.board_id, action, plan.binding, classification)
                if plan.disclosure != reproduced:
                    plan.status, plan.close_reason = "invalidated", "destructive-disclosure-changed"
                    raise GuardError(
                        "guard/disclosure-stale",
                        "Destructive physical effects changed; create a new plan and exact permission request.",
                    )
            if self.action_specs[tool].serial_bound:
                if self._serial_identity_for is None or action.serial_identity is None:
                    plan.status, plan.close_reason = "invalidated", "serial-identity-unavailable"
                    raise GuardError(
                        "guard/serial-identity-unavailable",
                        "The planned UART identity is unavailable; re-detect the UART and create a new plan.",
                    )
                port = canonical.get("port")
                try:
                    observed_serial = self._serial_identity_for(
                        plan.board_id, cast(str | None, port)
                    )
                except GuardError:
                    plan.status, plan.close_reason = "invalidated", "serial-identity-unavailable"
                    raise
                if observed_serial != action.serial_identity:
                    plan.status, plan.close_reason = "invalidated", "serial-identity-changed"
                    raise GuardError(
                        "guard/serial-identity-stale",
                        "The resolved UART identity changed; re-detect the UART and create a new plan.",
                    )
            grant = self._grants.get(plan.grant_id or "")
            if (
                action.remaining_calls <= 0
                or grant is None
                or not grant.active
                or grant.remaining_calls <= 0
                or grant.binding != plan_binding
            ):
                plan.status, plan.close_reason = "invalidated", "action-or-grant-exhausted"
                raise GuardError(
                    "guard/grant-inactive",
                    "The exact plan action or bound grant is inactive or exhausted.",
                )
            if classification["risk"] == "destructive" and grant.disclosure != plan.disclosure:
                plan.status, plan.close_reason = (
                    "invalidated",
                    "destructive-grant-disclosure-changed",
                )
                raise GuardError(
                    "guard/disclosure-stale",
                    "The activated destructive grant does not match this plan disclosure.",
                )
            before_action, before_grant = action.remaining_calls, grant.remaining_calls
            action.remaining_calls -= 1
            grant.remaining_calls -= 1
            attempt = {
                "tool": tool,
                "board_id": plan.board_id,
                "plan_id": plan.plan_id,
                "grant_id": grant.grant_id,
                "action_remaining_before": before_action,
                "action_remaining_after": action.remaining_calls,
                "grant_remaining_before": before_grant,
                "grant_remaining_after": grant.remaining_calls,
                "risk": classification["risk"],
                "disclosure": deepcopy(plan.disclosure) if plan.disclosure is not None else None,
            }
            plan.attempts.append(attempt)
            if all(item.remaining_calls == 0 for item in plan.actions):
                plan.status, plan.close_reason = "exhausted", "all-actions-consumed"
            if grant.remaining_calls == 0:
                grant.active, grant.close_reason = False, "budget-exhausted"
            callback = self._on_attempt
            self._execution_files.set(snapshots)
        if callback is not None:
            callback(tool, dict(attempt))
        return attempt

    def invalidate_board(self, board_id: str, reason: str) -> None:
        try:
            with self._permission_transaction(write=True) as data:
                requests, receipts = data["requests"], data["receipts"]
                assert isinstance(requests, dict) and isinstance(receipts, dict)
                for request_id, request in requests.items():
                    if isinstance(request, dict) and request.get("board_id") == board_id:
                        request["invalidated_reason"] = reason
                        receipt = receipts.get(request_id)
                        if isinstance(receipt, dict):
                            receipt["invalidated_reason"] = reason
        except GuardError:
            # An unknown durable outcome must never leave local authority live.
            self._invalidate_runtime_board(board_id, reason)
            raise
        self._invalidate_runtime_board(board_id, reason)

    def _invalidate_runtime_board(self, board_id: str, reason: str) -> None:
        """Conservatively close only this process's transient authority."""

        with self._lock:
            for grant in self._grants.values():
                if grant.board_id == board_id:
                    grant.active, grant.close_reason = False, reason
            for plan in self._plans.values():
                if plan.board_id == board_id and plan.status in {
                    "active",
                    "disclosure-required",
                }:
                    plan.status, plan.close_reason = "invalidated", reason

    def cancel_plan(self, plan_id: str) -> dict[str, object]:
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                return {
                    "plan_id": plan_id,
                    "existed": False,
                    "status": "missing",
                    "refunded": False,
                }
            if plan.status in {"active", "disclosure-required"}:
                plan.status, plan.close_reason = "invalidated", "cancelled"
            return {"plan_id": plan_id, "existed": True, "status": plan.status, "refunded": False}

    def plan_record(self, plan_id: str) -> dict[str, object]:
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise GuardError("guard/plan-not-found", f"Unknown plan '{plan_id}'.")
            grant = self._grants.get(plan.grant_id or "")
            return {
                "plan_id": plan.plan_id,
                "board_id": plan.board_id,
                "objective": plan.objective,
                "expected_result": plan.expected_result,
                "grant_id": plan.grant_id,
                "binding": dict(plan.binding),
                "status": plan.status,
                "close_reason": plan.close_reason,
                "risk": "destructive" if plan.disclosure is not None else "routine",
                "disclosure": deepcopy(plan.disclosure),
                "actions": [
                    {
                        "tool": action.tool,
                        "arguments": dict(action.arguments),
                        "max_calls": action.max_calls,
                        "remaining_calls": action.remaining_calls,
                        "serial_identity": dict(action.serial_identity)
                        if action.serial_identity is not None
                        else None,
                    }
                    for action in plan.actions
                ],
                "grant_remaining_calls": grant.remaining_calls if grant else None,
                "attempts": [dict(item) for item in plan.attempts],
            }
