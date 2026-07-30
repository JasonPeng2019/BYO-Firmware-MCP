"""Phase 2: Regression tests for uncovered edge cases and failure modes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from pyocd_debug_mcp.discovery_failures import (
    DiscoveryFailure,
    no_native_probe_failure,
    no_native_uart_failure,
)
from pyocd_debug_mcp.discovery_hooks import (
    DiscoveryHookError,
    decode_hook_stdout,
    parse_hook_declaration,
    resolve_declaration,
)
from pyocd_debug_mcp.serial_resolver import normalize_port_name
from pyocd_debug_mcp.tools.discovery import RetryContext


class NormalizePortNameTests(unittest.TestCase):
    """Edge cases for Windows port prefix stripping and case folding."""

    def test_basic_port_name_lowercased(self) -> None:
        """Normal port names are lowercased."""
        self.assertEqual(normalize_port_name("COM1"), "com1")
        self.assertEqual(normalize_port_name("COM12"), "com12")
        self.assertEqual(normalize_port_name("TTYUSB0"), "ttyusb0")

    def test_windows_device_namespace_prefix_stripped(self) -> None:
        r"""Windows \\. prefix is removed before lowercasing."""
        self.assertEqual(normalize_port_name("\\\\.\\COM1"), "com1")
        self.assertEqual(normalize_port_name("\\\\.\\COM99"), "com99")
        self.assertEqual(normalize_port_name("\\\\.\\TTYUSB0"), "ttyusb0")

    def test_whitespace_stripped(self) -> None:
        """Leading/trailing whitespace is removed."""
        self.assertEqual(normalize_port_name("  COM1  "), "com1")
        self.assertEqual(normalize_port_name("\tCOM5\n"), "com5")
        self.assertEqual(normalize_port_name("  \\\\.\\COM1  "), "com1")

    def test_empty_string_becomes_empty(self) -> None:
        """Empty strings stay empty."""
        self.assertEqual(normalize_port_name(""), "")
        self.assertEqual(normalize_port_name("   "), "")

    def test_mixed_case_and_numbers(self) -> None:
        """Case folding handles alphanumeric strings."""
        self.assertEqual(normalize_port_name("Cu_MotherBoard_2"), "cu_motherboard_2")
        self.assertEqual(normalize_port_name("TTYUSBFAKE"), "ttyusbfake")

    def test_prefix_with_backslash_escape_only_removes_exact_prefix(self) -> None:
        """Only exact \\.\\ prefix is removed, others pass through."""
        # String with backslash but not matching the exact prefix stays
        self.assertEqual(normalize_port_name("\\.COM1"), "\\.com1")


class DecodeHookStdoutTests(unittest.TestCase):
    """Edge cases for hook stdout parsing (UTF-8, JSON, schema validation)."""

    def test_valid_utf8_json_output(self) -> None:
        """Valid UTF-8 JSON is decoded and validated."""
        payload = json.dumps({"status": "success", "data": []}).encode("utf-8")
        # This will fail validation (DiscoveryHookError) because the output doesn't
        # match the expected hook output schema, but that's the next layer
        with self.assertRaises(DiscoveryHookError):
            decode_hook_stdout(payload, expected_kind="probe")

    def test_invalid_utf8_raises_error(self) -> None:
        """Non-UTF-8 bytes are rejected with clear error."""
        payload = b"\x80\x81\x82\x83"  # Invalid UTF-8
        with self.assertRaisesRegex(DiscoveryHookError, "not valid UTF-8"):
            decode_hook_stdout(payload, expected_kind="probe")

    def test_utf8_with_replacement_chars_raises_error(self) -> None:
        """UTF-8 decoding that would require replacement chars is rejected."""
        # Latin-1 encoded string that's not valid UTF-8
        payload = b"Invalid: \xe9"  # é in Latin-1, not valid UTF-8
        with self.assertRaisesRegex(DiscoveryHookError, "not valid UTF-8"):
            decode_hook_stdout(payload, expected_kind="probe")

    def test_invalid_json_raises_error(self) -> None:
        """Valid UTF-8 but invalid JSON is rejected."""
        payload = b'{"incomplete": '
        with self.assertRaisesRegex(DiscoveryHookError, "not valid JSON"):
            decode_hook_stdout(payload, expected_kind="probe")

    def test_valid_json_but_wrong_schema_raises_error(self) -> None:
        """Valid JSON that doesn't match expected output schema is rejected."""
        payload = json.dumps({"wrong": "structure"}).encode("utf-8")
        with self.assertRaises(DiscoveryHookError):
            decode_hook_stdout(payload, expected_kind="probe")

    def test_empty_payload_raises_error(self) -> None:
        """Empty output is rejected as invalid JSON."""
        with self.assertRaisesRegex(DiscoveryHookError, "not valid JSON"):
            decode_hook_stdout(b"", expected_kind="probe")


class ParseHookDeclarationTests(unittest.TestCase):
    """Edge cases for hook declaration validation."""

    def test_not_a_dict_raises_error(self) -> None:
        """Non-dict input is rejected."""
        with self.assertRaisesRegex(DiscoveryHookError, "must be a JSON object"):
            parse_hook_declaration([])

        with self.assertRaisesRegex(DiscoveryHookError, "must be a JSON object"):
            parse_hook_declaration("not a dict")

        with self.assertRaisesRegex(DiscoveryHookError, "must be a JSON object"):
            parse_hook_declaration(None)

    def test_missing_required_field_raises_error(self) -> None:
        """Missing required fields are rejected."""
        # Missing 'kind'
        with self.assertRaisesRegex(DiscoveryHookError, "must be non-empty text"):
            parse_hook_declaration({
                "hook_id": "test",
                "runner": "executable",
                "entrypoint": "/bin/true",
            })

        # Missing 'runner'
        with self.assertRaisesRegex(DiscoveryHookError, "must be non-empty text"):
            parse_hook_declaration({
                "hook_id": "test",
                "kind": "probe",
                "entrypoint": "/bin/true",
            })

    def test_unknown_kind_raises_error(self) -> None:
        """Unknown kind values are rejected."""
        with self.assertRaisesRegex(DiscoveryHookError, "kind must be one of"):
            parse_hook_declaration({
                "hook_id": "test",
                "kind": "unknown_kind",
                "runner": "executable",
                "entrypoint": "/bin/true",
            })

    def test_unknown_runner_raises_error(self) -> None:
        """Unknown runner values are rejected."""
        with self.assertRaisesRegex(DiscoveryHookError, "runner must be one of"):
            parse_hook_declaration({
                "hook_id": "test",
                "kind": "probe",
                "runner": "unknown_runner",
                "entrypoint": "/bin/true",
            })

    def test_extra_fields_raise_error(self) -> None:
        """Extra unknown fields are rejected."""
        with self.assertRaisesRegex(DiscoveryHookError, "unknown field"):
            parse_hook_declaration({
                "hook_id": "test",
                "kind": "probe",
                "runner": "executable",
                "entrypoint": "/bin/true",
                "extra_field": "not allowed",
            })

    def test_wrong_type_fields_raise_error(self) -> None:
        """Wrong-typed fields are rejected."""
        # kind should be a string, not a number
        with self.assertRaisesRegex(DiscoveryHookError, "must be non-empty text"):
            parse_hook_declaration({
                "hook_id": "test",
                "kind": 123,
                "runner": "executable",
                "entrypoint": "/bin/true",
            })

    def test_hook_id_validation(self) -> None:
        """hook_id with invalid characters are rejected."""
        # hook_id must match certain pattern (e.g., no spaces)
        with self.assertRaisesRegex(DiscoveryHookError, "hook_id"):
            parse_hook_declaration({
                "hook_id": "test hook",  # Space not allowed
                "kind": "probe",
                "runner": "executable",
                "entrypoint": "/bin/true",
            })


class ResolveDeclarationTests(unittest.TestCase):
    """Edge cases for declaration resolution (path containment, symlinks, etc.)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

    def _write_file(self, *parts: str) -> Path:
        """Write a file in the temp directory."""
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/bash\necho ok\n")
        return path

    def test_executable_runner_requires_absolute_path(self) -> None:
        """Executable runner requires absolute path."""
        from pyocd_debug_mcp.discovery_hooks import DiscoveryHookDeclaration

        decl = DiscoveryHookDeclaration(
            hook_id="test",
            kind="probe",
            platforms=frozenset(["linux", "windows"]),
            runner="executable",
            entrypoint="not_absolute",
            argv=(),
            timeout_seconds=10.0,
        )
        with self.assertRaisesRegex(DiscoveryHookError, "must be an absolute path"):
            resolve_declaration(decl, root=self.root, source="project")

    def test_project_runner_requires_containment(self) -> None:
        """Project runner requires entrypoint to be under root."""
        from pyocd_debug_mcp.discovery_hooks import DiscoveryHookDeclaration

        decl = DiscoveryHookDeclaration(
            hook_id="test",
            kind="probe",
            platforms=frozenset(["linux"]),
            runner="server-python",
            entrypoint="../outside",
            argv=(),
            timeout_seconds=10.0,
        )
        with self.assertRaisesRegex(DiscoveryHookError, "must stay below"):
            resolve_declaration(decl, root=self.root, source="project")

    def test_project_runner_with_null_bytes_raises_error(self) -> None:
        """Entrypoint with NUL bytes is rejected."""
        from pyocd_debug_mcp.discovery_hooks import DiscoveryHookDeclaration

        decl = DiscoveryHookDeclaration(
            hook_id="test",
            kind="probe",
            platforms=frozenset(["linux"]),
            runner="server-python",
            entrypoint="test\x00file",
            argv=(),
            timeout_seconds=10.0,
        )
        with self.assertRaisesRegex(DiscoveryHookError, "must not contain NUL"):
            resolve_declaration(decl, root=self.root, source="project")

    def test_file_not_found_raises_error(self) -> None:
        """Non-existent entrypoint is rejected."""
        from pyocd_debug_mcp.discovery_hooks import DiscoveryHookDeclaration

        decl = DiscoveryHookDeclaration(
            hook_id="test",
            kind="probe",
            platforms=frozenset(["linux"]),
            runner="server-python",
            entrypoint="nonexistent",
            argv=(),
            timeout_seconds=10.0,
        )
        with self.assertRaisesRegex(DiscoveryHookError, "is not a file"):
            resolve_declaration(decl, root=self.root, source="project")


class RetryContextTests(unittest.TestCase):
    """Edge cases for retry context and retry call generation."""

    def test_retry_call_with_no_tool_returns_none(self) -> None:
        """retry_call() returns None when retry_tool is None."""
        ctx = RetryContext(
            retry_id="test_id",
            run_id="run_id",
            kind="probe",
            created_at=0.0,
            retry_tool=None,
        )
        self.assertIsNone(ctx.retry_call())

    def test_retry_call_deep_copies_arguments(self) -> None:
        """retry_call() makes a deep copy of arguments."""
        original_args = {"nested": {"list": [1, 2, 3]}}
        ctx = RetryContext(
            retry_id="test_id",
            run_id="run_id",
            kind="probe",
            created_at=0.0,
            retry_tool="some_tool",
            retry_arguments=original_args,
        )

        call = ctx.retry_call()
        self.assertIsNotNone(call)
        if call is None:
            self.fail("retry_call() returned None")

        # Modify the returned arguments
        call_dict = cast(dict, call)
        call_dict["arguments"]["nested"]["list"].append(999)

        # Original should be unchanged
        self.assertEqual(original_args["nested"]["list"], [1, 2, 3])

    def test_retry_call_with_empty_arguments(self) -> None:
        """retry_call() handles empty or None arguments."""
        ctx1 = RetryContext(
            retry_id="test_id",
            run_id="run_id",
            kind="probe",
            created_at=0.0,
            retry_tool="tool",
            retry_arguments={},
        )
        call1 = ctx1.retry_call()
        self.assertIsNotNone(call1)
        if call1 is not None:
            self.assertEqual(call1["arguments"], {})

        ctx2 = RetryContext(
            retry_id="test_id",
            run_id="run_id",
            kind="probe",
            created_at=0.0,
            retry_tool="tool",
            retry_arguments={},
        )
        call2 = ctx2.retry_call()
        self.assertIsNotNone(call2)
        if call2 is not None:
            self.assertEqual(call2["arguments"], {})

    def test_retry_context_fields(self) -> None:
        """RetryContext stores all fields correctly."""
        ctx = RetryContext(
            retry_id="id123",
            run_id="run123",
            kind="uart",
            created_at=1234.5,
            retry_tool="my_tool",
            retry_arguments={"arg": "value"},
            board_id="board_abc",
        )
        self.assertEqual(ctx.retry_id, "id123")
        self.assertEqual(ctx.run_id, "run123")
        self.assertEqual(ctx.kind, "uart")
        self.assertEqual(ctx.created_at, 1234.5)
        self.assertEqual(ctx.retry_tool, "my_tool")
        self.assertEqual(ctx.board_id, "board_abc")


class DiscoveryFailureTests(unittest.TestCase):
    """Tests for DiscoveryFailure payload serialization."""

    def test_minimal_failure_payload(self) -> None:
        """Minimal failure with only required fields."""
        failure = DiscoveryFailure(
            code="TEST_CODE",
            message="Test message",
            kind="probe",
        )
        payload = failure.to_payload()

        self.assertEqual(payload["code"], "TEST_CODE")
        self.assertEqual(payload["agent_prompt"], "Test message")
        self.assertEqual(payload["kind"], "probe")
        # Optional fields should not be in payload
        self.assertNotIn("hook_contract_call", payload)
        self.assertNotIn("refresh_call", payload)
        self.assertNotIn("hook_diagnostics", payload)
        self.assertNotIn("native_diagnostics", payload)
        self.assertNotIn("remedies", payload)

    def test_failure_with_all_optional_fields(self) -> None:
        """Failure with all optional fields populated."""
        failure = DiscoveryFailure(
            code="CODE",
            message="msg",
            kind="uart",
            hook_contract_call={"tool": "write_hook"},
            refresh_call={"tool": "refresh_discovery"},
            hook_diagnostics=({"hook_id": "h1"}, {"hook_id": "h2"}),
            native_diagnostics={"version": "1.0"},
            remedies=("remedy1", "remedy2"),
        )
        payload = failure.to_payload()

        self.assertEqual(payload["hook_contract_call"], {"tool": "write_hook"})
        self.assertEqual(payload["refresh_call"], {"tool": "refresh_discovery"})
        hook_diags = cast(list, payload["hook_diagnostics"])
        self.assertEqual(len(hook_diags), 2)
        self.assertEqual(payload["native_diagnostics"], {"version": "1.0"})
        self.assertEqual(payload["remedies"], ["remedy1", "remedy2"])

    def test_no_native_probe_failure_structure(self) -> None:
        """no_native_probe_failure creates correct structure."""
        failure = no_native_probe_failure(
            native_diagnostics={"ls_output": "nothing"},
            hook_diagnostics=[{"hook_id": "test_hook"}],
        )

        payload = failure.to_payload()
        self.assertEqual(failure.kind, "probe")
        self.assertIn("hook_contract_call", payload)  # Always provided
        self.assertEqual(payload["native_diagnostics"], {"ls_output": "nothing"})
        hook_diags = cast(list, payload["hook_diagnostics"])
        self.assertEqual(len(hook_diags), 1)

    def test_no_native_uart_failure_structure(self) -> None:
        """no_native_uart_failure creates correct structure."""
        failure = no_native_uart_failure(
            hook_diagnostics=[{"hook_id": "uart_hook"}],
        )

        payload = failure.to_payload()
        self.assertEqual(failure.kind, "uart")
        self.assertIn("hook_contract_call", payload)  # Always provided
        hook_diags = cast(list, payload["hook_diagnostics"])
        self.assertEqual(len(hook_diags), 1)
        self.assertNotIn("native_diagnostics", payload)  # Not set

    def test_failure_payload_is_serializable_to_json(self) -> None:
        """Failure payload can be serialized to JSON without error."""
        failure = DiscoveryFailure(
            code="CODE",
            message="message",
            kind="probe",
            hook_contract_call={"tool": "t"},
            remedies=("r1", "r2"),
        )
        payload = failure.to_payload()

        # Should not raise
        json_str = json.dumps(payload)
        self.assertIsInstance(json_str, str)
        # Verify round-trip
        parsed = json.loads(json_str)
        self.assertEqual(parsed["code"], "CODE")


class VendorUartRowsTests(unittest.TestCase):
    """Tests for vendor_uart_rows producer and adapters."""

    def test_empty_serial_fallbacks_returns_no_rows(self) -> None:
        """When SERIAL_FALLBACKS is empty, vendor_uart_rows returns no rows."""
        from pyocd_debug_mcp import hardware_inventory
        from unittest.mock import patch

        with patch.object(hardware_inventory, "SERIAL_FALLBACKS", ()):
            result = hardware_inventory.vendor_uart_rows(lambda *a: (0, "", ""))
            self.assertEqual(result, [])

    def test_executable_absent_skips_spec(self) -> None:
        """When resolve_command_path returns None, spec is skipped and run_cmd not called."""
        from pyocd_debug_mcp import hardware_inventory
        from pyocd_debug_mcp.serial_resolver import SerialFallbackSpec
        from unittest.mock import patch

        spec = SerialFallbackSpec(
            provider_id="test_provider",
            probe_families=("nrf52", "nrf53"),
            executable="nrfjprog",
            executable_env="",
            argv=("--com",),
            parser="nrfjprog_com",
        )

        run_cmd_called = []

        def fake_run_cmd(argv):
            run_cmd_called.append(argv)
            return (0, "test", "")

        with patch.object(hardware_inventory, "SERIAL_FALLBACKS", (spec,)):
            with patch.object(hardware_inventory, "resolve_command_path", return_value=None):
                result = hardware_inventory.vendor_uart_rows(fake_run_cmd)

        self.assertEqual(result, [])
        self.assertEqual(run_cmd_called, [])

    def test_nonzero_exit_code_124_timeout_skips_spec(self) -> None:
        """Exit code 124 (timeout) results in no rows and continues."""
        from pyocd_debug_mcp import hardware_inventory
        from pyocd_debug_mcp.serial_resolver import SerialFallbackSpec
        from unittest.mock import patch

        spec = SerialFallbackSpec(
            provider_id="test",
            probe_families=("nrf52",),
            executable="nrfjprog",
            executable_env="",
            argv=("--com",),
            parser="nrfjprog_com",
        )

        def fake_run_cmd(argv):
            return (124, "", "")  # 124 = timeout

        with patch.object(hardware_inventory, "SERIAL_FALLBACKS", (spec,)):
            with patch.object(
                hardware_inventory, "resolve_command_path", return_value="/bin/nrfjprog"
            ):
                result = hardware_inventory.vendor_uart_rows(fake_run_cmd)

        self.assertEqual(result, [])

    def test_nonzero_exit_code_127_not_found_skips_spec(self) -> None:
        """Exit code 127 (not found) results in no rows and continues."""
        from pyocd_debug_mcp import hardware_inventory
        from pyocd_debug_mcp.serial_resolver import SerialFallbackSpec
        from unittest.mock import patch

        spec = SerialFallbackSpec(
            provider_id="test",
            probe_families=("nrf52",),
            executable="nrfjprog",
            executable_env="",
            argv=("--com",),
            parser="nrfjprog_com",
        )

        def fake_run_cmd(argv):
            return (127, "", "")  # 127 = not found

        with patch.object(hardware_inventory, "SERIAL_FALLBACKS", (spec,)):
            with patch.object(
                hardware_inventory, "resolve_command_path", return_value="/bin/nrfjprog"
            ):
                result = hardware_inventory.vendor_uart_rows(fake_run_cmd)

        self.assertEqual(result, [])

    def test_garbage_output_no_exception_empty_rows(self) -> None:
        """Garbage/unparseable output produces no rows and no exception."""
        from pyocd_debug_mcp import hardware_inventory
        from pyocd_debug_mcp.serial_resolver import SerialFallbackSpec
        from unittest.mock import patch

        spec = SerialFallbackSpec(
            provider_id="test",
            probe_families=("nrf52",),
            executable="nrfjprog",
            executable_env="",
            argv=("--com",),
            parser="nrfjprog_com",
        )

        def fake_run_cmd(argv):
            return (0, "garbage\ninvalid\noutput\n", "")

        with patch.object(hardware_inventory, "SERIAL_FALLBACKS", (spec,)):
            with patch.object(
                hardware_inventory, "resolve_command_path", return_value="/bin/nrfjprog"
            ):
                result = hardware_inventory.vendor_uart_rows(fake_run_cmd)

        self.assertEqual(result, [])

    def test_valid_nrfjprog_output_produces_rows(self) -> None:
        """Valid nrfjprog output produces VendorUartRow with vendor:provider_id."""
        from pyocd_debug_mcp import hardware_inventory
        from pyocd_debug_mcp.serial_resolver import SerialFallbackSpec
        from unittest.mock import patch

        spec = SerialFallbackSpec(
            provider_id="nrf_provider",
            probe_families=("nrf52",),
            executable="nrfjprog",
            executable_env="",
            argv=("--com",),
            parser="nrfjprog_com",
        )

        nrfjprog_output = "680123456 COM5 VCOM0\n680123457 COM6 VCOM0\n"

        def fake_run_cmd(argv):
            return (0, nrfjprog_output, "")

        with patch.object(hardware_inventory, "SERIAL_FALLBACKS", (spec,)):
            with patch.object(
                hardware_inventory, "resolve_command_path", return_value="/bin/nrfjprog"
            ):
                result = hardware_inventory.vendor_uart_rows(fake_run_cmd)

        self.assertEqual(len(result), 2)
        for row in result:
            self.assertEqual(row.provenance, "vendor:nrf_provider")
            self.assertIsNone(row.usb_serial)
            self.assertIsNone(row.vid)
            self.assertIsNone(row.pid)
        self.assertEqual(result[0].port_path, "COM5")
        self.assertEqual(result[1].port_path, "COM6")

    def test_valid_stm32_programmer_output_produces_rows(self) -> None:
        """Valid STM32_Programmer_CLI output produces VendorUartRow."""
        from pyocd_debug_mcp import hardware_inventory
        from pyocd_debug_mcp.serial_resolver import SerialFallbackSpec
        from unittest.mock import patch

        spec = SerialFallbackSpec(
            provider_id="stlink_provider",
            probe_families=("stm32",),
            executable="STM32_Programmer_CLI",
            executable_env="",
            argv=("-list",),
            parser="stm32_programmer_list",
        )

        stlink_output = """
===== ST-LINK/V2-1 :  ST-LINK SN  : 0673FE733248495034303254
===== DFU interface : 0x0483:0x2244
===== JTAG interface : available
===== SWD interface : available
===== UART interface #1 :
ST-LINK SN: 0673FE733248495034303254
Port: COM10
Location: on board
Description: STM32 Virtual COM Port in FS Mode

===== UART interface #2 :
ST-LINK SN: 0673FE733248495034303254
Port: COM11
Location: on board
Description: STM32 Virtual COM Port in HS Mode
"""

        def fake_run_cmd(argv):
            return (0, stlink_output, "")

        with patch.object(hardware_inventory, "SERIAL_FALLBACKS", (spec,)):
            with patch.object(
                hardware_inventory,
                "resolve_command_path",
                return_value="/bin/STM32_Programmer_CLI",
            ):
                result = hardware_inventory.vendor_uart_rows(fake_run_cmd)

        self.assertEqual(len(result), 2)
        for row in result:
            self.assertEqual(row.provenance, "vendor:stlink_provider")
            self.assertIsNone(row.usb_serial)
            self.assertIsNone(row.vid)
            self.assertIsNone(row.pid)
        self.assertEqual(result[0].port_path, "COM10")
        self.assertEqual(result[1].port_path, "COM11")

    def test_vendor_rows_classified_session_not_stable(self) -> None:
        """Vendor rows with None identity fields should be session-local, not stable."""
        from pyocd_debug_mcp import hardware_inventory
        from pyocd_debug_mcp.hardware_inventory import _uart_scope
        from pyocd_debug_mcp.serial_resolver import SerialFallbackSpec
        from unittest.mock import patch

        spec = SerialFallbackSpec(
            provider_id="test",
            probe_families=("test",),
            executable="cmd",
            executable_env="",
            argv=(),
            parser="nrfjprog_com",
        )

        def fake_run_cmd(argv):
            return (0, "SN1 COM5 VCOM0\n", "")

        with patch.object(hardware_inventory, "SERIAL_FALLBACKS", (spec,)):
            with patch.object(hardware_inventory, "resolve_command_path", return_value="/cmd"):
                result = hardware_inventory.vendor_uart_rows(fake_run_cmd)

        self.assertEqual(len(result), 1)
        row = result[0]

        # Verify it's classified as session-local, not stable
        scope = _uart_scope(row.usb_serial, row.vid, row.pid)
        self.assertEqual(scope, "session")


if __name__ == "__main__":
    unittest.main()
