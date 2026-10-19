"""Independent M09 public-dispatch and real worker/proxy regressions."""

from __future__ import annotations

import json
import sys
import time
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.adapters import swd_process
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from pyocd_debug_mcp.target_errors import TargetConnectionError, TargetInputError
from tests.tiered_acceptance_support import (
    isolated_project,
    public_call,
    public_text,
    tiered_test_server,
)


class RegisterPublicTests(unittest.TestCase):
    def fixture(self):
        project = isolated_project()
        root = project.__enter__()
        self.addCleanup(project.__exit__, None, None, None)
        context = tiered_test_server(root)
        server = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        journal = root / "worker.jsonl"
        backend = swd_process.ProcessIsolatedSWDInterface(
            worker_argv=(
                sys.executable,
                "-m",
                "tests.b01_provider_backend",
                str(journal),
            )
        )
        server.configure_tiered_test_seams(backend=backend)
        self.enterContext(
            patch.object(
                server,
                "_validation_inventory",
                return_value=ValidationInventory(
                    probes=(ValidationProbe("b01-probe", "Fixture probe", "jlink", "b01-probe"),)
                ),
            )
        )
        connected = public_text(
            server,
            "connect",
            {
                "board_id": "b01",
                "probe_uid": "b01-probe",
                "target": "cortex_m",
            },
        )
        self.assertIn("Connected", connected)
        self.addCleanup(public_text, server, "disconnect", {"board_id": "b01"})
        return server, journal

    def test_both_public_setters_refuse_without_worker_write_and_keep_exact_session(self):
        server, journal = self.fixture()
        connection = server.connection_manager.connection_for("b01")
        handle = connection.handle
        identity = (
            connection.connection_id,
            connection.runtime_session.session_id,
            handle.metadata,
            handle.worker,
            handle.worker._process.pid,
        )
        for tool, register in (
            ("write_cpu_register_raw", "r0"),
            ("set_execution_state_raw", "xpsr"),
        ):
            for value in (-1, "-0x2", "-17"):
                with self.subTest(tool=tool, value=value):
                    before = journal.read_bytes()
                    reply = public_call(
                        server,
                        tool,
                        {
                            "board_id": "b01",
                            "name": register,
                            "value": value,
                        },
                    )
                    self.assertEqual(reply["status"], "error")
                    self.assertEqual(reply["code"], "input/invalid-argument")
                    self.assertEqual(journal.read_bytes(), before, "no backend request on refusal")
                    self.assertIs(server.connection_manager.connection_for("b01"), connection)
                    self.assertEqual(
                        (
                            connection.connection_id,
                            connection.runtime_session.session_id,
                            handle.metadata,
                            handle.worker,
                            handle.worker._process.pid,
                        ),
                        identity,
                    )
                    self.assertIsNone(connection.runtime_session.closed_at)
                    self.assertIsNone(handle.worker._process.poll())
                    read = public_text(
                        server, "read_cpu_register", {"board_id": "b01", "name": "r0"}
                    )
                    self.assertIn("12345678", read)
        operations = [json.loads(row)["operation"] for row in journal.read_text().splitlines()]
        self.assertEqual(operations, ["open"] + ["read_core_register"] * 6)

    def test_actual_worker_argument_refusal_is_nonfatal_and_wide_values_survive(self):
        server, journal = self.fixture()
        connection = server.connection_manager.connection_for("b01")
        worker = connection.handle.worker
        before = journal.read_bytes()
        request_id = worker._request_id
        with self.assertRaises(TargetInputError):
            worker.call("write_core_register", {"name": "r0", "value": -1})
        self.assertEqual(worker._request_id, request_id + 1)
        self.assertEqual(journal.read_bytes(), before)
        self.assertFalse(worker._closed)
        self.assertIn(
            "12345678", public_text(server, "read_cpu_register", {"board_id": "b01", "name": "r0"})
        )
        for value in (0, 0xFFFFFFFF, 0x123456789ABCDEF0):
            reply = public_call(
                server,
                "write_cpu_register_raw",
                {
                    "board_id": "b01",
                    "name": "d0",
                    "value": value,
                },
            )
            self.assertEqual(reply["status"], "ok")
        writes = [
            json.loads(row)
            for row in journal.read_text().splitlines()
            if json.loads(row)["operation"] == "write_core_register"
        ]
        self.assertEqual([row["value"] for row in writes], [0, 0xFFFFFFFF, 0x123456789ABCDEF0])
        self.assertIs(server.connection_manager.connection_for("b01"), connection)

    def test_arbitrary_backend_value_error_is_not_input_validation(self):
        server, journal = self.fixture()
        worker = server.connection_manager.connection_for("b01").handle.worker
        reply = public_call(
            server,
            "write_cpu_register_raw",
            {
                "board_id": "b01",
                "name": "backend_failure",
                "value": 1,
            },
        )
        self.assertEqual(reply["status"], "error")
        self.assertNotEqual(reply["code"], "input/invalid-argument")
        self.assertIn("B01 actual backend dispatch failure", reply["message"])
        self.assertIn("provider_failure", reply["message"])
        with self.assertRaises(TargetConnectionError):
            worker.call("write_core_register", {"name": "backend_failure", "value": 1})
        self.assertIn('"value": 1', journal.read_text())

    def test_genuine_termination_and_broken_pipe_invalidate_the_proxy(self):
        for fault in ("termination", "broken_pipe"):
            with self.subTest(fault=fault):
                server, _ = self.fixture()
                worker = server.connection_manager.connection_for("b01").handle.worker
                if fault == "termination":
                    worker._process.terminate()
                    worker._process.wait(timeout=10)
                else:
                    worker._process.stdin.close()
                with self.assertRaises(TargetConnectionError):
                    worker.call(
                        "read_core_register", {"name": "r0"}, deadline=time.monotonic() + 10
                    )
                self.assertTrue(worker._closed)
                self.assertTrue(worker._cleanup_confirmed)
                self.assertIsNotNone(worker._process.poll())
                self.assertIsNone(worker._marker)
                with self.assertRaises(TargetConnectionError):
                    worker.call("read_core_register", {"name": "r0"})
                with self.assertRaises(Exception):
                    public_text(server, "read_cpu_register", {"board_id": "b01", "name": "r0"})
                self.assertIsNone(server.connection_manager.maybe_connection("b01"))
                # Release this board before the next independent project fixture.
                public_text(server, "disconnect", {"board_id": "b01"})
                self.doCleanups()


if __name__ == "__main__":
    unittest.main()
