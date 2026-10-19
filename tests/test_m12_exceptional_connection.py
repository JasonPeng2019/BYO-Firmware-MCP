"""M12 implementer checks through registered MCP dispatch, with host-only target I/O.

The FULL profile is created through public setup using synthetic evidence.
Only native inventory and the target-control backend are replaced.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.guardrails.plan_defs import definition_for_action
from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from pyocd_debug_mcp.target_errors import TargetConnectionError
from tests.m05_public_support import PublicSetup
from tests.test_m05_public_lifecycle import MemoryProbe
from tests.tiered_acceptance_support import (
    isolated_project,
    public_text,
    tiered_test_server,
    write_json,
)


UID = "M12-HOST-PROBE"
RAM = 0x20000000
OUTAGE = "host fixture: normal endpoint unavailable"
OVERRIDE = {"probe_uid": UID, "target_override": "nrf52840", "external_board_config": None}


class ExceptionalConnectionTests(unittest.TestCase):
    @contextmanager
    def project(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = MemoryProbe()
            server.configure_tiered_test_seams(backend=backend)
            inventory = ValidationInventory(
                probes=(ValidationProbe(UID, "Host probe", "jlink", UID),)
            )
            snapshot = snapshot_from_validation_inventory(inventory)
            f = SimpleNamespace(server=server, backend=backend, root=root, wire=[])
            with (
                patch.object(
                    server,
                    "_hardware_inventory",
                    SimpleNamespace(
                        snapshot=lambda: snapshot,
                        validation_inventory=lambda: inventory,
                    ),
                ),
                patch.object(
                    server, "_run_cmd", side_effect=AssertionError("unexpected native command")
                ),
                patch.object(
                    server,
                    "list_connected_probes_detailed",
                    side_effect=AssertionError("unexpected native probe discovery"),
                ),
            ):
                try:
                    flow = PublicSetup(server, call=lambda name, args: self.call(f, name, args))
                    route = flow.overview("M12 host-only board")["routes"][0]
                    f.board = route["board_id"]
                    pdf = root / "synthetic.pdf"
                    pdf.write_bytes(b"%PDF-1.4\n% M12 synthetic host evidence only\n")
                    _, setup_plan = flow.accept(route, pdf, "nRF52840-QIAA", tier="setup-full")
                    pending = flow.start(setup_plan)
                    self.assertEqual(pending["status"], "setup_research_required", pending)
                    flow.answer(f.board, pending, flow.target_answer(pending, "nrf52840"))
                    pending = flow.fix(setup_plan)
                    self.assertEqual(pending["status"], "setup_completed", pending)
                    for board in server.connection_manager.assigned_board_ids():
                        self.call(f, "disconnect", {"board_id": board})
                    flow.overview("M12 host-only board")
                    f.profile_path = server._profile_repository.load(f.board).source_path
                    f.profile_bytes = f.profile_path.read_bytes()
                    yield f
                finally:
                    for board in server.connection_manager.assigned_board_ids():
                        self.call(f, "disconnect", {"board_id": board})
                    if hasattr(f, "profile_bytes"):
                        self.assertEqual(f.profile_path.read_bytes(), f.profile_bytes)
                    if evidence_root := os.environ.get("BYO_MANAGED_TEST_EVIDENCE"):
                        evidence = Path(evidence_root) / self.id()
                        evidence.mkdir(parents=True, exist_ok=True)
                        (evidence / "dispatch.json").write_text(
                            json.dumps(
                                {"wire": f.wire, "hardware": backend.calls},
                                indent=2,
                                default=str,
                            ),
                            encoding="utf-8",
                        )
                        imports = []
                        for name, module in sorted(sys.modules.items()):
                            filename = getattr(module, "__file__", None)
                            if filename and name.startswith(("pyocd_debug_mcp", "tests.")):
                                path = Path(filename).resolve()
                                if path.is_file():
                                    imports.append(
                                        {
                                            "module": name,
                                            "path": str(path),
                                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                        }
                                    )
                        write_json(evidence / "imports.json", {"imports": imports})

    def call(self, f, name, args):
        row = {"tool": name, "arguments": args}
        f.wire.append(row)
        try:
            row["reply"] = public_text(f.server, name, args)
            return row["reply"]
        except Exception as exc:
            row["error"] = str(exc)
            raise

    def plan(self, f, action="connect_override", parameters=None):
        definition = definition_for_action(action)
        self.call(f, definition.plan_tool_name, {key: None for key in definition.null_field_names})
        return json.loads(
            self.call(
                f,
                definition.plan_tool_name,
                {
                    "board_id": f.board,
                    "hypothesis": f"Normal connect failed with '{OUTAGE}'; enumerated {UID} matches the reviewed nRF52840.",
                    "strategy": "Connect once to the exact reviewed probe and target; validate live identity before the planned SRAM read; disconnect on failure.",
                    "hypothesis_made": True,
                    "strategy_evaluated": True,
                    "expected_fail_return": "The selected endpoint still fails; remain disconnected and report its error.",
                    "expected_success_return": "A new unvalidated session on the selected probe; fresh board_validate permits the SRAM read.",
                    "max_calls": 1,
                    "max_calls_buffer": 0,
                    "action_parameters": OVERRIDE if parameters is None else parameters,
                },
            )
        )

    def validate(self, f):
        loaded = json.loads(
            self.call(f, "load_setup_tool", {"board_id": f.board, "tool_name": "board_validate"})
        )
        call = loaded["next_call"]
        return json.loads(self.call(f, call["tool"], call["arguments"]))

    def outage(self, f):
        f.backend.open_error = TargetConnectionError(OUTAGE)
        with self.assertRaisesRegex(Exception, OUTAGE):
            self.call(f, "connect", {"board_id": f.board})
        self.assertIsNone(f.server.connection_manager.maybe_connection(f.board))
        f.backend.open_error = None

    def test_public_guidance_allows_disconnected_recovery_before_validation(self):
        with self.project() as f:
            tools = asyncio.run(f.server.mcp.list_tools())
            description = next(t.description for t in tools if t.name == "connect_override-plan")
            definition = definition_for_action("connect_override")
            guidance = self.call(
                f, definition.plan_tool_name, {key: None for key in definition.null_field_names}
            )
            write_json(
                Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / "guidance.json",
                {"description": description, "initialization": guidance},
            )
            self.assertNotIn("until setup status reports ready_for_code=true", guidance)
            self.assertNotIn("Complete setup routing and board_validate before", description)
            for text in (guidance, description):
                self.assertIn("disconnected", text)
                self.assertIn("board_validate", text)
            self.assertIn("exact error", guidance)
            for action in ("read_memory_address", "connect_under_reset"):
                definition = definition_for_action(action)
                ordinary = self.call(
                    f, definition.plan_tool_name, {key: None for key in definition.null_field_names}
                )
                self.assertIn("until setup status reports ready_for_code=true", ordinary)

    def test_normal_failure_override_fresh_validation_read_and_saved_reconnect(self):
        with self.project() as f:
            self.outage(f)
            plan = self.plan(f)
            self.assertEqual(plan["status"], "plan_accepted")
            call = plan["preferred_call"]
            self.assertIn("Connected", self.call(f, call["tool_name"], call["arguments"]))
            opened = [c[1] for c in f.backend.calls if c[0] == "open"]
            self.assertEqual((opened[-1]["unique_id"], opened[-1]["target"]), (UID, "nrf52840"))
            params = {"address": RAM, "width": 32, "length": None}
            read_plan = self.plan(f, "read_memory_address", params)
            call = read_plan["preferred_call"]
            before = list(f.backend.calls)
            with self.assertRaisesRegex(Exception, "not validated for this connection"):
                self.call(f, call["tool_name"], call["arguments"])
            self.assertEqual(f.backend.calls, before)
            validation = self.validate(f)
            self.assertEqual(validation["status"], "validation_passed", validation)
            self.assertIn("0x12345678", self.call(f, call["tool_name"], call["arguments"]))
            self.assertEqual(f.profile_path.read_bytes(), f.profile_bytes)
            self.call(f, "disconnect", {"board_id": f.board})
            self.call(f, "setup_overview", {"board_names": ["M12 host-only board"]})
            self.assertIn("Connected", self.call(f, "connect", {"board_id": f.board}))
            read_plan = self.plan(f, "read_memory_address", params)
            call = read_plan["preferred_call"]
            with self.assertRaisesRegex(Exception, "not validated for this connection"):
                self.call(f, call["tool_name"], call["arguments"])
            self.assertEqual(self.validate(f)["status"], "validation_passed")

    def test_failed_override_consumes_budget_and_keeps_disconnected_profile(self):
        with self.project() as f:
            self.outage(f)
            plan = self.plan(f)
            f.backend.open_error = TargetConnectionError(
                "host fixture: exceptional endpoint unavailable"
            )
            call = plan["preferred_call"]
            with self.assertRaisesRegex(Exception, "exceptional endpoint unavailable"):
                self.call(f, call["tool_name"], call["arguments"])
            before = list(f.backend.calls)
            with self.assertRaises(Exception):
                self.call(f, call["tool_name"], call["arguments"])
            self.assertEqual(f.backend.calls, before)
            self.assertIsNone(f.server.connection_manager.maybe_connection(f.board))
            self.assertIsNone(f.server.plan_engine.active_plan("connect_override", f.board))

    def test_changed_plan_parameters_and_board_never_open_a_probe(self):
        with self.project() as f:
            self.outage(f)
            plan = self.plan(f)
            call = plan["preferred_call"]
            before = list(f.backend.calls)
            for changed in ({"target_override": "cortex_m"}, {"board_id": "other_board"}):
                with self.subTest(changed=changed), self.assertRaises(Exception):
                    self.call(f, call["tool_name"], {**call["arguments"], **changed})
                self.assertEqual(f.backend.calls, before)

    def test_connected_session_cannot_execute_a_disconnected_override_plan(self):
        with self.project() as f:
            self.outage(f)
            plan = self.plan(f)
            self.call(f, "connect", {"board_id": f.board})
            before = list(f.backend.calls)
            call = plan["preferred_call"]
            with self.assertRaises(Exception):
                self.call(f, call["tool_name"], call["arguments"])
            self.assertEqual(f.backend.calls, before)

    def test_closed_run_cannot_execute_its_accepted_override(self):
        with self.project() as f:
            self.outage(f)
            plan = self.plan(f)
            f.server.plan_engine.close_run()
            before = list(f.backend.calls)
            call = plan["preferred_call"]
            with self.assertRaises(Exception):
                self.call(f, call["tool_name"], call["arguments"])
            self.assertEqual(f.backend.calls, before)


if __name__ == "__main__":
    unittest.main()
