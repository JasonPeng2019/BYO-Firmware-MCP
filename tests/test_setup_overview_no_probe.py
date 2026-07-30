"""Step 0 precursor fixes: the no-probe status and the single connection_id mint."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Mapping, cast
from unittest.mock import Mock, patch

from pyocd_debug_mcp import server
from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
from pyocd_debug_mcp.services.connections import (
    LEGACY_PROBE_CONNECTION_PREFIX,
    PROBE_CONNECTION_PREFIX,
    parse_probe_connection_id,
    probe_connection_id,
)


def _rows(overview: Mapping[str, object], key: str) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", overview[key])


def _probe(
    index: int, *, usb_serial: str | None = None, probe_family: str = "cmsis-dap"
) -> SimpleNamespace:
    serial = f"usb-{index}" if usb_serial is None else usb_serial
    return SimpleNamespace(
        usb_serial=serial,
        probe_id=f"probe-{index}",
        probe_family=probe_family,
        description=f"Probe {index}",
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
    """Drive `_setup_overview` with a synthetic inventory and no profile store.

    Patches the inventory *service*, not just `_validation_inventory`. There is real
    debug hardware on the developer bench this suite runs on, so a harness that only
    patched the legacy shape would still take a live snapshot and report real probes in
    the no-probe payload's diagnostics.
    """

    def __init__(self, probes: tuple[SimpleNamespace, ...], serial: tuple[SimpleNamespace, ...]):
        self.inventory = SimpleNamespace(probes=probes, serial_ports=serial)
        self.snapshot = snapshot_from_validation_inventory(
            cast(Any, self.inventory),
        )
        self.replace = Mock()

    def run(self, names: list[str] | None, assignments: dict[str, str] | None = None):
        service = SimpleNamespace(
            snapshot=lambda: self.snapshot,
            validation_inventory=lambda: self.inventory,
        )
        with (
            patch.object(server, "_profile_repository", SimpleNamespace(load_all=lambda: ())),
            patch.object(server, "_hardware_inventory", service),
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
        self.assertIn("not a board-naming ambiguity", prompt)

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

        # Every key its sibling early returns carry is still present. Step 6 adds the
        # typed code and the hook remedy on top; it does not drop any of these.
        self.assertLessEqual(
            {
                "status",
                "agent_prompt",
                "profiles",
                "connections",
                "serial_choices",
                "inventory_error",
                "routes",
            },
            set(overview),
        )
        self.assertEqual(overview["code"], "discovery/no-native-probe")
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
        assignments = {"Nucleo": probe_connection_id("cmsis-dap", "usb-1")}

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
    """0b/FIX 8: one mint site, now provider-qualified (C7/D8).

    Historical note on this class's rewrite: before FIX 8, `probe_connection_id` took
    a bare UID and this class asserted the mint was byte-identical to the four former
    2-part construction sites it replaced (step 0b, a pure hygiene refactor with no
    behavior change). FIX 8 adds a required `provider` parameter and changes the
    minted *string* -- that is the fix, not a regression -- so
    `test_mint_is_stripped_and_casefolded` and the old
    `test_canonical_key_is_unchanged_versus_every_former_construction` (which asserted
    the new mint's `_setup_connection_key` was byte-equal to the legacy 2-part key)
    could not be preserved unchanged; both are updated below to assert the new
    provider-qualified contract instead. Every other test in this class needed only a
    provider argument added to its `probe_connection_id(...)` calls -- the asserted
    *values* were unaffected, because `_same_setup_connection`/`_setup_connection_key`
    still tolerate a legacy 2-part token on the other side of a comparison.
    """

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

    def test_mint_is_provider_qualified_stripped_and_casefolded(self) -> None:
        self.assertEqual(probe_connection_id("JLink", "  AbC  "), "probeid:jlink:abc")
        self.assertEqual(probe_connection_id("jlink", "ABC"), "probeid:jlink:abc")

    def test_stable_connection_identity_routes_through_the_shared_mint(self) -> None:
        for uid in self.CASES:
            with self.subTest(uid=uid):
                handle = object()
                metadata = SimpleNamespace(
                    probe_uid=uid, probe_family="jlink", runtime_token="token-1"
                )
                with patch(
                    "pyocd_debug_mcp.services.connections.session_metadata",
                    return_value=metadata,
                ):
                    from pyocd_debug_mcp.services.connections import stable_connection_identity

                    self.assertEqual(
                        stable_connection_identity(handle),  # type: ignore[arg-type]
                        probe_connection_id("jlink", uid),
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

    def test_canonical_key_is_provider_qualified_but_still_tolerates_a_legacy_token(
        self,
    ) -> None:
        """FIX 8: the key now differs by provider; a legacy 2-part token is untouched."""

        for uid in self.CASES:
            with self.subTest(uid=uid):
                minted = probe_connection_id("jlink", uid)
                key = server._setup_connection_key
                # The canonical key carries the provider -- it can no longer equal a
                # provider-less legacy key (that collapse is exactly the C7/D8 bug).
                self.assertNotEqual(key(minted), key(f"probe:{uid.casefold()}"))
                self.assertIn("jlink", key(minted))
                # Two different providers, identical UID text, must key differently.
                self.assertNotEqual(key(minted), key(probe_connection_id("cmsisdap", uid)))
                # The same provider still normalizes decimal leading zeros as before.
                self.assertEqual(
                    key(probe_connection_id("jlink", "0" + uid)) if uid.isdecimal() else "skip",
                    key(minted) if uid.isdecimal() else "skip",
                )

    def test_comparison_helper_tolerates_a_legacy_token_on_either_side(self) -> None:
        """A legacy 2-part token is provider-blind by necessity; equality still holds."""

        for uid in self.CASES:
            with self.subTest(uid=uid):
                minted = probe_connection_id("jlink", uid)
                self.assertTrue(server._same_setup_connection(minted, f"probe:{uid}"))
                self.assertTrue(server._same_setup_connection(f"probe:{uid}", minted))
                self.assertTrue(
                    server._same_setup_connection(minted, f"probe:{uid.casefold()}")
                )

    def test_two_canonical_tokens_for_the_same_provider_still_compare_equal(self) -> None:
        for uid in self.CASES:
            with self.subTest(uid=uid):
                self.assertTrue(
                    server._same_setup_connection(
                        probe_connection_id("jlink", uid),
                        probe_connection_id("JLink", uid.casefold()),
                    )
                )

    def test_decimal_leading_zeros_still_compare_equal_after_the_mint(self) -> None:
        self.assertTrue(
            server._same_setup_connection(
                probe_connection_id("jlink", "000683710208"),
                probe_connection_id("jlink", "683710208"),
            )
        )
        self.assertEqual(
            server._setup_connection_key(probe_connection_id("jlink", "0000")),
            server._setup_connection_key(probe_connection_id("jlink", "0")),
        )

    def test_mixed_case_hex_uids_still_compare_equal_after_the_mint(self) -> None:
        self.assertTrue(
            server._same_setup_connection(
                probe_connection_id("jlink", "AbCdEf123456"),
                probe_connection_id("jlink", "abcdef123456"),
            )
        )

    def test_distinct_uids_do_not_collide_after_the_mint(self) -> None:
        self.assertFalse(
            server._same_setup_connection(
                probe_connection_id("jlink", "683710208"),
                probe_connection_id("jlink", "683854191"),
            )
        )
        # Leading-zero stripping applies to decimals only; hex text is not renumbered.
        self.assertNotEqual(
            server._setup_connection_key(probe_connection_id("jlink", "0abc")),
            server._setup_connection_key(probe_connection_id("jlink", "abc")),
        )

    def test_distinct_providers_with_identical_uid_text_do_not_collide(self) -> None:
        """FIX 8 (C7/D8): the exact collision that used to silently drop a probe."""

        self.assertFalse(
            server._same_setup_connection(
                probe_connection_id("cmsisdap", "12345"),
                probe_connection_id("jlink", "12345"),
            )
        )
        self.assertNotEqual(
            server._setup_connection_key(probe_connection_id("cmsisdap", "12345")),
            server._setup_connection_key(probe_connection_id("jlink", "12345")),
        )

    def test_session_scoped_identity_is_not_treated_as_a_probe_mint(self) -> None:
        self.assertFalse(
            server._same_setup_connection(
                "session:token-1", probe_connection_id("jlink", "token-1")
            )
        )


class StructuralLegacyDiscriminationTests(unittest.TestCase):
    """C12/D11: canonical-vs-legacy is a prefix, never a colon-count heuristic.

    `parse_probe_connection_id` used to decide "canonical" by asking whether the text
    after `probe:` contained a second colon. A legacy (pre-FIX-8, unencoded) token
    whose UID itself contains a colon -- realistic for hook-reported values; the
    guide's own cross-platform hook guidance points authors at USB topology values
    like Linux's `3-1.4:1.0` -- defeated that heuristic and could misparse into a
    fabricated provider, in the worst case resolving a token to a different, real
    probe (see `tests/test_probe_selection_records.py::TokenDerivationTests` for the
    end-to-end reproductions). The fix gives the canonical format a dedicated prefix
    (`probeid:`) that no legacy token can ever produce, regardless of UID content.
    """

    def test_the_two_prefixes_are_structurally_distinct(self) -> None:
        self.assertNotEqual(PROBE_CONNECTION_PREFIX, LEGACY_PROBE_CONNECTION_PREFIX)
        self.assertFalse(PROBE_CONNECTION_PREFIX.startswith(LEGACY_PROBE_CONNECTION_PREFIX))

    def test_a_legacy_token_with_a_colon_in_the_uid_is_never_parsed_as_canonical(
        self,
    ) -> None:
        for uid_with_colon in ("3-1.4:1.0", "jlink:001", "a:b:c:d"):
            with self.subTest(uid=uid_with_colon):
                legacy_token = f"{LEGACY_PROBE_CONNECTION_PREFIX}{uid_with_colon}"
                self.assertIsNone(parse_probe_connection_id(legacy_token))

    def test_a_canonical_token_still_parses_correctly_regardless_of_uid_content(
        self,
    ) -> None:
        for uid_with_colon in ("3-1.4:1.0", "jlink:001", "a:b:c:d"):
            with self.subTest(uid=uid_with_colon):
                minted = probe_connection_id("cmsisdap", uid_with_colon)
                self.assertTrue(minted.startswith(PROBE_CONNECTION_PREFIX))
                parsed = parse_probe_connection_id(minted)
                self.assertEqual(parsed, ("cmsisdap", uid_with_colon.casefold()))

    def test_a_provider_containing_a_colon_still_round_trips(self) -> None:
        """The guide asks for robustness even though no real provider name has one."""

        minted = probe_connection_id("weird:vendor", "12345")
        self.assertEqual(parse_probe_connection_id(minted), ("weird:vendor", "12345"))


class CrossProviderCollisionTests(unittest.TestCase):
    """FIX 8 (C7/D8): two providers with identical UID text stay distinct and visible.

    The guide states the merge rule as a hard requirement ("never merge across
    providers, even on identical UID text") and `HardwareInventoryService` already
    honored it -- the bug was that `_setup_overview`'s dedup, downstream of the merge,
    threw it away by minting `connection_id` from UID text alone. These drive the
    scenario end to end through `_setup_overview`, the layer that actually silently
    dropped one of the two real, distinct probes before this fix.
    """

    def test_two_providers_with_the_same_uid_text_both_stay_visible_and_selectable(
        self,
    ) -> None:
        probes = (
            _probe(1, usb_serial="12345", probe_family="cmsisdap"),
            _probe(2, usb_serial="12345", probe_family="jlink"),
        )
        harness = _OverviewHarness(probes, ())

        overview = harness.run(None)

        connection_ids = {row["connection_id"] for row in _rows(overview, "connections")}
        self.assertEqual(
            len(connection_ids), 2, "one of the two distinct probes was silently dropped"
        )
        self.assertIn(probe_connection_id("cmsisdap", "12345"), connection_ids)
        self.assertIn(probe_connection_id("jlink", "12345"), connection_ids)

    def test_each_colliding_uid_still_resolves_to_its_own_provider(self) -> None:
        cmsisdap_row = _probe(1, usb_serial="12345", probe_family="cmsisdap")
        jlink_row = _probe(2, usb_serial="12345", probe_family="jlink")
        harness = _OverviewHarness((cmsisdap_row, jlink_row), ())

        harness.run(None)

        cmsisdap_selection = server._probe_selection_store.resolve(
            probe_connection_id("cmsisdap", "12345"), harness.snapshot
        )
        jlink_selection = server._probe_selection_store.resolve(
            probe_connection_id("jlink", "12345"), harness.snapshot
        )
        self.assertEqual(cmsisdap_selection.provider, "cmsisdap")
        self.assertEqual(jlink_selection.provider, "jlink")

    def test_decimal_normalized_collisions_across_providers_stay_distinct(self) -> None:
        """A J-Link-style decimal UID and its zero-padded twin, under two providers."""

        probes = (
            _probe(1, usb_serial="683710208", probe_family="jlink"),
            _probe(2, usb_serial="000683710208", probe_family="cmsisdap"),
        )
        harness = _OverviewHarness(probes, ())

        overview = harness.run(None)

        connection_ids = {row["connection_id"] for row in _rows(overview, "connections")}
        self.assertEqual(len(connection_ids), 2)

    def test_a_legacy_ambiguous_token_is_refused_not_guessed(self) -> None:
        from pyocd_debug_mcp.hardware_inventory import (
            SelectionDisappeared,
            snapshot_from_validation_inventory,
        )

        probes = (
            _probe(1, usb_serial="12345", probe_family="cmsisdap"),
            _probe(2, usb_serial="12345", probe_family="jlink"),
        )
        snapshot = snapshot_from_validation_inventory(
            cast(Any, SimpleNamespace(probes=probes, serial_ports=()))
        )
        store = server.ProbeSelectionStore()

        with self.assertRaises(SelectionDisappeared):
            store.resolve("probe:12345", snapshot)

    def test_a_legacy_unambiguous_token_still_works(self) -> None:
        from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory

        probes = (_probe(1, usb_serial="12345", probe_family="jlink"),)
        snapshot = snapshot_from_validation_inventory(
            cast(Any, SimpleNamespace(probes=probes, serial_ports=()))
        )
        store = server.ProbeSelectionStore()

        selection = store.resolve("probe:12345", snapshot)

        self.assertEqual(selection.provider, "jlink")
        self.assertEqual(selection.unique_id, "12345")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
