"""Focused reproductions of the four reviewed design flaws; no hardware writes."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pyocd_debug_mcp import sidecar
from pyocd_debug_mcp.discovery_hooks import DiscoveryHookSpec
from pyocd_debug_mcp.firmstore.cache import ProbeIdentity, SerialEndpoint
from pyocd_debug_mcp.hardware_inventory import SessionUartSelection
from pyocd_debug_mcp.kernel import hygiene, processes
from pyocd_debug_mcp.native_build import command_template
from pyocd_debug_mcp.probe_families import configured_probe_cli_commands
from pyocd_debug_mcp.setup_flow.device_support import (
    PackAddressRegion,
    PackMemoryGeometry,
    _svd_peripheral_regions,
    resolve_builtin_target_support,
)
from pyocd_debug_mcp.safety.regions import ActionCategory, AddressRange, Allowed, Refusal
from pyocd_debug_mcp.safety.map_build import SafetyMapError, require_reconciled_authority
from tests.test_probe_selection_records import _snapshot, _uart_row
from tests.tiered_acceptance_support import isolated_project, tiered_test_server


def svd_regions():
    # ALIAS deliberately sorts before KEYR at the same address. Policy must
    # survive access intersection rather than depend on the displayed name.
    registers = "".join(
        f"<register><name>{name}</name><addressOffset>{offset}</addressOffset>"
        f"<size>32</size><access>{'write-only' if name in ('KEYR', 'ALIAS', 'OPTKEYR') else 'read-write'}</access></register>"
        for name, offset in (
            ("ACR", 0),
            ("KEYR", 8),
            ("ALIAS", 8),
            ("OPTKEYR", 12),
            ("SR", 16),
            ("CR", 20),
            ("OPTR", 32),
            ("WRP1AR", 44),
        )
    )
    payload = (
        '<device schemaVersion="1.3"><name>Fixture</name><version>1</version>'
        "<description>Fixture</description><addressUnitBits>8</addressUnitBits>"
        "<width>32</width><size>32</size><peripherals><peripheral>"
        "<name>FLASH</name><baseAddress>0x40022000</baseAddress>"
        f"<registers>{registers}</registers></peripheral></peripherals></device>"
    ).encode()
    return _svd_peripheral_regions(SimpleNamespace(svd=io.BytesIO(payload)))


class SvdRegisterSafety(unittest.TestCase):
    def test_flash_controls_are_prohibited_even_through_aliases(self):
        rows = {row.start: row for row in svd_regions()}
        for offset in (8, 12, 20, 32, 44):
            with self.subTest(offset=offset):
                self.assertTrue(getattr(rows[0x40022000 + offset], "prohibited", False))

    def test_status_and_access_configuration_remain_usable(self):
        rows = {row.start: row for row in svd_regions()}
        for offset in (0, 16):
            with self.subTest(offset=offset):
                self.assertTrue(rows[0x40022000 + offset].writable)
                self.assertFalse(getattr(rows[0x40022000 + offset], "prohibited", False))

    def test_generated_map_denies_control_writes_and_keeps_benign_registers(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            candidate = resolve_builtin_target_support(
                "nRF52840-QIAA", "nrf52840"
            ).with_identity_proof(0x410FC241)
            source = candidate.to_authority_document()
            profile = SimpleNamespace(
                mcu_part_number="nRF52840-QIAA",
                device_support=source,
                board=SimpleNamespace(pyocd_target="nrf52840"),
                to_document=lambda: {
                    "board_id": "fixture",
                    "mcu_part_number": "nRF52840-QIAA",
                    "pyocd_target": "nrf52840",
                    "datasheet_ref": "fixture.pdf",
                    "datasheet_sha256": "a" * 64,
                },
            )
            geometry = PackMemoryGeometry(
                0,
                4096,
                0x20000000,
                0x20001000,
                peripheral_regions=svd_regions(),
                cpu_system_regions=(PackAddressRegion("CPU", 0xE0000000, 0xE0100000),),
            )
            with (
                patch.object(server, "_replay_profile_device_support", return_value=candidate),
                patch.object(server, "resolve_device_support_geometry", return_value=geometry),
                patch.object(
                    server,
                    "replay_datasheet_evidence",
                    return_value=SimpleNamespace(source_document=lambda: {}),
                ),
            ):
                document = server._derive_generic_safety_map("fixture", profile=profile)
            stale = replace(
                document,
                source_digests=replace(document.source_digests, map_generator_schema="0" * 64),
            )
            with self.assertRaisesRegex(SafetyMapError, "board_safety_refresh"):
                require_reconciled_authority(stale, generic_support_resolver=lambda part: candidate)
            for offset in (8, 12, 20, 32, 44):
                with self.subTest(offset=offset):
                    result = document.safety_map.check(
                        ActionCategory.REGISTER_WRITE,
                        [AddressRange(0x40022000 + offset, 0x40022004 + offset)],
                    )
                    self.assertIsInstance(result, Refusal)
                    self.assertEqual(result.code, "safety/prohibited")
            for offset in (0, 16):
                self.assertIsInstance(
                    document.safety_map.check(
                        ActionCategory.REGISTER_WRITE,
                        [AddressRange(0x40022000 + offset, 0x40022004 + offset)],
                    ),
                    Allowed,
                )
            self.assertIsInstance(
                document.safety_map.check(
                    ActionCategory.MEMORY_READ, [AddressRange(0x40022008, 0x4002200C)]
                ),
                Refusal,
            )
            self.assertIsInstance(
                document.safety_map.check(
                    ActionCategory.MEMORY_READ, [AddressRange(0x40022020, 0x40022024)]
                ),
                Allowed,
            )

    def test_ordinary_clock_and_peripheral_controls_are_not_security_controls(self):
        from pyocd_debug_mcp.safety.register_policy import prohibited_svd_register

        for peripheral, register in (("RCC", "CLOCKCR"), ("GPIOA", "CR"), ("USART1", "CR1")):
            with self.subTest(peripheral=peripheral, register=register):
                self.assertFalse(prohibited_svd_register(peripheral, register))
        for peripheral, register in (
            ("AES", "KEYR0"),
            ("CRYP", "K0LR"),
            ("OTP", "DATA"),
            ("NVMCTRL", "CTRLA"),
        ):
            with self.subTest(peripheral=peripheral, register=register):
                self.assertTrue(prohibited_svd_register(peripheral, register))


class PackagedCommands(unittest.TestCase):
    def test_compiled_inventory_uses_a_sidecar_command_and_preserves_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "byo-mcp-sidecar.exe"
            executable.write_bytes(b"fixture")
            with (
                patch.dict(os.environ, {"BYO_SIDECAR_COMPILED": "1", "PYOCD_CLI": ""}),
                patch.object(sys, "argv", [str(executable)]),
                patch.object(sys, "executable", str(executable)),
            ):
                self.assertEqual(
                    configured_probe_cli_commands()[0],
                    (str(executable.resolve()), "probe-list", "list", "--probes"),
                )
                with patch.dict(os.environ, {"PYOCD_CLI": "explicit-pyocd"}):
                    self.assertEqual(
                        configured_probe_cli_commands()[0],
                        ("explicit-pyocd", "list", "--probes"),
                    )

    def test_compiled_python_hook_does_not_treat_sidecar_as_an_interpreter(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "byo-mcp-sidecar.exe"
            executable.write_bytes(b"fixture")
            script = Path(temporary) / "hook.py"
            spec = DiscoveryHookSpec(
                "fixture",
                "probe",
                frozenset({"windows"}),
                "server-python",
                script,
                ("argument",),
                1.0,
                "project",
                "a" * 64,
            )
            with (
                patch.dict(os.environ, {"BYO_SIDECAR_COMPILED": "1"}),
                patch.object(sys, "argv", [str(executable)]),
                patch.object(sys, "executable", str(executable)),
            ):
                self.assertEqual(
                    spec.command(),
                    (str(executable.resolve()), "run-hook", str(script), "argument"),
                )

    def test_managed_build_template_uses_the_launcher_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sidecar").mkdir()
            executable = root / "sidecar" / "byo-mcp-sidecar.exe"
            executable.write_bytes(b"fixture")
            launcher = root / ("byo.exe" if os.name == "nt" else "byo")
            launcher.write_bytes(b"fixture")
            with (
                patch.dict(os.environ, {"BYO_SIDECAR_COMPILED": "1"}),
                patch.object(sys, "argv", [str(executable)]),
                patch.object(sys, "executable", str(executable)),
            ):
                template = command_template()
            argv = template["argv_template"]
            self.assertTrue(Path(argv[0]).samefile(launcher), argv)
            self.assertEqual(
                argv[1:],
                [
                    "internal",
                    "native-build",
                    "--project-dir",
                    "<project-dir>",
                    "--build-dir",
                    "<artifact-search-root>",
                    "--cwd",
                    "<build-working-directory>",
                    "--artifact",
                    "<role>=<output-path>",
                    "--",
                    "<build-executable>",
                    "<build-argument>",
                ],
            )
            self.assertEqual(template["invocation_cwd_template"], "<project-dir>")

    def test_probe_listing_command_forwards_pyocd_output_and_exit_status(self):
        output = io.StringIO()
        with (
            patch("pyocd.__main__.PyOCDTool.run", return_value=7) as run,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(sidecar.main(["probe-list", "list", "--probes"]), 7)
        run.assert_called_once_with(["list", "--probes"])

    def test_run_hook_has_script_argv_and_can_import_a_sibling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "local_hook_helper.py").write_text("VALUE = 'works'\n", encoding="utf-8")
            script = root / "hook.py"
            script.write_text(
                "import json, sys\nfrom local_hook_helper import VALUE\n"
                "print(json.dumps([VALUE, sys.argv[1:]]))\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            old_argv = sys.argv[:]
            old_path = sys.path[:]
            try:
                with contextlib.redirect_stdout(output):
                    self.assertEqual(sidecar.main(["run-hook", str(script), "a b", ""]), 0)
                self.assertEqual(json.loads(output.getvalue()), ["works", ["a b", ""]])
                self.assertEqual(sys.argv, old_argv)
                self.assertEqual(sys.path, old_path)
            finally:
                sys.modules.pop("local_hook_helper", None)

    def test_self_test_rejects_failed_discovery_before_starting_a_worker(self):
        with (
            patch.object(
                sidecar, "_check_probe_discovery", side_effect=RuntimeError("probe listing failed")
            ),
            patch.object(sidecar.subprocess, "Popen") as worker,
        ):
            with self.assertRaisesRegex(RuntimeError, "probe listing failed"):
                sidecar.main(["self-test"])
        worker.assert_not_called()

    def test_discovery_self_test_accepts_zero_probes_and_checks_child_failure(self):
        with patch("pyocd_debug_mcp.kernel.processes.run_owned") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"No available debug probes", b"")
            sidecar._check_probe_discovery()
            argv = run.call_args.args[0]
            self.assertEqual(argv[-3:], ("probe-list", "list", "--probes"))
            self.assertGreater(run.call_args.kwargs["timeout"], 0)
            self.assertEqual(run.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")
            self.assertFalse(run.call_args.kwargs["text"])
            run.return_value = subprocess.CompletedProcess([], 2, b"", b"invalid command")
            with self.assertRaisesRegex(RuntimeError, "invalid command"):
                sidecar._check_probe_discovery()

    def test_probe_list_refuses_non_listing_operations(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sidecar.main(["probe-list", "erase"]), 2)

    def test_probe_list_can_emit_unicode_on_a_legacy_windows_stream(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1252")

        def listing(arguments):
            print("\u2714 probe")
            return 0

        with (
            patch.object(sys, "stdout", stream),
            patch("pyocd.__main__.PyOCDTool.run", side_effect=listing),
        ):
            self.assertEqual(sidecar.main(["probe-list", "list", "--probes"]), 0)
            stream.flush()
            self.assertEqual(buffer.getvalue().decode("utf-8"), "\u2714 probe" + os.linesep)


class SavedUartLoss(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(isolated_project())
        self.server = self.enterContext(tiered_test_server(self.root))
        self.probe = ProbeIdentity("jlink", "P1")
        self.server._attachment_cache.confirm(
            "board_a", self.probe, SerialEndpoint("COM1", "UART_A", 1, 2)
        )

    def test_saved_stable_adapter_loss_refuses_unrelated_or_empty_inventory(self):
        for rows in ((), (_uart_row("COM7", scope="stable", serial="UART_B"),)):
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(
                    RuntimeError, "uart/saved-adapter-unavailable"
                ) as caught:
                    self.server._resolve_recorded_uart("board_a", self.probe, _snapshot(uarts=rows))
                message = str(caught.exception)
                self.assertIn("UART_A", message)
                self.assertIn("new", message)
                self.assertIn("plan", message)
                self.assertIn("board_setup", message)
                if rows:
                    self.assertIn("COM7", message)

    def test_saved_stable_adapter_is_found_after_port_renumbering(self):
        row = _uart_row("COM8", scope="stable", serial="UART_A")
        resolved = self.server._resolve_recorded_uart(
            "board_a", self.probe, _snapshot(uarts=(row,))
        )
        self.assertEqual(resolved.device, "COM8")

    def test_first_resolution_still_returns_none(self):
        self.assertIsNone(self.server._resolve_recorded_uart("new_board", self.probe, _snapshot()))

    def test_missing_session_choice_refuses_every_retry(self):
        self.server._session_uart_selections.record(
            SessionUartSelection("session_board", "COM1", "adapter A", ("native",))
        )
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                with self.assertRaisesRegex(RuntimeError, "uart/saved-adapter-unavailable"):
                    self.server._resolve_recorded_uart(
                        "session_board", self.probe, _snapshot(uarts=(_uart_row("COM7"),))
                    )

    def test_saved_loss_never_reaches_the_fallback_scorer(self):
        row = _uart_row("COM7", scope="stable", serial="UART_B")
        with (
            patch.object(
                self.server,
                "_require_loaded_board",
                return_value=SimpleNamespace(board_id="board_a"),
            ),
            patch.object(
                self.server,
                "session_metadata",
                return_value=SimpleNamespace(probe_uid="P1", probe_family="jlink"),
            ),
            patch.object(
                self.server._hardware_inventory,
                "uart_snapshot",
                return_value=_snapshot(uarts=(row,)),
            ),
            patch.object(self.server, "resolve_serial_port") as scorer,
        ):
            with self.assertRaisesRegex(RuntimeError, "saved-adapter-unavailable"):
                self.server._resolve_serial_port_for_session(SimpleNamespace(), override=None)
        scorer.assert_not_called()

    def test_explicit_planned_override_is_scored_without_replacing_saved_identity(self):
        row = _uart_row("COM7", scope="stable", serial="UART_B")
        port = self.server._uart_row_as_port(row)
        before = self.server._attachment_cache.load_records()
        with (
            patch.object(
                self.server,
                "_require_loaded_board",
                return_value=SimpleNamespace(board_id="board_a"),
            ),
            patch.object(
                self.server,
                "session_metadata",
                return_value=SimpleNamespace(probe_uid="P1", probe_family="jlink"),
            ),
            patch.object(
                self.server._hardware_inventory,
                "uart_snapshot",
                return_value=_snapshot(uarts=(row,)),
            ),
            patch.object(
                self.server, "resolve_serial_port", return_value=SimpleNamespace(port=port)
            ) as scorer,
        ):
            self.assertEqual(
                self.server._resolve_serial_port_for_session(
                    SimpleNamespace(), override="COM7"
                ).device,
                "COM7",
            )
        self.assertEqual(scorer.call_args.kwargs["override"], "COM7")
        self.assertEqual(self.server._attachment_cache.load_records(), before)

    def test_changed_probe_and_revoked_attachment_refuse(self):
        row = _uart_row("COM8", scope="stable", serial="UART_A")
        snapshot = _snapshot(uarts=(row,))
        with self.assertRaisesRegex(RuntimeError, "probe_changed"):
            self.server._resolve_recorded_uart("board_a", ProbeIdentity("jlink", "P2"), snapshot)
        self.server._attachment_cache.revoke(
            "board_a", self.probe, SerialEndpoint("COM1", "UART_A", 1, 2)
        )
        with self.assertRaisesRegex(RuntimeError, "revoked"):
            self.server._resolve_recorded_uart("board_a", self.probe, snapshot)


class PosixStaleMarkers(unittest.TestCase):
    def posix_os(self, killpg):
        return SimpleNamespace(name="posix", killpg=killpg)

    def test_absent_leader_and_empty_group_are_clean(self):
        killpg = Mock(side_effect=ProcessLookupError())
        with (
            patch.object(processes, "_start_token", return_value=None),
            patch.object(processes, "os", self.posix_os(killpg)),
        ):
            self.assertTrue(processes.terminate_marked_group(123, "old-birth"))
        killpg.assert_called_once_with(123, 0)

    def test_leaderless_nonempty_group_is_not_authority_to_signal(self):
        killpg = Mock()
        with (
            patch.object(processes, "_start_token", return_value=None),
            patch.object(processes, "os", self.posix_os(killpg)),
        ):
            self.assertFalse(processes.terminate_marked_group(123, "old-birth"))
        self.assertTrue(all(call.args[1] == 0 for call in killpg.call_args_list))

    def test_permission_error_does_not_remove_identity_evidence(self):
        with (
            patch.object(processes, "_start_token", return_value=None),
            patch.object(processes, "os", self.posix_os(Mock(side_effect=PermissionError()))),
        ):
            self.assertFalse(processes.terminate_marked_group(123, "old-birth"))

    def test_startup_removes_an_empty_group_marker_without_manual_intervention(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = processes.ProcessMarker(
                2, "fixture", 111, "owner-birth", 123, "child-birth", "a" * 64, "worker", "then"
            )
            path = root / "fixture.json"
            path.write_text(json.dumps(asdict(marker)), encoding="utf-8")
            with (
                patch.object(hygiene, "_start_token", return_value=None),
                patch.object(processes, "_start_token", return_value=None),
                patch.object(
                    processes, "os", self.posix_os(Mock(side_effect=ProcessLookupError()))
                ),
            ):
                result = hygiene.require_clean_startup(root)
            self.assertEqual(result.unresolved, 0)
            self.assertEqual(result.stale_removed, 1)
            self.assertFalse(path.exists())

    @unittest.skipIf(os.name == "nt", "requires native POSIX process groups")
    def test_real_reaped_owned_process_does_not_block_recovery(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            token = processes._start_token(child.pid)
            self.assertIsNotNone(token)
            child.stdin.close()
            child.wait(timeout=5)
            self.assertTrue(processes.terminate_marked_group(child.pid, token))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
