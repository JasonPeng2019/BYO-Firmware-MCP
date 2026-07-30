"""Opaque connection tokens resolve to exact selectors, or fail loudly.

The rule the whole file exists to protect: when a recorded selection can no longer be
re-derived, the assignment is cleared and the agent is routed back through setup. A
similarly described probe is never substituted.
"""

from __future__ import annotations

import unittest
from typing import Any, cast

from pyocd_debug_mcp.discovery_hooks import EMPTY_SNAPSHOT
from pyocd_debug_mcp.hardware_inventory import (
    InventorySnapshot,
    ProbeRow,
    ProbeSelection,
    ProbeSelectionStore,
    SelectionDisappeared,
    SelectionNotRecorded,
    SessionUartSelection,
    SessionUartSelectionStore,
    UartRow,
    derive_selection_from_token,
    find_selected_row,
)
from pyocd_debug_mcp.probe_inventory import EMPTY_NATIVE_PROBE_LISTING
from pyocd_debug_mcp.services.connections import probe_connection_id

SHA_A = "a" * 64
SHA_B = "b" * 64


def _probe_row(
    provider: str,
    uid: str | None,
    *,
    probe_id: str | None = None,
    provenance: tuple[str, ...] = ("native",),
    sha: str | None = None,
    scope: str = "stable",
) -> ProbeRow:
    identity = probe_id if probe_id is not None else (uid or "")
    return ProbeRow(
        provider=provider,
        probe_id=identity,
        unique_id=uid,
        row_id=f"row-{provider}-{identity}",
        description=f"{provider} {identity}",
        stable_identity=uid,
        provenance=provenance,
        hook_source_sha256=sha,
        identity_scope=cast(Any, scope),
        snapshot_id="snap",
    )


def _uart_row(path: str, *, scope: str = "session", serial: str | None = None) -> UartRow:
    return UartRow(
        port_path=path,
        description=f"UART {path}",
        usb_serial=serial,
        vid=1 if scope == "stable" else None,
        pid=2 if scope == "stable" else None,
        provenance=("hook:h",),
        identity_scope=cast(Any, scope),
        row_id=f"row-{path}",
        snapshot_id="snap",
    )


def _snapshot(*rows: ProbeRow, uarts: tuple[UartRow, ...] = ()) -> InventorySnapshot:
    return InventorySnapshot(
        snapshot_id="snap",
        probes=rows,
        uarts=uarts,
        native_probe_diagnostics=EMPTY_NATIVE_PROBE_LISTING,
        native_uart_available=True,
        hook_diagnostics=(),
    )


class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProbeSelectionStore()

    def test_connection_id_resolves_to_provider_and_unique_id(self) -> None:
        row = _probe_row("jlink", "683710208")
        token = probe_connection_id("683710208")
        self.store.record(ProbeSelection.from_row(token, row))

        selection = self.store.resolve(token, _snapshot(row))

        self.assertEqual(selection.provider, "jlink")
        self.assertEqual(selection.unique_id, "683710208")
        self.assertEqual(selection.identity_scope, "stable")
        self.assertTrue(selection.durable)

    def test_resolution_is_case_insensitive_on_the_token(self) -> None:
        row = _probe_row("cmsisdap", "AbCdEf")
        self.store.record(ProbeSelection.from_row("probe:abcdef", row))

        selection = self.store.resolve("PROBE:ABCDEF", _snapshot(row))

        self.assertEqual(selection.unique_id, "AbCdEf")

    def test_a_decimal_uid_still_resolves_after_leading_zeros_change(self) -> None:
        recorded_row = _probe_row("jlink", "683710208")
        self.store.record(ProbeSelection.from_row("probe:683710208", recorded_row))
        fresh = _probe_row("jlink", "000683710208")

        selection = self.store.resolve("probe:683710208", _snapshot(fresh))

        self.assertEqual(selection.unique_id, "000683710208")

    def test_a_missing_row_raises_instead_of_selecting_another_probe(self) -> None:
        recorded = _probe_row("jlink", "111")
        other = _probe_row("jlink", "222")
        self.store.record(ProbeSelection.from_row("probe:111", recorded))

        with self.assertRaises(SelectionDisappeared) as caught:
            self.store.resolve("probe:111", _snapshot(other))

        self.assertEqual(caught.exception.code, "discovery/selection-disappeared")
        self.assertIn("no longer present", caught.exception.reason)

    def test_a_same_uid_row_under_a_different_provider_is_not_a_match(self) -> None:
        recorded = _probe_row("jlink", "12345")
        self.store.record(ProbeSelection.from_row("probe:12345", recorded))

        with self.assertRaises(SelectionDisappeared):
            self.store.resolve("probe:12345", _snapshot(_probe_row("cmsisdap", "12345")))

    def test_an_empty_snapshot_raises(self) -> None:
        self.store.record(ProbeSelection.from_row("probe:111", _probe_row("jlink", "111")))

        with self.assertRaises(SelectionDisappeared):
            self.store.resolve("probe:111", _snapshot())

    def test_a_hook_source_change_raises_and_names_the_refresh(self) -> None:
        recorded = _probe_row("jlink", "111", provenance=("hook:h",), sha=SHA_A)
        self.store.record(ProbeSelection.from_row("probe:111", recorded))
        changed = _probe_row("jlink", "111", provenance=("hook:h",), sha=SHA_B)

        with self.assertRaises(SelectionDisappeared) as caught:
            self.store.resolve("probe:111", _snapshot(changed))

        self.assertIn("refresh_discovery_hooks", caught.exception.reason)

    def test_the_same_hook_source_still_resolves(self) -> None:
        row = _probe_row("jlink", "111", provenance=("hook:h",), sha=SHA_A)
        self.store.record(ProbeSelection.from_row("probe:111", row))

        selection = self.store.resolve("probe:111", _snapshot(row))

        self.assertEqual(selection.hook_source_sha256, SHA_A)

    def test_a_device_that_becomes_natively_visible_still_resolves(self) -> None:
        """A hook row on one refresh and a native row on the next is the same device."""

        recorded = _probe_row("jlink", "111", provenance=("hook:h",), sha=SHA_A)
        self.store.record(ProbeSelection.from_row("probe:111", recorded))
        native = _probe_row("jlink", "111", provenance=("native",), sha=None)

        selection = self.store.resolve("probe:111", _snapshot(native))

        self.assertEqual(selection.provenance, ("native",))
        self.assertIsNone(selection.hook_source_sha256)

    def test_a_device_that_becomes_hook_only_still_resolves(self) -> None:
        recorded = _probe_row("jlink", "111", provenance=("native",))
        self.store.record(ProbeSelection.from_row("probe:111", recorded))
        hooked = _probe_row("jlink", "111", provenance=("hook:h",), sha=SHA_A)

        selection = self.store.resolve("probe:111", _snapshot(hooked))

        self.assertEqual(selection.hook_source_sha256, SHA_A)

    def test_an_unrecorded_token_is_derived_from_the_snapshot(self) -> None:
        """An assignment can predate the store; deriving is not trusting."""

        row = _probe_row("jlink", "683710208")

        selection = self.store.resolve("probe:683710208", _snapshot(row))

        self.assertEqual(selection.unique_id, "683710208")
        self.assertIsNotNone(self.store.recorded("probe:683710208"))

    def test_an_unrecorded_token_with_no_matching_row_is_refused(self) -> None:
        with self.assertRaises(SelectionNotRecorded) as caught:
            self.store.resolve("probe:nope", _snapshot(_probe_row("jlink", "111")))

        self.assertEqual(caught.exception.code, "discovery/selection-disappeared")
        self.assertIn("no longer present", caught.exception.reason)

    def test_forget_removes_only_the_named_selection(self) -> None:
        self.store.record(ProbeSelection.from_row("probe:a", _probe_row("jlink", "a")))
        self.store.record(ProbeSelection.from_row("probe:b", _probe_row("jlink", "b")))

        self.store.forget("probe:a")

        self.assertIsNone(self.store.recorded("probe:a"))
        self.assertIsNotNone(self.store.recorded("probe:b"))

    def test_clear_removes_everything(self) -> None:
        self.store.record(ProbeSelection.from_row("probe:a", _probe_row("jlink", "a")))

        self.store.clear()

        self.assertIsNone(self.store.recorded("probe:a"))


class SessionScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProbeSelectionStore()

    def test_a_session_selection_is_not_durable(self) -> None:
        row = _probe_row("jlink", None, probe_id="session:tok", scope="session")

        selection = ProbeSelection.from_row("session:tok", row)

        self.assertFalse(selection.durable)
        self.assertIsNone(selection.unique_id)
        self.assertEqual(selection.identity_scope, "session")

    def test_a_session_selection_resolves_only_by_its_exact_token(self) -> None:
        row = _probe_row("jlink", None, probe_id="session:tok", scope="session")
        self.store.record(ProbeSelection.from_row("session:tok", row))

        selection = self.store.resolve("session:tok", _snapshot(row))

        self.assertIsNone(selection.unique_id)
        self.assertEqual(selection.connection_id, "session:tok")

    def test_a_session_token_does_not_match_a_different_live_session(self) -> None:
        recorded = _probe_row("jlink", None, probe_id="session:old", scope="session")
        self.store.record(ProbeSelection.from_row("session:old", recorded))
        reconnected = _probe_row("jlink", None, probe_id="session:new", scope="session")

        with self.assertRaises(SelectionDisappeared):
            self.store.resolve("session:old", _snapshot(reconnected))

    def test_a_session_selection_never_matches_a_stable_row(self) -> None:
        recorded = _probe_row("jlink", None, probe_id="session:tok", scope="session")
        selection = ProbeSelection.from_row("session:tok", recorded)

        self.assertIsNone(find_selected_row(selection, _snapshot(_probe_row("jlink", "111"))))

    def test_a_stable_selection_never_matches_a_session_row(self) -> None:
        selection = ProbeSelection.from_row("probe:111", _probe_row("jlink", "111"))
        session_row = _probe_row("jlink", None, probe_id="session:tok", scope="session")

        self.assertIsNone(find_selected_row(selection, _snapshot(session_row)))


class TokenDerivationTests(unittest.TestCase):
    """Reproduces exactly what the former `_connection_matches_probe` accepted."""

    def test_a_prefixed_token_matches_a_stable_identity(self) -> None:
        selection = derive_selection_from_token(
            "probe:683710208", _snapshot(_probe_row("jlink", "683710208"))
        )

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(selection.unique_id, "683710208")

    def test_a_bare_token_matches_a_stable_identity(self) -> None:
        selection = derive_selection_from_token(
            "683710208", _snapshot(_probe_row("jlink", "683710208"))
        )

        self.assertIsNotNone(selection)

    def test_a_session_token_matches_the_whole_probe_id(self) -> None:
        row = _probe_row("jlink", None, probe_id="session:tok", scope="session")

        selection = derive_selection_from_token("session:tok", _snapshot(row))

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertIsNone(selection.unique_id)

    def test_decimal_leading_zeros_are_accepted(self) -> None:
        self.assertIsNotNone(
            derive_selection_from_token(
                "probe:000683710208", _snapshot(_probe_row("jlink", "683710208"))
            )
        )

    def test_no_match_returns_none_rather_than_guessing(self) -> None:
        self.assertIsNone(
            derive_selection_from_token("probe:999", _snapshot(_probe_row("jlink", "111")))
        )


class SessionUartSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionUartSelectionStore()

    def _record(self, board: str = "board-1", path: str = "COM9") -> None:
        self.store.record(
            SessionUartSelection(
                board_id=board, port_path=path, description="Session UART",
                provenance=("hook:h",),
            )
        )

    def test_a_recorded_session_endpoint_resolves_to_its_row(self) -> None:
        self._record()
        row = _uart_row("COM9")

        resolved = self.store.resolve("board-1", _snapshot(uarts=(row,)))

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.port_path, "COM9")

    def test_the_windows_device_prefix_is_normalized(self) -> None:
        self._record(path=r"\\.\COM9")

        resolved = self.store.resolve("board-1", _snapshot(uarts=(_uart_row("COM9"),)))

        self.assertIsNotNone(resolved)

    def test_a_disappeared_endpoint_is_forgotten_not_substituted(self) -> None:
        self._record()

        resolved = self.store.resolve("board-1", _snapshot(uarts=(_uart_row("COM4"),)))

        self.assertIsNone(resolved)
        self.assertIsNone(self.store.recorded("board-1"), "the stale choice was retained")

    def test_a_newly_ambiguous_endpoint_is_forgotten(self) -> None:
        """Two rows on the same normalized path is not a single unambiguous endpoint."""

        self._record()
        rows = (_uart_row("COM9"), _uart_row(r"\\.\COM9"))

        resolved = self.store.resolve("board-1", _snapshot(uarts=rows))

        self.assertIsNone(resolved)
        self.assertIsNone(self.store.recorded("board-1"))

    def test_a_stable_row_does_not_satisfy_a_session_selection(self) -> None:
        self._record()
        stable = _uart_row("COM9", scope="stable", serial="SER9")

        self.assertIsNone(self.store.resolve("board-1", _snapshot(uarts=(stable,))))

    def test_an_unrecorded_board_resolves_to_none_without_error(self) -> None:
        self.assertIsNone(self.store.resolve("board-9", _snapshot(uarts=(_uart_row("COM9"),))))

    def test_clear_board_leaves_other_boards_alone(self) -> None:
        self._record("board-1", "COM9")
        self._record("board-2", "COM8")

        self.store.clear_board("board-1")

        self.assertIsNone(self.store.recorded("board-1"))
        self.assertIsNotNone(self.store.recorded("board-2"))

    def test_recording_twice_for_one_board_replaces_the_choice(self) -> None:
        self._record("board-1", "COM9")
        self._record("board-1", "COM8")

        recorded = self.store.recorded("board-1")

        assert recorded is not None
        self.assertEqual(recorded.port_path, "COM8")


class HookRefreshInvalidationTests(unittest.TestCase):
    """A refresh can change or remove the hook that found a device."""

    def test_a_refresh_clears_both_selection_stores(self) -> None:
        from pyocd_debug_mcp import server

        server._probe_selection_store.record(
            ProbeSelection.from_row("probe:111", _probe_row("jlink", "111"))
        )
        server._session_uart_selections.record(
            SessionUartSelection("board-1", "COM9", "Session UART", ("hook:h",))
        )

        server._on_discovery_hooks_refreshed(EMPTY_SNAPSHOT)

        self.assertIsNone(server._probe_selection_store.recorded("probe:111"))
        self.assertIsNone(server._session_uart_selections.recorded("board-1"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
