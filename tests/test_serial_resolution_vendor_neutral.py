"""Regression coverage for metadata-only serial selection."""

from __future__ import annotations

from dataclasses import dataclass
import unittest

from firmware_mcp.serial_resolver import (
    SerialPortInfo,
    resolve_serial_port,
)


@dataclass
class _Board:
    board_id: str = "novel"
    display_name: str = "Novel board"
    serial_hint_terms: tuple[str, ...] = ()


@dataclass
class _Probe:
    uid: str | None


class VendorNeutralSerialResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = _Board()

    def test_exact_probe_uid_metadata_selects_an_unknown_port(self) -> None:
        resolution = resolve_serial_port(
            self.board,
            [SerialPortInfo("port-a", serial_number="AB-CD-123")],
            _Probe(uid="abcd123"),
            None,
            False,
            lambda _argv: (0, "", ""),
            False,
        )
        self.assertEqual(resolution.port, SerialPortInfo("port-a", serial_number="AB-CD-123"))
        self.assertIn("exact probe UID", resolution.note)

    def test_ambiguous_ports_require_explicit_selection(self) -> None:
        ports = [SerialPortInfo("port-a"), SerialPortInfo("port-b")]
        resolution = resolve_serial_port(
            self.board,
            ports,
            None,
            None,
            False,
            lambda _argv: (0, "", ""),
            False,
        )
        self.assertIsNone(resolution.port)
        self.assertIn("select one explicitly", resolution.note)

        selected = resolve_serial_port(
            self.board,
            ports,
            None,
            "port-b",
            False,
            lambda _argv: (0, "", ""),
            False,
        )
        self.assertEqual(selected.port, ports[1])
        self.assertEqual(selected.note, "caller selected serial port")

    def test_single_port_fallback_does_not_claim_probe_mapping(self) -> None:
        resolution = resolve_serial_port(
            self.board,
            [SerialPortInfo("port-a")],
            None,
            None,
            True,
            lambda _argv: (0, "", ""),
            False,
        )
        self.assertEqual(resolution.port, SerialPortInfo("port-a"))
        self.assertIn("without probe mapping", resolution.note)


if __name__ == "__main__":
    unittest.main()
