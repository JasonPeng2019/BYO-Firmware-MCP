"""Independent R11 public recovery oracles; only native hardware is synthetic.

FULL policy, selection, admission, validation, plan and monitoring code are real.
The pyOCD session factory and inventory are the only substituted boundaries.
No persisted policy, live proof, guard outcome or permission is manufactured.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pyocd.core.session import Session

from pyocd_debug_mcp.adapters import swd_pyocd as native
from pyocd_debug_mcp.probe_inventory import NativeProbeListing, ProbeInfo
from tests.m05_public_support import NULL_PLAN, PublicSetup
from tests.tiered_acceptance_support import isolated_project, public_text, tiered_test_server


UID = "M12-SYNTHETIC-JLINK-ONLY"
TARGET = "nrf52840"
RAM = 0x20000000
WORD = 0x573A91EC


def batch_word(batch):
    assert batch["status"] == "batch_completed", batch
    result = batch["completed"][0]["result"]
    if isinstance(result, list):
        result = result[0]["text"]
    return int(result.split("\n", 1)[0], 0)


class SyntheticSession:
    """Native session/register boundary, with real pyOCD option precedence."""

    def __init__(self, bench, options):
        self.bench = bench
        self.number = len(bench.sessions)
        self.options = Session(None, options=options, no_config=True).options
        self.probe = SimpleNamespace(
            unique_id=bench.selected_uid,
            associated_board_info=None,
            assert_reset=self.assert_reset,
        )
        self.target = self
        self.part_number = "nRF52840"
        self.closed = False
        self.state = "HALTED"
        bench.sessions.append(self)

    def open(self):
        assert self.options.get("auto_unlock") is False, "implicit unlock is forbidden"
        self.bench.hardware.append(
            ("open", self.number, self.probe.unique_id, dict(self.bench.last_options))
        )
        if self.bench.fail_open:
            raise RuntimeError("M12 controlled exceptional transport failure")

    def close(self):
        self.bench.hardware.append(("close", self.number))
        self.closed = True

    def read_memory(self, address, width):
        assert not self.closed
        self.bench.hardware.append(("read", self.number, address, width))
        if address == 0xE000ED00:
            return 0 if self.bench.wrong_identity else 0x410FC241
        if address in (0, 4) and width == 32:
            return 0x20001000 if address == 0 else 0x101
        assert address == RAM and width == 32, (address, width)
        return WORD

    def get_state(self):
        assert not self.closed
        return SimpleNamespace(name=self.state)

    def halt(self):
        self.state = "HALTED"

    def resume(self):
        self.state = "RUNNING"

    def assert_reset(self, asserted):
        # Managed-operation cleanup releases reset after ordinary hardware calls.
        # Model that native capability; never permit reset assertion in this bench.
        assert asserted is False and not self.closed
        self.bench.hardware.append(("reset_release", self.number))


class IndependentRecoveryTests(unittest.TestCase):
    @contextmanager
    def bench(self, *, initial_provider="jlink", initial_uid=UID):
        with isolated_project() as root, tiered_test_server(root) as server:
            f = SimpleNamespace(
                root=root,
                server=server,
                rows=[
                    ProbeInfo(
                        initial_uid, "Synthetic " + initial_provider, "", family=initial_provider
                    )
                ],
                selected_uid=initial_uid,
                selected_family=initial_provider,
                selected_target=TARGET,
                sessions=[],
                hardware=[],
                wire=[],
                fail_open=False,
                wrong_identity=False,
            )

            def call(name, args):
                row = {"tool": name, "arguments": copy.deepcopy(args)}
                f.wire.append(row)
                try:
                    row["reply"] = public_text(server, name, args)
                    return row["reply"]
                except Exception as exc:
                    row["error"] = f"{type(exc).__name__}: {exc}"
                    raise

            def listing(*args):
                return NativeProbeListing(tuple(f.rows), (), 0, False, "synthetic boundary", "")

            def choose(**kwargs):
                assert kwargs["unique_id"] == f.selected_uid, kwargs
                assert kwargs["options"]["target_override"] == f.selected_target, kwargs
                assert kwargs["auto_open"] is False
                f.last_options = kwargs["options"]
                return SyntheticSession(f, kwargs["options"])

            def forbidden(*args, **kwargs):
                raise AssertionError("M12 host fixture attempted real discovery/CLI/target I/O")

            f.flow = PublicSetup(server, call=call)

            class ObservedHardware(native.PyOCDSWDInterface):
                def open(self, **kwargs):
                    handle = super().open(**kwargs)
                    # Production workers enrich native session metadata with
                    # the observed probe family. These are synthetic I/O facts.
                    return replace(
                        handle, metadata=replace(handle.metadata, probe_family=f.selected_family)
                    )

            server.configure_tiered_test_seams(backend=ObservedHardware())
            with (
                patch.object(server, "list_connected_probes_detailed", side_effect=listing),
                patch.object(server._hardware_inventory, "native_uarts", return_value=[]),
                patch.object(server._hardware_inventory, "vendor_uarts", return_value=[]),
                patch.object(server, "_run_cmd", side_effect=forbidden),
                patch.object(native, "_run_cmd", side_effect=forbidden),
                patch.object(
                    native.ConnectHelper, "get_all_connected_probes", side_effect=forbidden
                ),
                patch.object(native.ConnectHelper, "session_with_chosen_probe", side_effect=choose),
            ):
                try:
                    f.flow.call("initialization_handshake", {})
                    route = f.flow.overview("Independent M12 recovery")["routes"][0]
                    f.board = route["board_id"]
                    pdf = root / "synthetic.pdf"
                    pdf.write_bytes(
                        b"%PDF-1.4\n% Synthetic host-only document, no physical claim\n"
                    )
                    _, setup = f.flow.accept(route, pdf, "nRF52840-QIAA", tier="setup-full")
                    pending = f.flow.start(setup)
                    self.assertEqual(pending["status"], "setup_research_required", pending)
                    f.flow.answer(f.board, pending, f.flow.target_answer(pending, TARGET))
                    completed = f.flow.fix(setup)
                    self.assertEqual(completed["status"], "setup_completed", completed)
                    self.assertEqual(
                        f.flow.json("get_capabilities", {"board_id": f.board})["tier"], "setup-full"
                    )
                    self.assertEqual(self.validate(f)["status"], "validation_passed")
                    f.profile = next((root / ".firm" / "boards").glob(f.board + ".*"))
                    f.original = f.profile.read_bytes()
                    f.flow.call("disconnect", {"board_id": f.board})
                    yield f
                finally:
                    for board in server.connection_manager.assigned_board_ids():
                        f.flow.call("disconnect", {"board_id": board})
                    self.assertTrue(
                        all(s.closed for s in f.sessions), "owned synthetic session leaked"
                    )
                    if hasattr(f, "profile"):
                        self.assertEqual(f.profile.read_bytes(), f.original)
                    evidence = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / self.id()
                    evidence.mkdir(parents=True, exist_ok=True)
                    (evidence / "wire.json").write_text(
                        json.dumps(f.wire, indent=2), encoding="utf-8"
                    )
                    (evidence / "hardware.json").write_text(
                        json.dumps(f.hardware, indent=2), encoding="utf-8"
                    )
                    imports = []
                    for name, module in sorted(sys.modules.items()):
                        path = getattr(module, "__file__", None)
                        if (
                            name.startswith(("pyocd_debug_mcp", "tests."))
                            and path
                            and Path(path).is_file()
                        ):
                            imports.append(
                                {
                                    "module": name,
                                    "path": str(Path(path).resolve()),
                                    "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                                }
                            )
                    (evidence / "imports.json").write_text(
                        json.dumps(imports, indent=2), encoding="utf-8"
                    )
                    server._monitor.closeout("M12 independent fixture cleanup")

    def validate(self, f):
        loaded = f.flow.json(
            "load_setup_tool", {"board_id": f.board, "tool_name": "board_validate"}
        )
        if loaded["status"] != "setup_tool_loaded":
            return loaded
        route = loaded["next_call"]
        return f.flow.json(route["tool"], route["arguments"])

    def outage(self, f):
        f.flow.overview("Independent M12 recovery")
        f.rows.clear()
        with self.assertRaises(Exception) as caught:
            f.flow.call("connect", {"board_id": f.board})
        self.assertIn("assigned probe", str(caught.exception))
        self.assertIn("no longer present", str(caught.exception))
        f.failure = str(caught.exception)
        self.assertIsNone(f.server.connection_manager.maybe_connection(f.board))
        f.rows.append(ProbeInfo(UID, "Synthetic J-Link", "", family="jlink"))
        f.flow.overview("Independent M12 recovery")

    def override(self, f, *, budget=1, target=TARGET):
        initialized = f.flow.call("connect_override-plan", NULL_PLAN)
        self.assertIn("does not require ready_for_code=true", initialized)
        args = {"probe_uid": UID, "target_override": target, "external_board_config": None}
        envelope = {
            "board_id": f.board,
            "hypothesis": f"Normal connect failed: {f.failure}",
            "strategy": "Review current exact synthetic probe and target, connect, then validate before reading SRAM.",
            "hypothesis_made": True,
            "strategy_evaluated": True,
            "expected_fail_return": "A transport refusal leaves disconnected readiness and consumes one attempt.",
            "expected_success_return": "Exact selected session opens without changing the saved normal profile.",
            "max_calls": budget,
            "max_calls_buffer": 0,
            "action_parameters": args,
        }
        plan = f.flow.json("connect_override-plan", envelope)
        self.assertEqual(plan["status"], "plan_accepted", plan)
        return plan

    def read_plan(self, f):
        f.flow.call("read_memory_address-plan", NULL_PLAN)
        return f.flow.plan(
            "read_memory_address", f.board, {"address": RAM, "width": 32, "length": None}
        )

    def test_disconnected_instructions_and_backend_then_fresh_validation(self):
        with self.bench() as f:
            self.outage(f)
            description = next(
                t.description
                for t in asyncio.run(f.server.mcp.list_tools())
                if t.name == "connect_override-plan"
            )
            self.assertIn("Use while disconnected", description)
            plan = self.override(f)
            before = len(f.sessions)
            reply = f.flow.execute(plan["stable_client_fallback"])
            self.assertEqual(reply["status"], "batch_completed", reply)
            self.assertEqual(len(f.sessions), before + 1, "override backend was not executed")
            connection = f.server.connection_manager.connection_for(f.board)
            self.assertIs(connection.handle.session, f.sessions[-1])
            self.assertEqual(connection.handle.probe_uid, UID)
            self.assertEqual(connection.handle.target_override, TARGET)
            self.assertIsNone(f.server.gate_manager.live_identity(f.board))
            reads = [r for r in f.hardware if r[0] == "read"]
            read = self.read_plan(f)
            denied = f.flow.execute(read["stable_client_fallback"])
            self.assertEqual(denied["status"], "batch_failed", denied)
            self.assertIn("board_validate", json.dumps(denied).lower())
            self.assertEqual([r for r in f.hardware if r[0] == "read"], reads)
            self.assertEqual(self.validate(f)["status"], "validation_passed")
            self.assertIsNotNone(f.server.gate_manager.live_identity(f.board))
            read = self.read_plan(f)
            result = f.flow.execute(read["stable_client_fallback"])
            self.assertEqual(result["status"], "batch_completed", result)
            self.assertEqual(batch_word(result), WORD)
            self.assertIn(("read", f.sessions[-1].number, RAM, 32), f.hardware)

    def test_incompatible_validation_cannot_enable_protected_read(self):
        with self.bench() as f:
            self.outage(f)
            plan = self.override(f)
            self.assertEqual(
                f.flow.execute(plan["stable_client_fallback"])["status"], "batch_completed"
            )
            f.wrong_identity = True
            result = self.validate(f)
            self.assertEqual(result["status"], "validation_failed", result)
            self.assertEqual(result["code"], "validation/silicon-mismatch")
            self.assertIsNone(f.server.gate_manager.live_identity(f.board))
            reads = [r for r in f.hardware if r[0] == "read"]
            read = self.read_plan(f)
            self.assertEqual(
                f.flow.execute(read["stable_client_fallback"])["status"], "batch_failed"
            )
            self.assertEqual([r for r in f.hardware if r[0] == "read"], reads)

    def test_failed_backend_spends_each_call_and_relocks_at_exhaustion(self):
        with self.bench() as f:
            self.outage(f)
            plan = self.override(f, budget=2)
            f.fail_open = True
            before = len(f.sessions)
            for remaining in (1, 0):
                result = f.flow.execute(plan["stable_client_fallback"])
                self.assertEqual(result["status"], "batch_failed", result)
                self.assertIn("M12 controlled exceptional transport failure", json.dumps(result))
                self.assertIsNone(f.server.connection_manager.maybe_connection(f.board))
                self.assertTrue(f.sessions[-1].closed)
                active = f.server.plan_engine.active_plan("connect_override", f.board)
                self.assertEqual(active.remaining_calls if active else 0, remaining)
            self.assertEqual(len(f.sessions), before + 2)
            self.assertFalse(f.server.tool_registry.is_unlocked("connect_override", f.board))
            replay = f.flow.execute(plan["stable_client_fallback"])
            self.assertEqual(replay["status"], "batch_failed", replay)
            self.assertEqual(len(f.sessions), before + 2)

    def test_exact_probe_target_and_board_cannot_change_after_plan(self):
        with self.bench() as f:
            self.outage(f)
            plan = self.override(f)
            before = len(f.sessions)
            for field, value in (
                ("probe_uid", "OTHER-PROBE"),
                ("target_override", "cortex_m"),
                ("board_id", "other_logical_board"),
            ):
                changed = copy.deepcopy(plan["stable_client_fallback"])
                changed["arguments"]["actions"][0]["arguments"][field] = value
                if field == "board_id":
                    changed["arguments"]["board_id"] = value
                result = f.flow.execute(changed)
                self.assertEqual(result["status"], "batch_failed", result)
                self.assertEqual(len(f.sessions), before)
            self.assertEqual(
                f.flow.execute(plan["stable_client_fallback"])["status"], "batch_completed"
            )

    def test_intervening_normal_session_invalidates_disconnected_plan(self):
        with self.bench() as f:
            self.outage(f)
            plan = self.override(f, budget=2)
            f.flow.call("connect", {"board_id": f.board})
            current = f.server.connection_manager.connection_for(f.board)
            before = len(f.sessions)
            result = f.flow.execute(plan["stable_client_fallback"])
            self.assertEqual(result["status"], "batch_failed", result)
            self.assertIn("session", json.dumps(result).lower())
            self.assertIs(f.server.connection_manager.connection_for(f.board), current)
            self.assertEqual(len(f.sessions), before)
            self.assertIsNone(f.server.plan_engine.active_plan("connect_override", f.board))

    def test_closed_run_relocks_override_before_backend(self):
        with self.bench() as f:
            self.outage(f)
            plan = self.override(f)
            before = len(f.sessions)
            f.server.plan_engine.close_run()
            result = f.flow.execute(plan["stable_client_fallback"])
            self.assertEqual(result["status"], "batch_failed", result)
            self.assertEqual(len(f.sessions), before)
            self.assertFalse(f.server.tool_registry.is_unlocked("connect_override", f.board))

    def test_old_fallback_has_no_authority_in_fresh_server_run(self):
        with self.bench() as f:
            self.outage(f)
            plan = self.override(f)
            old_run = f.server.server_run.run_id
            before = len(f.sessions)
            try:
                with tiered_test_server(f.root) as restarted:
                    self.assertNotEqual(restarted.server_run.run_id, old_run)
                    # No authority is copied into the new process-local Server Run.
                    result = json.loads(
                        public_text(
                            restarted,
                            plan["stable_client_fallback"]["tool_name"],
                            plan["stable_client_fallback"]["arguments"],
                        ).split("\n", 1)[0]
                    )
                    self.assertEqual(result["status"], "batch_failed", result)
                    self.assertIsNone(
                        restarted.plan_engine.active_plan("connect_override", f.board)
                    )
                    self.assertFalse(
                        restarted.tool_registry.is_unlocked("connect_override", f.board)
                    )
                    self.assertEqual(len(f.sessions), before)
                    restarted._monitor.closeout("M12 restarted run cleanup")
            finally:
                # Restore the outer fixture's import identity after nested isolation.
                sys.modules["pyocd_debug_mcp.server"] = f.server
                sys.modules["pyocd_debug_mcp"].server = f.server

    def test_normal_profile_control_requires_new_validation_and_keeps_other_guidance(self):
        with self.bench() as f:
            f.flow.overview("Independent M12 recovery")
            f.flow.call("connect", {"board_id": f.board})
            self.assertIsNone(f.server.gate_manager.live_identity(f.board))
            read = self.read_plan(f)
            self.assertEqual(
                f.flow.execute(read["stable_client_fallback"])["status"], "batch_failed"
            )
            self.assertEqual(self.validate(f)["status"], "validation_passed")
            read = self.read_plan(f)
            self.assertEqual(batch_word(f.flow.execute(read["stable_client_fallback"])), WORD)
            guidance = f.flow.call("connect_under_reset-plan", NULL_PLAN)
            self.assertIn("Do not populate this hardware plan until", guidance)
            self.assertIn("ready_for_code=true", guidance)

    def test_saved_remote_profile_has_explicit_local_validation_after_override(self):
        with self.bench(initial_provider="remote", initial_uid="remote:127.0.0.1:9") as f:
            self.outage(f)
            f.selected_uid = UID
            f.selected_family = "jlink"
            # A retained remote registration remains an inventory option after
            # the controlled outage, matching the physical reproduction.
            f.rows.append(
                ProbeInfo("remote:127.0.0.1:9", "Synthetic retained remote", "", family="remote")
            )
            overview = f.flow.overview("Independent M12 recovery")
            local = next(c for c in overview["connections"] if c["probe_family"] == "jlink")
            ordinary = f.flow.json(
                "setup_overview",
                {
                    "board_names": ["Independent M12 recovery"],
                    "connection_assignments": {"Independent M12 recovery": local["connection_id"]},
                },
            )
            self.assertEqual(ordinary["status"], "setup_probe_unavailable", ordinary)
            saved_route = f.flow.json(
                "load_setup_tool", {"board_id": f.board, "tool_name": "board_validate"}
            )["next_call"]
            plan = self.override(f)
            self.assertEqual(
                f.flow.execute(plan["stable_client_fallback"])["status"], "batch_completed"
            )
            connection = f.server.connection_manager.connection_for(f.board)
            self.assertEqual(connection.handle.metadata.probe_uid, UID)
            self.assertEqual(connection.handle.metadata.probe_family, "jlink")
            self.assertIsNone(f.server.gate_manager.live_identity(f.board))
            reads = [row for row in f.hardware if row[0] == "read"]
            read = self.read_plan(f)
            self.assertEqual(
                f.flow.execute(read["stable_client_fallback"])["status"], "batch_failed"
            )
            self.assertEqual([row for row in f.hardware if row[0] == "read"], reads)
            for selector in (saved_route["arguments"]["probe_id"], "probeid:jlink:other-probe"):
                with self.assertRaisesRegex(Exception, "exact current setup_overview assignment"):
                    f.flow.call("board_validate", {"board_id": f.board, "probe_id": selector})
            other_board = f.flow.json(
                "load_setup_tool", {"board_id": "other_board", "tool_name": "board_validate"}
            )
            self.assertEqual(other_board["status"], "setup_assignment_required", other_board)
            self.assertEqual([row for row in f.hardware if row[0] == "read"], reads)
            # The advertised call itself must work, without substituting the
            # explicit selector to repair the loader's continuation.
            loaded = f.flow.json(
                "load_setup_tool", {"board_id": f.board, "tool_name": "board_validate"}
            )
            self.assertEqual(loaded["status"], "setup_tool_loaded", loaded)
            self.assertEqual(loaded["next_call"]["arguments"]["probe_id"], local["connection_id"])
            validation = f.flow.json(loaded["next_call"]["tool"], loaded["next_call"]["arguments"])
            self.assertEqual(validation["status"], "validation_passed", validation)
            try:
                validation = f.flow.json(
                    "board_validate", {"board_id": f.board, "probe_id": local["connection_id"]}
                )
            except Exception as exc:
                self.fail(
                    "Reviewed exceptional local session has no explicit fresh validation route: "
                    + str(exc)
                )
            self.assertEqual(validation["status"], "validation_passed", validation)
            self.assertEqual(
                batch_word(f.flow.execute(self.read_plan(f)["stable_client_fallback"])), WORD
            )
            # The exceptional assignment must not turn into normal transport
            # policy. A later ordinary reconnect uses the original remote profile
            # and cannot reuse this local selector or its readiness.
            f.flow.call("disconnect", {"board_id": f.board})
            f.flow.overview("Independent M12 recovery")
            f.selected_uid = "remote:127.0.0.1:9"
            f.selected_family = "remote"
            f.flow.call("connect", {"board_id": f.board})
            self.assertIsNone(f.server.gate_manager.live_identity(f.board))
            self.assertEqual(
                f.flow.execute(self.read_plan(f)["stable_client_fallback"])["status"],
                "batch_failed",
            )
            reads = [row for row in f.hardware if row[0] == "read"]
            with self.assertRaisesRegex(Exception, "exact current setup_overview assignment"):
                f.flow.call("board_validate", loaded["next_call"]["arguments"])
            self.assertEqual([row for row in f.hardware if row[0] == "read"], reads)
            self.assertEqual(self.validate(f)["status"], "validation_passed")
            self.assertEqual(
                batch_word(f.flow.execute(self.read_plan(f)["stable_client_fallback"])), WORD
            )

    def test_cross_provider_validation_still_checks_live_identity(self):
        with self.bench(initial_provider="remote", initial_uid="remote:127.0.0.1:9") as f:
            self.outage(f)
            f.selected_uid, f.selected_family = UID, "jlink"
            plan = self.override(f)
            self.assertEqual(
                f.flow.execute(plan["stable_client_fallback"])["status"], "batch_completed"
            )
            f.wrong_identity = True
            validation = self.validate(f)
            self.assertEqual(validation["status"], "validation_failed", validation)
            self.assertEqual(validation["code"], "validation/silicon-mismatch", validation)
            self.assertIsNone(f.server.gate_manager.live_identity(f.board))
            reads = [row for row in f.hardware if row[0] == "read"]
            self.assertEqual(
                f.flow.execute(self.read_plan(f)["stable_client_fallback"])["status"],
                "batch_failed",
            )
            self.assertEqual([row for row in f.hardware if row[0] == "read"], reads)

    def test_validated_override_has_no_readiness_or_selection_in_fresh_run(self):
        with self.bench(initial_provider="remote", initial_uid="remote:127.0.0.1:9") as f:
            self.outage(f)
            f.selected_uid, f.selected_family = UID, "jlink"
            plan = self.override(f)
            self.assertEqual(
                f.flow.execute(plan["stable_client_fallback"])["status"], "batch_completed"
            )
            loaded = f.flow.json(
                "load_setup_tool", {"board_id": f.board, "tool_name": "board_validate"}
            )
            self.assertEqual(
                f.flow.json(loaded["next_call"]["tool"], loaded["next_call"]["arguments"])[
                    "status"
                ],
                "validation_passed",
            )
            self.assertTrue(
                f.flow.json("get_setup_status", {"board_id": f.board})["ready_for_code"]
            )
            hardware = list(f.hardware)
            try:
                with tiered_test_server(f.root) as restarted:
                    with (
                        patch.object(restarted, "_run_cmd", side_effect=AssertionError("real CLI")),
                        patch.object(
                            restarted,
                            "list_connected_probes_detailed",
                            side_effect=AssertionError("real discovery"),
                        ),
                    ):
                        fresh = PublicSetup(restarted)
                        status = fresh.json("get_setup_status", {"board_id": f.board})
                        route = fresh.json(
                            "load_setup_tool", {"board_id": f.board, "tool_name": "board_validate"}
                        )
                        f.wire.append(
                            {
                                "fresh_run": restarted.server_run.run_id,
                                "setup_status": status,
                                "validation_loader": route,
                            }
                        )
                        self.assertNotEqual(restarted.server_run.run_id, f.server.server_run.run_id)
                        self.assertTrue(status["configuration_ready"], status)
                        self.assertFalse(status["ready_for_code"], status)
                        self.assertFalse(status["live_session_ready"], status)
                        self.assertEqual(route["status"], "setup_assignment_required", route)
                        self.assertIsNone(restarted.connection_manager.maybe_connection(f.board))
                        self.assertIsNone(restarted.gate_manager.live_identity(f.board))
                        self.assertEqual(f.hardware, hardware)
                    restarted._monitor.closeout("M12 fresh run cleanup")
            finally:
                sys.modules["pyocd_debug_mcp.server"] = f.server
                sys.modules["pyocd_debug_mcp"].server = f.server

    def test_cross_provider_wrong_backend_target_cannot_inherit_profile_authority(self):
        with self.bench(initial_provider="remote", initial_uid="remote:127.0.0.1:9") as f:
            self.outage(f)
            f.selected_uid, f.selected_family, f.selected_target = UID, "jlink", "cortex_m"
            plan = self.override(f, target=f.selected_target)
            self.assertEqual(
                f.flow.execute(plan["stable_client_fallback"])["status"], "batch_completed"
            )
            connection = f.server.connection_manager.connection_for(f.board)
            reads = [row for row in f.hardware if row[0] == "read"]
            validation = self.validate(f)
            self.assertEqual(validation["status"], "validation_blocked", validation)
            self.assertIn("backend target differs", json.dumps(validation))
            self.assertIsNone(f.server.gate_manager.live_identity(f.board))
            self.assertIs(f.server.connection_manager.connection_for(f.board), connection)
            self.assertEqual(
                f.flow.execute(self.read_plan(f)["stable_client_fallback"])["status"],
                "batch_failed",
            )
            self.assertEqual([row for row in f.hardware if row[0] == "read"], reads)

    def test_override_does_not_take_another_boards_exact_selection(self):
        with self.bench(initial_provider="remote", initial_uid="remote:127.0.0.1:9") as f:
            self.outage(f)
            f.selected_uid, f.selected_family = UID, "jlink"
            inventory = f.flow.json("setup_overview", {})
            local = next(c for c in inventory["connections"] if c["probe_family"] == "jlink")
            other = f.flow.json(
                "setup_overview",
                {
                    "board_names": ["Other M12 board"],
                    "connection_assignments": {"Other M12 board": local["connection_id"]},
                },
            )["routes"][0]["board_id"]
            plan = self.override(f)
            before = len(f.sessions)
            reply = f.flow.execute(plan["stable_client_fallback"])
            self.assertEqual(reply["status"], "batch_failed", reply)
            self.assertIn("assigned to another logical board", json.dumps(reply))
            self.assertEqual(len(f.sessions), before + 1)
            self.assertTrue(f.sessions[-1].closed)
            self.assertIsNone(f.server.connection_manager.maybe_connection(f.board))
            self.assertIsNone(f.server.gate_manager.live_identity(f.board))
            self.assertEqual(
                f.server.assignment_store.connection_for(other), local["connection_id"]
            )


if __name__ == "__main__":
    unittest.main()
