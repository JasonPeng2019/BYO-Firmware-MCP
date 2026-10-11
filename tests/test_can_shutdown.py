"""Production-composed cancellation and process-shutdown CAN regressions."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.adapters.can_interface import (
    CanBackendCapabilities,
    CanBusHandle,
    CanDirection,
    CanFrame,
    CapabilityStatus,
)
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.can_errors import CanTimeoutError
from tests.tiered_acceptance_support import confirmed_lite_policy, known_good_full_policy

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PROFILE = ROOT / "fixtures" / "can" / "virtual_tx_allowed.yaml"
BOARD = "legacy_full"


def _text(result: object) -> str:
    if not isinstance(result, list) or not result:
        raise AssertionError(f"unexpected MCP result: {result!r}")
    value = getattr(result[0], "text", None)
    if not isinstance(value, str):
        raise AssertionError(f"unexpected MCP content: {result!r}")
    return value


def _assert_canonical_json(test: unittest.TestCase, text: str) -> dict[str, object]:
    payload = json.loads(text)
    test.assertEqual(text, json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return payload


def _capabilities() -> CanBackendCapabilities:
    supported = CapabilityStatus.SUPPORTED
    return CanBackendCapabilities(
        bounded_open=supported,
        bounded_send=supported,
        bounded_receive=supported,
        bounded_close=supported,
        pending_tx_quiescence=supported,
        physical=False,
        pending_tx_empty=True,
        driver="virtual",
        device_identity=("registry-boundary-worker",),
        listen_only=False,
        one_shot=True,
        loopback=False,
        bus_state="active",
    )


class BoundaryWorker:
    """Backend seam that blocks at one selected production-manager boundary."""

    def __init__(self, operation: str, profile_sha256: str) -> None:
        self.operation = operation
        self.profile_sha256 = profile_sha256
        self.cleanup_uncertain = False
        self.last_escalation_stage = "none"
        self.last_cleanup_outcome = "none"
        self.started = threading.Event()
        self.inspect_calls = 0
        self.open_calls = 0
        self.receive_calls = 0
        self.send_calls = 0
        self.cancel_calls = 0
        self.inner_timeout_operation: str | None = None

    async def _block(self) -> None:
        self.started.set()
        await asyncio.sleep(3600)

    async def inspect(self, *, timeout_seconds: float) -> CanBackendCapabilities:
        del timeout_seconds
        self.inspect_calls += 1
        if self.operation == "connect":
            await self._block()
        return _capabilities()

    async def open(self, *, timeout_seconds: float) -> CanBusHandle:
        del timeout_seconds
        self.open_calls += 1
        return CanBusHandle(
            native_handle=object(),
            profile_id="virtual_tx_allowed",
            profile_sha256=self.profile_sha256,
            interface="virtual",
            channel="byo-tx-allowed",
            protocol="classic",
            filters=(),
        )

    async def send(
        self,
        frame: CanFrame,
        *,
        timeout_seconds: float,
        operation_timeout_seconds: float,
        not_after_monotonic: float | None = None,
    ) -> CanFrame:
        del timeout_seconds, operation_timeout_seconds, not_after_monotonic
        self.send_calls += 1
        if self.inner_timeout_operation == "send":
            self.started.set()
            self.last_escalation_stage = "cleanup-acknowledged"
            self.last_cleanup_outcome = "closed"
            raise CanTimeoutError("injected bounded send timeout")
        if self.operation == "send":
            await self._block()
        return CanFrame(
            frame.arbitration_id,
            frame.extended,
            frame.data,
            direction=CanDirection.TX,
            channel="byo-tx-allowed",
        )

    async def receive(
        self, *, timeout_seconds: float, operation_timeout_seconds: float
    ) -> CanFrame | None:
        del timeout_seconds, operation_timeout_seconds
        self.receive_calls += 1
        if self.inner_timeout_operation == "read":
            self.started.set()
            self.last_escalation_stage = "cleanup-acknowledged"
            self.last_cleanup_outcome = "closed"
            raise CanTimeoutError("injected bounded receive timeout")
        if self.operation == "read":
            await self._block()
        return None

    async def close(self, *, timeout_seconds: float) -> None:
        del timeout_seconds
        self.last_escalation_stage = "graceful-shutdown"
        self.last_cleanup_outcome = "closed"

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self.last_escalation_stage = "cleanup-acknowledged"
        self.last_cleanup_outcome = "closed"


class ProductionCanHarness:
    """Real server composition with only the CAN adapter worker replaced."""

    def __init__(self, operation: str) -> None:
        self.temporary = tempfile.mkdtemp(prefix="byo-can-registry-")
        self.root = Path(self.temporary)
        self.profile = self.root / "can.yaml"
        self.profile.write_text(
            SOURCE_PROFILE.read_text(encoding="utf-8").replace("nucleo_l476rg", BOARD),
            encoding="utf-8",
        )
        self.environment = patch.dict(
            os.environ,
            {
                "BYO_MCP_ARTIFACT_ROOT": str(self.root),
                "BYO_MCP_MONITOR_ROOT": str(self.root / "monitor"),
                "BYO_MCP_TEST_SEAMS": "1",
                "PYOCD_CAN_MODE": "available",
                "PYOCD_CAN_CONFIG": str(self.profile),
                "PYOCD_CAN_CONFIG_SHA256": "",
                "PYOCD_CAN_RUNTIME_SHA256": "",
                "PYOCD_CAN_READ_SECONDS": "",
            },
            clear=False,
        )
        self.environment.start()
        sys.modules.pop("pyocd_debug_mcp.server", None)
        self.server: ModuleType = importlib.import_module("pyocd_debug_mcp.server")

        policies = CapabilityPolicyRepository(self.root)
        self.policies = policies
        profile_snapshot, map_snapshot = known_good_full_policy()
        policies.commit(
            BOARD,
            Tier.SETUP_FULL,
            profile_snapshot=profile_snapshot,
            map_snapshot=map_snapshot,
        )
        self.server.configure_tiered_test_seams(policy_repository=policies)
        # The historical full-policy fixture deliberately carries a retired
        # board-catalog label. Keep the production plan/gate path, but give its
        # unrelated safety-map digest resolver the same deterministic digest
        # stamped below (the established tier acceptance seam).
        self.map_digest = "test-map-digest"
        self.map_digest_patch = patch.object(
            self.server, "_active_snapshot_aggregate", return_value=self.map_digest
        )
        self.map_digest_patch.start()

        self.debug_session = object()
        self.debug_handle = TargetSessionHandle(
            session=self.debug_session,
            board=None,
            probe_uid="probe-1",
            route_used="test",
            target_override="nrf52840",
        )
        self.connection_id = "probe:test:probe-1"
        runtime = self.server._session_store.start_session(
            board_id=BOARD,
            connection_id=self.connection_id,
            probe_uid="probe-1",
            route_used="test",
        )
        self.connection = self.server.connection_manager.assign(
            BOARD,
            self.debug_handle,
            runtime,
            connection_id=self.connection_id,
        )
        self.server.gate_manager.stamp_validation(
            board_id=BOARD,
            connection_id=self.connection_id,
            probe_identity="probe-1",
            observed_mcu="nRF52840",
            validation_run="validation-1",
            map_digest=self.map_digest,
        )
        self.validation = self.server.gate_manager.require_validated(BOARD, self.connection_id)

        service = self.server._can_manager._service
        if service is None:
            raise AssertionError("available CAN startup did not construct a service")
        fixture = self.server._can_manager.startup.fixture
        if fixture is None:
            raise AssertionError("available CAN startup did not load its fixture")
        self.worker = BoundaryWorker(operation, fixture.canonical_sha256)
        service._worker_factory = lambda: self.worker

    async def connect(self) -> None:
        payload = json.loads(
            _text(await self.server.mcp.call_tool("connect_can", {"board_id": BOARD}))
        )
        if payload.get("status") != "connected":
            raise AssertionError(payload)

    async def install_plan(self, operation: str, *, max_calls: int = 2) -> dict[str, object]:
        parameters: dict[str, object]
        if operation == "read":
            parameters = {"read_seconds": 1.0, "max_frames": 1}
        elif operation == "send":
            parameters = {
                "arbitration_id": 0x123,
                "data_hex": "CAFE",
                "extended": False,
                "timeout_seconds": None,
            }
        else:
            raise AssertionError(operation)
        null_plan = {
            "board_id": None,
            "hypothesis": None,
            "strategy": None,
            "hypothesis_made": None,
            "strategy_evaluated": None,
            "expected_fail_return": None,
            "expected_success_return": None,
            "max_calls": None,
            "max_calls_buffer": None,
            "action_parameters": None,
            "user_permission": None,
        }
        plan = {
            "board_id": BOARD,
            "hypothesis": "Cancellation must retire only the CAN generation.",
            "strategy": "Run one bounded operation and cancel at the adapter seam.",
            "hypothesis_made": True,
            "strategy_evaluated": True,
            "expected_fail_return": "CAN closes without changing debug authority.",
            "expected_success_return": "The bounded operation completes.",
            "max_calls": max_calls,
            "max_calls_buffer": 0,
            "action_parameters": parameters,
        }
        await self.server.mcp.call_tool(f"{operation}_can-plan", null_plan)
        await self.server.mcp.call_tool(f"{operation}_can-plan", plan)
        return parameters

    def assert_debug_authority_survives(self, operation: str) -> None:
        self_case = unittest.TestCase()
        self_case.assertIs(self.server.connection_manager.connection_for(BOARD), self.connection)
        self_case.assertIs(self.connection.handle, self.debug_handle)
        self_case.assertIs(self.connection.handle.session, self.debug_session)
        self_case.assertEqual(
            self.server.gate_manager.require_validated(BOARD, self.connection_id),
            self.validation,
        )
        self_case.assertEqual(self.server._can_manager.phase, "closed")
        self_case.assertIsNone(self.server._can_manager.owner_board_id)
        self_case.assertEqual(self.worker.cancel_calls, 1)
        if operation in {"read", "send"}:
            active = self.server.plan_engine.active_plan(f"{operation}_can", BOARD)
            self_case.assertIsNotNone(active)
            assert active is not None
            self_case.assertEqual(active.remaining_calls, 1)

    def close(self) -> None:
        try:
            self.server._can_manager.shutdown_sync()
            self.server._monitor.closeout("test")
        finally:
            sys.modules.pop("pyocd_debug_mcp.server", None)
            self.map_digest_patch.stop()
            self.environment.stop()
            shutil.rmtree(self.root, ignore_errors=True)


class CanRegistryOuterBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, operation: str, *, outer_timeout: bool) -> None:
        harness = ProductionCanHarness(operation)
        try:
            parameters: dict[str, object] = {"board_id": BOARD}
            if operation != "connect":
                await harness.connect()
                parameters.update(await harness.install_plan(operation))

            name = f"{operation}_can"
            if outer_timeout:
                original = harness.server.mcp._timeout_resolver
                harness.server.mcp._timeout_resolver = lambda tool, args: (
                    0.05 if tool == name else original(tool, args)
                )
                with self.assertRaises(ToolError) as caught:
                    await harness.server.mcp.call_tool(name, parameters)
                payload = _assert_canonical_json(self, str(caught.exception))
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["status"], "failed")
                self.assertEqual(payload["code"], "can/operation-timeout")
                self.assertEqual(payload["operation"], name)
                self.assertEqual(payload["board_id"], BOARD)
                self.assertIn("can", payload)
            else:
                task = asyncio.create_task(harness.server.mcp.call_tool(name, parameters))
                self.assertTrue(await asyncio.to_thread(harness.worker.started.wait, 2.0))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            self.assertTrue(harness.worker.started.is_set())
            harness.assert_debug_authority_survives(operation)
        finally:
            harness.close()

    async def test_connect_caller_cancellation_preserves_debug_authority(self) -> None:
        await self.exercise("connect", outer_timeout=False)

    async def test_connect_outer_timeout_preserves_debug_authority(self) -> None:
        await self.exercise("connect", outer_timeout=True)

    async def test_read_caller_cancellation_preserves_plan_and_debug_authority(self) -> None:
        await self.exercise("read", outer_timeout=False)

    async def test_read_outer_timeout_preserves_plan_and_debug_authority(self) -> None:
        await self.exercise("read", outer_timeout=True)

    async def test_send_caller_cancellation_preserves_plan_and_debug_authority(self) -> None:
        await self.exercise("send", outer_timeout=False)

    async def test_send_outer_timeout_preserves_plan_and_debug_authority(self) -> None:
        await self.exercise("send", outer_timeout=True)

    async def test_locked_guard_rechecks_tier_and_validation_after_route_race(self) -> None:
        for operation in ("read", "send"):
            for mutation, expected_code in (
                ("tier", "tier/wrong-route"),
                ("validation", "gate/validation-required"),
            ):
                with self.subTest(operation=operation, mutation=mutation):
                    harness = ProductionCanHarness(operation)
                    try:
                        await harness.connect()
                        parameters: dict[str, object] = {"board_id": BOARD}
                        parameters.update(await harness.install_plan(operation))
                        name = f"{operation}_can"
                        active_before = harness.server.plan_engine.active_plan(name, BOARD)
                        self.assertIsNotNone(active_before)
                        assert active_before is not None
                        budget_before = harness.server._can_manager.budget.snapshot()
                        original_route = harness.server.mcp._route_guards[name]

                        def mutate_after_route(
                            tool_name: str,
                            board_id: str,
                            arguments: dict[str, object],
                        ) -> None:
                            original_route(tool_name, board_id, arguments)
                            if mutation == "tier":
                                harness.policies.commit(
                                    BOARD,
                                    Tier.SETUP_LITE,
                                    map_snapshot=confirmed_lite_policy(BOARD),
                                )
                            else:
                                harness.server.gate_manager.clear(
                                    BOARD, "injected post-route validation race"
                                )

                        harness.server.mcp._route_guards[name] = mutate_after_route
                        with self.assertRaises(ToolError) as caught:
                            await harness.server.mcp.call_tool(name, parameters)
                        refusal = json.loads(str(caught.exception))
                        self.assertEqual(refusal["code"], expected_code)
                        self.assertFalse(harness.worker.started.is_set())
                        self.assertEqual(harness.server._can_manager.phase, "open")
                        self.assertEqual(harness.server._can_manager.owner_board_id, BOARD)
                        self.assertEqual(
                            harness.server._can_manager.budget.snapshot(), budget_before
                        )
                        active_after = harness.server.plan_engine.active_plan(name, BOARD)
                        self.assertIsNotNone(active_after)
                        assert active_after is not None
                        self.assertEqual(
                            active_after.remaining_calls, active_before.remaining_calls
                        )
                    finally:
                        harness.close()

    async def test_actual_board_lock_bounds_admission_without_consuming_state(self) -> None:
        for operation in ("connect", "read", "send"):
            with self.subTest(operation=operation):
                harness = ProductionCanHarness(operation)
                release_lock = threading.Event()
                lock_held = threading.Event()
                holder: threading.Thread | None = None
                try:
                    parameters: dict[str, object] = {"board_id": BOARD}
                    if operation != "connect":
                        await harness.connect()
                        parameters.update(await harness.install_plan(operation))

                    name = f"{operation}_can"
                    active_before = (
                        None
                        if operation == "connect"
                        else harness.server.plan_engine.active_plan(name, BOARD)
                    )
                    budget_before = harness.server._can_manager.budget.snapshot()
                    calls_before = (
                        harness.worker.inspect_calls,
                        harness.worker.open_calls,
                        harness.worker.receive_calls,
                        harness.worker.send_calls,
                    )
                    board_lock = harness.server.connection_manager.lock_for(BOARD)

                    def hold_board_lock() -> None:
                        with board_lock:
                            lock_held.set()
                            release_lock.wait(2.0)

                    holder = threading.Thread(target=hold_board_lock, daemon=True)
                    holder.start()
                    self.assertTrue(await asyncio.to_thread(lock_held.wait, 2.0))

                    original_timeout = harness.server.mcp._timeout_resolver
                    harness.server.mcp._timeout_resolver = lambda tool, args: (
                        0.05 if tool == name else original_timeout(tool, args)
                    )
                    started = asyncio.get_running_loop().time()
                    with self.assertRaises(ToolError) as caught:
                        await harness.server.mcp.call_tool(name, parameters)
                    payload = _assert_canonical_json(self, str(caught.exception))
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["status"], "refused")
                    self.assertEqual(payload["code"], "can/board-busy")
                    self.assertEqual(payload["operation"], name)
                    self.assertEqual(payload["board_id"], BOARD)
                    self.assertIn("can", payload)
                    self.assertLess(asyncio.get_running_loop().time() - started, 0.5)

                    self.assertEqual(
                        (
                            harness.worker.inspect_calls,
                            harness.worker.open_calls,
                            harness.worker.receive_calls,
                            harness.worker.send_calls,
                        ),
                        calls_before,
                    )
                    self.assertFalse(harness.worker.started.is_set())
                    self.assertEqual(harness.server._can_manager.budget.snapshot(), budget_before)
                    if active_before is not None:
                        active_after = harness.server.plan_engine.active_plan(name, BOARD)
                        self.assertIsNotNone(active_after)
                        assert active_after is not None
                        self.assertEqual(
                            active_after.remaining_calls, active_before.remaining_calls
                        )
                    if operation == "connect":
                        self.assertEqual(harness.server._can_manager.phase, "closed")
                        self.assertIsNone(harness.server._can_manager.owner_board_id)
                    else:
                        self.assertEqual(harness.server._can_manager.phase, "open")
                        self.assertEqual(harness.server._can_manager.owner_board_id, BOARD)
                finally:
                    release_lock.set()
                    if holder is not None:
                        await asyncio.to_thread(holder.join, 2.0)
                    harness.close()

    async def test_real_dispatch_plan_mismatch_and_exhaustion_are_prehandler_refusals(
        self,
    ) -> None:
        for operation in ("read", "send"):
            with self.subTest(operation=operation, case="mismatch"):
                harness = ProductionCanHarness("idle")
                try:
                    await harness.connect()
                    parameters: dict[str, object] = {"board_id": BOARD}
                    parameters.update(await harness.install_plan(operation))
                    mismatched = dict(parameters)
                    if operation == "read":
                        mismatched["max_frames"] = 2
                    else:
                        mismatched["data_hex"] = "00"
                    name = f"{operation}_can"
                    budget_before = harness.server._can_manager.budget.snapshot()
                    active_before = harness.server.plan_engine.active_plan(name, BOARD)
                    self.assertIsNotNone(active_before)
                    assert active_before is not None

                    with self.assertRaises(ToolError) as caught:
                        await harness.server.mcp.call_tool(name, mismatched)
                    refusal = json.loads(str(caught.exception))
                    self.assertEqual(refusal["code"], "plan/parameter-mismatch")
                    self.assertEqual(harness.server._can_manager.budget.snapshot(), budget_before)
                    active_after = harness.server.plan_engine.active_plan(name, BOARD)
                    self.assertIsNotNone(active_after)
                    assert active_after is not None
                    self.assertEqual(active_after.remaining_calls, active_before.remaining_calls)
                    self.assertEqual(harness.worker.receive_calls, 0)
                    self.assertEqual(harness.worker.send_calls, 0)
                finally:
                    harness.close()

            with self.subTest(operation=operation, case="exhaustion"):
                harness = ProductionCanHarness("idle")
                try:
                    await harness.connect()
                    parameters = {"board_id": BOARD}
                    parameters.update(await harness.install_plan(operation, max_calls=1))
                    name = f"{operation}_can"
                    first = json.loads(_text(await harness.server.mcp.call_tool(name, parameters)))
                    self.assertEqual(first["status"], "captured" if operation == "read" else "sent")
                    budget_after_first = harness.server._can_manager.budget.snapshot()
                    calls_after_first = (
                        harness.worker.receive_calls,
                        harness.worker.send_calls,
                    )

                    with self.assertRaises(ToolError) as caught:
                        await harness.server.mcp.call_tool(name, parameters)
                    refusal = json.loads(str(caught.exception))
                    self.assertEqual(refusal["code"], "plan/tool-locked")
                    self.assertEqual(
                        harness.server._can_manager.budget.snapshot(), budget_after_first
                    )
                    self.assertEqual(
                        (harness.worker.receive_calls, harness.worker.send_calls),
                        calls_after_first,
                    )
                finally:
                    harness.close()

    async def test_inner_can_timeout_is_failed_json_not_outer_mcp_timeout(self) -> None:
        for operation in ("read", "send"):
            with self.subTest(operation=operation):
                harness = ProductionCanHarness("idle")
                try:
                    await harness.connect()
                    parameters: dict[str, object] = {"board_id": BOARD}
                    parameters.update(await harness.install_plan(operation))
                    harness.worker.inner_timeout_operation = operation

                    payload = json.loads(
                        _text(await harness.server.mcp.call_tool(f"{operation}_can", parameters))
                    )
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["status"], "failed")
                    self.assertEqual(payload["code"], "can/operation-timeout")
                    self.assertTrue(harness.worker.started.is_set())
                    self.assertEqual(harness.server._can_manager.phase, "closed")
                    self.assertIsNone(harness.server._can_manager.owner_board_id)
                    self.assertIs(
                        harness.server.connection_manager.connection_for(BOARD),
                        harness.connection,
                    )
                    self.assertEqual(
                        harness.server.gate_manager.require_validated(BOARD, harness.connection_id),
                        harness.validation,
                    )
                finally:
                    harness.close()


class CanSubprocessShutdownTests(unittest.TestCase):
    def test_uncertain_cleanup_real_stdio_retains_owner_and_records_failure(self) -> None:
        script = r"""
import asyncio
import json
import sys

from pyocd_debug_mcp.adapters.can_interface import CanBackendCapabilities, CanBusHandle, CapabilityStatus
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from tests.tiered_acceptance_support import known_good_full_policy
import pyocd_debug_mcp.server as server

closed_debug_sessions = []

class MarkerBackend:
    def close(self, handle):
        closed_debug_sessions.append(handle.session)

class FailingCleanupWorker:
    def __init__(self, profile_sha256):
        self.profile_sha256 = profile_sha256
        self.cleanup_uncertain = False
        self.last_escalation_stage = "none"
        self.last_cleanup_outcome = "none"
        self.close_calls = 0
    async def inspect(self, *, timeout_seconds):
        del timeout_seconds
        yes = CapabilityStatus.SUPPORTED
        return CanBackendCapabilities(
            bounded_open=yes, bounded_send=yes, bounded_receive=yes,
            bounded_close=yes, pending_tx_quiescence=yes, physical=False,
            pending_tx_empty=True, driver="virtual", device_identity=("shutdown-worker",),
            listen_only=False, one_shot=True, loopback=False, bus_state="active",
        )
    async def open(self, *, timeout_seconds):
        del timeout_seconds
        return CanBusHandle(
            native_handle=object(), profile_id="virtual_tx_allowed",
            profile_sha256=self.profile_sha256, interface="virtual",
            channel="byo-tx-allowed", protocol="classic", filters=(),
        )
    async def close(self, *, timeout_seconds):
        del timeout_seconds
        self.close_calls += 1
        self.cleanup_uncertain = True
        self.last_escalation_stage = "cleanup-unknown"
        self.last_cleanup_outcome = "unknown"
        raise RuntimeError("SECRET:/tmp/can-backend")
    async def cancel(self):
        self.cleanup_uncertain = True
        self.last_escalation_stage = "cleanup-unknown"
        self.last_cleanup_outcome = "unknown"
        raise RuntimeError("SECRET:/tmp/can-backend")

policies = CapabilityPolicyRepository(server._project_root)
profile_snapshot, map_snapshot = known_good_full_policy()
policies.commit(
    "legacy_full", Tier.SETUP_FULL,
    profile_snapshot=profile_snapshot, map_snapshot=map_snapshot,
)
server.configure_tiered_test_seams(
    backend=MarkerBackend(), policy_repository=policies,
)

for board, probe, marker in (
    ("legacy_full", "probe-owner", "owner"),
    ("other_board", "probe-other", "other"),
):
    connection_id = "probe:test:" + probe
    runtime = server._session_store.start_session(
        board_id=board, connection_id=connection_id,
        probe_uid=probe, route_used="test",
    )
    server.connection_manager.assign(
        board,
        TargetSessionHandle(
            session=marker, board=None, probe_uid=probe,
            route_used="test", target_override="nrf52840",
        ),
        runtime,
        connection_id=connection_id,
    )

owner_connection = server.connection_manager.connection_for("legacy_full")
server.gate_manager.stamp_validation(
    board_id="legacy_full",
    connection_id=owner_connection.connection_id,
    probe_identity="probe-owner",
    observed_mcu="nRF52840",
    validation_run="validation-1",
    map_digest="test-map-digest",
)
fixture = server._can_manager.startup.fixture
assert fixture is not None
worker = FailingCleanupWorker(fixture.canonical_sha256)
service = server._can_manager._service
assert service is not None
service._worker_factory = lambda: worker
connected = asyncio.run(
    server.mcp.call_tool("connect_can", {"board_id": "legacy_full"})
)
assert "\"status\":\"connected\"" in connected[0].text

try:
    server.main()
except SystemExit:
    state = {
        "closed_debug_sessions": closed_debug_sessions,
        "assigned_boards": list(server.connection_manager.assigned_board_ids()),
        "can_owner": server._can_manager.owner_board_id,
        "can_phase": server._can_manager.phase,
        "close_calls": worker.close_calls,
    }
    sys.stderr.write("CAN_SHUTDOWN_STATE=" + json.dumps(state, sort_keys=True) + "\n")
    raise
"""
        with tempfile.TemporaryDirectory(prefix="byo-can-stdio-") as temporary:
            root = Path(temporary)
            profile = root / "can.yaml"
            profile.write_text(
                SOURCE_PROFILE.read_text(encoding="utf-8").replace("nucleo_l476rg", BOARD),
                encoding="utf-8",
            )
            environment = dict(os.environ)
            environment.update(
                {
                    "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT))),
                    "BYO_MCP_ARTIFACT_ROOT": str(root),
                    "BYO_MCP_MONITOR_ROOT": str(root / "monitor"),
                    "BYO_MCP_TEST_SEAMS": "1",
                    "PYOCD_CAN_MODE": "available",
                    "PYOCD_CAN_CONFIG": str(profile),
                    "PYOCD_CAN_CONFIG_SHA256": "",
                    "PYOCD_CAN_RUNTIME_SHA256": "",
                    "PYOCD_CAN_READ_SECONDS": "",
                }
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                input="",
                text=True,
                capture_output=True,
                env=environment,
                cwd=ROOT,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            marker = next(
                line
                for line in result.stderr.splitlines()
                if line.startswith("CAN_SHUTDOWN_STATE=")
            )
            state = json.loads(marker.removeprefix("CAN_SHUTDOWN_STATE="))
            self.assertEqual(state["closed_debug_sessions"], ["owner", "other"])
            self.assertEqual(state["assigned_boards"], [])
            self.assertEqual(state["can_owner"], BOARD)
            self.assertEqual(state["can_phase"], "uncertain")
            self.assertEqual(state["close_calls"], 3)

            event_records = []
            for event_path in (root / ".firm" / "runs").glob("*/logs/events.jsonl"):
                event_records.extend(
                    json.loads(line)
                    for line in event_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            failures = [
                record for record in event_records if record.get("tool_name") == "disconnect_can"
            ]
            self.assertEqual(len(failures), 1, failures)
            failure = failures[0]
            self.assertEqual(
                set(failure),
                {
                    "event_id",
                    "session_id",
                    "timestamp",
                    "tool_name",
                    "board_id",
                    "probe_uid",
                    "route_used",
                    "normalized_args",
                    "outcome_kind",
                    "error_code",
                    "duration_ms",
                    "details",
                },
            )
            self.assertEqual(failure["board_id"], BOARD)
            self.assertEqual(failure["probe_uid"], "probe-owner")
            self.assertEqual(failure["route_used"], "test")
            self.assertEqual(failure["normalized_args"], {"board_id": BOARD})
            self.assertEqual(failure["outcome_kind"], "failed")
            self.assertEqual(failure["error_code"], "can/cleanup-failed")
            self.assertEqual(
                failure["details"],
                {"message": "CAN cleanup remained uncertain at process shutdown."},
            )
            self.assertIsInstance(failure["duration_ms"], int)
            self.assertGreaterEqual(failure["duration_ms"], 0)
            self.assertNotIn("SECRET", json.dumps(failure))

            monitor_records = []
            for monitor_path in (root / "monitor").rglob("*.jsonl"):
                monitor_records.extend(
                    json.loads(line)
                    for line in monitor_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            closes = [record for record in monitor_records if record.get("kind") == "close"]
            self.assertTrue(closes, monitor_records)
            self.assertEqual(closes[-1]["detail"]["reason"], "eof")


class CanSpawnBoundaryDocumentationTests(unittest.TestCase):
    def test_public_can_docs_disclose_unbounded_synchronous_process_start(self) -> None:
        for relative in (
            "SERVER_GUIDE.md",
            "docs/architecture.md",
            "docs/client-contract.md",
            "docs/server-reference.md",
            "docs/RELEASE_STATUS.md",
        ):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("multiprocessing.Process.start()", text)
                disclosure = text[text.index("multiprocessing.Process.start()") :][:400]
                self.assertIn("deadline", disclosure)
                self.assertTrue(any(term in disclosure for term in (" no ", "not", "cannot")))


if __name__ == "__main__":
    unittest.main()
