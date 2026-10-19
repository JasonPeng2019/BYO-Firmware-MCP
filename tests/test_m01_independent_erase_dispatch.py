"""Independent M01 proof through registered MCP tools and real on-disk grants.

No probe is opened: only the target-control backend is replaced. Session/gate
fixtures establish starting state; all disclosure, approval and erase requests
cross FastMCP dispatch. These assertions prove host authority, never flash state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import unittest
from dataclasses import replace
from typing import Any

from pyocd_debug_mcp.capabilities.manual_permissions import ManualPermissionRepository
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier

from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    known_good_full_policy,
    public_call,
    public_text,
    tiered_test_server,
)


BOARD = "legacy_full"
MECHANISM = "backend_mass_erase"


def plan_fields(permission: str | None = None) -> dict[str, Any]:
    return {
        "board_id": BOARD,
        "hypothesis": "Documented recovery is needed to regain debug access.",
        "hypothesis_made": True,
        "strategy": "Attempt the disclosed backend whole-device recovery once.",
        "strategy_evaluated": True,
        "expected_fail_return": "Uncertain effects require reconnect and revalidation.",
        "expected_success_return": "Reconnect and validate before further target use.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "action_parameters": {"recovery_mechanism": MECHANISM},
        "user_permission": permission,
    }


def lite_map() -> dict[str, Any]:
    # Complete flash geometry is required for a truthful native lite disclosure.
    return {
        "board_id": BOARD,
        "regions": [
            {
                "name": "flash",
                "kind": "physical_flash",
                "start": 0,
                "end": 65536,
                "readable": True,
                "writable": False,
                "executable": False,
                "source_pages": [1],
                "source_note": "fixture vendor flash geometry",
            },
            {
                "name": "RAM",
                "kind": "ram",
                "start": 0x20000000,
                "end": 0x20001000,
                "readable": True,
                "writable": True,
                "executable": False,
                "source_pages": [2],
                "source_note": "fixture vendor RAM geometry",
            },
        ],
        "flash": {
            "backend_target": "nrf52840",
            "erase_sectors": [{"start": 0, "end": 65536, "bank": "main"}],
        },
        "recovery": {
            "mechanism": MECHANISM,
            "source_pages": [3],
            "source_note": "fixture vendor recovery primitive",
        },
    }


class ObservedEraseBackend(DeterministicTargetBackend):
    """Observe durable authority at the hardware attempt, with controlled faults."""

    def __init__(self) -> None:
        super().__init__(recovery_supported=True)
        self.before_erase = lambda: None
        self.fault: BaseException | None = None
        self.events: list[str] = []

    def recover(self, handle: Any) -> None:
        self.calls.append(("recover", handle.probe_uid))
        self.events.append("backend-enter")
        self.before_erase()
        if self.fault is not None:
            raise self.fault

    def close(self, handle: Any) -> None:
        self.events.append("backend-close")
        super().close(handle)


class EraseDispatchProof(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, *, raw_only: bool = False):  # type: ignore[no-untyped-def]
        with isolated_project() as root, tiered_test_server(root) as server:
            policies = CapabilityPolicyRepository(root)
            profile, _memory_map = known_good_full_policy()
            if raw_only:
                policies.resolve(BOARD)
            else:
                policies.commit(
                    BOARD,
                    Tier.SETUP_LITE,
                    profile_snapshot=profile,
                    map_snapshot=lite_map(),
                    evidence={"confirmed": "independent M01 fixture"},
                )
            manual = ManualPermissionRepository(root, server.server_run.run_id)
            manual.reset_startup()
            backend = ObservedEraseBackend()
            server.configure_tiered_test_seams(
                backend=backend,
                policy_repository=policies,
                manual_permission_repository=manual,
            )

            def connect() -> Any:
                handle = backend.open(board=None, unique_id="M01-probe", target="nrf52840")
                handle = replace(
                    handle, metadata=replace(handle.metadata, live_part_number="nRF52840")
                )
                connection = server._promote_open_session(
                    BOARD,
                    handle,
                    gate_reason="independent fixture starting session",
                )
                server.gate_manager.stamp_validation(
                    board_id=BOARD,
                    connection_id=connection.connection_id,
                    probe_identity="M01-probe",
                    observed_mcu="nRF52840",
                    validation_run="fixture-validation",
                    map_digest=str(policies.resolve(BOARD).policy_digest),
                )
                return connection

            connect()
            try:
                yield server, manual, backend, connect
            finally:
                if server.connection_manager.maybe_connection(BOARD) is not None:
                    public_text(server, "disconnect", {"board_id": BOARD})

    def raw(self, server: Any) -> dict[str, Any]:
        return public_call(
            server,
            "target_unlock_raw",
            {
                "board_id": BOARD,
                "recovery_mechanism": MECHANISM,
            },
        )

    def disclose(self, server: Any, route: str) -> dict[str, Any]:
        if route == "raw":
            response = self.raw(server)
            self.assertEqual(response["status"], "refused", response)
            self.assertFalse(response["attempted"], response)
            return response["manual_grant"]
        public_text(server, "target_unlock-plan", dict.fromkeys(plan_fields()))
        response = json.loads(public_text(server, "target_unlock-plan", plan_fields()))
        self.assertEqual(response["status"], "unlock_permission_requested", response)
        return response["manual_grant"]

    def grant(self, manual: Any, disclosure: dict[str, Any], **changes: Any) -> None:
        manual.write_unlocked_grant(
            **{
                "action": "mass-erase",
                "grant_id": "independent-grant",
                "board_id": BOARD,
                "policy_digest": disclosure["policy_digest"],
                "binding_digest": disclosure["binding_digest"],
                **changes,
            }
        )

    def approve(self, server: Any, manual: Any) -> None:
        response = json.loads(public_text(server, "target_unlock-plan", plan_fields("one-time")))
        self.assertEqual(response["status"], "unlock_plan_approved", response)
        self.assertEqual(manual.status("mass-erase").state, "reserved")

    def attempt(self, server: Any, route: str) -> Any:
        if route == "raw":
            return self.raw(server)
        return public_text(
            server,
            "target_unlock",
            {
                "board_id": BOARD,
                "recovery_mechanism": MECHANISM,
            },
        )

    def erase_count(self, backend: Any) -> int:
        return sum(call[0] == "recover" for call in backend.calls)

    def assert_healthy(self, server: Any, connection: Any, stamp: Any) -> None:
        self.assertIs(server.connection_manager.maybe_connection(BOARD), connection)
        self.assertEqual(server.gate_manager.snapshot(BOARD), stamp)
        read = public_call(server, "read_memory_raw", {"board_id": BOARD, "address": 0x20000000})
        self.assertEqual(read["status"], "ok", read)
        self.assertEqual(read["result"], "0x12345678")

    def test_invalid_authority_refuses_both_routes_without_erase_or_session_loss(self) -> None:
        for route in ("raw", "guarded"):
            for invalid in (
                "absent",
                "stale-policy",
                "wrong-target",
                "changed-scope",
                "changed-session",
                "stale-epoch",
            ):
                with self.subTest(route=route, invalid=invalid), self.fixture() as fixture:
                    server, manual, backend, connect = fixture
                    disclosure = self.disclose(server, route)
                    if invalid == "changed-scope":
                        other = self.disclose(server, "guarded" if route == "raw" else "raw")
                        self.assertNotEqual(disclosure["binding_digest"], other["binding_digest"])
                        self.grant(manual, other)
                    elif invalid != "absent":
                        changes = {}
                        if invalid == "stale-policy":
                            changes["policy_digest"] = "0" * 64
                        if invalid == "wrong-target":
                            changes["board_id"] = "different_target"
                        self.grant(manual, disclosure, **changes)
                    if invalid in {"changed-session", "stale-epoch"}:
                        previous = server.connection_manager.connection_for(BOARD)
                        public_text(server, "disconnect", {"board_id": BOARD})
                        if invalid == "stale-epoch":
                            restarted = ManualPermissionRepository(manual.project_root, "new-epoch")
                            restarted.reset_startup()
                        current = connect()
                        self.assertEqual(current.connection_id, previous.connection_id)
                        self.assertNotEqual(
                            current.runtime_session.session_id, previous.runtime_session.session_id
                        )
                        if route == "guarded":
                            self.disclose(server, route)
                    connection = server.connection_manager.connection_for(BOARD)
                    stamp = server.gate_manager.snapshot(BOARD)
                    if route == "raw":
                        refusal = self.raw(server)
                        self.assertEqual(refusal["status"], "refused", refusal)
                        self.assertFalse(refusal["attempted"], refusal)
                        self.assertIn(
                            refusal["code"],
                            {
                                "manual/locked",
                                "manual/stale-policy",
                                "manual/wrong-board",
                                "manual/binding-mismatch",
                                "manual/stale-epoch",
                            },
                        )
                    else:
                        with self.assertRaisesRegex(Exception, "manual/"):
                            self.approve(server, manual)
                        with self.assertRaises(Exception):
                            self.attempt(server, route)
                    self.assertEqual(self.erase_count(backend), 0)
                    self.assert_healthy(server, connection, stamp)

    def test_no_setup_missing_grant_never_reaches_backend(self) -> None:
        """The original F01/F02 bypass must fail on the zero-attempt oracle."""
        with self.fixture(raw_only=True) as fixture:
            server, manual, backend, _connect = fixture
            connection = server.connection_manager.connection_for(BOARD)
            stamp = server.gate_manager.snapshot(BOARD)
            response = self.raw(server)
            self.assertEqual(self.erase_count(backend), 0, response)
            self.assertEqual(manual.status("mass-erase").state, "locked")
            self.assertEqual(response["code"], "manual/locked", response)
            self.assertFalse(response["attempted"])
            self.assert_healthy(server, connection, stamp)

    def test_attempt_spends_exact_grant_before_backend_and_revokes_after_every_outcome(
        self,
    ) -> None:
        for route in ("raw", "guarded"):
            for outcome in ("success", "uncertain-error", "cancelled"):
                with self.subTest(route=route, outcome=outcome), self.fixture() as fixture:
                    server, manual, backend, connect = fixture
                    disclosure = self.disclose(server, route)
                    self.grant(manual, disclosure)
                    if route == "guarded":
                        self.approve(server, manual)
                    self.assertIsNotNone(server.gate_manager.snapshot(BOARD))

                    def at_attempt() -> None:
                        self.assertEqual(manual.status("mass-erase").state, "consumed")
                        self.assertIsNotNone(server.connection_manager.maybe_connection(BOARD))
                        if route == "guarded":
                            self.assertIsNone(server.gate_manager.snapshot(BOARD))
                        backend.events.append("authority-observed-consumed")

                    backend.before_erase = at_attempt
                    if outcome == "uncertain-error":
                        backend.fault = RuntimeError("erase transport lost; effects unknown")
                    elif outcome == "cancelled":
                        backend.fault = asyncio.CancelledError("cancel at backend boundary")
                    if outcome == "cancelled":
                        with self.assertRaises(asyncio.CancelledError):
                            self.attempt(server, route)
                    elif outcome == "uncertain-error" and route == "guarded":
                        with self.assertRaisesRegex(Exception, "effects unknown"):
                            self.attempt(server, route)
                    else:
                        response = self.attempt(server, route)
                        if route == "raw":
                            self.assertEqual(
                                response["status"],
                                "ok" if outcome == "success" else "error",
                                response,
                            )
                        else:
                            self.assertIn("Target unlock completed", response)
                    self.assertEqual(self.erase_count(backend), 1)
                    self.assertEqual(
                        backend.events,
                        ["backend-enter", "authority-observed-consumed", "backend-close"],
                    )
                    self.assertEqual(manual.status("mass-erase").state, "consumed")
                    self.assertIsNone(server.connection_manager.maybe_connection(BOARD))
                    self.assertIsNone(server.gate_manager.snapshot(BOARD))
                    # Reconnect the same probe and try the spent disk grant through
                    # public dispatch. Refusal must preserve this new healthy session.
                    connection = connect()
                    stamp = server.gate_manager.snapshot(BOARD)
                    if route == "guarded":
                        self.disclose(server, route)
                        with self.assertRaisesRegex(Exception, "manual/already-consumed"):
                            self.approve(server, manual)
                        with self.assertRaises(Exception):
                            self.attempt(server, route)
                    else:
                        replay = self.raw(server)
                        self.assertEqual(replay["code"], "manual/already-consumed", replay)
                        self.assertFalse(replay["attempted"])
                    self.assertEqual(self.erase_count(backend), 1)
                    self.assert_healthy(server, connection, stamp)

    def test_replacement_and_disconnect_cancel_reserved_safe_authority_before_attempt(self) -> None:
        for cancellation in ("replacement", "disconnect", "parameter-drift"):
            with self.subTest(cancellation=cancellation), self.fixture() as fixture:
                server, manual, backend, connect = fixture
                self.grant(manual, self.disclose(server, "guarded"))
                self.approve(server, manual)
                if cancellation == "replacement":
                    fields = plan_fields()
                    fields["strategy"] = "Replace the disclosed recovery plan after review."
                    response = json.loads(public_text(server, "target_unlock-plan", fields))
                    self.assertEqual(response["status"], "unlock_permission_requested")
                elif cancellation == "disconnect":
                    public_text(server, "disconnect", {"board_id": BOARD})
                    connect()
                else:
                    with self.assertRaisesRegex(Exception, "parameters differ"):
                        public_text(
                            server,
                            "target_unlock",
                            {
                                "board_id": BOARD,
                                "recovery_mechanism": "changed-mechanism",
                            },
                        )
                connection = server.connection_manager.connection_for(BOARD)
                stamp = server.gate_manager.snapshot(BOARD)
                self.assertEqual(manual.status("mass-erase").state, "locked")
                self.assertIsNone(manual.status("mass-erase").claim_id)
                with self.assertRaises(Exception):
                    self.attempt(server, "guarded")
                self.assertEqual(self.erase_count(backend), 0)
                self.assert_healthy(server, connection, stamp)

    def test_changed_committed_scope_invalidates_safe_validation_before_backend(self) -> None:
        """An approved plan must revalidate actual committed geometry before use."""
        with self.fixture() as fixture:
            server, manual, backend, _connect = fixture
            self.grant(manual, self.disclose(server, "guarded"))
            self.approve(server, manual)
            connection = server.connection_manager.connection_for(BOARD)
            stamp = server.gate_manager.snapshot(BOARD)
            changed_map = lite_map()
            changed_map["regions"][0]["end"] = 131072
            changed_map["flash"]["erase_sectors"][0]["end"] = 131072
            profile, _memory_map = known_good_full_policy()
            CapabilityPolicyRepository(manual.project_root).commit(
                BOARD,
                Tier.SETUP_LITE,
                profile_snapshot=profile,
                map_snapshot=changed_map,
                evidence={"confirmed": "changed physical flash scope"},
            )
            with self.assertRaisesRegex(Exception, "changed before execution"):
                self.attempt(server, "guarded")
            self.assertEqual(self.erase_count(backend), 0)
            self.assertEqual(manual.status("mass-erase").state, "locked")
            self.assert_healthy(server, connection, stamp)


if __name__ == "__main__":
    unittest.main()
