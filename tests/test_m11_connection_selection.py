"""Implementer regressions for explicit connection intent and saved routing."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pyocd_debug_mcp import server
from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.capabilities.policy import Tier
from pyocd_debug_mcp.hardware_inventory import (
    ProbeSelectionStore,
    snapshot_from_validation_inventory,
)
from pyocd_debug_mcp.services.connections import ConnectionManager, probe_connection_id
from pyocd_debug_mcp.setup_flow.setup import RunAssignmentStore
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from pyocd_debug_mcp.target_errors import TargetConnectionError


def inventory(*uids: str, provider: str = "jlink") -> ValidationInventory:
    return ValidationInventory(
        probes=tuple(ValidationProbe(uid, "Probe", provider, uid) for uid in uids)
    )


class RawSelectionTests(unittest.TestCase):
    def connect(
        self,
        uid: str | None,
        probes: ValidationInventory,
        *,
        assigned: bool = False,
        after_inventory: ValidationInventory | None = None,
    ) -> tuple[str, Mock]:
        bindings = RunAssignmentStore({})
        if assigned:
            bindings.assign(probe_connection_id("jlink", "12345"), "new_label")
        opener = Mock(
            side_effect=lambda **kw: TargetSessionHandle(
                None, None, kw["unique_id"], "fake", kw["target"]
            )
        )
        with (
            patch.object(
                server,
                "_tier_router",
                SimpleNamespace(state_for=lambda _b: SimpleNamespace(tier=Tier.NO_SETUP)),
            ),
            patch.object(server, "assignment_store", bindings),
            patch.object(server, "connection_manager", ConnectionManager()),
            patch.object(server, "_probe_selection_store", ProbeSelectionStore()),
            patch.object(
                server,
                "_validation_inventory",
                return_value=probes,
                side_effect=[probes, after_inventory] if after_inventory is not None else None,
            ),
            patch.object(server, "_tiered_auto_target_resolver", None),
            patch.object(server.target_control, "open_session", opener),
            patch.object(
                server,
                "_promote_open_session",
                return_value=SimpleNamespace(runtime_session=SimpleNamespace(session_id="test")),
            ),
            patch.object(server, "_record_event"),
        ):
            try:
                result = server.connect("new_label", probe_uid=uid, target="cortex_m")
            except Exception:
                opener.assert_not_called()
                raise
        return result, opener

    def test_unknown_label_or_bad_selector_cannot_acquire_singleton(self) -> None:
        for selector in (None, "null", "", "typo"):
            with self.subTest(selector=selector), self.assertRaises((RuntimeError, ValueError)):
                self.connect(selector, inventory("12345"))

    def test_exact_partial_and_assigned_raw_labels_work(self) -> None:
        for selector, assigned in (("12345", False), ("234", False), (None, True)):
            with self.subTest(selector=selector, assigned=assigned):
                result, opened = self.connect(selector, inventory("12345"), assigned=assigned)
                self.assertIn("Connected", result)
                self.assertEqual(opened.call_args.kwargs["unique_id"], "12345")
                self.assertIsNone(opened.call_args.kwargs["board"])
                self.assertEqual(opened.call_args.kwargs["target"], "cortex_m")

    def test_ambiguous_and_missing_selectors_refuse_before_open(self) -> None:
        for probes in (inventory("12345", "12346"), inventory()):
            with self.subTest(probes=probes), self.assertRaises(RuntimeError):
                self.connect("1234", probes)

    def test_exact_selector_and_opaque_token_work_with_multiple_probes(self) -> None:
        for selector in ("12345", probe_connection_id("jlink", "12345")):
            with self.subTest(selector=selector):
                _, opened = self.connect(selector, inventory("12345", "67890"))
                self.assertIn("12345", opened.call_args.kwargs["unique_id"])

    def test_disappearing_assignment_is_never_reinterpreted_as_partial_uid(self) -> None:
        _, opened = self.connect(
            None,
            inventory("12345"),
            assigned=True,
            after_inventory=inventory("12345-unrelated"),
        )
        self.assertEqual(opened.call_args.kwargs["unique_id"], "12345")

    def test_same_uid_in_two_providers_is_ambiguous(self) -> None:
        probes = ValidationInventory(
            probes=inventory("12345").probes + inventory("12345", provider="cmsisdap").probes
        )
        with self.assertRaises(RuntimeError):
            self.connect("12345", probes)
        _, opened = self.connect("jlink:12345", probes)
        self.assertEqual(opened.call_args.kwargs["unique_id"], "jlink:12345")


class KnownProfileGuidanceTests(unittest.TestCase):
    def overview(
        self,
        probes: ValidationInventory,
        *,
        busy: bool = False,
        explicit: str | None = None,
        stored: bool = True,
    ):
        expected = probe_connection_id("stlink", "saved-stm")
        assignments = RunAssignmentStore({})
        if stored:
            assignments.assign(expected, "stm")
        manager = ConnectionManager()
        if busy:
            manager.assign(
                "other_owner",
                TargetSessionHandle(None, None, "saved-stm", "fake", None),
                Mock(),
                connection_id=expected,
            )
        profile = SimpleNamespace(
            board_id="stm",
            display_name="STM",
            mcu_part_number="STM32L476RGT6",
            board=SimpleNamespace(probe_family="stlink"),
        )
        snapshot = snapshot_from_validation_inventory(probes)
        with (
            patch.object(
                server, "_profile_repository", SimpleNamespace(load_all=lambda: (profile,))
            ),
            patch.object(
                server,
                "_capability_policies",
                SimpleNamespace(resolve=lambda _b: SimpleNamespace(tier=Tier.SETUP_FULL)),
            ),
            patch.object(server, "_profile_needs_repair", return_value=False),
            patch.object(
                server,
                "_safety_repository",
                SimpleNamespace(load_current=lambda _b: SimpleNamespace(regions=())),
            ),
            patch.object(server, "_require_current_reviewed_map"),
            patch.object(server, "_hardware_inventory", SimpleNamespace(snapshot=lambda: snapshot)),
            patch.object(server, "_validation_inventory", return_value=probes),
            patch.object(server, "_probe_selection_store", ProbeSelectionStore()),
            patch.object(server, "assignment_store", assignments),
            patch.object(server, "connection_manager", manager),
            patch.object(server, "_replace_setup_assignments") as replace,
        ):
            result = server._setup_overview(["STM"], {"STM": explicit} if explicit else None)
        self.assertEqual(assignments.connection_for("stm"), expected if stored else None)
        replace.assert_not_called()
        return result

    def test_absent_saved_stm_with_only_nordic_never_routes_validation(self) -> None:
        result = self.overview(inventory("nordic"))
        self.assertEqual(result["status"], "setup_probe_unavailable")
        self.assertEqual(result["routes"][0]["route"], "probe-unavailable")
        self.assertIsNone(result["routes"][0]["next_tool"])
        self.assertIsNone(result["connections"][0]["assigned_board_id"])
        self.assertIn("not visible", result["routes"][0]["reason"])

    def test_known_stm_without_run_assignment_cannot_adopt_nordic(self) -> None:
        result = self.overview(inventory("nordic"), stored=False)
        self.assertEqual(result["status"], "setup_probe_unavailable")
        self.assertIsNone(result["routes"][0]["next_tool"])
        self.assertIsNone(result["connections"][0]["assigned_board_id"])

    def test_saved_binding_refuses_explicit_same_provider_substitution(self) -> None:
        result = self.overview(
            inventory("saved-stm", "different-stm", provider="stlink"),
            explicit=probe_connection_id("stlink", "different-stm"),
        )
        self.assertIn("differs", result["routes"][0]["reason"])

    def test_busy_saved_probe_reports_only_evidenced_owner(self) -> None:
        result = self.overview(inventory("saved-stm", provider="stlink"), busy=True)
        self.assertIn("busy in this server", result["routes"][0]["reason"])
        self.assertIn("other_owner", result["routes"][0]["reason"])

    def test_no_probes_or_explicit_unrelated_choice_preserves_binding(self) -> None:
        self.assertEqual(self.overview(inventory())["status"], "setup_probe_unavailable")
        result = self.overview(inventory("nordic"), explicit=probe_connection_id("jlink", "nordic"))
        self.assertEqual(result["status"], "setup_probe_unavailable")


class AdapterAdmissionTests(unittest.TestCase):
    def open(
        self,
        *,
        uid: str = "selected",
        observed: str = "nrf52840",
        target: str = "stm32f103rc",
        reset: bool = False,
    ):
        session = SimpleNamespace(
            probe=SimpleNamespace(
                unique_id=uid,
                associated_board_info=SimpleNamespace(target=observed),
                assert_reset=Mock(),
            ),
            open=Mock(),
            close=Mock(),
            target=Mock(),
        )
        session.target.get_state.return_value = SimpleNamespace(name="HALTED")
        adapter = swd_pyocd.PyOCDSWDInterface()
        with (
            patch.object(adapter, "_choose_session", return_value=session),
            patch.object(swd_pyocd, "verified_pack_for_target", return_value=None),
        ):
            try:
                method = adapter.connect_under_reset if reset else adapter.open
                method(board=None, unique_id="selected", target=target)
            except TargetConnectionError:
                session.open.assert_not_called()
                session.probe.assert_reset.assert_not_called()
                session.target.halt.assert_not_called()
                session.close.assert_called_once()
                raise
        return session

    def test_catalog_vendor_contradiction_refuses_before_open_or_reset(self) -> None:
        for reset in (False, True):
            with (
                self.subTest(reset=reset),
                self.assertRaisesRegex(TargetConnectionError, "catalog"),
            ):
                self.open(reset=reset)

    def test_wrong_selected_uid_refuses_before_target_initialization(self) -> None:
        with self.assertRaisesRegex(TargetConnectionError, "requested UID"):
            self.open(uid="unrelated", target="cortex_m")

    def test_generic_matching_and_unknown_catalog_access_remain_usable(self) -> None:
        for observed, target in (
            ("nrf52840", "cortex_m"),
            ("nrf52840", "nrf52840"),
            ("unknown", "stm32f103rc"),
        ):
            with self.subTest(observed=observed, target=target):
                session = self.open(observed=observed, target=target)
                session.open.assert_called_once()

    def test_native_qualified_selector_matches_its_bare_probe_uid(self) -> None:
        self.assertTrue(swd_pyocd._same_probe_uid("jlink:00123", "123"))
        self.assertFalse(swd_pyocd._same_probe_uid("jlink:123", "stlink:123"))


if __name__ == "__main__":
    unittest.main()
