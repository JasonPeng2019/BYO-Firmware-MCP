"""AT07: manual grants bind safe recovery disclosure and are consumed before attempts."""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.manual_permissions import (
    ManualPermissionError,
    ManualPermissionRepository,
)
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.capabilities.routing import TierRouteRefusal, TierRouter

from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    confirmed_lite_policy,
    isolated_project,
    known_good_full_policy,
    public_call,
    public_text,
    tiered_test_server,
)


BOARD = "legacy_full"
RAW_BOARD = "raw_board"
POLICY = "a" * 64
DISCLOSURE = "b" * 64


def _native_lite_recovery_policy() -> tuple[dict[str, object], dict[str, object]]:
    """Return independent valid setup-lite evidence with its one native recovery fact."""

    profile, _full_map = known_good_full_policy()
    # A native mass-erase disclosure must account for *physical* flash and
    # its complete erase geometry.  Do not reuse the ordinary lite fixture:
    # it is intentionally partial and has executable SRAM for safe-read tests.
    lite_map: dict[str, object] = {
        "board_id": BOARD,
        "regions": [
            {
                "name": "physical flash",
                "kind": "physical_flash",
                "start": "0x00000000",
                "end": "0x00010000",
                "readable": True,
                "writable": False,
                "executable": False,
                "source_pages": [101],
                "source_note": "confirmed physical flash range",
            },
            {
                "name": "SRAM",
                "kind": "ram",
                "start": "0x20000000",
                "end": "0x20001000",
                "readable": True,
                "writable": True,
                "executable": False,
                "source_pages": [102],
                "source_note": "confirmed RAM containment range",
            },
        ],
        "flash": {
            "backend_target": "nrf52840",
            "erase_sectors": [{"start": 0, "end": 0x00010000, "bank": "main"}],
        },
    }
    lite_map["recovery"] = {
        "mechanism": "backend_mass_erase",
        "source_pages": [101],
        "source_note": "vendor recovery chapter confirms typed backend mass erase",
    }
    return profile, lite_map


def _unlock_fields(*, permission: str | None) -> dict[str, object]:
    """Use one disclosed immutable recovery request across public plan/action calls."""

    return {
        "board_id": BOARD,
        "hypothesis": "The target is locked and requires its documented recovery primitive.",
        "hypothesis_made": True,
        "strategy": "Run exactly one disclosed typed backend recovery after fresh approval.",
        "strategy_evaluated": True,
        "expected_fail_return": "The server preserves the grant as unavailable after a failed attempt.",
        "expected_success_return": "The board is disconnected and requires fresh validation.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "action_parameters": {"recovery_mechanism": "backend_mass_erase"},
        "user_permission": permission,
    }


def _unlock_null_fields() -> dict[str, object]:
    return {field: None for field in _unlock_fields(permission=None)}


def _public_json_reply(
    server: Any, tool_name: str, arguments: dict[str, object]
) -> dict[str, object]:
    """Decode a structured public tool reply that predates the common envelope."""

    response = json.loads(public_text(server, tool_name, arguments))
    if not isinstance(response, dict):
        raise AssertionError(f"{tool_name} reply must be a JSON object: {response!r}")
    return response


def _configure_public_native_lite(
    server: Any, root: Path, backend: DeterministicTargetBackend
) -> ManualPermissionRepository:
    """Install only persistent lite evidence plus a deterministic live session."""

    profile, lite_map = _native_lite_recovery_policy()
    policies = CapabilityPolicyRepository(root)
    policies.commit(
        BOARD,
        Tier.SETUP_LITE,
        profile_snapshot=profile,
        map_snapshot=lite_map,
        evidence={"confirmed": "native recovery disclosed by fixture"},
    )
    manual = ManualPermissionRepository(root, server.server_run.run_id)
    manual.reset_startup()
    server.configure_tiered_test_seams(
        backend=backend,
        policy_repository=policies,
        manual_permission_repository=manual,
    )
    handle = backend.open(board=None, unique_id="recovery-probe", target="nrf52840")
    server._promote_open_session(BOARD, handle, gate_reason="acceptance recovery fixture")
    return manual


def _public_disclosure_and_reservation(
    testcase: unittest.TestCase,
    server: Any,
    manual: ManualPermissionRepository,
) -> dict[str, object]:
    """Walk the public disclosure -> human grant -> one-time reservation protocol."""

    public_text(server, "target_unlock-plan", _unlock_null_fields())
    disclosure = _public_json_reply(server, "target_unlock-plan", _unlock_fields(permission=None))
    testcase.assertEqual(disclosure["status"], "unlock_permission_requested", disclosure)
    grant = disclosure.get("manual_grant")
    testcase.assertIsInstance(grant, dict, disclosure)
    assert isinstance(grant, dict)
    testcase.assertEqual(grant.get("action"), "mass-erase", grant)
    testcase.assertEqual(grant.get("state"), "locked", grant)
    policy_digest = grant.get("policy_digest")
    binding_digest = grant.get("binding_digest")
    testcase.assertIsInstance(policy_digest, str, grant)
    testcase.assertIsInstance(binding_digest, str, grant)
    assert isinstance(policy_digest, str)
    assert isinstance(binding_digest, str)
    # This models the separately invoked workspace skill's persisted result;
    # the server itself never invents a human grant.
    manual.write_unlocked_grant(
        action="mass-erase",
        grant_id="recovery-grant",
        board_id=BOARD,
        policy_digest=policy_digest,
        binding_digest=binding_digest,
    )
    approved = _public_json_reply(
        server, "target_unlock-plan", _unlock_fields(permission="one-time")
    )
    testcase.assertEqual(approved["status"], "unlock_plan_approved", approved)
    testcase.assertEqual(manual.status("mass-erase").state, "reserved")
    return disclosure


def _promote_raw_session(server: Any, backend: DeterministicTargetBackend) -> None:
    handle = backend.open(board=None, unique_id="raw-recovery-probe", target="nrf52840")
    server._promote_open_session(RAW_BOARD, handle, gate_reason="raw recovery fixture")


def _configure_public_no_setup_raw(
    server: Any, root: Path, backend: DeterministicTargetBackend
) -> ManualPermissionRepository:
    """Install a no-setup policy, a fresh grant store and one identifiable live session."""

    policies = CapabilityPolicyRepository(root)
    policies.resolve(RAW_BOARD)
    manual = ManualPermissionRepository(root, server.server_run.run_id)
    manual.reset_startup()
    server.configure_tiered_test_seams(
        backend=backend,
        policy_repository=policies,
        manual_permission_repository=manual,
    )
    _promote_raw_session(server, backend)
    return manual


def _raw_recovery(server: Any, board_id: str = RAW_BOARD) -> dict[str, Any]:
    return public_call(
        server,
        "target_unlock_raw",
        {"board_id": board_id, "recovery_mechanism": "backend_mass_erase"},
    )


def _human_grants_raw_refusal(
    manual: ManualPermissionRepository, refusal: dict[str, Any], board_id: str = RAW_BOARD
) -> None:
    """Model the separately invoked $mass-erase skill for the refusal's exact bindings."""

    grant = refusal["manual_grant"]
    manual.write_unlocked_grant(
        action="mass-erase",
        grant_id="raw-grant",
        board_id=board_id,
        policy_digest=grant["policy_digest"],
        binding_digest=grant["binding_digest"],
    )


def _recover_calls(backend: DeterministicTargetBackend) -> int:
    return sum(1 for call in backend.calls if call[0] == "recover")


def _disconnect_if_connected(server: Any, board_id: str) -> None:
    if server.connection_manager.maybe_connection(board_id) is not None:
        server.disconnect(board_id)


class TieredRecoveryAcceptanceTests(unittest.TestCase):
    def test_safe_mass_erase_grant_is_consumed_before_failed_attempt_and_cannot_retry(self) -> None:
        """AT07: destructive authorization is spent before backend execution begins."""

        with isolated_project() as root:
            grants = ManualPermissionRepository(root, "run-one")
            grants.reset_startup()
            grants.write_unlocked_grant(
                action="mass-erase",
                grant_id="mass-grant",
                board_id=BOARD,
                policy_digest=POLICY,
                binding_digest=DISCLOSURE,
            )
            claim = grants.reserve_mass_erase(BOARD, "mass-grant", POLICY, DISCLOSURE)
            grants.consume_mass_erase(BOARD, POLICY, DISCLOSURE, claim)
            self.assertEqual(grants.status("mass-erase").state, "consumed")
            # Model a backend failure after the server's mandated consumption
            # ordering.  Retrying the authorization is not permitted.
            with self.assertRaisesRegex(ManualPermissionError, r"manual/already-consumed"):
                grants.consume_mass_erase(BOARD, POLICY, DISCLOSURE, claim)

    def test_changed_disclosure_cannot_reuse_a_reserved_or_unlocked_mass_erase_grant(self) -> None:
        """AT07: protected-span scope/loss changes demand fresh disclosure and grant."""

        with isolated_project() as root:
            grants = ManualPermissionRepository(root, "run-one")
            grants.reset_startup()
            grants.write_unlocked_grant(
                action="mass-erase",
                grant_id="mass-grant",
                board_id=BOARD,
                policy_digest=POLICY,
                binding_digest=DISCLOSURE,
            )
            with self.assertRaisesRegex(ManualPermissionError, r"manual/binding-mismatch"):
                grants.reserve_mass_erase(BOARD, "mass-grant", POLICY, "c" * 64)
            self.assertEqual(grants.status("mass-erase").state, "unlocked")

    def test_concurrent_mass_erase_claims_produce_one_owner_and_one_already_reserved_refusal(
        self,
    ) -> None:
        """AT07: at-most-once safe recovery is explicit even before target I/O starts."""

        with isolated_project() as root:
            grants = ManualPermissionRepository(root, "run-one")
            grants.reset_startup()
            grants.write_unlocked_grant(
                action="mass-erase",
                grant_id="mass-grant",
                board_id=BOARD,
                policy_digest=POLICY,
                binding_digest=DISCLOSURE,
            )
            barrier = threading.Barrier(2)
            claims: list[str] = []
            refusals: list[str] = []

            def reserve() -> None:
                barrier.wait()
                try:
                    claims.append(
                        grants.reserve_mass_erase(BOARD, "mass-grant", POLICY, DISCLOSURE)
                    )
                except ManualPermissionError as exc:
                    refusals.append(exc.code)

            workers = [threading.Thread(target=reserve) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5)
            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(len(claims), 1)
            self.assertEqual(refusals, ["already-reserved"])

    def test_full_raw_recovery_is_refused_without_claiming_the_manual_grant_replaces_full_proofs(
        self,
    ) -> None:
        """AT07/C02: manual recovery scope cannot become a generic full-tier raw bypass."""

        with isolated_project() as root:
            policies = CapabilityPolicyRepository(root)
            profile, memory_map = known_good_full_policy()
            policies.commit(
                BOARD,
                Tier.SETUP_FULL,
                profile_snapshot=profile,
                map_snapshot=memory_map,
                evidence={"identity": "compatible"},
            )
            with self.assertRaises(TierRouteRefusal) as raised:
                TierRouter(policies).require_raw("target_unlock_raw", BOARD)
            self.assertEqual(raised.exception.code, "tier/wrong-route")
            self.assertIn("target_unlock", raised.exception.remedies)

    def test_public_replacement_null_disclosure_abandons_an_approved_reserved_claim(self) -> None:
        """AT07: a new disclosure kills, rather than recycles, a prior destructive reservation."""

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                _public_disclosure_and_reservation(self, server, manual)
                reserved = manual.status("mass-erase")
                self.assertEqual(reserved.state, "reserved")
                self.assertIsNotNone(reserved.claim_id)

                replacement = _public_json_reply(
                    server, "target_unlock-plan", _unlock_fields(permission=None)
                )
                self.assertEqual(replacement["status"], "unlock_permission_requested", replacement)
                # Abandon means the old human authorization is dead, not restored
                # to an unlocked state that an old caller could reuse.
                self.assertEqual(manual.status("mass-erase").state, "locked")
                self.assertIsNone(manual.status("mass-erase").claim_id)
                self.assertFalse(any(call[0] == "recover" for call in backend.calls))
            finally:
                server.disconnect(BOARD)

    def test_public_changed_execution_parameters_abandon_reserved_claim_before_io(self) -> None:
        """AT07: immutable target_unlock arguments reject and invalidate their manual claim."""

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                _public_disclosure_and_reservation(self, server, manual)
                with self.assertRaisesRegex(
                    Exception, "parameters differ from the immutable plan binding"
                ) as raised:
                    asyncio.run(
                        server.mcp.call_tool(
                            "target_unlock",
                            {"board_id": BOARD, "recovery_mechanism": "not-the-approved-mechanism"},
                        )
                    )
                self.assertIn("parameters differ", str(raised.exception))
                self.assertEqual(manual.status("mass-erase").state, "locked")
                self.assertFalse(any(call[0] == "recover" for call in backend.calls))
                with self.assertRaisesRegex(Exception, "unlock/approval-inactive"):
                    asyncio.run(
                        server.mcp.call_tool(
                            "target_unlock",
                            {"board_id": BOARD, "recovery_mechanism": "backend_mass_erase"},
                        )
                    )
            finally:
                server.disconnect(BOARD)

    def test_public_plan_submit_failure_stays_primary_when_abandon_cleanup_fails(self) -> None:
        """AT07: failure to release a just-reserved claim cannot disguise the plan failure."""

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                public_text(server, "target_unlock-plan", _unlock_null_fields())
                disclosure = _public_json_reply(
                    server, "target_unlock-plan", _unlock_fields(permission=None)
                )
                grant = disclosure["manual_grant"]
                self.assertIsInstance(grant, dict, disclosure)
                assert isinstance(grant, dict)
                manual.write_unlocked_grant(
                    action="mass-erase",
                    grant_id="recovery-grant",
                    board_id=BOARD,
                    policy_digest=str(grant["policy_digest"]),
                    binding_digest=str(grant["binding_digest"]),
                )
                # The plan-engine refusal happens after the manual reservation.
                # Its public code must remain visible even if cleanup also faults.
                with (
                    patch.object(
                        server.plan_engine,
                        "submit",
                        side_effect=RuntimeError("primary simulated plan submit failure"),
                    ),
                    patch.object(
                        manual,
                        "abandon_mass_erase",
                        side_effect=RuntimeError("secondary simulated abandon failure"),
                    ),
                ):
                    with self.assertRaisesRegex(
                        Exception, "primary simulated plan submit failure"
                    ) as raised:
                        asyncio.run(
                            server.mcp.call_tool(
                                "target_unlock-plan", _unlock_fields(permission="one-time")
                            )
                        )
                self.assertNotIn("secondary simulated abandon failure", str(raised.exception))
                self.assertEqual(manual.status("mass-erase").state, "reserved")
            finally:
                server.disconnect(BOARD)

    def test_public_backend_recovery_failure_consumes_grant_and_disconnects_the_board(self) -> None:
        """AT07: even a failed destructive attempt spends authority and revokes old lifecycle state."""

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(
                recovery_supported=True,
                recover_error=RuntimeError("simulated backend recovery failure"),
            )
            manual = _configure_public_native_lite(server, root, backend)
            _public_disclosure_and_reservation(self, server, manual)

            with self.assertRaisesRegex(Exception, "simulated backend recovery failure"):
                asyncio.run(
                    server.mcp.call_tool(
                        "target_unlock",
                        {"board_id": BOARD, "recovery_mechanism": "backend_mass_erase"},
                    )
                )
            self.assertEqual(manual.status("mass-erase").state, "consumed")
            self.assertTrue(any(call[0] == "recover" for call in backend.calls))
            self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
            self.assertIsNone(server.gate_manager.snapshot(BOARD))
            with self.assertRaisesRegex(Exception, "target_unlock.*locked"):
                asyncio.run(
                    server.mcp.call_tool(
                        "target_unlock",
                        {"board_id": BOARD, "recovery_mechanism": "backend_mass_erase"},
                    )
                )

    def test_public_safe_consume_failure_abandons_claim_before_backend_and_keeps_session(
        self,
    ) -> None:
        """M01: a grant that cannot be spent leaves no stale reservation and no erase."""

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                _public_disclosure_and_reservation(self, server, manual)
                with patch.object(
                    manual,
                    "consume_mass_erase",
                    side_effect=ManualPermissionError("stale-policy", "simulated policy change"),
                ):
                    with self.assertRaisesRegex(Exception, "simulated policy change"):
                        asyncio.run(
                            server.mcp.call_tool(
                                "target_unlock",
                                {"board_id": BOARD, "recovery_mechanism": "backend_mass_erase"},
                            )
                        )
                self.assertEqual(manual.status("mass-erase").state, "locked")
                self.assertEqual(_recover_calls(backend), 0)
                self.assertIsNotNone(server.connection_manager.maybe_connection(BOARD))
            finally:
                _disconnect_if_connected(server, BOARD)

    def test_public_failed_attempt_report_error_still_revokes_readiness(self) -> None:
        """M01: an uncertain erase must disconnect even if its failure report is unwritable."""

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(
                recovery_supported=True,
                recover_error=RuntimeError("simulated backend recovery failure"),
            )
            manual = _configure_public_native_lite(server, root, backend)
            _public_disclosure_and_reservation(self, server, manual)
            with patch.object(
                server._report_writer,
                "create_target_unlock",
                side_effect=OSError("simulated report disk failure"),
            ):
                with self.assertRaisesRegex(Exception, "simulated backend recovery failure"):
                    asyncio.run(
                        server.mcp.call_tool(
                            "target_unlock",
                            {"board_id": BOARD, "recovery_mechanism": "backend_mass_erase"},
                        )
                    )
            self.assertEqual(manual.status("mass-erase").state, "consumed")
            self.assertEqual(_recover_calls(backend), 1)
            self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
            self.assertIsNone(server.gate_manager.snapshot(BOARD))

    def test_public_lite_null_recovery_refuses_native_safe_route_without_full_profile_fallback(
        self,
    ) -> None:
        """AT07: missing lite recovery evidence is a native-safe denial, not a full-policy parser error."""

        with isolated_project() as root, tiered_test_server(root) as server:
            policies = CapabilityPolicyRepository(root)
            policies.commit(
                BOARD,
                Tier.SETUP_LITE,
                map_snapshot=confirmed_lite_policy(BOARD),
                evidence={"confirmed": "no native recovery"},
            )
            backend = DeterministicTargetBackend(recovery_supported=True)
            server.configure_tiered_test_seams(backend=backend, policy_repository=policies)
            handle = backend.open(board=None, unique_id="recovery-probe", target="nrf52840")
            server._promote_open_session(BOARD, handle, gate_reason="acceptance recovery fixture")
            try:
                public_text(server, "target_unlock-plan", _unlock_null_fields())
                with self.assertRaisesRegex(
                    Exception, "setup-lite policy documents no native"
                ) as raised:
                    asyncio.run(
                        server.mcp.call_tool("target_unlock-plan", _unlock_fields(permission=None))
                    )
                message = str(raised.exception)
                self.assertIn("setup-full", message)
                self.assertNotIn("profile", message.lower())
                self.assertFalse(any(call[0] == "recover" for call in backend.calls))
            finally:
                server.disconnect(BOARD)

    def test_wrong_board_or_claim_cannot_revoke_an_existing_mass_erase_reservation(self) -> None:
        """AT07: failed cleanup identities leave the rightful reserved grant untouched."""

        with isolated_project() as root:
            grants = ManualPermissionRepository(root, "run-one")
            grants.reset_startup()
            grants.write_unlocked_grant(
                action="mass-erase",
                grant_id="mass-grant",
                board_id=BOARD,
                policy_digest=POLICY,
                binding_digest=DISCLOSURE,
            )
            claim = grants.reserve_mass_erase(BOARD, "mass-grant", POLICY, DISCLOSURE)
            grants.abandon_mass_erase("another_board", claim)
            self.assertEqual(grants.status("mass-erase").state, "reserved")
            grants.abandon_mass_erase(BOARD, "0" * 32)
            still_reserved = grants.status("mass-erase")
            self.assertEqual(still_reserved.state, "reserved")
            self.assertEqual(still_reserved.claim_id, claim)


class TieredRawRecoveryAuthorizationTests(unittest.TestCase):
    """M01: raw recovery is a mass-erase entry point and needs the same fresh human grant."""

    def test_public_raw_recovery_without_grant_refuses_before_backend_and_keeps_session(
        self,
    ) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_no_setup_raw(server, root, backend)
            try:
                connection = server.connection_manager.maybe_connection(RAW_BOARD)
                gate = server.gate_manager.snapshot(RAW_BOARD)
                refusal = _raw_recovery(server)
                self.assertEqual(refusal["status"], "refused", refusal)
                self.assertEqual(refusal["code"], "manual/locked")
                self.assertFalse(refusal["attempted"])
                self.assertEqual(refusal["tier"], "no-setup")
                self.assertEqual(refusal["disclosure"]["containment"], "none")
                self.assertTrue(refusal["disclosure"]["all_nonvolatile_erased"])
                self.assertEqual(refusal["live_identity"]["probe_identity"], "raw-recovery-probe")
                grant = refusal["manual_grant"]
                self.assertEqual((grant["action"], grant["state"]), ("mass-erase", "locked"))
                self.assertEqual(_recover_calls(backend), 0)
                self.assertEqual(manual.status("mass-erase").state, "locked")
                self.assertIs(server.connection_manager.maybe_connection(RAW_BOARD), connection)
                self.assertEqual(server.gate_manager.snapshot(RAW_BOARD), gate)
            finally:
                _disconnect_if_connected(server, RAW_BOARD)

    def test_public_raw_recovery_spends_one_exact_grant_then_refuses_replay(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_no_setup_raw(server, root, backend)
            try:
                _human_grants_raw_refusal(manual, _raw_recovery(server))
                recovered = _raw_recovery(server)
                self.assertEqual(recovered["status"], "ok", recovered)
                self.assertEqual(_recover_calls(backend), 1)
                self.assertEqual(manual.status("mass-erase").state, "consumed")
                self.assertIsNone(server.connection_manager.maybe_connection(RAW_BOARD))
                self.assertIsNone(server.gate_manager.snapshot(RAW_BOARD))

                _promote_raw_session(server, backend)
                replay = _raw_recovery(server)
                self.assertEqual(replay["status"], "refused", replay)
                self.assertEqual(replay["code"], "manual/already-consumed")
                self.assertEqual(_recover_calls(backend), 1)
                self.assertIsNotNone(server.connection_manager.maybe_connection(RAW_BOARD))
            finally:
                _disconnect_if_connected(server, RAW_BOARD)

    def test_public_raw_grant_for_a_previous_session_cannot_authorize_a_reconnect(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_no_setup_raw(server, root, backend)
            try:
                first = _raw_recovery(server)
                _human_grants_raw_refusal(manual, first)
                server.disconnect(RAW_BOARD)
                _promote_raw_session(server, backend)
                changed = _raw_recovery(server)
                self.assertEqual(changed["status"], "refused", changed)
                self.assertEqual(changed["code"], "manual/binding-mismatch")
                self.assertNotEqual(
                    changed["manual_grant"]["binding_digest"],
                    first["manual_grant"]["binding_digest"],
                )
                self.assertEqual(_recover_calls(backend), 0)
                self.assertEqual(manual.status("mass-erase").state, "unlocked")
                self.assertIsNotNone(server.connection_manager.maybe_connection(RAW_BOARD))
            finally:
                _disconnect_if_connected(server, RAW_BOARD)

    def test_public_raw_backend_failure_spends_grant_and_revokes_the_session(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(
                recovery_supported=True,
                recover_error=RuntimeError("simulated raw erase failure"),
            )
            manual = _configure_public_no_setup_raw(server, root, backend)
            try:
                _human_grants_raw_refusal(manual, _raw_recovery(server))
                failed = _raw_recovery(server)
                self.assertEqual(failed["status"], "error", failed)
                self.assertEqual(failed["code"], "backend/recovery-failed")
                self.assertIn("simulated raw erase failure", failed["message"])
                self.assertEqual(_recover_calls(backend), 1)
                self.assertEqual(manual.status("mass-erase").state, "consumed")
                self.assertIsNone(server.connection_manager.maybe_connection(RAW_BOARD))
                self.assertIsNone(server.gate_manager.snapshot(RAW_BOARD))
            finally:
                _disconnect_if_connected(server, RAW_BOARD)

    def test_public_safe_grant_for_a_previous_session_cannot_be_reserved_after_reconnect(
        self,
    ) -> None:
        """A reconnect keeps the probe-derived connection id but must still void the grant."""

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                public_text(server, "target_unlock-plan", _unlock_null_fields())
                first = _public_json_reply(
                    server, "target_unlock-plan", _unlock_fields(permission=None)
                )
                first_grant = first["manual_grant"]
                assert isinstance(first_grant, dict)
                manual.write_unlocked_grant(
                    action="mass-erase",
                    grant_id="safe-grant",
                    board_id=BOARD,
                    policy_digest=first_grant["policy_digest"],
                    binding_digest=first_grant["binding_digest"],
                )
                server.disconnect(BOARD)
                handle = backend.open(board=None, unique_id="recovery-probe", target="nrf52840")
                server._promote_open_session(BOARD, handle, gate_reason="reconnected fixture")

                public_text(server, "target_unlock-plan", _unlock_null_fields())
                second = _public_json_reply(
                    server, "target_unlock-plan", _unlock_fields(permission=None)
                )
                second_grant = second["manual_grant"]
                assert isinstance(second_grant, dict)
                self.assertNotEqual(second_grant["binding_digest"], first_grant["binding_digest"])
                with self.assertRaisesRegex(Exception, "manual/binding-mismatch"):
                    public_text(server, "target_unlock-plan", _unlock_fields(permission="one-time"))
                self.assertEqual(manual.status("mass-erase").state, "unlocked")
                with self.assertRaisesRegex(Exception, "target_unlock"):
                    asyncio.run(
                        server.mcp.call_tool(
                            "target_unlock",
                            {"board_id": BOARD, "recovery_mechanism": "backend_mass_erase"},
                        )
                    )
                self.assertEqual(_recover_calls(backend), 0)
                self.assertIsNotNone(server.connection_manager.maybe_connection(BOARD))
            finally:
                _disconnect_if_connected(server, BOARD)

    def test_public_safe_route_grant_cannot_authorize_lite_raw_recovery(self) -> None:
        """The raw route's uncontained scope is a different disclosure than the safe plan's."""

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(recovery_supported=True)
            manual = _configure_public_native_lite(server, root, backend)
            try:
                public_text(server, "target_unlock-plan", _unlock_null_fields())
                disclosure = _public_json_reply(
                    server, "target_unlock-plan", _unlock_fields(permission=None)
                )
                safe_grant = disclosure["manual_grant"]
                assert isinstance(safe_grant, dict)
                manual.write_unlocked_grant(
                    action="mass-erase",
                    grant_id="safe-grant",
                    board_id=BOARD,
                    policy_digest=safe_grant["policy_digest"],
                    binding_digest=safe_grant["binding_digest"],
                )
                refusal = _raw_recovery(server, BOARD)
                self.assertEqual(refusal["status"], "refused", refusal)
                self.assertEqual(refusal["code"], "manual/binding-mismatch")
                self.assertIn("warning", refusal, "lite raw responses carry the bypass warning")
                self.assertNotEqual(
                    refusal["manual_grant"]["binding_digest"], safe_grant["binding_digest"]
                )
                self.assertEqual(_recover_calls(backend), 0)
                self.assertEqual(manual.status("mass-erase").state, "unlocked")
                self.assertIsNotNone(server.connection_manager.maybe_connection(BOARD))
            finally:
                _disconnect_if_connected(server, BOARD)


if __name__ == "__main__":
    unittest.main()
