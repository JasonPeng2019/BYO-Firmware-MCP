"""Plan-id-bound destructive target recovery with complete erase disclosure."""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle, session_metadata
from pyocd_debug_mcp.board_config import (
    RECOVER_MODE_MANUAL_ONLY,
    RECOVER_MODE_BACKEND_MASS_ERASE,
)
from pyocd_debug_mcp.firmstore.profiles import BoardProfile, ProfileRepository
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
from pyocd_debug_mcp.safety.map_build import (
    GenericSafetyMapDocument,
    SafetyMapDocument,
    SafetyMapError,
    SafetyMapRepository,
)
from pyocd_debug_mcp.safety.enforce import SafetyPolicyError
from pyocd_debug_mcp.safety.regions import (
    Provenance,
    RecoveryEraseDisclosure,
    RegionError,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
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
    map_digest: str


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
class LiteRecoveryPolicy:
    """Committed partial-policy facts for one native, typed recovery route."""

    display_name: str
    pyocd_target: str
    mechanism: str
    disclosure: RecoveryEraseDisclosure


@dataclass(frozen=True, slots=True)
class UnlockToolServices:
    server_run: ServerRun
    plan_engine: PlanEngine
    profiles: ProfileRepository
    safety_repository: SafetyMapRepository
    reports: ReportWriter
    gate_manager: GateManager
    handle_for: Callable[[str], TargetSessionHandle]
    connection_id_for: Callable[[str], str]
    session_id_for: Callable[[str], str | None]
    current_map_digest: Callable[[str], str]
    supports_recovery: Callable[[TargetSessionHandle, str], bool]
    recover_target: Callable[[TargetSessionHandle, str], str]
    finalize_recovery: Callable[[str], None]
    revoke_permission: Callable[[str, str], None]
    manual_mass_erase_disclosure: (
        Callable[[UnlockBinding, RecoveryEraseDisclosure], Mapping[str, object]] | None
    ) = None
    reserve_manual_mass_erase: Callable[[UnlockBinding], str] | None = None
    consume_manual_mass_erase: Callable[[UnlockBinding, str], None] | None = None
    abandon_manual_mass_erase: Callable[[UnlockBinding, str], None] | None = None
    policy_profile: Callable[[str], BoardProfile] | None = None
    policy_safety: Callable[[str], SafetyMapDocument] | None = None
    policy_lite_recovery: Callable[[str], LiteRecoveryPolicy | None] | None = None


def _json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


def _geometry(document: SafetyMapDocument) -> Mapping[str, object]:
    geometry = document.geometry
    if geometry.erase_sectors:
        return {
            "sectors": [sector.to_document() for sector in geometry.erase_sectors],
        }
    return {
        "erase_origin": geometry.erase_origin,
        "erase_size": geometry.erase_size,
    }


def _recovery_regions(document: SafetyMapDocument) -> list[SafetyRegion]:
    """Combine persisted hazards with reviewed deployment partitions for disclosure."""

    regions = list(document.regions)
    provenance = (
        Provenance(
            SourceAuthority.RECONCILED,
            "memory_map.partitions",
            "reviewed deployment-partition authority",
        ),
    )
    if document.partitions.application is not None:
        regions.append(
            SafetyRegion(
                "application",
                RegionKind.APPLICATION_FLASH,
                document.partitions.application,
                provenance,
                True,
            )
        )
    if document.partitions.bootloader is not None:
        regions.append(
            SafetyRegion(
                "bootloader",
                RegionKind.BOOTLOADER_FLASH,
                document.partitions.bootloader,
                provenance,
                True,
            )
        )
    return regions


class UnlockCoordinator:
    """Own the non-authorizing draft and one exact approved execution binding."""

    def __init__(self, services: UnlockToolServices) -> None:
        self.services = services
        self.definition = PLAN_DEFINITIONS["target_unlock"]
        self._pending: dict[str, PendingUnlockApproval] = {}
        self._approved: dict[str, UnlockBinding] = {}
        self._manual_claims: dict[str, str] = {}
        self._guard = threading.RLock()

    def _abandon_manual_claim(self, binding: UnlockBinding | None, claim_id: str | None) -> None:
        if (
            binding is not None
            and claim_id is not None
            and self.services.abandon_manual_mass_erase is not None
        ):
            self.services.abandon_manual_mass_erase(binding, claim_id)

    def _abandon_without_masking(
        self,
        binding: UnlockBinding | None,
        claim_id: str | None,
        primary: BaseException,
    ) -> None:
        """Best-effort reservation cleanup must not replace the real failure."""

        try:
            self._abandon_manual_claim(binding, claim_id)
        except Exception as cleanup_error:  # noqa: BLE001 - keep the submit/backend error primary
            primary.add_note(f"manual mass-erase reservation cleanup failed: {cleanup_error}")

    def _identity(
        self, board_id: str
    ) -> tuple[LiveUnlockIdentity, SafetyMapDocument | LiteRecoveryPolicy]:
        lite = (
            self.services.policy_lite_recovery(board_id)
            if self.services.policy_lite_recovery is not None
            else None
        )
        if lite is not None:
            handle = self.services.handle_for(board_id)
            metadata = session_metadata(handle)
            probe = (metadata.probe_uid or "").strip()
            if not probe:
                raise PlanRefusal(
                    "unlock/probe-identity-missing",
                    "The active probe has no stable identity; reconnect with an identifiable probe.",
                )
            # Lite trusts the named policy facts but does not turn a live part
            # string into full identity proof. Its mechanism/disclosure were
            # validated from the immutable confirmation; backend support is
            # checked by _mechanism below.
            return (
                LiveUnlockIdentity(
                    self.services.server_run.run_id,
                    board_id,
                    lite.display_name,
                    "not-asserted",
                    str(metadata.live_part_number or metadata.target_override or "not-asserted"),
                    lite.pyocd_target,
                    probe,
                    self.services.connection_id_for(board_id),
                    self.services.current_map_digest(board_id),
                ),
                lite,
            )
        profile = (
            self.services.policy_profile(board_id)
            if self.services.policy_profile is not None
            else self.services.profiles.load(board_id)
        )
        handle = self.services.handle_for(board_id)
        metadata = session_metadata(handle)
        probe = (metadata.probe_uid or "").strip()
        if not probe:
            raise PlanRefusal(
                "unlock/probe-identity-missing",
                "The active probe has no stable identity; reconnect with an identifiable probe.",
            )
        live_part = str(metadata.live_part_number or "").strip()
        if not live_part:
            raise PlanRefusal(
                "unlock/target-identity-missing",
                "The active target exposes no exact live part identity; recovery stays unavailable.",
            )
        pyocd_target = (
            str(metadata.target_override or "").strip() or profile.board.pyocd_target.strip()
        )
        map_digest = self.services.current_map_digest(board_id)
        try:
            artifacts = (
                self.services.policy_safety(board_id)
                if self.services.policy_safety is not None
                else self.services.safety_repository.load_current(board_id)
            )
        except (SafetyMapError, SafetyPolicyError) as exc:
            raise PlanRefusal(
                "unlock/safety-map-invalid",
                f"The current safety map is unavailable or invalid: {exc}. "
                "Run board_safety_refresh first.",
            ) from exc
        if artifacts.canonical_digest != map_digest:
            raise PlanRefusal(
                "unlock/safety-map-digest-mismatch",
                "The current safety map digest changed; run board_safety_refresh first.",
            )
        if isinstance(artifacts, GenericSafetyMapDocument):
            # A resolved device support record proves neither board ownership
            # nor a safe recovery mechanism. Do not turn generic physical-map
            # knowledge into a whole-device destructive capability.
            raise PlanRefusal(
                "unlock/generic-device-policy-unavailable",
                "Generic device support has no reviewed recovery policy; target recovery is unavailable.",
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
                map_digest,
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
        configured = (
            artifacts.mechanism
            if isinstance(artifacts, LiteRecoveryPolicy)
            else handle.board.recover_mode
            if handle.board is not None
            else None
        )
        parameters = fields.get("action_parameters")
        if not isinstance(parameters, Mapping):
            raise PlanRefusal(
                "unlock/action-parameters-invalid",
                "target_unlock-plan requires one nested action_parameters JSON object.",
            )
        mechanism = self._mechanism(identity, configured, parameters["recovery_mechanism"])
        if mechanism is None:
            return None, None, identity
        if isinstance(artifacts, LiteRecoveryPolicy):
            disclosure = artifacts.disclosure
        else:
            try:
                disclosure = build_recovery_erase_disclosure(
                    _recovery_regions(artifacts),
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
        payload: dict[str, object] = {
            "status": "unlock_permission_requested",
            "agent_prompt": prompt,
            "plan_id": binding.plan_id,
            "live_identity": asdict(identity),
            "mechanism": asdict(binding.mechanism),
            "disclosure": disclosure.to_document(),
            "expected_losses": list(disclosure.expected_losses),
            "report": report,
        }
        if binding.mechanism.mass_erase and self.services.manual_mass_erase_disclosure is not None:
            payload["manual_grant"] = dict(
                self.services.manual_mass_erase_disclosure(binding, disclosure)
            )
        return _json(payload)

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
                old_approved = self._approved.pop(preview.board_id, None)
                old_claim = self._manual_claims.pop(preview.board_id, None)
            if old_approved is not None or old_claim is not None:
                try:
                    self._abandon_manual_claim(old_approved, old_claim)
                except (
                    Exception
                ) as exc:  # cannot safely promise a replacement while old state is unknown
                    raise PlanRefusal(
                        "manual/abandon-failed",
                        "The previous destructive recovery reservation could not be cleared; "
                        "resolve the manual grant state before requesting a replacement disclosure.",
                    ) from exc
            with self._guard:
                self._pending.pop(preview.board_id, None)
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
        manual_claim: str | None = None
        if (
            pending.binding.mechanism.mass_erase
            and self.services.reserve_manual_mass_erase is not None
        ):
            manual_claim = self.services.reserve_manual_mass_erase(pending.binding)
        try:
            result = self.services.plan_engine.submit(
                self.definition.plan_tool_name,
                fields,
                session_id=session_id,
                plan_id_override=pending.binding.plan_id,
            )
        except BaseException as primary:
            self._abandon_without_masking(pending.binding, manual_claim, primary)
            raise
        assert result.plan is not None
        with self._guard:
            self._pending.pop(preview.board_id, None)
            self._approved[preview.board_id] = pending.binding
            if manual_claim is not None:
                self._manual_claims[preview.board_id] = manual_claim
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
                "unlock/approval-inactive: no active plan-id-bound one-time unlock approval "
                "exists; request a new disclosure.",
            )
        if canonical_json(dict(parameters)) != active.canonical_parameters:
            self.services.plan_engine.invalidate(
                "target_unlock", board_id, "destructive recovery execution parameters changed"
            )
            self.services.revoke_permission(
                board_id, "destructive recovery execution parameters changed"
            )
            with self._guard:
                stale = self._approved.pop(board_id, None)
                claim = self._manual_claims.pop(board_id, None)
            self._abandon_manual_claim(stale, claim)
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
                claim = self._manual_claims.pop(board_id, None)
            self._abandon_manual_claim(approved, claim)
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
                claim = self._manual_claims.pop(board_id, None)
            self._abandon_manual_claim(approved, claim)
            raise PlanRefusal(
                "unlock/binding-changed",
                "The target, probe, connection, safety map, erase ranges, mechanism, or plan "
                "changed before execution. Fresh disclosure and one-time approval are required.",
            )

    def invalidate_execution_mismatch(self, board_id: str) -> None:
        """Invalidate only this board's outstanding destructive approval/claim.

        ``PlanEngine`` validates the public action schema before invoking this
        coordinator's normal precondition.  A changed, but schema-valid,
        recovery mechanism can therefore be rejected there first.  Its
        reservation is still stale and must be abandoned before returning the
        generic immutable-plan refusal.
        """

        with self._guard:
            approved = self._approved.pop(board_id, None)
            claim = self._manual_claims.pop(board_id, None)
        if approved is None:
            return
        # Do not revoke the plan-engine permission here: its revocation hook
        # would immediately relock the action and hide the more useful
        # ``unlock/approval-inactive`` answer on a same-run retry.  The
        # coordinator has removed the only destructive approval and durable
        # claim, so the retained plan/permission pair grants no authority; a
        # fresh disclosure and plan are still required before recovery can
        # execute.
        self._abandon_manual_claim(approved, claim)

    def invalidate_board(self, board_id: str) -> None:
        """Drop non-authorizing drafts and approved in-memory bindings on disconnect."""

        with self._guard:
            self._pending.pop(board_id, None)
            approved = self._approved.pop(board_id, None)
            claim = self._manual_claims.pop(board_id, None)
        self._abandon_manual_claim(approved, claim)
        self.services.revoke_permission(board_id, "target unlock binding invalidated")

    def execute(self, board_id: str, recovery_mechanism: str) -> str:
        with self._guard:
            approved = self._approved.pop(board_id, None)
            manual_claim = self._manual_claims.pop(board_id, None)
        if approved is None:
            raise PlanRefusal(
                "unlock/approval-inactive",
                "The fresh one-time approval is no longer active.",
            )
        if recovery_mechanism != approved.mechanism.mechanism_id:
            self._abandon_manual_claim(approved, manual_claim)
            raise PlanRefusal(
                "unlock/parameter-mismatch",
                "The recovery mechanism differs from the approved typed vendor operation.",
            )
        if approved.mechanism.mass_erase:
            if manual_claim is None or self.services.consume_manual_mass_erase is None:
                raise PlanRefusal(
                    "manual/locked",
                    "Ask the human to invoke $mass-erase for the current disclosed recovery scope.",
                )
            self.services.consume_manual_mass_erase(approved, manual_claim)
        plan_id = approved.plan_id
        fields = {
            "live_identity": asdict(approved.identity),
            "mechanism": asdict(approved.mechanism),
            "disclosure": json.loads(approved.erase_disclosure_json),
        }
        self.services.gate_manager.clear(
            board_id, "target unlock attempt started; board_validate is required"
        )
        backend: str | None = None
        backend_error: BaseException | None = None
        try:
            backend = self.services.recover_target(
                self.services.handle_for(board_id), approved.mechanism.mechanism_id
            )
        except BaseException as exc:
            backend_error = exc
            self._report(
                status="unlock_failed_revalidation_required",
                board_id=board_id,
                plan_id=plan_id,
                fields=fields,
            )
        try:
            self.services.finalize_recovery(board_id)
        except Exception as cleanup_error:
            if backend_error is not None:
                backend_error.add_note(
                    f"recovery cleanup after failed backend attempt also failed: {cleanup_error}"
                )
            else:
                report = self._report(
                    status="unlock_completed_cleanup_uncertain_reconnect_required",
                    board_id=board_id,
                    plan_id=plan_id,
                    fields={**fields, "backend": backend, "cleanup_error": str(cleanup_error)},
                )
                raise RuntimeError(
                    "Target recovery completed, but connection cleanup was uncertain. The old "
                    f"connection was revoked; reconnect and run board_validate before further use. Report: {report}"
                ) from cleanup_error
        if backend_error is not None:
            raise backend_error
        report = self._report(
            status="unlock_completed_revalidation_required",
            board_id=board_id,
            plan_id=plan_id,
            fields={**fields, "backend": backend},
        )
        return (
            f"Target unlock completed using {approved.mechanism.description}. The operation "
            f"performed mass erase and consumed plan {plan_id}. The board was disconnected; "
            f"reconnect and run board_validate before any debug, validation, read, or write action. Report: {report}"
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
