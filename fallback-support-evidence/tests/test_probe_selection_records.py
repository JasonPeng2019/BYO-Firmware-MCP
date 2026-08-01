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
    MAX_PROBE_SELECTIONS,
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
        token = probe_connection_id("jlink", "683710208")
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


class BoundedStoreTests(unittest.TestCase):
    """FIX 2 regression (C2): `ProbeSelectionStore` must not grow without bound.

    A UID-less provider mints `connection_id` as `session:<uuid4>`, freshly random on
    every connect, and the only thing that ever clears this store is a hook refresh --
    which a server whose native discovery works normally may never call. Bounded the
    same way `DiscoveryRetryStore` is: an `OrderedDict`, a cap, oldest-evicted-on-insert.
    """

    def setUp(self) -> None:
        self.store = ProbeSelectionStore()

    def test_the_cap_holds(self) -> None:
        for index in range(MAX_PROBE_SELECTIONS + 25):
            row = _probe_row("jlink", str(index))
            self.store.record(ProbeSelection.from_row(f"probe:{index}", row))

        self.assertEqual(self.store.count(), MAX_PROBE_SELECTIONS)

    def test_eviction_is_oldest_first(self) -> None:
        overflow = 3
        for index in range(MAX_PROBE_SELECTIONS + overflow):
            row = _probe_row("jlink", str(index))
            self.store.record(ProbeSelection.from_row(f"probe:{index}", row))

        for index in range(overflow):
            self.assertIsNone(
                self.store.recorded(f"probe:{index}"), f"probe:{index} should have been evicted"
            )
        for index in range(overflow, MAX_PROBE_SELECTIONS + overflow):
            self.assertIsNotNone(
                self.store.recorded(f"probe:{index}"), f"probe:{index} should have survived"
            )

    def test_re_recording_refreshes_position_instead_of_duplicating(self) -> None:
        self.store.record(ProbeSelection.from_row("probe:same", _probe_row("jlink", "same")))
        for index in range(MAX_PROBE_SELECTIONS - 1):
            self.store.record(
                ProbeSelection.from_row(f"probe:{index}", _probe_row("jlink", str(index)))
            )
        # The store is exactly at its cap. "probe:same" is the oldest entry; refresh it.
        self.store.record(ProbeSelection.from_row("probe:same", _probe_row("jlink", "same-2")))
        # One more insert would evict the oldest entry -- "probe:same" must not be it,
        # since re-recording just moved it to the back of the eviction order.
        self.store.record(ProbeSelection.from_row("probe:new", _probe_row("jlink", "new")))

        self.assertEqual(self.store.count(), MAX_PROBE_SELECTIONS)
        recorded = self.store.recorded("probe:same")
        self.assertIsNotNone(recorded, "re-recording must not make an entry evictable early")
        assert recorded is not None
        self.assertEqual(recorded.unique_id, "same-2", "the refreshed value must be kept")
        self.assertIsNone(self.store.recorded("probe:0"), "the actual oldest entry should evict")

    def test_a_stable_selection_still_resolves_normally_once_bounded(self) -> None:
        row = _probe_row("jlink", "683710208")
        token = probe_connection_id("jlink", "683710208")
        self.store.record(ProbeSelection.from_row(token, row))
        for index in range(10):
            self.store.record(
                ProbeSelection.from_row(f"probe:{index}", _probe_row("jlink", str(index)))
            )

        selection = self.store.resolve(token, _snapshot(row))

        self.assertEqual(selection.unique_id, "683710208")
        self.assertTrue(selection.durable)

    def test_a_repeatedly_resolved_entry_survives_eviction_pressure(self) -> None:
        """FIX 9 (C9): a successful resolve() must touch recency like record() does.

        Without this, eviction is pure insertion order and a long-lived, actively-used
        connection is no better protected than an entry nobody has touched since it was
        created -- exactly what makes an eviction-driven cross-provider misresolution
        (C8) realistic. Interleaves resolve() calls with unrelated churn far exceeding
        the cap; the kept entry must survive, and (to prove the churn was real, not a
        vacuous test) an entry that was never re-touched must actually be evicted.
        """

        kept_row = _probe_row("jlink", "kept-uid")
        kept_token = "probe:kept"
        self.store.record(ProbeSelection.from_row(kept_token, kept_row))
        snapshot = _snapshot(kept_row)

        untouched_row = _probe_row("jlink", "untouched-uid")
        untouched_token = "probe:untouched-first"
        self.store.record(ProbeSelection.from_row(untouched_token, untouched_row))

        batch_size = 50
        for batch in range(10):
            for index in range(batch_size):
                self.store.record(
                    ProbeSelection.from_row(
                        f"probe:churn-{batch}-{index}",
                        _probe_row("jlink", f"churn-{batch}-{index}"),
                    )
                )
            # Simulates the common case -- connect / board_validate / status checks for
            # an already-set-up board -- resolving the same entry again and again.
            self.store.resolve(kept_token, snapshot)

        # Total unrelated churn (500) is comfortably more than the cap (256), so the
        # store definitely evicted *something* -- proving the pressure was real.
        self.assertIsNone(
            self.store.recorded(untouched_token),
            "the churn was not actually enough to evict anything, making this test vacuous",
        )
        self.assertIsNotNone(
            self.store.recorded(kept_token),
            "an actively-resolved entry was evicted despite being resolved every batch",
        )


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

    def test_a_legacy_uid_containing_a_colon_still_resolves_to_its_own_probe(self) -> None:
        """C12/D11 reproduction 1: a colon in the UID must not read as gone.

        A legacy (pre-FIX-8) token never percent-encodes its UID. A hook-reported
        `unique_id` has no charset restriction beyond NUL-freedom and length (the
        guide's own platform guidance points hook authors at USB topology values like
        Linux's `3-1.4:1.0`, which contains a colon). Before the structural prefix fix,
        `parse_probe_connection_id` mistook the colon for the canonical delimiter,
        split the UID's own text into a fabricated provider, found no matching row for
        that fabricated provider, and returned None -- reporting a probe that was
        genuinely still attached as gone.
        """

        uid_with_colon = "3-1.4:1.0"
        row = _probe_row("cmsisdap", uid_with_colon)
        legacy_token = f"probe:{uid_with_colon}"

        selection = derive_selection_from_token(legacy_token, _snapshot(row))

        self.assertIsNotNone(selection, "a present probe was reported as gone")
        assert selection is not None
        self.assertEqual(selection.unique_id, uid_with_colon)
        self.assertEqual(selection.provider, "cmsisdap")

    def test_a_legacy_uid_containing_a_colon_never_resolves_to_a_different_probe(
        self,
    ) -> None:
        """C12/D11 reproduction 2 (the severe one): must never select the WRONG hardware.

        The legacy token's UID text (`"jlink:001"`) happens to look like
        `provider="jlink", uid="001"` under the old colon-counting heuristic. A second,
        unrelated, real jlink probe with unique_id `"001"` is present in the same
        snapshot. Before the structural prefix fix, `parse_probe_connection_id` would
        confidently split `"probe:jlink:001"` into a fabricated `provider="jlink"`,
        `uid="001"` and resolve this token to that unrelated jlink probe instead of the
        cmsisdap probe it actually names -- a silent selection of different, real
        hardware, which is exactly the class of defect FIX 8 exists to prevent, now
        reachable through the compatibility path FIX 8 itself added.

        With the structural prefix fix, `"probe:jlink:001"` does not start with the
        canonical `probeid:` prefix at all, so `parse_probe_connection_id` never even
        attempts a split; the legacy path strips only the literal `probe:` prefix,
        leaving the whole `"jlink:001"` as one UID candidate -- which matches exactly
        one row (the intended one) and no others, so the resolution is unambiguous
        AND correct, not merely "not wrong."
        """

        intended_uid = "jlink:001"
        intended_row = _probe_row("cmsisdap", intended_uid)
        unrelated_row = _probe_row("jlink", "001")
        snapshot = _snapshot(intended_row, unrelated_row)
        legacy_token = f"probe:{intended_uid}"

        selection = derive_selection_from_token(legacy_token, snapshot)

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(
            selection.provider,
            "cmsisdap",
            "resolved to a different, real probe instead of the one this token names",
        )
        self.assertEqual(selection.unique_id, intended_uid)


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


class ResolvedProbeUidDegradationTests(unittest.TestCase):
    """FIX 3c (C3): an unexpected inventory-scan exception must become a typed failure.

    `_resolved_probe_uid_for_connection` is one of the two new `.snapshot()` call
    sites the TOCTOU fix (K1) closes at its root by making `_active_connection_rows`
    skip a vanished board instead of raising. This guards the boundary itself: whatever
    `_hardware_inventory.snapshot()` raises -- that race or anything else -- must
    surface as a typed `TargetControlError`, matching how `_setup_overview` and
    `_get_setup_status` already degrade to a diagnostic rather than propagating raw.
    """

    def test_a_snapshot_exception_becomes_a_typed_target_control_error(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        from pyocd_debug_mcp import server
        from pyocd_debug_mcp.target_errors import TargetControlError

        def broken_snapshot() -> InventorySnapshot:
            raise RuntimeError("boom: simulated inventory failure")

        with patch.object(
            server, "_hardware_inventory", SimpleNamespace(snapshot=broken_snapshot)
        ):
            with self.assertRaises(TargetControlError) as caught:
                server._resolved_probe_uid_for_connection("probe:683710208")

        self.assertIn("boom", str(caught.exception))

    def test_a_disappeared_selection_surfaces_the_structured_payload(self) -> None:
        """D26: `SelectionDisappeared` must reach the caller as the typed
        `discovery/selection-disappeared` payload, not a bare, code-free message.

        Drives the real path: `_hardware_inventory.snapshot()` returns a snapshot
        with no matching row, so `ProbeSelectionStore.resolve()` raises
        `SelectionNotRecorded` (a `SelectionDisappeared`), and
        `_resolved_probe_uid_for_connection`'s own `except SelectionDisappeared`
        handler is what must convert it -- not the payload constructor called
        directly, which would prove the shape but not that this site uses it.
        """

        from types import SimpleNamespace
        from unittest.mock import patch

        from pyocd_debug_mcp import server
        from pyocd_debug_mcp.target_errors import TargetControlError

        empty = _snapshot()

        with patch.object(server, "_hardware_inventory", SimpleNamespace(snapshot=lambda: empty)):
            with self.assertRaises(TargetControlError) as caught:
                server._resolved_probe_uid_for_connection("probe:no-such-uid-anywhere")

        message = str(caught.exception)
        self.assertIn("discovery/selection-disappeared", message)
        self.assertIn("rerun setup_overview and reselect the connection", message)
        self.assertIn("no longer present", message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
