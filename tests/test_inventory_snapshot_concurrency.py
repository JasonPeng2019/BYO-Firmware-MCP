"""One operation never mixes rows from two different scans.

The fixtures here increment a counter file on every invocation, so each scan produces
*different* rows. That is what makes interleaving detectable: with constant fixtures a
snapshot built from two half-scans would look identical to a correct one.

Both shapes are exercised deliberately:

* two operations driven through one shared `HardwareInventoryService` with real threads,
  which catches a snapshot assembled from interleaved partial state; and
* repeated sequential snapshots, which catches a snapshot swapped whole between calls.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, cast

from pyocd_debug_mcp.discovery_hooks import (
    DiscoveryHookSnapshot,
    execute_eligible_hooks,
    load_hook_snapshot,
)
from pyocd_debug_mcp.hardware_inventory import (
    HardwareInventoryService,
    InventorySnapshot,
    snapshot_id_of,
)
from pyocd_debug_mcp.probe_inventory import EMPTY_NATIVE_PROBE_LISTING
from tests.discovery_hook_fixtures import FAKE_HOOK, hook_entry, write_manifest


class SnapshotAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        shutil.copy(FAKE_HOOK, self.root / "hook.py")
        self.probe_counter = self.root / "probe.count"
        self.uart_counter = self.root / "uart.count"
        write_manifest(
            self.root,
            [
                hook_entry(
                    "probe-hook", "probe", argv=["counter_probe", str(self.probe_counter)]
                ),
                hook_entry(
                    "uart-hook", "uart", argv=["counter_uart", str(self.uart_counter)]
                ),
            ],
        )
        self.hooks = load_hook_snapshot(self.root, environ={})
        # Both native kinds empty, so both hooks run and every row comes from a hook.
        self.service = HardwareInventoryService(
            native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
            native_uarts=lambda: cast("Any", []),
            hook_snapshot=lambda: self.hooks,
            run_hooks=lambda snapshot, kind: execute_eligible_hooks(snapshot, kind),
        )

    @staticmethod
    def _scan_numbers(snapshot: InventorySnapshot) -> tuple[set[str], set[str]]:
        probes = {row.unique_id or "" for row in snapshot.probes}
        uarts = {row.usb_serial or "" for row in snapshot.uarts}
        return probes, uarts

    def test_every_row_in_one_snapshot_carries_the_same_snapshot_id(self) -> None:
        snapshot = self.service.snapshot()

        self.assertTrue(snapshot.probes)
        self.assertTrue(snapshot.uarts)
        for row in snapshot.probes:
            self.assertEqual(row.snapshot_id, snapshot.snapshot_id)
            self.assertEqual(snapshot_id_of(row.row_id), snapshot.snapshot_id)
        for row in snapshot.uarts:
            self.assertEqual(row.snapshot_id, snapshot.snapshot_id)
            self.assertEqual(snapshot_id_of(row.row_id), snapshot.snapshot_id)

    def test_the_fixtures_really_do_change_on_every_scan(self) -> None:
        """Without this, every other assertion in the file is vacuous."""

        first = self.service.snapshot()
        second = self.service.snapshot()

        self.assertNotEqual(
            self._scan_numbers(first),
            self._scan_numbers(second),
            "the counter fixtures returned identical rows; interleaving would be invisible",
        )

    def test_sequential_snapshots_never_share_rows(self) -> None:
        seen_probes: list[set[str]] = []
        seen_uarts: list[set[str]] = []

        for _repeat in range(8):
            probes, uarts = self._scan_numbers(self.service.snapshot())
            seen_probes.append(probes)
            seen_uarts.append(uarts)

        flat_probes = [value for group in seen_probes for value in group]
        flat_uarts = [value for group in seen_uarts for value in group]
        self.assertEqual(len(flat_probes), len(set(flat_probes)))
        self.assertEqual(len(flat_uarts), len(set(flat_uarts)))

    def test_concurrent_snapshots_are_internally_consistent(self) -> None:
        """Real threads through one shared service instance."""

        results: list[InventorySnapshot] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        start = threading.Barrier(6)

        def worker() -> None:
            try:
                start.wait(timeout=30)
                snapshot = self.service.snapshot()
                with lock:
                    results.append(snapshot)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        for snapshot in results:
            with self.subTest(snapshot=snapshot.snapshot_id):
                # Internal consistency: one id across probes and UARTs alike.
                ids = {row.snapshot_id for row in snapshot.probes}
                ids |= {row.snapshot_id for row in snapshot.uarts}
                self.assertEqual(ids, {snapshot.snapshot_id})
                self.assertEqual(len(snapshot.probes), 1)
                self.assertEqual(len(snapshot.uarts), 1)

    def test_concurrent_snapshots_do_not_share_scan_results(self) -> None:
        """No two operations may see the same scan, nor half of another's."""

        results: list[InventorySnapshot] = []
        lock = threading.Lock()
        start = threading.Barrier(6)

        def worker() -> None:
            start.wait(timeout=30)
            snapshot = self.service.snapshot()
            with lock:
                results.append(snapshot)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        probe_values = [row.unique_id for snapshot in results for row in snapshot.probes]
        uart_values = [row.usb_serial for snapshot in results for row in snapshot.uarts]
        self.assertEqual(
            len(probe_values), len(set(probe_values)), "two operations shared a probe scan"
        )
        self.assertEqual(
            len(uart_values), len(set(uart_values)), "two operations shared a UART scan"
        )
        self.assertEqual(len({snapshot.snapshot_id for snapshot in results}), len(results))

    def test_snapshot_ids_are_unique_across_concurrent_operations(self) -> None:
        ids: list[str] = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def worker() -> None:
            start.wait(timeout=30)
            snapshot = self.service.snapshot()
            with lock:
                ids.append(snapshot.snapshot_id)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        self.assertEqual(len(ids), 8)
        self.assertEqual(len(set(ids)), 8)

    def test_row_ids_are_unique_across_concurrent_operations(self) -> None:
        collected: list[str] = []
        lock = threading.Lock()
        start = threading.Barrier(6)

        def worker() -> None:
            start.wait(timeout=30)
            snapshot = self.service.snapshot()
            with lock:
                collected.extend(row.row_id for row in snapshot.probes)
                collected.extend(row.row_id for row in snapshot.uarts)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        self.assertEqual(len(collected), len(set(collected)))


class TwoBoardOverviewConcurrencyTests(unittest.TestCase):
    """Two boards routing setup_overview at once must not cross-contaminate."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        shutil.copy(FAKE_HOOK, self.root / "hook.py")

    def _service(self, tag: str) -> HardwareInventoryService:
        counter = self.root / f"{tag}.count"
        directory = self.root / tag
        directory.mkdir()
        shutil.copy(FAKE_HOOK, directory / "hook.py")
        write_manifest(
            directory,
            [
                hook_entry("probe-hook", "probe", argv=["counter_probe", str(counter)]),
                hook_entry(
                    "uart-hook", "uart", argv=["counter_uart", str(counter) + ".uart"]
                ),
            ],
        )
        hooks: DiscoveryHookSnapshot = load_hook_snapshot(directory, environ={})
        return HardwareInventoryService(
            native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
            native_uarts=lambda: cast("Any", []),
            hook_snapshot=lambda: hooks,
            run_hooks=lambda snapshot, kind: execute_eligible_hooks(snapshot, kind),
        )

    def test_two_boards_with_different_fixtures_keep_their_own_rows(self) -> None:
        services = {"alpha": self._service("alpha"), "beta": self._service("beta")}
        results: dict[str, InventorySnapshot] = {}
        lock = threading.Lock()
        start = threading.Barrier(2)

        def worker(tag: str) -> None:
            start.wait(timeout=30)
            snapshot = services[tag].snapshot()
            with lock:
                results[tag] = snapshot

        threads = [threading.Thread(target=worker, args=(tag,)) for tag in services]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        self.assertEqual(set(results), {"alpha", "beta"})
        for tag, snapshot in results.items():
            with self.subTest(board=tag):
                ids = {row.snapshot_id for row in snapshot.probes}
                ids |= {row.snapshot_id for row in snapshot.uarts}
                self.assertEqual(ids, {snapshot.snapshot_id})
        self.assertNotEqual(
            results["alpha"].snapshot_id, results["beta"].snapshot_id
        )


class RowIdHelperTests(unittest.TestCase):
    def test_snapshot_id_is_recoverable_from_a_row_id(self) -> None:
        self.assertEqual(snapshot_id_of("abcDEF123-007"), "abcDEF123")

    def test_a_snapshot_id_containing_a_dash_still_round_trips(self) -> None:
        self.assertEqual(snapshot_id_of("ab-cd-ef-012"), "ab-cd-ef")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
