"""Plan-id-bound destructive target recovery with complete erase disclosure."""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from pyocd_debug_mcp.adapters.target_backend import TargetSessionDescription, TargetSessionHandle
from pyocd_debug_mcp.board_config import (
    RECOVER_MODE_MANUAL_ONLY,
    RECOVER_MODE_BACKEND_MASS_ERASE,
)
from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.guardrails.gate import GateManager
from pyocd_debug_mcp.guardrails.plan_defs import PLAN_DEFINITIONS
from pyocd_debug_mcp.guardrails.plan_engine import (
    PlanEngine,
    PlanRefusal,
    accepted_plan_payload,
    canonical_json,
)
from pyocd_debug_mcp.kernel.operations import wrap_layer2_response
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.safety.map_build import SafetyArtifactRepository, SafetyArtifacts
from pyocd_debug_mcp.safety.regions import (
    RecoveryEraseDisclosure,
    RegionError,
    build_recovery_erase_disclosure,
)

NO_INTERNALS = (
    "Relay this request in ordinary language and do not expose structured payloads, "
    "continuation tokens, or internal field names."
)


@dataclass(frozen=True, slots=True)
class RecoveryMechanism:
    mechanism_id: str
    vendor: str
    description: str
    mass_erase: bool


RECOVERY_MECHANISMS = {
    RECOVER_MODE_BACKEND_MASS_ERASE: RecoveryMechanism(
        RECOVER_MODE_BACKEND_MASS_ERASE,
        "connected target backend",
        "the backend's typed, documented whole-device mass-erase recovery primitive",
        True,
    )
}


@dataclass(frozen=True, slots=True)
class LiveUnlockIdentity:
    run_id: str
    board_id: str
    display_name: str
    mcu_part_number: str
    live_target_part: str
    pyocd_target: str
    probe_identity: str
    connection_id: str
    safety_map_fingerprint: str


@dataclass(frozen=True, slots=True)
class UnlockBinding:
    plan_id: str
    identity: LiveUnlockIdentity
    mechanism: RecoveryMechanism
    erase_disclosure_json: str
    plan_without_permission_json: str


@dataclass(frozen=True, slots=True)
class PendingUnlockApproval:
    binding: UnlockBinding
    disclosure: RecoveryEraseDisclosure


@dataclass(frozen=True, slots=True)
class UnlockToolServices:
    server_run: ServerRun
    plan_engine: PlanEngine
    profiles: ProfileRepository
    safety_repository: SafetyArtifactRepository
    reports: ReportWriter
    gate_manager: GateManager
    handle_for: Callable[[str], TargetSessionHandle]
    connection_id_for: Callable[[str], str]
    session_id_for: Callable[[str], str | None]
    current_fingerprint: Callable[[str], str]
    describe_session: Callable[[TargetSessionHandle], TargetSessionDescription]
    supports_recovery: Callable[[TargetSessionHandle, str], bool]
    recover_target: Callable[[TargetSessionHandle, str], str]
    mark_recover_completed: Callable[[str], None]
    revoke_permission: Callable[[str, str], None]


def _json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


def _geometry(artifacts: SafetyArtifacts) -> Mapping[str, object]:
    geometry = artifacts.geometry
    if not geometry:
        raise PlanRefusal(
            "unlock/geometry-missing",
            "The safety map has no complete erase geometry; run board_safety_refresh.",
        )
    return geometry


class UnlockCoordinator:
    """Own the non-authorizing draft and one exact approved execution binding."""

    def __init__(self, services: UnlockToolServices) -> None:
        self.services = services
        self.definition = PLAN_DEFINITIONS["target_unlock"]
        self._pending: dict[str, PendingUnlockApproval] = {}
        self._approved: dict[str, UnlockBinding] = {}
        self._guard = threading.RLock()

    def _identity(self, board_id: str) -> tuple[LiveUnlockIdentity, SafetyArtifacts]:
        profile = self.services.profiles.load(board_id, include_legacy=False)
        handle = self.services.handle_for(board_id)
        probe = (handle.probe_uid or "").strip()
        if not probe:
            raise PlanRefusal(
                "unlock/probe-identity-missing",
                "The active probe has no stable identity; reconnect with an identifiable probe.",
            )
        live_part = self.services.describe_session(handle).live_target_part
        if not live_part:
            raise PlanRefusal(
                "unlock/target-identity-missing",
                "The active target exposes no exact live part identity; recovery stays unavailable.",
            )
        pyocd_target = (
            str(handle.target_override or "").strip() or profile.board.pyocd_target.strip()
        )
        fingerprint = self.services.current_fingerprint(board_id)
        artifacts = self.services.safety_repository.load_current(board_id)
        if artifacts.map_digest != fingerprint:
            raise PlanRefusal(
                "unlock/safety-fingerprint-mismatch",
                "The current stable memory-map digest changed; run board_safety_refresh first.",
            )
        return (
            LiveUnlockIdentity(
                self.services.server_run.run_id,
                board_id,
                profile.display_name,
                profile.mcu_part_number or "",
                live_part,
                pyocd_target,
                probe,
                self.services.connection_id_for(board_id),
                fingerprint,
            ),
            artifacts,
        )

    @staticmethod
    def _without_permission(fields: Mapping[str, object]) -> str:
        normalized = dict(fields)
        normalized["user_permission"] = None
        return canonical_json(normalized)

    def _mechanism(
        self,
        identity: LiveUnlockIdentity,
        configured: str | None,
        requested: object,
    ) -> RecoveryMechanism | None:
        if configured == RECOVER_MODE_MANUAL_ONLY:
            raise PlanRefusal(
                "unlock/manual-only",
                f"{identity.display_name} is configured as manual_only. This server will not "
                "substitute an automated mass erase; follow the documented manual procedure.",
            )
        requested_text = requested.strip() if isinstance(requested, str) else ""
        candidate = requested_text or (configured or "")
        if not candidate:
            return None
        mechanism = RECOVERY_MECHANISMS.get(candidate)
        if mechanism is None:
            raise PlanRefusal(
                "unlock/mechanism-unsupported",
                f"Recovery mechanism '{candidate}' is not a typed documented vendor operation "
                "supported by this server.",
            )
        if configured and configured != candidate:
            raise PlanRefusal(
                "unlock/mechanism-mismatch",
                f"The reviewed mechanism '{candidate}' does not match configured mechanism "
                f"'{configured}'.",
            )
        handle = self.services.handle_for(identity.board_id)
        if not self.services.supports_recovery(handle, candidate):
            raise PlanRefusal(
                "unlock/mechanism-backend-unsupported",
                "The connected target backend does not report support for this typed recovery "
                "primitive. Use the documented manual recovery path for this target.",
            )
        return mechanism

    def _binding(
        self,
        plan_id: str,
        fields: Mapping[str, object],
    ) -> tuple[UnlockBinding | None, RecoveryEraseDisclosure | None, LiveUnlockIdentity]:
        board_id = fields["board_id"]
        assert isinstance(board_id, str)
        identity, artifacts = self._identity(board_id)
        handle = self.services.handle_for(board_id)
        configured = handle.board.recover_mode if handle.board is not None else None
        parameters = fields.get("action_parameters")
        if not isinstance(parameters, Mapping):
            raise PlanRefusal(
                "unlock/action-parameters-invalid",
                "target_unlock-plan requires one nested action_parameters JSON object.",
            )
        mechanism = self._mechanism(identity, configured, parameters["recovery_mechanism"])
        if mechanism is None:
            return None, None, identity
        try:
            disclosure = build_recovery_erase_disclosure(
                [item.region for item in artifacts.regions],
                _geometry(artifacts),
                mass_erase=mechanism.mass_erase,
            )
        except RegionError as exc:
            raise PlanRefusal(
                "unlock/erase-disclosure-incomplete",
                f"The current safety map cannot prove the complete recovery erase disclosure: "
                f"{exc}. Run board_safety_refresh before requesting permission.",
            ) from exc
        return (
            UnlockBinding(
                plan_id,
                identity,
                mechanism,
                canonical_json(disclosure.to_document()),
                self._without_permission(fields),
            ),
            disclosure,
            identity,
        )

    def _report(
        self,
        *,
        status: str,
        board_id: str,
        plan_id: str | None,
        fields: Mapping[str, object],
    ) -> str:
        attempt_id = f"target-unlock-{secrets.token_hex(8)}"
        safe_fields = {key: value for key, value in fields.items() if key != "user_permission"}
        paths = self.services.reports.create_target_unlock(
            attempt_id,
            {
                "board_id": board_id,
                "terminal_status": status,
                "plan_id": plan_id,
                "details": dict(safe_fields),
            },
        )
        self.services.reports.append_target_unlock_event(
            attempt_id,
            {"event": status, "board_id": board_id, "plan_id": plan_id},
        )
        return str(paths.report)

    def _research_response(
        self,
        fields: Mapping[str, object],
        identity: LiveUnlockIdentity,
    ) -> str:
        board_id = identity.board_id
        with self._guard:
            self._pending.pop(board_id, None)
        report = self._report(
            status="unlock_research_required",
            board_id=board_id,
            plan_id=None,
            fields={
                "live_identity": asdict(identity),
                "requested_fields": ["recovery_mechanism", "vendor", "mass_erase"],
            },
        )
        return _json(
            {
                "status": "unlock_research_required",
                "agent_prompt": (
                    f"The documented recovery mechanism for {identity.display_name} "
                    f"({identity.live_target_part}) is unknown. Research the exact vendor "
                    "recovery primitive supported by the attached probe and target. Return only "
                    "its mechanism identifier, vendor, and whether it performs mass erase; "
                    f"research does not authorize execution. {NO_INTERNALS}"
                ),
                "board_id": board_id,
                "live_identity": asdict(identity),
                "requested_fields": ["recovery_mechanism", "vendor", "mass_erase"],
                "report": report,
            }
        )

    def _permission_response(
        self,
        pending: PendingUnlockApproval,
        fields: Mapping[str, object],
    ) -> str:
        binding = pending.binding
        identity = binding.identity
        disclosure = pending.disclosure
        spans = "; ".join(
            f"0x{item.address_range.start:08X}-0x{item.address_range.end:08X} "
            f"({item.bank}, sectors {item.first_sector}-{item.last_sector})"
            for item in disclosure.spans
        )
        affected = ", ".join(
            f"{item.name} [{item.kind.value}] 0x{item.address_range.start:08X}-"
            f"0x{item.address_range.end:08X}"
            for item in disclosure.affected_regions
        )
        losses = ", ".join(disclosure.expected_losses)
        all_nv = (
            "The entire addressable nonvolatile memory will be erased."
            if disclosure.all_nonvolatile_erased
            else "Only the listed nonvolatile ranges will be erased."
        )
        report = self._report(
            status="unlock_permission_requested",
            board_id=identity.board_id,
            plan_id=binding.plan_id,
            fields={
                "live_identity": asdict(identity),
                "mechanism": asdict(binding.mechanism),
                "disclosure": disclosure.to_document(),
                "planned_fields": {
                    key: value for key, value in fields.items() if key != "user_permission"
                },
            },
        )
        prompt = (
            f"Ask the user to approve this exact one-time destructive recovery plan. Board "
            f"{identity.display_name} ({identity.board_id}), exact MCU "
            f"{identity.mcu_part_number}, live target {identity.live_target_part}, pyOCD target "
            f"{identity.pyocd_target}, probe {identity.probe_identity}. Vendor mechanism: "
            f"{binding.mechanism.vendor} - {binding.mechanism.description}. Mass erase: "
            f"{'yes' if binding.mechanism.mass_erase else 'no'}. Erased ranges: {spans}. "
            f"Known affected regions: {affected}. {all_nv} Expected losses: {losses}. "
            f"Plan identifier: {binding.plan_id}. If the user approves, resubmit the complete "
            "target_unlock-plan with every other field unchanged and user_permission set to "
            f"one-time. Full-session approval cannot authorize this operation. {NO_INTERNALS}"
        )
        return _json(
            {
                "status": "unlock_permission_requested",
                "agent_prompt": prompt,
                "plan_id": binding.plan_id,
                "live_identity": asdict(identity),
                "mechanism": asdict(binding.mechanism),
                "disclosure": disclosure.to_document(),
                "expected_losses": list(disclosure.expected_losses),
                "report": report,
            }
        )

    def plan(self, fields: Mapping[str, object]) -> str:
        if all(value is None for value in fields.values()):
            return self.services.plan_engine.submit(
                self.definition.plan_tool_name,
                fields,
                session_id=None,
            ).message
        board = fields.get("board_id")
        permission = fields.get("user_permission")
        if permission == "one-time" and isinstance(board, str):
            with self._guard:
                pending = self._pending.get(board)
            if (
                pending is not None
                and self._without_permission(fields) != pending.binding.plan_without_permission_json
            ):
                with self._guard:
                    self._pending.pop(board, None)
                raise PlanRefusal(
                    "unlock/plan-changed",
                    "A plan field changed after disclosure. The approval does not transfer; "
                    "request a new disclosure for the complete replacement plan.",
                )
        session_id = self.services.session_id_for(board) if isinstance(board, str) else None
        preview = self.services.plan_engine.preview_submission(
            self.definition.plan_tool_name,
            fields,
            session_id=session_id,
        )
        permission = fields["user_permission"]
        if permission == "full-session":
            raise PlanRefusal(
                "permission/fresh-one-time-required",
                "Mass erase always requires fresh one-time permission; full-session permission "
                "cannot authorize target_unlock.",
            )
        if permission is None:
            with self._guard:
                self._approved.pop(preview.board_id, None)
            # Revocation also invalidates any active plan through the store's
            # callback. It is deliberate even when no grant exists: a new
            # disclosure can never coexist with reusable prior authority.
            self.services.revoke_permission(
                preview.board_id,
                "a replacement destructive recovery disclosure was requested",
            )
            plan_id = f"plan-{secrets.token_hex(8)}"
            binding, disclosure, identity = self._binding(plan_id, fields)
            if binding is None or disclosure is None:
                return self._research_response(fields, identity)
            pending = PendingUnlockApproval(binding, disclosure)
            with self._guard:
                self._pending[preview.board_id] = pending
                self._approved.pop(preview.board_id, None)
            return self._permission_response(pending, fields)
        if permission != "one-time":
            raise PlanRefusal(
                "permission/required",
                "target_unlock-plan accepts only NULL for disclosure or fresh one-time approval.",
            )
        with self._guard:
            pending = self._pending.get(preview.board_id)
        if pending is None:
            raise PlanRefusal(
                "unlock/approval-handshake-required",
                "Request the exact plan disclosure with user_permission=NULL before submitting "
                "fresh one-time approval.",
            )
        if self._without_permission(fields) != pending.binding.plan_without_permission_json:
            with self._guard:
                self._pending.pop(preview.board_id, None)
            raise PlanRefusal(
                "unlock/plan-changed",
                "A plan field changed after disclosure. The approval does not transfer; request "
                "a new disclosure for the complete replacement plan.",
            )
        try:
            current, disclosure, _identity = self._binding(pending.binding.plan_id, fields)
        except Exception:
            with self._guard:
                self._pending.pop(preview.board_id, None)
            raise
        if current != pending.binding or disclosure != pending.disclosure:
            with self._guard:
                self._pending.pop(preview.board_id, None)
            raise PlanRefusal(
                "unlock/binding-changed",
                "The target, probe, connection, safety map, erase ranges, mechanism, or Server "
                "Run changed after disclosure. Fresh disclosure and approval are required.",
            )
        result = self.services.plan_engine.submit(
            self.definition.plan_tool_name,
            fields,
            session_id=session_id,
            plan_id_override=pending.binding.plan_id,
        )
        assert result.plan is not None
        with self._guard:
            self._pending.pop(preview.board_id, None)
            self._approved[preview.board_id] = pending.binding
        payload = accepted_plan_payload(result.plan)
        payload.update(
            {
                "status": "unlock_plan_approved",
                "underlying_tool": "target_unlock",
                "redirect": (
                    "Prefer target_unlock directly. If it is absent from static client bindings, "
                    "submit only stable_client_fallback unchanged."
                ),
            }
        )
        return _json(payload)

    def validate_execution(self, board_id: str, parameters: Mapping[str, object]) -> None:
        active = self.services.plan_engine.active_plan("target_unlock", board_id)
        with self._guard:
            approved = self._approved.get(board_id)
        if active is None or approved is None or active.plan_id != approved.plan_id:
            raise PlanRefusal(
                "unlock/approval-inactive",
                "No active plan-id-bound one-time unlock approval exists; request a new disclosure.",
            )
        if canonical_json(dict(parameters)) != active.canonical_parameters:
            raise PlanRefusal(
                "unlock/parameter-mismatch",
                "target_unlock parameters differ from the approved immutable plan.",
            )
        fields = active.submitted_fields
        try:
            current, disclosure, _identity = self._binding(active.plan_id, fields)
        except Exception as exc:
            self.services.plan_engine.invalidate(
                "target_unlock", board_id, "destructive recovery approval binding changed"
            )
            self.services.revoke_permission(
                board_id, "destructive recovery approval binding changed"
            )
            with self._guard:
                self._approved.pop(board_id, None)
            raise PlanRefusal(
                "unlock/binding-changed",
                "The target, probe, connection, safety map, erase ranges, mechanism, or plan "
                "could not be revalidated. Fresh disclosure and one-time approval are required.",
            ) from exc
        if current != approved or disclosure is None:
            self.services.plan_engine.invalidate(
                "target_unlock", board_id, "destructive recovery approval binding changed"
            )
            self.services.revoke_permission(
                board_id, "destructive recovery approval binding changed"
            )
            with self._guard:
                self._approved.pop(board_id, None)
            raise PlanRefusal(
                "unlock/binding-changed",
                "The target, probe, connection, safety map, erase ranges, mechanism, or plan "
                "changed before execution. Fresh disclosure and one-time approval are required.",
            )

    def invalidate_board(self, board_id: str) -> None:
        """Drop non-authorizing drafts and approved in-memory bindings on disconnect."""

        with self._guard:
            self._pending.pop(board_id, None)
            self._approved.pop(board_id, None)
        self.services.revoke_permission(board_id, "target unlock binding invalidated")

    def execute(self, board_id: str, recovery_mechanism: str) -> str:
        with self._guard:
            approved = self._approved.pop(board_id, None)
        if approved is None:
            raise PlanRefusal(
                "unlock/approval-inactive",
                "The fresh one-time approval is no longer active.",
            )
        if recovery_mechanism != approved.mechanism.mechanism_id:
            raise PlanRefusal(
                "unlock/parameter-mismatch",
                "The recovery mechanism differs from the approved typed vendor operation.",
            )
        plan_id = approved.plan_id
        fields = {
            "live_identity": asdict(approved.identity),
            "mechanism": asdict(approved.mechanism),
            "disclosure": json.loads(approved.erase_disclosure_json),
        }
        self.services.gate_manager.clear(
            board_id, "target unlock attempt started; board_validate is required"
        )
        try:
            backend = self.services.recover_target(
                self.services.handle_for(board_id), approved.mechanism.mechanism_id
            )
        except Exception:
            self._report(
                status="unlock_failed_revalidation_required",
                board_id=board_id,
                plan_id=plan_id,
                fields=fields,
            )
            raise
        self.services.mark_recover_completed(board_id)
        report = self._report(
            status="unlock_completed_revalidation_required",
            board_id=board_id,
            plan_id=plan_id,
            fields={**fields, "backend": backend},
        )
        return (
            f"Target unlock completed using {approved.mechanism.description}. The operation "
            f"performed mass erase and consumed plan {plan_id}. The validation gate remains "
            f"closed; run board_validate before any guarded read or write. Report: {report}"
        )


def build_unlock_handlers(
    coordinator: UnlockCoordinator,
) -> dict[str, Callable[..., str]]:
    def target_unlock_plan(
        board_id: str | None = None,
        hypothesis: str | None = None,
        strategy: str | None = None,
        hypothesis_made: bool | None = None,
        strategy_evaluated: bool | None = None,
        expected_fail_return: str | None = None,
        expected_success_return: str | None = None,
        max_calls: int | None = None,
        max_calls_buffer: int | None = None,
        action_parameters: dict[str, object] | None = None,
        user_permission: str | None = None,
    ) -> str:
        """Prepare destructive recovery only after setup/validation reports a locked target.

        First call every parameter NULL for the full mechanism and research guidance. Then submit
        one exact JSON plan with user_permission NULL to receive the live identity, complete erase
        ranges/losses, and plan_id disclosure. Relay it plainly, obtain fresh one-time approval,
        and resubmit the otherwise unchanged JSON with user_permission='one-time'. Full-session or
        prior permission never applies; any target, probe, map, range, or plan change invalidates it.
        """

        return coordinator.plan(
            {
                "board_id": board_id,
                "hypothesis": hypothesis,
                "hypothesis_made": hypothesis_made,
                "strategy": strategy,
                "strategy_evaluated": strategy_evaluated,
                "expected_fail_return": expected_fail_return,
                "expected_success_return": expected_success_return,
                "max_calls": max_calls,
                "max_calls_buffer": max_calls_buffer,
                "action_parameters": action_parameters,
                "user_permission": user_permission,
            }
        )

    def target_unlock(board_id: str, recovery_mechanism: str) -> str:
        """Execute exactly one approved typed vendor recovery operation."""

        return wrap_layer2_response(coordinator.execute(board_id, recovery_mechanism))

    return {
        "target_unlock-plan": target_unlock_plan,
        "target_unlock": target_unlock,
    }

