"""Focused Slice 4A1 regression coverage for the run-scoped guard core."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import shlex
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Mapping, TypedDict, cast
from unittest.mock import patch

from pydantic import ValidationError

from firmware_mcp import server
from firmware_mcp.guardrails.core import ActionSpec, GuardCore, GuardError
from firmware_mcp.serial_resolver import SerialPortInfo


def _process_evidence(board_id: str) -> dict[str, object]:
    return {"board_id": board_id, "profile": "shared", "session": "shared"}


def _approval_process(
    root: str,
    request_id: str,
    budget: int,
    start: Any,
    result: Any,
) -> None:
    """Spawn-safe competing fallback-CLI equivalent, intentionally independent core."""

    core = GuardCore(
        project_root=Path(root),
        run_id="shared-run",
        action_specs={},
        evidence_for=_process_evidence,
    )
    start.wait()
    try:
        core.approve_request(request_id, approved=True, call_budget=budget)
    except GuardError as error:
        result.put((error.code, budget))
    else:
        result.put(("approved", budget))


def _consume_or_invalidate_process(
    root: str,
    request_id: str,
    start: Any,
    action: str,
    result: Any,
) -> None:
    core = GuardCore(
        project_root=Path(root),
        run_id="shared-run",
        action_specs={},
        evidence_for=_process_evidence,
    )
    start.wait()
    try:
        if action == "consume":
            core.get_permission(request_id)
        else:
            core.invalidate_board("board-a", "concurrent-disconnect")
    except GuardError as error:
        result.put(error.code)
    else:
        result.put(action)


class _PlanAction(TypedDict):
    tool: str
    arguments: dict[str, object]
    max_calls: int


class GuardCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.evidence = {
            "board-a": {"board_id": "board-a", "profile": "a", "session": "one", "serial": "A"},
            "board-b": {"board_id": "board-b", "profile": "b", "session": "two", "serial": "B"},
        }
        self.attempts: list[dict[str, object]] = []
        self.core = GuardCore(
            project_root=self.root,
            run_id="run-one",
            action_specs={
                "read_memory": ActionSpec(
                    "read_memory",
                    "routine",
                    "connected-and-safety",
                    ("board_id", "address", "width_bits", "length_bytes"),
                ),
                "read_serial": ActionSpec(
                    "read_serial", "routine", "connected", ("board_id", "port"), serial_bound=True
                ),
                "flash_firmware": ActionSpec(
                    "flash_firmware",
                    "destructive",
                    "connected-and-safety",
                    ("board_id", "firmware_path", "halt_after_reset"),
                    artifact_bound=True,
                ),
                "program_image": ActionSpec(
                    "program_image",
                    "routine",
                    "connected-and-safety",
                    ("board_id", "firmware_path"),
                    artifact_bound=True,
                ),
            },
            evidence_for=lambda board_id: dict(self.evidence[board_id]),
            on_attempt=lambda _tool, attempt: self.attempts.append(attempt),
            serial_identity_for=lambda board_id, override: {
                "port": override or "COM9",
                "kind": "serial_number",
                "value": cast(str, self.evidence[board_id]["serial"]),
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _grant(self, board_id: str = "board-a", budget: int = 2) -> str:
        request = self.core.request_permission(
            board_id=board_id,
            scope="routine-session",
            requested_call_budget=999999,
            plan_id=None,
        )
        self.core.approve_request(request.request_id, approved=True, call_budget=budget)
        return self.core.get_permission(request.request_id).grant_id

    @staticmethod
    def _read_action(board_id: str = "board-a", calls: int = 1) -> _PlanAction:
        return {
            "tool": "read_memory",
            "arguments": {
                "board_id": board_id,
                "address": "0x20000000",
                "width_bits": 32,
                "length_bytes": 4,
            },
            "max_calls": calls,
        }

    def _evidence_publishing_connect_plan(self) -> tuple[GuardCore, str, str]:
        """Make one exact connect attempt for post-operation wrapper coverage."""

        core = GuardCore(
            project_root=self.root / "post-operation-wrapper",
            run_id="post-operation-wrapper",
            action_specs={
                "connect_board": ActionSpec("connect_board", "routine", "profile", ("board_id",))
            },
            evidence_for=lambda board_id: dict(self.evidence[board_id]),
        )
        request = core.request_permission(
            board_id="board-a",
            scope="routine-session",
            requested_call_budget=None,
            plan_id=None,
        )
        core.approve_request(request.request_id, approved=True, call_budget=1)
        grant = core.get_permission(request.request_id)
        plan = core.create_plan(
            grant_id=grant.grant_id,
            board_id="board-a",
            objective="connect this board",
            expected_result="connected session evidence",
            actions=[
                {
                    "tool": "connect_board",
                    "arguments": {"board_id": "board-a"},
                    "max_calls": 1,
                }
            ],
        )
        return core, grant.grant_id, plan.plan_id

    @staticmethod
    def _record_actions(record: object) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], cast(dict[str, object], record)["actions"])

    def test_exact_arguments_board_and_no_refund_after_started_failure(self) -> None:
        grant_id = self._grant(budget=2)
        plan = self.core.create_plan(
            grant_id=grant_id,
            board_id="board-a",
            objective="inspect RAM",
            expected_result="four bytes",
            actions=[self._read_action(calls=2)],
        )
        with self.assertRaisesRegex(GuardError, "arguments do not exactly match"):
            self.core.execute(
                tool="read_memory",
                plan_id=plan.plan_id,
                arguments={**self._read_action()["arguments"], "length_bytes": 8},
            )
        self.core.execute(
            tool="read_memory", plan_id=plan.plan_id, arguments=self._read_action()["arguments"]
        )
        record = self.core.plan_record(plan.plan_id)
        self.assertEqual(self._record_actions(record)[0]["remaining_calls"], 1)
        self.assertEqual(record["grant_remaining_calls"], 1)
        self.assertEqual(len(self.attempts), 1)

    def test_started_failure_or_cancellation_never_refunds(self) -> None:
        grant_id = self._grant(budget=2)
        plan = self.core.create_plan(
            grant_id=grant_id,
            board_id="board-a",
            objective="inspect RAM twice",
            expected_result="two attempted reads",
            actions=[self._read_action(calls=2)],
        )

        def start_then_backend_fails(error: BaseException) -> None:
            # The real wrapper calls GuardCore.execute before its backend. This
            # explicit seam proves either backend outcome leaves that attempt spent.
            self.core.execute(
                tool="read_memory",
                plan_id=plan.plan_id,
                arguments=self._read_action()["arguments"],
            )
            raise error

        with self.assertRaisesRegex(RuntimeError, "backend failed"):
            start_then_backend_fails(RuntimeError("backend failed"))
        with self.assertRaises(asyncio.CancelledError):
            start_then_backend_fails(asyncio.CancelledError())
        record = self.core.plan_record(plan.plan_id)
        self.assertEqual(self._record_actions(record)[0]["remaining_calls"], 0)
        self.assertEqual(record["grant_remaining_calls"], 0)
        self.assertEqual(len(cast(list[object], record["attempts"])), 2)

    def test_missing_extra_and_cross_board_actions_are_rejected(self) -> None:
        grant_id = self._grant()
        bad = self._read_action()
        bad["arguments"] = {"board_id": "board-a"}
        with self.assertRaisesRegex(GuardError, "arguments must exactly match"):
            self.core.create_plan(
                grant_id=grant_id,
                board_id="board-a",
                objective="x",
                expected_result="y",
                actions=[bad],
            )
        with self.assertRaisesRegex(GuardError, "board_id"):
            self.core.create_plan(
                grant_id=grant_id,
                board_id="board-a",
                objective="x",
                expected_result="y",
                actions=[self._read_action("board-b")],
            )
        with self.assertRaisesRegex(GuardError, "Call request_hardware_permission"):
            self.core.execute(
                tool="read_memory", plan_id=None, arguments=self._read_action()["arguments"]
            )

    def test_user_budget_is_authoritative_and_types_are_strict(self) -> None:
        request = self.core.request_permission(
            board_id="board-a", scope="routine-session", requested_call_budget=1000000, plan_id=None
        )
        self.core.approve_request(request.request_id, approved=True, call_budget=3)
        grant = self.core.get_permission(request.request_id)
        self.assertEqual(grant.initial_calls, 3)
        for bad in (True, 0, -1, "2"):
            request = self.core.request_permission(
                board_id="board-a",
                scope="routine-session",
                requested_call_budget=None,
                plan_id=None,
            )
            with self.assertRaises(GuardError):
                self.core.approve_request(request.request_id, approved=True, call_budget=bad)

        for bad in (True, "2"):
            with self.assertRaises(ValidationError):
                server._HardwarePermissionReply.model_validate(
                    {"approved": True, "call_budget": bad}
                )
        with self.assertRaises(ValidationError):
            server._HardwarePermissionReply.model_validate({"approved": "true", "call_budget": 2})

    def test_permission_reply_uses_mcp_primitive_schema_without_coercion(self) -> None:
        """The installed MCP validator accepts the reply model's primitive annotations."""

        from mcp.server.elicitation import _validate_elicitation_schema

        _validate_elicitation_schema(server._HardwarePermissionReply)
        with self.assertRaises(ValidationError):
            server._HardwarePermissionReply.model_validate({"approved": True, "call_budget": "2"})
        with self.assertRaises(ValidationError):
            server._HardwarePermissionReply.model_validate({"approved": "true", "call_budget": 2})

    def test_permission_store_failures_are_explicit_and_recoverable(self) -> None:
        """Every store boundary reports uncertainty without returning authority."""

        def new_core() -> GuardCore:
            return GuardCore(
                project_root=self.root,
                run_id="store-failure-run",
                action_specs={},
                evidence_for=lambda board_id: {"board_id": board_id},
            )

        def request(core: GuardCore) -> object:
            return core.request_permission(
                board_id="board-a",
                scope="routine-session",
                requested_call_budget=None,
                plan_id=None,
            )

        core = new_core()
        # The explicit cases below keep each injected boundary isolated.  The
        # lock case is expanded rather than hiding context-manager cleanup in
        # a generic test helper.
        with patch.object(core._store_lock, "acquire", side_effect=PermissionError("locked")):
            with self.assertRaises(GuardError) as raised:
                request(core)
        self.assertEqual(raised.exception.code, "guard/permission-store-unavailable")
        self.assertIn("Restore project .firm access", raised.exception.message)
        self.assertIn("inspect permission status/request again", raised.exception.message)

        for boundary, patcher in (
            (
                "directory",
                lambda current: patch("pathlib.Path.mkdir", side_effect=PermissionError("mkdir")),
            ),
            (
                "read",
                lambda current: patch(
                    "pathlib.Path.read_text", side_effect=PermissionError("read")
                ),
            ),
            (
                "fsync",
                lambda current: patch(
                    "firmware_mcp.guardrails.core.os.fsync",
                    side_effect=OSError("fsync"),
                ),
            ),
            (
                "replace",
                lambda current: patch(
                    "firmware_mcp.guardrails.core.os.replace",
                    side_effect=OSError("replace"),
                ),
            ),
            (
                "release",
                lambda current: patch.object(
                    current._store_lock, "release", side_effect=OSError("release")
                ),
            ),
        ):
            with self.subTest(boundary=boundary):
                current = new_core()
                if boundary == "read":
                    request(current)
                with patcher(current):
                    with self.assertRaises(GuardError) as boundary_error:
                        request(current)
                self.assertEqual(
                    boundary_error.exception.code, "guard/permission-store-unavailable"
                )
                self.assertIn("Restore project .firm access", boundary_error.exception.message)
                if boundary == "release":
                    # The injected release failure intentionally leaves the
                    # real OS lock attached; restore it before this test's
                    # temporary project is removed on Windows.
                    current._store_lock.release()
                # Once access is restored, a fresh request/status observes
                # only actual durable records rather than a fabricated result.
                recovered = cast(Any, request(current))
                status = current.permission_status(recovered.request_id)
                recovered_request = cast(dict[str, object], status["request"])
                self.assertEqual(recovered_request["request_id"], recovered.request_id)

        cleanup_core = new_core()
        with (
            patch("firmware_mcp.guardrails.core.os.fsync", side_effect=OSError("fsync")),
            patch("firmware_mcp.guardrails.core.os.unlink", side_effect=OSError("cleanup")),
        ):
            with self.assertRaises(GuardError) as cleanup_error:
                request(cleanup_core)
        self.assertEqual(cleanup_error.exception.code, "guard/permission-store-unavailable")

    def test_failed_receipt_publication_never_leaves_a_grant_or_clean_invalidation(self) -> None:
        request = self.core.request_permission(
            board_id="board-a", scope="routine-session", requested_call_budget=None, plan_id=None
        )
        self.core.approve_request(request.request_id, approved=True, call_budget=1)
        with patch("firmware_mcp.guardrails.core.os.replace", side_effect=OSError("replace")):
            with self.assertRaises(GuardError) as consumption_error:
                self.core.get_permission(request.request_id)
        self.assertEqual(consumption_error.exception.code, "guard/permission-store-unavailable")
        self.assertEqual(self.core._grants, {})
        receipt = cast(
            dict[str, object], self.core.permission_status(request.request_id)["receipt"]
        )
        self.assertIsNone(receipt["consumed_by"])

        grant = self.core.get_permission(request.request_id)
        self.assertTrue(grant.active)
        with patch("firmware_mcp.guardrails.core.os.replace", side_effect=OSError("replace")):
            with self.assertRaises(GuardError) as invalidation_error:
                self.core.invalidate_board("board-a", "disconnect")
        self.assertEqual(invalidation_error.exception.code, "guard/permission-store-unavailable")
        self.assertFalse(self.core._grants[grant.grant_id].active)
        persisted_request = cast(
            dict[str, object], self.core.permission_status(request.request_id)["request"]
        )
        self.assertNotIn("invalidated_reason", persisted_request)

    def test_mcp_elicitation_records_the_user_selected_budget(self) -> None:
        class _Elicitation:
            action = "accept"
            data = server._HardwarePermissionReply(approved=True, call_budget=4)

        class _Context:
            async def elicit(self, _message: str, _schema: object) -> _Elicitation:
                return _Elicitation()

        original = server._guard_core
        server._guard_core = self.core
        try:
            reply = asyncio.run(
                server.request_hardware_permission(
                    board_id="board-a",
                    scope="routine-session",
                    requested_call_budget=999_999,
                    ctx=_Context(),  # type: ignore[arg-type]
                )
            )
            self.assertIsInstance(reply, dict)
            reply_record = cast(dict[str, object], reply)
            self.assertEqual(reply_record["approval"], "recorded")
            grant = self.core.get_permission(cast(str, reply_record["request_id"]))
            self.assertEqual(grant.initial_calls, 4)
        finally:
            server._guard_core = original

    def test_losing_mcp_decline_returns_the_immutable_receipt_error(self) -> None:
        class _CompetingDeclineContext:
            async def elicit(self, _message: str, _schema: object) -> SimpleNamespace:
                record = json.loads(self_core.request_path.read_text(encoding="utf-8"))
                request_id = next(iter(cast(dict[str, object], record["requests"])))
                self_core.approve_request(request_id, approved=True, call_budget=1)
                return SimpleNamespace(action="decline", data=None)

        self_core = self.core
        original = server._guard_core
        server._guard_core = self_core
        try:
            result = asyncio.run(
                server.request_hardware_permission(
                    board_id="board-a",
                    scope="routine-session",
                    ctx=_CompetingDeclineContext(),  # type: ignore[arg-type]
                )
            )
        finally:
            server._guard_core = original
        self.assertIn("guard/receipt-exists", cast(str, result))
        record = json.loads(self_core.request_path.read_text(encoding="utf-8"))
        request_id = next(iter(cast(dict[str, object], record["requests"])))
        self.assertTrue(record["receipts"][request_id]["approved"])

    def test_grants_and_plans_never_change_tool_visibility(self) -> None:
        before = set(server.mcp._tool_manager._tools)
        grant_id = self._grant()
        self.core.create_plan(
            grant_id=grant_id,
            board_id="board-a",
            objective="inspect",
            expected_result="state",
            actions=[self._read_action()],
        )
        self.assertEqual(set(server.mcp._tool_manager._tools), before)

    def test_concurrent_attempts_cannot_overspend(self) -> None:
        grant_id = self._grant(budget=2)
        plan = self.core.create_plan(
            grant_id=grant_id,
            board_id="board-a",
            objective="x",
            expected_result="y",
            actions=[self._read_action(calls=2)],
        )
        barrier = threading.Barrier(3)
        results: list[str] = []

        def invoke() -> None:
            barrier.wait()
            try:
                self.core.execute(
                    tool="read_memory",
                    plan_id=plan.plan_id,
                    arguments=self._read_action()["arguments"],
                )
                results.append("started")
            except GuardError:
                results.append("rejected")

        workers = [threading.Thread(target=invoke) for _ in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(results.count("started"), 2)
        self.assertEqual(results.count("rejected"), 1)
        self.assertEqual(self.core.plan_record(plan.plan_id)["grant_remaining_calls"], 0)

    def test_evidence_disconnect_and_serial_changes_invalidate(self) -> None:
        grant_id = self._grant()
        plan = self.core.create_plan(
            grant_id=grant_id,
            board_id="board-a",
            objective="serial",
            expected_result="capture",
            actions=[
                {
                    "tool": "read_serial",
                    "arguments": {"board_id": "board-a", "port": None},
                    "max_calls": 1,
                }
            ],
        )
        self.evidence["board-a"]["serial"] = "changed"
        with self.assertRaisesRegex(GuardError, "profile, assignment, session, or identity"):
            self.core.execute(
                tool="read_serial",
                plan_id=plan.plan_id,
                arguments={"board_id": "board-a", "port": None},
            )
        self.assertEqual(self.core.plan_record(plan.plan_id)["status"], "invalidated")

        grant_id = self._grant()
        provider_plan = self.core.create_plan(
            grant_id=grant_id,
            board_id="board-a",
            objective="inspect after provider replay",
            expected_result="current target state",
            actions=[self._read_action()],
        )
        self.evidence["board-a"]["provider_support_identity"] = "provider-a"
        with self.assertRaisesRegex(GuardError, "profile, assignment, session, or identity"):
            self.core.execute(
                tool="read_memory",
                plan_id=provider_plan.plan_id,
                arguments=self._read_action()["arguments"],
            )
        self.assertEqual(self.core.plan_record(provider_plan.plan_id)["status"], "invalidated")

        request = self.core.request_permission(
            board_id="board-b", scope="routine-session", requested_call_budget=None, plan_id=None
        )
        self.core.invalidate_board("board-b", "disconnect")
        with self.assertRaisesRegex(GuardError, "invalidated"):
            self.core.approve_request(request.request_id, approved=True, call_budget=1)

    def test_artifact_change_and_destructive_pending_never_start_backend(self) -> None:
        image = self.root / "image.hex"
        image.write_text(":0100000001FE\n:00000001FF\n", encoding="ascii")
        plan = self.core.create_plan(
            grant_id=None,
            board_id="board-a",
            objective="program",
            expected_result="verified",
            actions=[
                {
                    "tool": "flash_firmware",
                    "arguments": {
                        "board_id": "board-a",
                        "firmware_path": str(image),
                        "halt_after_reset": False,
                    },
                    "max_calls": 1,
                }
            ],
        )
        self.assertEqual(self.core.plan_record(plan.plan_id)["status"], "disclosure-required")
        with self.assertRaisesRegex(GuardError, "exact destructive permission"):
            self.core.execute(
                tool="flash_firmware",
                plan_id=plan.plan_id,
                arguments={
                    "board_id": "board-a",
                    "firmware_path": str(image),
                    "halt_after_reset": False,
                },
            )
        self.assertEqual(self.attempts, [])
        request = self.core.request_permission(
            board_id="board-a",
            scope="destructive-once",
            requested_call_budget=1,
            plan_id=plan.plan_id,
        )
        self.assertEqual(request.plan_id, plan.plan_id)

    def test_routine_artifact_mutation_invalidates_before_attempt(self) -> None:
        image = self.root / "routine.hex"
        image.write_text(":0100000001FE\n:00000001FF\n", encoding="ascii")
        grant_id = self._grant()
        plan = self.core.create_plan(
            grant_id=grant_id,
            board_id="board-a",
            objective="program a checked image",
            expected_result="verified bytes",
            actions=[
                {
                    "tool": "program_image",
                    "arguments": {"board_id": "board-a", "firmware_path": str(image)},
                    "max_calls": 1,
                }
            ],
        )
        image.write_text(":0100000002FD\n:00000001FF\n", encoding="ascii")
        with self.assertRaisesRegex(GuardError, "Artifact bytes changed"):
            self.core.execute(
                tool="program_image",
                plan_id=plan.plan_id,
                arguments={"board_id": "board-a", "firmware_path": str(image)},
            )
        self.assertEqual(self.core.plan_record(plan.plan_id)["close_reason"], "artifact-changed")

    def test_nested_layout_evidence_uses_checked_execution_snapshots(self) -> None:
        """A nested layout source cannot change between plan check and parsing."""

        layout = self.root / "layout.json"
        source = self.root / "datasheet.txt"
        source.write_bytes(b"original evidence")
        layout.write_text(json.dumps({"regions": [{"source_path": str(source)}]}), encoding="utf-8")

        def resolve(
            spec: ActionSpec,
            arguments: Mapping[str, object],
            snapshots: Mapping[str, bytes] | None,
        ) -> Mapping[str, Path]:
            self.assertEqual(spec.name, "refresh_safety_map")
            payload = (
                Path(cast(str, arguments["layout_path"])).read_bytes()
                if snapshots is None
                else snapshots["layout_path"]
            )
            return {"layout_source:0": Path(json.loads(payload)["regions"][0]["source_path"])}

        core = GuardCore(
            project_root=self.root / "nested-file-binding",
            run_id="nested-file-binding",
            action_specs={
                "refresh_safety_map": ActionSpec(
                    "refresh_safety_map",
                    "routine",
                    "connected",
                    ("board_id", "layout_path"),
                    file_bindings=("layout_path",),
                )
            },
            evidence_for=lambda board_id: dict(self.evidence[board_id]),
            file_binding_resolver=resolve,
        )
        request = core.request_permission(
            board_id="board-a", scope="routine-session", requested_call_budget=None, plan_id=None
        )
        core.approve_request(request.request_id, approved=True, call_budget=1)
        grant = core.get_permission(request.request_id)
        plan = core.create_plan(
            grant_id=grant.grant_id,
            board_id="board-a",
            objective="refresh",
            expected_result="map",
            actions=[
                {
                    "tool": "refresh_safety_map",
                    "arguments": {"board_id": "board-a", "layout_path": str(layout)},
                    "max_calls": 1,
                }
            ],
        )
        core.execute(
            tool="refresh_safety_map",
            plan_id=plan.plan_id,
            arguments={"board_id": "board-a", "layout_path": str(layout)},
        )
        source.write_bytes(b"mutated after guard snapshot")
        self.assertEqual(core.execution_file("layout_source:0"), b"original evidence")
        self.assertEqual(self.attempts, [])

    def test_receipt_is_single_use_and_one_plan_binds_one_grant(self) -> None:
        grant_id = self._grant(budget=2)
        plan = self.core.create_plan(
            grant_id=grant_id,
            board_id="board-a",
            objective="x",
            expected_result="y",
            actions=[self._read_action()],
        )
        self.assertEqual(self.core.plan_record(plan.plan_id)["grant_id"], grant_id)
        self.assertEqual(self.core.revoke(grant_id)["invalidated_plan_ids"], [plan.plan_id])

    def test_permission_store_is_atomic_across_independent_processes(self) -> None:
        shared_core = GuardCore(
            project_root=self.root,
            run_id="shared-run",
            action_specs={},
            evidence_for=_process_evidence,
        )
        request = shared_core.request_permission(
            board_id="board-a", scope="routine-session", requested_call_budget=None, plan_id=None
        )
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(
                target=_approval_process,
                args=(str(self.root), request.request_id, budget, start, results),
            )
            for budget in (3, 7)
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(15)
            self.assertEqual(worker.exitcode, 0)
        approvals = [results.get(timeout=2) for _ in workers]
        self.assertEqual(sum(code == "approved" for code, _ in approvals), 1)
        self.assertEqual(sum(code == "guard/receipt-exists" for code, _ in approvals), 1)
        winner = next(budget for code, budget in approvals if code == "approved")
        persisted = json.loads(shared_core.request_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["receipts"][request.request_id]["call_budget"], winner)

        # Receipt consumption and board invalidation also share the same
        # transaction.  Whichever gets the lock first, neither update vanishes.
        start = context.Event()
        updates = context.Queue()
        workers = [
            context.Process(
                target=_consume_or_invalidate_process,
                args=(str(self.root), request.request_id, start, action, updates),
            )
            for action in ("consume", "invalidate")
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(15)
            self.assertEqual(worker.exitcode, 0)
        outcomes = {updates.get(timeout=2) for _ in workers}
        persisted = json.loads(shared_core.request_path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["requests"][request.request_id]["invalidated_reason"], "concurrent-disconnect"
        )
        self.assertEqual(
            persisted["receipts"][request.request_id]["invalidated_reason"], "concurrent-disconnect"
        )
        self.assertTrue(
            "consume" in outcomes or "guard/request-stale" in outcomes,
            outcomes,
        )
        if "consume" in outcomes:
            self.assertIsNotNone(persisted["receipts"][request.request_id]["consumed_by"])

    def test_serial_action_identity_is_per_action_and_rechecked_before_consumption(self) -> None:
        observed = {"port": "COM9", "kind": "serial_number", "value": "uart-a"}
        self.core._serial_identity_for = lambda _board, _port: dict(observed)
        grant_id = self._grant()
        plan = self.core.create_plan(
            grant_id=grant_id,
            board_id="board-a",
            objective="capture UART A",
            expected_result="bytes from UART A",
            actions=[
                {
                    "tool": "read_serial",
                    "arguments": {"board_id": "board-a", "port": None},
                    "max_calls": 1,
                }
            ],
        )
        action = self._record_actions(self.core.plan_record(plan.plan_id))[0]
        self.assertEqual(action["serial_identity"], observed)
        self.core.execute(
            tool="read_serial",
            plan_id=plan.plan_id,
            arguments={"board_id": "board-a", "port": None},
        )

        grant_id = self._grant()
        replacement = self.core.create_plan(
            grant_id=grant_id,
            board_id="board-a",
            objective="capture UART A again",
            expected_result="bytes from UART A",
            actions=[
                {
                    "tool": "read_serial",
                    "arguments": {"board_id": "board-a", "port": "COM9"},
                    "max_calls": 1,
                }
            ],
        )
        observed["value"] = "uart-b"
        with self.assertRaisesRegex(GuardError, "UART identity changed"):
            self.core.execute(
                tool="read_serial",
                plan_id=replacement.plan_id,
                arguments={"board_id": "board-a", "port": "COM9"},
            )
        self.assertEqual(
            self.core.plan_record(replacement.plan_id)["close_reason"], "serial-identity-changed"
        )

    def test_public_wrapper_prevents_uart_port_reassignment_and_no_identity_fallback(self) -> None:
        observed = SerialPortInfo(device="COM9", serial_number="uart-a")
        replacement = SerialPortInfo(device="COM9", serial_number="uart-b")
        raw_calls: list[str] = []
        core = GuardCore(
            project_root=self.root,
            run_id="run-one",
            action_specs={
                "read_serial": ActionSpec(
                    "read_serial", "routine", "connected", ("board_id", "port"), serial_bound=True
                )
            },
            evidence_for=lambda board_id: dict(self.evidence[board_id]),
            serial_identity_for=server._guard_serial_identity,
        )
        original_core = server._guard_core
        server._guard_core = core
        try:
            with (
                patch.object(
                    server.connection_manager,
                    "maybe_connection",
                    return_value=SimpleNamespace(handle=object()),
                ),
                patch.object(server, "_resolve_serial_port_for_session", return_value=observed),
                patch.object(server, "_runtime_for", return_value=None),
            ):
                request = core.request_permission(
                    board_id="board-a",
                    scope="routine-session",
                    requested_call_budget=None,
                    plan_id=None,
                )
                core.approve_request(request.request_id, approved=True, call_budget=2)
                grant_id = core.get_permission(request.request_id).grant_id
                plan_result = server.create_hardware_plan(
                    grant_id,
                    "board-a",
                    "capture UART A",
                    "capture",
                    [
                        {
                            "tool": "read_serial",
                            "arguments": {"board_id": "board-a", "port": None},
                            "max_calls": 1,
                        }
                    ],
                )
                self.assertIsInstance(plan_result, dict)
                plan_id = cast(str, cast(dict[str, object], plan_result)["plan_id"])
                wrapped = server._guarded_handler(
                    "read_serial",
                    lambda board_id, port=None: raw_calls.append(f"{board_id}:{port}") or "read",
                )
                self.assertEqual(wrapped(board_id="board-a", port=None, plan_id=plan_id), "read")
                self.assertEqual(raw_calls, ["board-a:None"])

                request = core.request_permission(
                    board_id="board-a",
                    scope="routine-session",
                    requested_call_budget=None,
                    plan_id=None,
                )
                core.approve_request(request.request_id, approved=True, call_budget=1)
                replacement_grant = core.get_permission(request.request_id).grant_id
                second = server.create_hardware_plan(
                    replacement_grant,
                    "board-a",
                    "capture UART A",
                    "capture",
                    [
                        {
                            "tool": "read_serial",
                            "arguments": {"board_id": "board-a", "port": "COM9"},
                            "max_calls": 1,
                        }
                    ],
                )
                second_id = cast(str, cast(dict[str, object], second)["plan_id"])
                with patch.object(
                    server, "_resolve_serial_port_for_session", return_value=replacement
                ):
                    result = wrapped(board_id="board-a", port="COM9", plan_id=second_id)
                self.assertIn("guard/serial-identity-stale", cast(str, result))
                self.assertEqual(raw_calls, ["board-a:None"])

                with patch.object(
                    server,
                    "_resolve_serial_port_for_session",
                    side_effect=RuntimeError("select one explicitly"),
                ):
                    unavailable = server.create_hardware_plan(
                        replacement_grant,
                        "board-a",
                        "capture",
                        "capture",
                        [
                            {
                                "tool": "read_serial",
                                "arguments": {"board_id": "board-a", "port": None},
                                "max_calls": 1,
                            }
                        ],
                    )
                self.assertIn("guard/serial-identity-unavailable", cast(str, unavailable))
        finally:
            server._guard_core = original_core

    def test_completed_evidence_publishing_operation_reports_invalidation_uncertainty(self) -> None:
        core, grant_id, plan_id = self._evidence_publishing_connect_plan()
        raw_calls: list[str] = []
        original_core = server._guard_core
        server._guard_core = core
        try:
            wrapped = server._guarded_handler(
                "connect_board",
                lambda board_id: raw_calls.append(board_id) or "connected",
            )
            with patch(
                "firmware_mcp.guardrails.core.os.replace",
                side_effect=OSError("receipt publication unavailable"),
            ):
                result = wrapped(board_id="board-a", plan_id=plan_id)
            self.assertIn("guard/permission-store-unavailable", cast(str, result))
            self.assertIn("connect_board completed", cast(str, result))
            self.assertIn("Do not retry blindly", cast(str, result))
            self.assertEqual(raw_calls, ["board-a"])
            self.assertFalse(core._grants[grant_id].active)
            record = cast(dict[str, object], core.plan_record(plan_id))
            self.assertEqual(record["grant_remaining_calls"], 0)
            self.assertIn(
                "guard/plan-inactive", cast(str, wrapped(board_id="board-a", plan_id=plan_id))
            )
            self.assertEqual(raw_calls, ["board-a"])
        finally:
            server._guard_core = original_core

    def test_failed_evidence_publishing_operation_chains_invalidation_uncertainty(self) -> None:
        core, grant_id, plan_id = self._evidence_publishing_connect_plan()
        raw_calls: list[str] = []
        original_core = server._guard_core
        server._guard_core = core
        try:

            def raw_failure(board_id: str) -> None:
                raw_calls.append(board_id)
                raise RuntimeError("raw connect failure")

            wrapped = server._guarded_handler("connect_board", raw_failure)
            with (
                patch(
                    "firmware_mcp.guardrails.core.os.replace",
                    side_effect=OSError("receipt publication unavailable"),
                ),
                self.assertRaisesRegex(RuntimeError, "raw connect failure") as raised,
            ):
                wrapped(board_id="board-a", plan_id=plan_id)
            uncertainty = raised.exception.__cause__
            self.assertIsInstance(uncertainty, GuardError)
            self.assertEqual(
                cast(GuardError, uncertainty).code, "guard/permission-store-unavailable"
            )
            self.assertIn("connect_board failed", str(uncertainty))
            self.assertEqual(raw_calls, ["board-a"])
            self.assertFalse(core._grants[grant_id].active)
            record = cast(dict[str, object], core.plan_record(plan_id))
            self.assertEqual(record["grant_remaining_calls"], 0)
            self.assertIn(
                "guard/plan-inactive", cast(str, wrapped(board_id="board-a", plan_id=plan_id))
            )
            self.assertEqual(raw_calls, ["board-a"])
        finally:
            server._guard_core = original_core

    def test_board_lock_spans_authorization_raw_call_and_bookkeeping(self) -> None:
        entered_raw, release_raw, replacement_entered, other_board_entered = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        core = GuardCore(
            project_root=self.root,
            run_id="run-one",
            action_specs={
                "read_memory": ActionSpec(
                    "read_memory",
                    "routine",
                    "connected",
                    ("board_id", "address"),
                )
            },
            evidence_for=lambda board_id: {"board_id": board_id, "session": "current"},
        )
        request = core.request_permission(
            board_id="board-a", scope="routine-session", requested_call_budget=None, plan_id=None
        )
        core.approve_request(request.request_id, approved=True, call_budget=2)
        grant = core.get_permission(request.request_id)
        plan = core.create_plan(
            grant_id=grant.grant_id,
            board_id="board-a",
            objective="read",
            expected_result="value",
            actions=[
                {
                    "tool": "read_memory",
                    "arguments": {"board_id": "board-a", "address": 1},
                    "max_calls": 2,
                }
            ],
        )
        original_core = server._guard_core
        server._guard_core = core
        try:
            wrapped = server._guarded_handler(
                "read_memory",
                lambda board_id, address: entered_raw.set() or release_raw.wait() or "done",
            )
            invocation = threading.Thread(
                target=lambda: wrapped(board_id="board-a", address=1, plan_id=plan.plan_id)
            )
            invocation.start()
            self.assertTrue(entered_raw.wait(2))

            def disconnect_replacement_session() -> None:
                # `disconnect()` takes the same board lock and invalidates the
                # old plan before a replacement session could be assigned.
                server.disconnect("board-a")
                replacement_entered.set()

            def take_other_board_lock() -> None:
                with server.connection_manager.lock_for("board-b"):
                    other_board_entered.set()

            replacement = threading.Thread(target=disconnect_replacement_session)
            other = threading.Thread(target=take_other_board_lock)
            replacement.start()
            other.start()
            self.assertTrue(other_board_entered.wait(2))
            self.assertFalse(replacement_entered.wait(0.1))
            release_raw.set()
            invocation.join(2)
            replacement.join(2)
            other.join(2)
            self.assertTrue(replacement_entered.is_set())
            record = core.plan_record(plan.plan_id)
            self.assertEqual(record["grant_remaining_calls"], 1)
            self.assertIn(
                "guard/plan-inactive",
                cast(str, wrapped(board_id="board-a", address=1, plan_id=plan.plan_id)),
            )
        finally:
            server._guard_core = original_core

    def test_external_cli_records_the_user_budget_not_agent_advisory(self) -> None:
        request = self.core.request_permission(
            board_id="board-a",
            scope="routine-session",
            requested_call_budget=999_999,
            plan_id=None,
        )
        output = StringIO()
        with patch("builtins.input", side_effect=["yes", "1000000"]), redirect_stdout(output):
            self.assertEqual(server._approve_hardware_cli(self.root, request.request_id), 0)
        grant = self.core.get_permission(request.request_id)
        self.assertEqual(grant.initial_calls, 1_000_000)
        self.assertIn("requested budget (advisory): 999999", output.getvalue())

    def test_losing_external_cli_decline_reports_the_immutable_receipt_error(self) -> None:
        request = self.core.request_permission(
            board_id="board-a", scope="routine-session", requested_call_budget=None, plan_id=None
        )
        self.core.approve_request(request.request_id, approved=True, call_budget=1)
        stderr = StringIO()
        with patch("builtins.input", return_value="no"), redirect_stderr(stderr):
            self.assertEqual(server._approve_hardware_cli(self.root, request.request_id), 2)
        self.assertIn("guard/receipt-exists", stderr.getvalue())
        receipt = cast(
            dict[str, object], self.core.permission_status(request.request_id)["receipt"]
        )
        self.assertTrue(receipt["approved"])

    def test_external_approval_command_has_exact_argv_and_platform_safe_rendering(self) -> None:
        roots = [self.root, Path("C:/Firmware & (Lab)/O'Brien")]
        interpreter = self.root / "runtime & (Lab)" / "O'Brien" / "python.exe"
        for root in roots:
            with (
                patch.object(server, "_project_root", root),
                patch.object(server.sys, "executable", str(interpreter)),
            ):
                argv, rendered = server._approval_command("permission-abc")
            self.assertEqual(
                argv,
                [
                    str(interpreter.resolve()),
                    "-m",
                    "firmware_mcp.server",
                    "approve-hardware",
                    "--project",
                    str(root),
                    "--request",
                    "permission-abc",
                ],
            )
            self.assertEqual(argv[0], str(interpreter.resolve()))
            if os.name == "nt":
                self.assertTrue(rendered.startswith("& "))
                script = """
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput(
    $env:APPROVAL_COMMAND, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { $errors | ForEach-Object { $_.Message }; exit 2 }
$command = $ast.EndBlock.Statements[0].PipelineElements[0]
$values = @()
foreach ($element in $command.CommandElements) { $values += $element.Value }
$values | ConvertTo-Json -Compress
"""
                parsed = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                    env={**os.environ, "APPROVAL_COMMAND": rendered},
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(parsed.returncode, 0, parsed.stderr)
                self.assertEqual(json.loads(parsed.stdout), argv)
            else:
                self.assertEqual(shlex.split(rendered), argv)

    def test_external_approval_argv_runs_without_console_script_on_path(self) -> None:
        """The self-contained fallback records a direct-user receipt without PATH help."""

        request = self.core.request_permission(
            board_id="board-a", scope="routine-session", requested_call_budget=None, plan_id=None
        )
        with patch.object(server, "_project_root", self.root):
            argv, _rendered = server._approval_command(request.request_id)

        self.assertEqual(
            argv[:4],
            [str(Path(sys.executable).resolve()), "-m", "firmware_mcp.server", "approve-hardware"],
        )
        completed = subprocess.run(
            argv,
            cwd=self.root,
            env={**os.environ, "PATH": ""},
            input="yes\n2\n",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        grant = self.core.get_permission(request.request_id)
        self.assertEqual(grant.initial_calls, 2)


if __name__ == "__main__":
    unittest.main()
