"""VI04: probe identity must survive normalization without becoming a substring."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import Mock, call, patch

from pyocd_debug_mcp import serial_resolver as resolver
from pyocd_debug_mcp.probe_inventory import registered_provider_ids

OBSERVATIONS = {}


def _vendor_fixture(vendor: str):
    # Use providers required from the real registry above. Nordic CLI discovery
    # is simulated via the same Picoprobe fallback spec as the existing control;
    # this does not require a J-Link SDK or claim a physical Nordic connection.
    if vendor == "nordic":
        provider, uid = "picoprobe", "000683710208"
        spec = resolver.SerialFallbackSpec(
            "nordic-fixture", (provider,), "nrfjprog", "", ("--com",), "nrfjprog_com", "VCOM0"
        )
    else:
        provider, uid = "stlink", "066EFF505057"
        spec = resolver.SerialFallbackSpec(
            "stlink-fixture",
            (provider,),
            "STM32_Programmer_CLI",
            "",
            ("-l",),
            "stm32_programmer_list",
        )
    return provider, uid, spec, f"{vendor}-fixture"


def _vendor_output(vendor: str, rows: tuple[tuple[str, str, str], ...]) -> str:
    if vendor == "nordic":
        return "".join(f"{serial} {port} {label}\n" for serial, port, label in rows)
    return "".join(
        f"===== UART Interface =====\nST-LINK SN: {serial}\nPort: {port}\n"
        for serial, port, _label in rows
    )


def setUpModule() -> None:
    required = {"cmsisdap", "picoprobe", "stlink"}
    actual = set(registered_provider_ids())
    if not required <= actual:
        raise RuntimeError(f"Real pyOCD registry lacks fixture prerequisites: {required - actual}")


class ManagedVendorIdentityTests(unittest.TestCase):
    """Admitted identities reach real managed routing and a transport spy on Actions.

    Discovery, vendor output and transport are simulated; provider registration,
    immutable session metadata, cache admission and serial handlers remain real.
    The historical invalid board ID established no runtime or physical I/O result.
    """

    def attempt(
        self,
        output: str,
        *,
        should_refuse: bool,
        reverse: bool,
        vendor: str = "stlink",
        expected_port: str = "COM8",
        uid: str | None = None,
        qualified: bool = False,
        override: str | None = None,
        foreign: bool = False,
    ) -> None:
        from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle, session_metadata
        from pyocd_debug_mcp.board_config import BoardConfig
        from pyocd_debug_mcp.firmstore.cache import ProbeIdentity, SerialEndpoint
        from pyocd_debug_mcp.services import uart_capture
        from pyocd_debug_mcp.services.connections import probe_connection_id
        from tests.inventory_fixture import _probe_row, _snapshot, _uart_row
        from tests.tiered_acceptance_support import isolated_project, tiered_test_server

        provider, default_uid, spec, executable = _vendor_fixture(vendor)
        uid = uid or default_uid
        board = BoardConfig(
            board_id="vendor_identity",
            display_name="Vendor identity fixture",
            mcu_family="fixture",
            probe_family=provider,
            probe_type=provider,
            pyocd_target="fixture",
            probe_hint_terms=(),
            serial_hint_terms=(),
            test_addr=0x20000000,
        )
        port_serials = (("COM7", "adapter-a"), ("COM8", "adapter-b"))
        if foreign:
            # Keep two equally eligible adapters after COM8 is excluded. A single
            # external adapter would intentionally permit generic first-use I/O.
            port_serials += (("COM9", "adapter-c"),)
        rows = tuple(
            replace(_uart_row(port, scope="stable", serial=serial), description="USB CP2102 UART")
            for port, serial in port_serials
        )
        if reverse:
            rows = rows[::-1]
        snapshot = _snapshot(_probe_row(provider, uid), uarts=rows)
        transport = Mock()
        transport.open.return_value = object()
        transport.write.side_effect = lambda handle, data: len(data)
        command = Mock(return_value=(0, output, ""))
        with isolated_project() as root, tiered_test_server(root) as server:
            self.assertIn(provider, registered_provider_ids())
            selection = server._probe_selection_store.resolve(
                probe_connection_id(provider, uid), snapshot
            )
            self.assertEqual(selection.provider, provider)
            self.assertEqual(selection.unique_id, uid)
            self.assertTrue(selection.durable)
            selector = f"{provider}:{selection.unique_id}" if qualified else selection.unique_id
            handle = TargetSessionHandle(None, board, selector, "local", None)
            metadata = session_metadata(handle)
            self.assertIs(metadata, handle.metadata)
            self.assertTrue(type(metadata).__dataclass_params__.frozen)
            self.assertEqual(metadata.probe_family, selection.provider)
            self.assertEqual(metadata.probe_uid, selector)
            server._promote_open_session(board.board_id, handle, gate_reason="registered fixture")
            self.assertIs(server.connection_manager.handle_for(board.board_id), handle)
            cache = server._attachment_cache
            endpoints = [
                SerialEndpoint(row.port_path, row.usb_serial, row.vid, row.pid) for row in rows
            ]
            admitted = cache.resolve(
                board.board_id, ProbeIdentity(metadata.probe_family, metadata.probe_uid), endpoints
            )
            self.assertFalse(admitted.reused)
            self.assertEqual(admitted.reason, "no_record")
            if foreign:
                # Real on-disk confirmation must still exclude this cable before
                # vendor scoring, even if that vendor names this probe's full UID.
                cache.confirm(
                    "other_board",
                    ProbeIdentity(provider, "other_probe"),
                    SerialEndpoint("COM8", "adapter-b", 1, 2),
                )
            before_records = cache.load_records()
            before_cache = cache.path.read_bytes() if cache.path.exists() else None
            error, returned = None, None
            with (
                patch.object(
                    server._hardware_inventory, "uart_snapshot", return_value=snapshot
                ) as inventory,
                patch.object(resolver, "SERIAL_FALLBACKS", (spec,)),
                patch.object(resolver, "resolve_command_path", return_value=executable),
                patch.object(server, "_run_cmd", command),
                patch.object(uart_capture, "_BACKEND", transport),
            ):
                try:
                    returned = server.serial_tool_handlers["write_serial"](
                        board.board_id, text="hello", port=override, timeout_seconds=0.1
                    )
                except Exception as exc:
                    error = exc
                finally:
                    key = (
                        f"{self.id()}:{vendor}:{selector}:reverse={reverse}:qualified={qualified}:"
                        f"override={override}:foreign={foreign}:output={output!r}"
                    )
                    OBSERVATIONS[key] = {
                        "registered_providers": registered_provider_ids(),
                        "selected_provider": selection.provider,
                        "selected_uid": selection.unique_id,
                        "metadata_uid": metadata.probe_uid,
                        "cache_admission": admitted.reason,
                        "inventory_ports": [row.port_path for row in rows],
                        "vendor_output": output,
                        "command_calls": [str(c) for c in command.mock_calls],
                        "backend_calls": [str(c) for c in transport.mock_calls],
                        "error": repr(error) if error else None,
                        "returned": returned,
                        "scope": "Actual managed handlers; simulated discovery/vendor/transport; no physical I/O",
                    }
            inventory.assert_called_once_with()
            if override is None:
                command.assert_called_once_with([executable, *spec.argv])
            else:
                command.assert_not_called()
            self.assertEqual(cache.load_records(), before_records)
            self.assertEqual(cache.path.read_bytes() if cache.path.exists() else None, before_cache)
            if should_refuse:
                self.assertEqual(
                    transport.mock_calls, [], "Unrelated vendor identity reached serial I/O"
                )
                self.assertIsNotNone(error, "Unrelated vendor identity must be refused")
                self.assertIn("uart/automatic-selection-refused", str(error))
            else:
                self.assertIsNone(error, repr(error))
                transport.open.assert_called_once()
                self.assertEqual(transport.open.call_args.args[0], expected_port)
                transport.write.assert_called_once()
                transport.close.assert_called_once()
                self.assertEqual(
                    transport.mock_calls,
                    [
                        call.open(
                            expected_port, baudrate=board.default_baudrate, timeout_seconds=0.1
                        ),
                        call.write(transport.open.return_value, b"hello"),
                        call.close(transport.open.return_value),
                    ],
                )
                self.assertIn(expected_port, returned)
                self.assertIn("5 byte(s)", returned)

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

    def attempt_rows(self, vendor, rows, *, should_refuse=False, **kwargs) -> None:
        for ordered in (rows, rows[::-1]):
            output = _vendor_output(vendor, ordered)
            for reverse in (False, True):
                with self.subTest(vendor=vendor, rows=ordered, reverse=reverse, **kwargs):
                    self.attempt(
                        output,
                        vendor=vendor,
                        should_refuse=should_refuse,
                        reverse=reverse,
                        **kwargs,
                    )

    def test_nordic_matching_vendor_identity_reaches_only_its_managed_port(self) -> None:
        self.attempt_rows(
            "nordic",
            (
                ("208", "COM7", "VCOM0"),
                ("683710208", "COM8", "VCOM0"),
            ),
        )

    def test_nordic_unrelated_vendor_identity_cannot_open_or_write_a_managed_port(self) -> None:
        self.attempt_rows("nordic", (("208", "COM7", "VCOM0"),), should_refuse=True)


if __name__ == "__main__":
    unittest.main()
