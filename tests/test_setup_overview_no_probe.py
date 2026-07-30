"""Step 0 precursor fixes: the no-probe status and the single connection_id mint."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Mapping, cast
from unittest.mock import Mock, patch

from pyocd_debug_mcp import server
from pyocd_debug_mcp.services.connections import probe_connection_id


def _rows(overview: Mapping[str, object], key: str) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", overview[key])


def _probe(index: int, *, usb_serial: str | None = None) -> SimpleNamespace:
    serial = f"usb-{index}" if usb_serial is None else usb_serial
    return SimpleNamespace(
        usb_serial=serial,
        probe_id=f"probe-{index}",
        probe_family="cmsis-dap",
        choice=lambda: SimpleNamespace(label=f"Probe {index}"),
    )


def _serial_port(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        serial_id=f"serial-{index}",
        port_path=f"COM{index}",
        description=f"UART {index}",
        usb_serial=f"usb-{index}",
        vid=0x1234,
        pid=0x5678,
    )


class _OverviewHarness:
    """Drive `_setup_overview` with a synthetic inventory and no profile store."""

    def __init__(self, probes: tuple[SimpleNamespace, ...], serial: tuple[SimpleNamespace, ...]):
        self.inventory = SimpleNamespace(probes=probes, serial_ports=serial)
        self.replace = Mock()

    def run(self, names: list[str] | None, assignments: dict[str, str] | None = None):
        with (
            patch.object(server, "_profile_repository", SimpleNamespace(load_all=lambda: ())),
            patch.object(server, "_validation_inventory", return_value=self.inventory),
            patch.object(server, "_replace_setup_assignments", self.replace),
        ):
            return server._setup_overview(names, assignments)


class SetupOverviewNoProbeTests(unittest.TestCase):
    def test_one_name_zero_connections_reports_missing_probe_not_naming_ambiguity(self) -> None:
        harness = _OverviewHarness((), ())

        overview = harness.run(["Nucleo"])

        self.assertEqual(overview["status"], "setup_no_probe")
        self.assertNotEqual(overview["status"], "setup_assignment_clarification_required")
        self.assertEqual(overview["routes"], [])
        self.assertEqual(overview["connections"], [])
        prompt = str(overview["agent_prompt"])
        self.assertIn("No debug probe is visible to the server", prompt)
        self.assertIn("not a naming ambiguity", prompt)

    def test_no_probe_return_clears_provisional_assignments_like_its_siblings(self) -> None:
        harness = _OverviewHarness((), ())

        harness.run(["Nucleo"])

        harness.replace.assert_called_once()
        bindings, reason = harness.replace.call_args.args
        self.assertEqual(bindings, {})
        self.assertIn("no debug connection", reason)

    def test_no_probe_payload_carries_the_same_diagnostic_keys_as_its_siblings(self) -> None:
        harness = _OverviewHarness((), (_serial_port(1),))

        overview = harness.run(["Nucleo"])

        self.assertEqual(
            set(overview),
            {
                "status",
                "agent_prompt",
                "profiles",
                "connections",
                "serial_choices",
                "inventory_error",
                "routes",
            },
        )
        # The UART inventory is still reported even though the probe is missing.
        choice_ids = [row["choice_id"] for row in _rows(overview, "serial_choices")]
        self.assertEqual(choice_ids, ["serial-1"])
        self.assertIsNone(overview["inventory_error"])

    def test_two_names_one_connection_still_returns_assignment_clarification(self) -> None:
        """Proves the comparison was narrowed by a zero test, not removed."""

        harness = _OverviewHarness((_probe(1),), ())

        overview = harness.run(["Nucleo", "Discovery"])

        self.assertEqual(overview["status"], "setup_assignment_clarification_required")
        self.assertEqual(overview["routes"], [])

    def test_three_names_two_connections_still_returns_assignment_clarification(self) -> None:
        harness = _OverviewHarness((_probe(1), _probe(2)), ())

        overview = harness.run(["A", "B", "C"])

        self.assertEqual(overview["status"], "setup_assignment_clarification_required")

    def test_zero_names_and_zero_connections_still_reports_no_board(self) -> None:
        harness = _OverviewHarness((), ())

        overview = harness.run([])

        self.assertEqual(overview["status"], "setup_no_board")

    def test_none_names_and_zero_connections_still_requires_names(self) -> None:
        harness = _OverviewHarness((), ())

        overview = harness.run(None)

        self.assertEqual(overview["status"], "setup_names_required")

    def test_no_board_sentinel_with_zero_connections_is_unchanged(self) -> None:
        harness = _OverviewHarness((), ())

        overview = harness.run(["no board"])

        self.assertEqual(overview["status"], "setup_no_board")

    def test_matched_name_count_with_probes_present_is_unaffected(self) -> None:
        harness = _OverviewHarness((_probe(1),), ())

        overview = harness.run(["Nucleo"])

        self.assertEqual(overview["status"], "setup_routes_ready")
        self.assertEqual(len(_rows(overview, "routes")), 1)

    def test_unknown_board_route_no_longer_asks_to_attach_a_probe(self) -> None:
        """The `== 0` arm inside the unknown-board route is dead and removed."""

        harness = _OverviewHarness((_probe(1),), ())

        overview = harness.run(["Nucleo"])

        facts = _rows(overview, "routes")[0]["required_user_facts"]
        self.assertNotIn("attach and identify one compatible debug probe", facts)

    def test_multiple_probe_route_still_asks_which_probe_belongs_to_the_board(self) -> None:
        harness = _OverviewHarness((_probe(1), _probe(2)), ())
        assignments = {"Nucleo": probe_connection_id("usb-1")}

        overview = harness.run(["Nucleo"], assignments)

        facts = _rows(overview, "routes")[0]["required_user_facts"]
        self.assertIn("which friendly debug-probe choice belongs to this board", facts)

    def test_zero_uart_rows_still_ask_conditionally_and_do_not_short_circuit(self) -> None:
        """The UART equivalent arm stays live -- it is not the probe case."""

        harness = _OverviewHarness((_probe(1),), ())

        overview = harness.run(["Nucleo"])

        self.assertEqual(overview["status"], "setup_routes_ready")
        facts = _rows(overview, "routes")[0]["required_user_facts"]
        self.assertIn("if UART is used, attach and identify the board's UART connection", facts)


class ProbeConnectionIdTests(unittest.TestCase):
    """0b: one mint site must reproduce what all four former sites produced."""

    CASES = (
        "683710208",
        "000683710208",
        "0",
        "0000",
        "ABCDEF123456",
        "abcdef123456",
        "AbCdEf123456",
        "066EFF505057717867163251",
    )

    def test_mint_is_stripped_and_casefolded(self) -> None:
        self.assertEqual(probe_connection_id("  AbC  "), "probe:abc")
        self.assertEqual(probe_connection_id("ABC"), "probe:abc")

    def test_stable_connection_identity_routes_through_the_shared_mint(self) -> None:
        for uid in self.CASES:
            with self.subTest(uid=uid):
                handle = object()
                metadata = SimpleNamespace(probe_uid=uid, runtime_token="token-1")
                with patch(
                    "pyocd_debug_mcp.services.connections.session_metadata",
                    return_value=metadata,
                ):
                    from pyocd_debug_mcp.services.connections import stable_connection_identity

                    self.assertEqual(
                        stable_connection_identity(handle),  # type: ignore[arg-type]
                        probe_connection_id(uid),
                    )

    def test_uid_less_provider_still_yields_a_session_token(self) -> None:
        handle = object()
        metadata = SimpleNamespace(probe_uid=None, runtime_token="token-9")
        with patch(
            "pyocd_debug_mcp.services.connections.session_metadata",
            return_value=metadata,
        ):
            from pyocd_debug_mcp.services.connections import stable_connection_identity

            self.assertEqual(
                stable_connection_identity(handle),  # type: ignore[arg-type]
                "session:token-9",
            )

    def test_canonical_key_is_unchanged_versus_every_former_construction(self) -> None:
        """`_setup_connection_key` must map old and new mints to the same key."""

        for uid in self.CASES:
            with self.subTest(uid=uid):
                former_casefolded = f"probe:{uid.casefold()}"
                former_raw = f"probe:{uid}"
                minted = probe_connection_id(uid)
                key = server._setup_connection_key
                self.assertEqual(key(minted), key(former_casefolded))
                self.assertEqual(key(minted), key(former_raw))

    def test_comparison_helper_is_unchanged_versus_every_former_construction(self) -> None:
        for uid in self.CASES:
            with self.subTest(uid=uid):
                minted = probe_connection_id(uid)
                self.assertTrue(server._same_setup_connection(minted, f"probe:{uid}"))
                self.assertTrue(server._same_setup_connection(f"probe:{uid}", minted))
                self.assertTrue(
                    server._same_setup_connection(minted, f"probe:{uid.casefold()}")
                )

    def test_decimal_leading_zeros_still_compare_equal_after_the_mint(self) -> None:
        self.assertTrue(
            server._same_setup_connection(
                probe_connection_id("000683710208"),
                probe_connection_id("683710208"),
            )
        )
        self.assertEqual(
            server._setup_connection_key(probe_connection_id("0000")),
            server._setup_connection_key(probe_connection_id("0")),
        )

    def test_mixed_case_hex_uids_still_compare_equal_after_the_mint(self) -> None:
        self.assertTrue(
            server._same_setup_connection(
                probe_connection_id("AbCdEf123456"),
                probe_connection_id("abcdef123456"),
            )
        )

    def test_distinct_uids_do_not_collide_after_the_mint(self) -> None:
        self.assertFalse(
            server._same_setup_connection(
                probe_connection_id("683710208"),
                probe_connection_id("683854191"),
            )
        )
        # Leading-zero stripping applies to decimals only; hex text is not renumbered.
        self.assertNotEqual(
            server._setup_connection_key(probe_connection_id("0abc")),
            server._setup_connection_key(probe_connection_id("abc")),
        )

    def test_session_scoped_identity_is_not_treated_as_a_probe_mint(self) -> None:
        self.assertFalse(
            server._same_setup_connection("session:token-1", probe_connection_id("token-1"))
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
