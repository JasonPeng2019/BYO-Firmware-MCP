"""VI04: probe identity must survive normalization without becoming a substring."""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pyocd_debug_mcp import serial_resolver as resolver
from pyocd_debug_mcp.probe_inventory import parse_pyocd_probe_listing, registered_provider_ids

OBSERVATIONS = {}


def setUpModule() -> None:
    required = {"cmsisdap", "picoprobe", "stlink"}
    actual = set(registered_provider_ids())
    if not required <= actual:
        raise RuntimeError(f"Real pyOCD registry lacks fixture prerequisites: {required - actual}")


class SerialIdentityTests(unittest.TestCase):
    def test_unrelated_suffixes_do_not_match_in_either_direction(self) -> None:
        for uid, serial in (
            ("260100001", "0001"),
            ("12345678901234567890", "567890"),
            ("ABCDEF1234", "EF1234"),
        ):
            for left, right in ((uid, serial), (serial, uid)):
                with self.subTest(uid=left, serial=right):
                    self.assertFalse(resolver.probe_uid_matches_serial(left, right))

    def test_letters_are_identity_not_discardable_numeric_decoration(self) -> None:
        for uid, serial in (
            ("A123", "B123"),
            ("AB-123", "CD123"),
            ("123", "AB-123"),
            ("066EFF505057", "066ABC505057"),
            ("000ABC123", "ABC123"),
        ):
            for left, right in ((uid, serial), (serial, uid)):
                with self.subTest(uid=left, serial=right):
                    self.assertFalse(resolver.probe_uid_matches_serial(left, right))

    def test_full_case_and_punctuation_normalized_identities_match(self) -> None:
        for uid, serial in (
            ("260100001", "260100001"),
            ("066EFF505057", "06:6e:ff:50:50:57"),
            ("0673FE733248495034303254", "0673fe733248495034303254"),
            ("ABC-123", "abc123"),
        ):
            with self.subTest(uid=uid, serial=serial):
                self.assertTrue(resolver.probe_uid_matches_serial(uid, serial))

    def test_leading_zeros_apply_only_to_entire_numeric_identities(self) -> None:
        for uid, serial in (("000683710208", "683710208"), ("01", "1"), ("000", "0")):
            for left, right in ((uid, serial), (serial, uid)):
                with self.subTest(uid=left, serial=right):
                    self.assertTrue(resolver.probe_uid_matches_serial(left, right))

    def test_registered_provider_qualifier_preserves_the_complete_serial(self) -> None:
        # pyOCD CLI selectors retain provider:unique-id in ProbeInfo.uid.
        for uid, serial in (
            ("picoprobe:000683710208", "683710208"),
            ("PICOPROBE:260100001", "0260100001"),
            ("stlink:066EFF505057", "066eff505057"),
            ("cmsisdap:ABC-123", "abc123"),
        ):
            with self.subTest(uid=uid, serial=serial):
                self.assertTrue(resolver.probe_uid_matches_serial(uid, serial))
        for uid, serial in (
            ("picoprobe:260100001", "0001"),
            ("picoprobe:AB123", "CD123"),
            ("picoprobe:AB123", "123"),
            ("unregistered:123", "123"),
        ):
            with self.subTest(uid=uid, serial=serial):
                self.assertFalse(resolver.probe_uid_matches_serial(uid, serial))

    def test_missing_or_punctuation_only_values_never_match(self) -> None:
        for uid, serial in (("", "123"), ("123", ""), ("---", "123"), ("123", "::")):
            with self.subTest(uid=uid, serial=serial):
                self.assertFalse(resolver.probe_uid_matches_serial(uid, serial))


class SerialResolverIdentityTests(unittest.TestCase):
    @staticmethod
    def port(device: str, serial: str, *, hwid: str | None = None):
        return resolver.SerialPortInfo(
            device=device, description="USB UART", manufacturer="Silicon Labs",
            product="CP2102", interface="", hwid=hwid if hwid is not None else f"SER={serial}",
            serial_number=serial, vid=0x10C4, pid=0xEA60,
        )

    @staticmethod
    def board(family: str = "picoprobe"):
        return SimpleNamespace(
            board_id="identity-board", display_name="Identity board", mcu_family="test",
            probe_family=family, serial_hint_terms=(),
        )

    def resolve(self, uid, ports, *, override=None, board=None, fallback=(), run_cmd=None):
        if run_cmd is None:
            run_cmd = Mock(side_effect=AssertionError("unexpected vendor command"))
        with patch.object(resolver, "SERIAL_FALLBACKS", fallback):
            return resolver.resolve_serial_port(
                board=board or self.board(), ports=ports, probe=SimpleNamespace(uid=uid),
                override=override, allow_single_fallback=False, run_cmd=run_cmd,
                interactive=False,
            )

    def test_suffix_alone_cannot_choose_between_two_external_adapters(self) -> None:
        for uid, other_serial in (("260100001", "0001"), ("0001", "260100001")):
            wrong = self.port("COM7", other_serial)
            other = self.port("COM8", "different-adapter")
            for ports in ([wrong, other], [other, wrong]):
                with self.subTest(uid=uid, order=[p.device for p in ports]):
                    resolution = self.resolve(uid, ports)
                    self.assertIsNone(resolution.port)
                    self.assertIn("multiple matching serial ports", resolution.note)
                    self.assertIn("COM7", resolution.note)
                    self.assertIn("COM8", resolution.note)

    def test_coincident_digits_and_decorated_substrings_cannot_select_an_adapter(self) -> None:
        for uid, other_serial in (
            ("A123", "B123"), ("123", "AB-123"), ("AB123", "XXAB123"),
            ("066EFF505057", "066ABC505057"),
        ):
            with self.subTest(uid=uid, serial=other_serial):
                resolution = self.resolve(uid, [self.port("COM7", other_serial),
                                                self.port("COM8", "different-adapter")])
                self.assertIsNone(resolution.port)
                self.assertIn("multiple matching serial ports", resolution.note)

    def test_complete_provider_formats_beat_a_competing_short_suffix(self) -> None:
        # Decimal Picoprobe/Nordic, hex ST-Link and provider-qualified CLI fixtures
        # already occur in test_probe_inventory/test_discovery_hook_safety.
        for uid, serial, suffix, family in (
            ("000683710208", "683710208", "208", "picoprobe"),
            ("picoprobe:000683710208", "683710208", "208", "picoprobe"),
            ("066EFF505057", "06:6e:ff:50:50:57", "5057", "stlink"),
            ("stlink:0673FE733248495034303254", "0673fe733248495034303254", "3254", "stlink"),
        ):
            probe = parse_pyocd_probe_listing(f"0  Debug probe  {uid}\n")[0]
            right = self.port("COM8", serial)
            wrong = self.port("COM7", suffix)
            for ports in ([wrong, right], [right, wrong]):
                with self.subTest(uid=uid, order=[p.device for p in ports]):
                    resolution = self.resolve(probe.uid, ports, board=self.board(family))
                    self.assertIs(resolution.port, right)
                    self.assertEqual(resolution.note, "resolved from serial-port metadata")

    def test_a_real_identity_wins_over_the_audited_false_suffix(self) -> None:
        right = self.port("COM8", "260100001")
        resolution = self.resolve("260100001", [self.port("COM7", "0001"), right])
        self.assertIs(resolution.port, right)

    def test_metadata_requires_a_whole_identity_even_without_serial_number(self) -> None:
        for uid, text in (("0001", "SER=260100001"), ("123", "SER=AB-123")):
            with self.subTest(uid=uid, hwid=text):
                resolution = self.resolve(uid, [self.port("COM7", "", hwid=text),
                                                self.port("COM8", "different-adapter")])
                self.assertIsNone(resolution.port)

    def test_complete_metadata_identity_remains_available_without_serial_number(self) -> None:
        right = self.port("COM8", "", hwid="USB VID:PID=1366:1015 SER=260100001 LOCATION=1-1")
        resolution = self.resolve("260100001", [self.port("COM7", "unrelated"), right])
        self.assertIs(resolution.port, right)
        self.assertEqual(resolution.note, "resolved from serial-port metadata")

    def test_explicit_port_selection_remains_separate_from_identity(self) -> None:
        wrong = self.port("COM7", "0001")
        ports = [wrong, self.port("COM8", "260100001")]
        resolution = self.resolve("260100001", ports, override="\\\\.\\com7")
        self.assertIs(resolution.port, wrong)
        self.assertEqual(resolution.note, "")
        missing = self.resolve("260100001", ports, override="COM99")
        self.assertIsNone(missing.port)
        self.assertIn("override port 'COM99' not found", missing.note)

    def test_nordic_vendor_output_keeps_the_complete_zero_padded_probe_link(self) -> None:
        spec = resolver.SerialFallbackSpec(
            "nordic-fixture", ("picoprobe",), "nrfjprog", "", ("--com",), "nrfjprog_com", "VCOM0"
        )
        right = self.port("COM8", "adapter-b")
        run_cmd = Mock(return_value=(0, "208 COM7 VCOM0\n683710208 COM8 VCOM0\n", ""))
        with patch.object(resolver, "resolve_command_path", return_value="nrfjprog-fixture"):
            resolution = self.resolve("picoprobe:000683710208", [self.port("COM7", "adapter-a"), right],
                                      fallback=(spec,), run_cmd=run_cmd)
        self.assertIs(resolution.port, right)
        self.assertIn("nrfjprog --com", resolution.note)
        run_cmd.assert_called_once_with(["nrfjprog-fixture", "--com"])

    def test_stlink_vendor_output_keeps_the_complete_hex_probe_link(self) -> None:
        spec = resolver.SerialFallbackSpec(
            "stlink-fixture", ("stlink",), "STM32_Programmer_CLI", "", ("-l",),
            "stm32_programmer_list",
        )
        output = (
            "===== UART Interface =====\nST-LINK SN: 5057\nPort: COM7\n"
            "===== UART Interface =====\nST-LINK SN: 066EFF505057\nPort: COM8\n"
        )
        right = self.port("COM8", "adapter-b")
        run_cmd = Mock(return_value=(0, output, ""))
        with patch.object(resolver, "resolve_command_path", return_value="stm32-fixture"):
            resolution = self.resolve("stlink:066eff505057", [self.port("COM7", "adapter-a"), right],
                                      board=self.board("stlink"), fallback=(spec,), run_cmd=run_cmd)
        self.assertIs(resolution.port, right)
        self.assertIn("STM32_Programmer_CLI", resolution.note)
        run_cmd.assert_called_once_with(["stm32-fixture", "-l"])


class ManagedVendorIdentityTests(unittest.TestCase):
    """A registered ST-Link premise reaches real managed routing and a transport spy.

    The unmatched-vendor case is a counterexample candidate for the complete
    Actions pass, not a claim of a verified product bug or physical serial I/O.
    """

    def attempt(self, output: str, *, should_refuse: bool, reverse: bool) -> None:
        from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
        from pyocd_debug_mcp.board_config import BoardConfig
        from pyocd_debug_mcp.services import uart_capture
        from tests.test_probe_selection_records import _probe_row, _snapshot, _uart_row
        from tests.tiered_acceptance_support import isolated_project, tiered_test_server

        uid = "066EFF505057"
        board = BoardConfig(
            board_id="vendor-identity", display_name="Vendor identity fixture",
            mcu_family="fixture", probe_family="stlink", probe_type="stlink",
            pyocd_target="fixture", probe_hint_terms=(), serial_hint_terms=(),
            test_addr=0x20000000,
        )
        rows = tuple(
            replace(_uart_row(port, scope="stable", serial=serial), description="USB CP2102 UART")
            for port, serial in (("COM7", "adapter-a"), ("COM8", "adapter-b"))
        )
        if reverse:
            rows = rows[::-1]
        snapshot = _snapshot(_probe_row("stlink", uid), uarts=rows)
        spec = resolver.SerialFallbackSpec(
            "stlink-fixture", ("stlink",), "STM32_Programmer_CLI", "", ("-l",),
            "stm32_programmer_list",
        )
        transport = Mock()
        transport.open.return_value = object()
        transport.write.side_effect = lambda handle, data: len(data)
        command = Mock(return_value=(0, output, ""))
        with isolated_project() as root, tiered_test_server(root) as server:
            selection = server._probe_selection_store.resolve("probe:" + uid, snapshot)
            self.assertEqual(selection.provider, "stlink")
            self.assertEqual(selection.unique_id, uid)
            handle = TargetSessionHandle(None, board, uid, "local", None)
            server._promote_open_session(board.board_id, handle, gate_reason="registered fixture")
            error, returned = None, None
            with (
                patch.object(server._hardware_inventory, "uart_snapshot", return_value=snapshot),
                patch.object(resolver, "SERIAL_FALLBACKS", (spec,)),
                patch.object(resolver, "resolve_command_path", return_value="stm32-fixture"),
                patch.object(server, "_run_cmd", command),
                patch.object(uart_capture, "_BACKEND", transport),
            ):
                try:
                    returned = server.serial_tool_handlers["write_serial"](
                        board.board_id, text="hello", timeout_seconds=0.1
                    )
                except Exception as exc:
                    error = exc
                finally:
                    OBSERVATIONS[f"{self.id()}:reverse={reverse}"] = {
                        "registered_providers": registered_provider_ids(),
                        "selected_provider": selection.provider, "selected_uid": selection.unique_id,
                        "vendor_output": output, "command_calls": [str(c) for c in command.mock_calls],
                        "backend_calls": [str(c) for c in transport.mock_calls],
                        "error": repr(error) if error else None, "returned": returned,
                        "scope": "Actual managed handlers; simulated discovery/vendor/transport; no physical I/O",
                    }
            command.assert_called_once_with(["stm32-fixture", "-l"])
            if should_refuse:
                self.assertEqual(transport.mock_calls, [], "Unrelated vendor identity reached serial I/O")
                self.assertIsNotNone(error, "Unrelated vendor identity must be refused")
                self.assertIn("uart/automatic-selection-refused", str(error))
            else:
                self.assertIsNone(error, repr(error))
                transport.open.assert_called_once()
                self.assertEqual(transport.open.call_args.args[0], "COM8")
                transport.write.assert_called_once()
                transport.close.assert_called_once()

    def test_matching_vendor_identity_reaches_only_its_managed_port(self) -> None:
        output = "===== UART Interface =====\nST-LINK SN: 066EFF505057\nPort: COM8\n"
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                self.attempt(output, should_refuse=False, reverse=reverse)

    def test_unrelated_vendor_identity_cannot_open_or_write_a_managed_port(self) -> None:
        output = "===== UART Interface =====\nST-LINK SN: 5057\nPort: COM7\n"
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                self.attempt(output, should_refuse=True, reverse=reverse)


if __name__ == "__main__":
    unittest.main()
