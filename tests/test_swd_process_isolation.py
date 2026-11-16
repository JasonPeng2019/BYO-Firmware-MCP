from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from pyocd_debug_mcp.timeouts import DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS


class ProcessIsolationContractTests(unittest.TestCase):
    def test_worker_bootstrap_isolates_import_and_between_request_stdout(self) -> None:
        script = r"""
import builtins
import json
import os
import sys
import types

from pyocd_debug_mcp.adapters import provider_worker

original_import = builtins.__import__

def injected_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "pyocd_debug_mcp.adapters.provider_worker_runtime":
        print("import-python", flush=True)
        os.write(1, b"import-fd\n")
        runtime = types.ModuleType(name)

        def run(protocol):
            protocol.write(b'{"version":1,"ready":true}\n')
            protocol.flush()
            first = json.loads(sys.stdin.buffer.readline())
            protocol.write(
                (json.dumps({"version": 1, "request_id": first["request_id"],
                             "ok": True, "result": "first"}, separators=(",", ":")) + "\n").encode()
            )
            protocol.flush()
            print("idle-python", flush=True)
            os.write(1, b"idle-fd\n")
            second = json.loads(sys.stdin.buffer.readline())
            protocol.write(
                (json.dumps({"version": 1, "request_id": second["request_id"],
                             "ok": True, "result": "second"}, separators=(",", ":")) + "\n").encode()
            )
            protocol.flush()

        runtime.main = run
        return runtime
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = injected_import
provider_worker.main()
"""
        requests = (
            '{"version":1,"request_id":1,"operation":"one","arguments":{}}\n'
            '{"version":1,"request_id":2,"operation":"two","arguments":{}}\n'
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            input=requests,
            capture_output=True,
            text=True,
            timeout=DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [json.loads(line) for line in result.stdout.splitlines()],
            [
                {"version": 1, "ready": True},
                {"version": 1, "request_id": 1, "ok": True, "result": "first"},
                {"version": 1, "request_id": 2, "ok": True, "result": "second"},
            ],
        )
        for marker in ("import-python", "import-fd", "idle-python", "idle-fd"):
            self.assertIn(marker, result.stderr)

    @staticmethod
    def _client(mode: str, *, deadline: float | None = None):
        from pyocd_debug_mcp.adapters.swd_process import _WorkerClient

        worker = Path(__file__).with_name("fake_provider_worker.py")
        return _WorkerClient(
            worker_argv=(sys.executable, str(worker), mode),
            deadline=deadline,
        )

    def test_wrong_id_malformed_frame_crash_and_timeout_are_terminal(self) -> None:
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        for mode in ("open_hang", "crash", "wrong_id", "malformed_reply"):
            with self.subTest(mode=mode):
                client = self._client(mode)
                with self.assertRaises(TargetConnectionError):
                    client.call("get_state", {}, timeout=0.05)
                with self.assertRaises(TargetConnectionError):
                    client.call("get_state", {}, timeout=0.05)

    def test_actual_worker_no_session_protocol_does_not_import_native_provider(self) -> None:
        from pyocd_debug_mcp.adapters.swd_process import _WorkerClient
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        # Refuse only the unneeded native import; use the actual worker loop and
        # parent protocol, keeping stdin open for both replies and normal close.
        script = """
import builtins
from pyocd_debug_mcp.adapters import provider_worker
original = builtins.__import__
def importing(name, *args, **kwargs):
    if name == "pyocd" or name.startswith("pyocd.") or name == "pyocd_debug_mcp.adapters.swd_pyocd":
        raise AssertionError("no-session protocol imported the native provider")
    return original(name, *args, **kwargs)
builtins.__import__ = importing
provider_worker.main()
"""
        client = _WorkerClient(
            worker_argv=[sys.executable, "-B", "-c", script],
            deadline=time.monotonic() + 10,
        )
        try:
            for _ in range(2):
                with self.assertRaisesRegex(TargetConnectionError, "no live target session"):
                    client.call("get_state", {}, timeout=1)
        finally:
            client.close()
        self.assertTrue(client._cleanup_confirmed)
        self.assertIsNotNone(client._process.returncode)
        self.assertIsNone(client._marker)

    def test_faulted_worker_does_not_interrupt_a_healthy_worker(self) -> None:
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        healthy = self._client("good")
        faulted = self._client("open_hang")
        try:
            self.assertEqual(healthy.call("get_state", {}, timeout=1), "RUNNING")
            with self.assertRaises(TargetConnectionError):
                faulted.call("get_state", {}, timeout=0.05)
            self.assertIsNotNone(faulted._process.returncode)
            self.assertEqual(healthy.call("get_state", {}, timeout=1), "RUNNING")
        finally:
            healthy.close()
        self.assertIsNotNone(healthy._process.returncode)

    def test_child_accepts_large_trusted_values_and_dispatches_complete_surface(self) -> None:
        from pyocd_debug_mcp.adapters.provider_worker_runtime import (
            _board,
            _dispatch,
            _validate_arguments,
        )
        from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
        from pyocd_debug_mcp.adapters.swd_process import _board_record
        from pyocd_debug_mcp.board_config import BoardConfig
        from pyocd_debug_mcp.timeouts import default_server_timeout_config

        long_text = "x" * 20_000
        board = BoardConfig(
            board_id="fake",
            display_name=long_text,
            mcu_family="fake",
            probe_family="fake",
            pyocd_target="fake",
            probe_type="fake",
            probe_hint_terms=tuple(f"probe-{index}" for index in range(1_000)),
            serial_hint_terms=tuple(f"serial-{index}" for index in range(1_000)),
            test_addr=0,
            source_path=Path(long_text),
        )
        record = json.loads(json.dumps(_board_record(board)))
        self.assertEqual(_board(record), board)
        _validate_arguments("read_memory_block", {"address": 0, "length": 70_000})
        _validate_arguments("flash", {"path": long_text, "halt_after_reset": False})

        reset = Mock(return_value=None)
        session = SimpleNamespace(
            board=SimpleNamespace(name="fake board"),
            probe=SimpleNamespace(unique_id="fake-probe", description="fake probe"),
            target=SimpleNamespace(part_number="fake-part"),
        )
        handle = TargetSessionHandle(session, board, "fake-probe", "fake-route", None)

        class FakeAdapter:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def __getattr__(self, name: str):
                results: dict[str, object] = {
                    "open": handle,
                    "connect_under_reset": handle,
                    "get_state": "HALTED",
                    "read_memory": 0x12,
                    "read_memory_block": [0, 1, 255],
                    "write_memory": None,
                    "read_core_register": 17,
                    "write_core_register": None,
                    "supported_core_registers": ("pc", "r0"),
                    "halt": None,
                    "resume": None,
                    "step": None,
                    "reset": None,
                    "reset_and_halt": None,
                    "release_reset": None,
                    "flash": None,
                    "recover": None,
                    "supports_recovery": True,
                    "set_breakpoint": None,
                    "remove_breakpoint": None,
                    "close": None,
                }
                if name not in results:
                    raise AttributeError(name)

                def call(*_args: object, **_kwargs: object) -> object:
                    self.calls.append(name)
                    if name == "release_reset":
                        reset(False)
                    return results[name]

                return call

        open_arguments = {
            "board": record,
            "unique_id": "fake-probe",
            "target": None,
            "server_timeouts": default_server_timeout_config().to_record(),
            "protocol": "swd",
            "connect_mode": "attach",
            "pack_path": None,
            "pack_sha256": None,
            "pdsc_device": None,
            "frequency_hz": 1_000_000,
        }
        with patch(
            "pyocd_debug_mcp.probe_inventory.probe_family_from_pyocd_probe",
            return_value="fake",
        ):
            for operation in ("open", "connect_under_reset"):
                adapter = FakeAdapter()
                opened, metadata, exit_after = _dispatch(
                    cast(Any, adapter), None, operation, dict(open_arguments)
                )
                self.assertIs(opened, handle)
                self.assertEqual(metadata["live_part_number"], "fake-part")
                self.assertFalse(exit_after)

        cases = {
            "get_state": ({}, "HALTED"),
            "read_memory": ({"address": 0, "width_bits": 8}, 0x12),
            "read_memory_block": ({"address": 0, "length": 3}, [0, 1, 255]),
            "write_memory": ({"address": 0, "value": 0x12, "width_bits": 8}, None),
            "read_core_register": ({"name": "pc"}, 17),
            "write_core_register": ({"name": "pc", "value": 17}, None),
            "supported_core_registers": ({}, ["pc", "r0"]),
            "halt": ({}, None),
            "resume": ({}, None),
            "step": ({}, None),
            "reset": ({}, None),
            "reset_and_halt": ({}, None),
            "release_reset": ({}, None),
            "flash": ({"path": long_text, "halt_after_reset": False}, None),
            "recover": ({}, None),
            "supports_recovery": ({"mechanism": "fake"}, True),
            "set_breakpoint": ({"address": 4}, None),
            "remove_breakpoint": ({"address": 4}, None),
        }
        for operation, (arguments, expected) in cases.items():
            with self.subTest(operation=operation):
                _validate_arguments(operation, arguments)
                adapter = FakeAdapter()
                retained, result, exit_after = _dispatch(
                    cast(Any, adapter), handle, operation, arguments
                )
                self.assertIs(retained, handle)
                self.assertEqual(result, expected)
                self.assertFalse(exit_after)
        reset.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
