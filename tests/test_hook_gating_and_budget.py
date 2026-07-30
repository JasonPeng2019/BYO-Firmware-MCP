"""Guards the two hot-path defects: when hooks run, and whether their time is reserved.

The gating assertions check *zero subprocess launches*, not merely empty results. An
implementation that ran every hook and discarded the output would satisfy a
results-only assertion while reintroducing exactly the latency this rule prevents.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence, cast
from unittest.mock import patch

from pyocd_debug_mcp import discovery_hooks, server
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.discovery_hooks import (
    DiscoveryHookSnapshot,
    HookExecution,
    HookSnapshotStore,
    execute_eligible_hooks,
)
from pyocd_debug_mcp.hardware_inventory import (
    ActiveConnectionRow,
    HardwareInventoryService,
)
from pyocd_debug_mcp.kernel import operations as ops
from pyocd_debug_mcp.probe_inventory import (
    EMPTY_NATIVE_PROBE_LISTING,
    NativeProbeListing,
    ProbeInfo,
)
from tests.discovery_hook_fixtures import hook_entry, snapshot_for


def _listing(*uids: str) -> NativeProbeListing:
    if not uids:
        return EMPTY_NATIVE_PROBE_LISTING
    return NativeProbeListing(
        probes=tuple(
            ProbeInfo(uid=uid, description=f"Probe {uid}", raw=uid, family="cmsisdap")
            for uid in uids
        ),
        command=("pyocd", "list", "--probes"),
        exit_code=0,
        timed_out=False,
        stdout_summary="",
        stderr_summary="",
    )


def _port(device: str, serial: str = "SER1") -> SimpleNamespace:
    return SimpleNamespace(
        device=device,
        description=f"UART {device}",
        product="",
        serial_number=serial,
        manufacturer="",
        interface="",
        hwid="",
        location="",
        vid=0x1234,
        pid=0x5678,
    )


class GatingTests(unittest.TestCase):
    """Hooks execute per kind, only when that kind's native result is empty."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.launched: list[str] = []

    def _service(
        self,
        native_uids: Sequence[str],
        ports: Sequence[SimpleNamespace],
        *,
        active: Sequence[ActiveConnectionRow] = (),
        hooks: DiscoveryHookSnapshot | None = None,
    ) -> HardwareInventoryService:
        snapshot = hooks if hooks is not None else self._both_kinds_snapshot()

        def run_hooks(
            hook_snapshot: DiscoveryHookSnapshot, kind: str
        ) -> tuple[HookExecution, ...]:
            # Records the launch, then genuinely executes, so this counts real
            # subprocess starts rather than intentions.
            self.launched.append(kind)
            return execute_eligible_hooks(hook_snapshot, kind, platform="linux")

        return HardwareInventoryService(
            native_probes=lambda: _listing(*native_uids),
            native_uarts=lambda: cast("Any", list(ports)),
            active_connections=lambda: tuple(active),
            hook_snapshot=lambda: snapshot,
            run_hooks=run_hooks,
        )

    def _both_kinds_snapshot(self) -> DiscoveryHookSnapshot:
        return snapshot_for(
            self.root,
            [
                hook_entry("probe-hook", "probe", argv=["probe"]),
                hook_entry("uart-hook", "uart", argv=["uart"]),
            ],
        )

    def test_both_native_kinds_present_executes_neither_hook(self) -> None:
        service = self._service(["U1"], [_port("COM3")])

        snapshot = service.snapshot()

        self.assertEqual(self.launched, [], "a hook ran while native discovery worked")
        self.assertEqual(snapshot.hook_diagnostics, ())
        self.assertEqual([row.probe_id for row in snapshot.probes], ["U1"])
        self.assertEqual([row.port_path for row in snapshot.uarts], ["COM3"])

    def test_native_probe_present_and_native_uart_empty_runs_only_the_uart_hook(self) -> None:
        """The mixed case. A single combined flag would pass case 1 and fail this."""

        service = self._service(["U1"], [])

        snapshot = service.snapshot()

        self.assertEqual(self.launched, ["uart"])
        self.assertEqual([row.probe_id for row in snapshot.probes], ["U1"])
        self.assertTrue(snapshot.probes[0].native)
        self.assertEqual([row.port_path for row in snapshot.uarts], ["COM7"])
        self.assertTrue(snapshot.uarts[0].from_hook)

    def test_native_uart_present_and_native_probe_empty_runs_only_the_probe_hook(self) -> None:
        service = self._service([], [_port("COM3")])

        snapshot = service.snapshot()

        self.assertEqual(self.launched, ["probe"])
        self.assertTrue(snapshot.probes[0].from_hook)
        self.assertTrue(snapshot.uarts[0].native)

    def test_both_native_kinds_empty_runs_both_hooks(self) -> None:
        service = self._service([], [])

        snapshot = service.snapshot()

        self.assertEqual(sorted(self.launched), ["probe", "uart"])
        self.assertEqual(len(snapshot.probes), 1)
        self.assertEqual(len(snapshot.uarts), 1)
        self.assertEqual(len(snapshot.hook_diagnostics), 2)

    def test_no_manifest_executes_nothing_and_reports_nothing(self) -> None:
        """Invariant 2: with no manifest, behave exactly as before."""

        service = self._service([], [], hooks=discovery_hooks.EMPTY_SNAPSHOT)

        snapshot = service.snapshot()

        self.assertEqual(self.launched, [])
        self.assertEqual(snapshot.probes, ())
        self.assertEqual(snapshot.uarts, ())
        self.assertEqual(snapshot.hook_diagnostics, ())

    def test_a_probe_only_manifest_never_runs_for_an_empty_uart_inventory(self) -> None:
        hooks = snapshot_for(self.root, [hook_entry("probe-hook", "probe", argv=["probe"])])
        service = self._service(["U1"], [], hooks=hooks)

        snapshot = service.snapshot()

        self.assertEqual(self.launched, [], "a probe hook ran for a missing UART")
        self.assertEqual(snapshot.uarts, ())

    def test_an_already_open_probe_counts_as_native_and_suppresses_probe_hooks(self) -> None:
        """pyOCD omits probes this process holds open; they are still native truth."""

        active = (ActiveConnectionRow("session:tok", None, "Live board", "jlink"),)
        service = self._service([], [_port("COM3")], active=active)

        snapshot = service.snapshot()

        self.assertEqual(self.launched, [])
        self.assertEqual([row.probe_id for row in snapshot.probes], ["session:tok"])
        self.assertEqual(snapshot.probes[0].identity_scope, "session")
        self.assertIsNone(snapshot.probes[0].unique_id)

    def test_gating_is_re_evaluated_fresh_on_every_snapshot(self) -> None:
        ports: list[SimpleNamespace] = [_port("COM3")]
        hooks = self._both_kinds_snapshot()

        def run_hooks(
            hook_snapshot: DiscoveryHookSnapshot, kind: str
        ) -> tuple[HookExecution, ...]:
            self.launched.append(kind)
            return execute_eligible_hooks(hook_snapshot, kind, platform="linux")

        service = HardwareInventoryService(
            native_probes=lambda: _listing("U1"),
            native_uarts=lambda: cast("Any", list(ports)),
            hook_snapshot=lambda: hooks,
            run_hooks=run_hooks,
        )

        service.snapshot()
        self.assertEqual(self.launched, [])

        ports.clear()  # the UART is unplugged between operations
        service.snapshot()
        self.assertEqual(self.launched, ["uart"])

    def test_pyserial_absent_is_distinguished_from_no_ports(self) -> None:
        service = HardwareInventoryService(
            native_probes=lambda: _listing("U1"),
            native_uarts=lambda: None,
            hook_snapshot=lambda: discovery_hooks.EMPTY_SNAPSHOT,
        )

        snapshot = service.snapshot()

        self.assertFalse(snapshot.native_uart_available)
        self.assertEqual(snapshot.uarts, ())

    def test_native_probe_diagnostics_survive_onto_the_snapshot(self) -> None:
        listing = NativeProbeListing(
            probes=(),
            command=("pyocd", "list", "--probes"),
            exit_code=127,
            timed_out=False,
            stdout_summary="",
            stderr_summary="command not found: pyocd",
        )
        service = HardwareInventoryService(
            native_probes=lambda: listing,
            native_uarts=lambda: [],
            hook_snapshot=lambda: discovery_hooks.EMPTY_SNAPSHOT,
        )

        snapshot = service.snapshot()

        self.assertEqual(snapshot.native_probe_diagnostics.exit_code, 127)
        self.assertFalse(snapshot.native_probe_diagnostics.available)
        self.assertIn("not found", snapshot.native_probe_diagnostics.stderr_summary)


class UartHotPathNeverListsProbesTests(unittest.TestCase):
    """FIX 1 regression (C1/D1): the UART hot path must never spawn `native_probes()`.

    `_resolve_serial_port_for_session` runs before every UART action and in the
    `on_exit` finalizer. Before this fix it called `_hardware_inventory.snapshot()`,
    which unconditionally runs `native_probes()` -- a real `pyocd list --probes`
    subprocess -- even with zero hooks configured. `uart_snapshot()` must never do
    that. These patch `server._hardware_inventory` with a real service (not just
    `server._validation_inventory`), per the house rule about the real debug hardware
    attached to this machine: a test that patched only the legacy shape would still
    take a live snapshot through the unpatched service.
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)

    @staticmethod
    def _handle(board_id: str) -> TargetSessionHandle:
        board = BoardConfig(
            board_id=board_id,
            display_name="Test board",
            mcu_family="nrf52",
            probe_family="jlink",
            pyocd_target="nrf52840",
            probe_type="jlink",
            probe_hint_terms=(),
            serial_hint_terms=(),
            test_addr=0x20000000,
        )
        return TargetSessionHandle(
            session=None, board=board, probe_uid=None, route_used="test", target_override=None
        )

    def test_no_manifest_at_all_lists_zero_probes(self) -> None:
        probe_calls: list[str] = []

        def native_probes() -> NativeProbeListing:
            probe_calls.append("listed")
            return EMPTY_NATIVE_PROBE_LISTING

        service = HardwareInventoryService(
            native_probes=native_probes,
            native_uarts=lambda: [],
            hook_snapshot=lambda: discovery_hooks.EMPTY_SNAPSHOT,
        )
        handle = self._handle("uart-hot-path-no-manifest")

        with patch.object(server, "_hardware_inventory", service):
            with self.assertRaises(RuntimeError):
                server._resolve_serial_port_for_session(handle, override=None)

        self.assertEqual(probe_calls, [], "the UART hot path listed probes with no manifest")

    def test_a_manifest_with_hooks_still_lists_zero_probes(self) -> None:
        probe_calls: list[str] = []
        launched: list[str] = []

        def native_probes() -> NativeProbeListing:
            probe_calls.append("listed")
            return EMPTY_NATIVE_PROBE_LISTING

        def run_hooks(
            hook_snapshot: DiscoveryHookSnapshot, kind: str
        ) -> tuple[HookExecution, ...]:
            launched.append(kind)
            return execute_eligible_hooks(hook_snapshot, kind, platform="linux")

        hooks = snapshot_for(
            self.root,
            [
                hook_entry("probe-hook", "probe", argv=["probe"]),
                hook_entry("uart-hook", "uart", argv=["uart"]),
            ],
        )
        service = HardwareInventoryService(
            native_probes=native_probes,
            native_uarts=lambda: [],
            hook_snapshot=lambda: hooks,
            run_hooks=run_hooks,
        )
        handle = self._handle("uart-hot-path-with-manifest")

        with patch.object(server, "_hardware_inventory", service):
            try:
                server._resolve_serial_port_for_session(handle, override=None)
            except RuntimeError:
                # Whether the fallback scorer resolved a single port is not this
                # test's concern; only the probe-listing call graph is.
                pass

        self.assertEqual(probe_calls, [], "the UART hot path listed probes with hooks loaded")
        # A real uart hook did run (proving this exercised the hook path genuinely,
        # not a manifest that happened to be empty), and the probe hook -- eligible
        # per the manifest, but never reachable from the UART-only snapshot -- did not.
        self.assertEqual(launched, ["uart"])

    def test_an_unexpected_snapshot_exception_becomes_a_typed_runtime_error(self) -> None:
        """FIX 3c (C3): `uart_snapshot()` raising must not escape this hot path raw.

        `_resolve_serial_port_for_session` is one of the two new `.snapshot()`-family
        call sites the TOCTOU fix (K1) closes at its root. This guards the boundary
        itself: whatever `uart_snapshot()` raises must surface as a typed
        `RuntimeError`, matching how `_setup_overview` and `_get_setup_status` already
        degrade to a diagnostic rather than propagating.
        """

        def broken_uart_snapshot() -> None:
            raise ValueError("boom: simulated inventory failure")

        service = SimpleNamespace(uart_snapshot=broken_uart_snapshot)
        handle = self._handle("uart-hot-path-broken-snapshot")

        with patch.object(server, "_hardware_inventory", service):
            with self.assertRaises(RuntimeError) as caught:
                server._resolve_serial_port_for_session(handle, override=None)

        self.assertIn("boom", str(caught.exception))


class BudgetTests(unittest.TestCase):
    """Every tool that can execute a hook must reserve time for it."""

    def setUp(self) -> None:
        ops.reset_eligible_hook_count_provider()
        self.addCleanup(ops.reset_eligible_hook_count_provider)

    @staticmethod
    def _counts(probe: int = 0, uart: int = 0):
        return lambda: {"probe": probe, "uart": uart}

    ONE_HOOK = discovery_hooks.MAX_HOOK_TIMEOUT_SECONDS + 1.5

    UART_CALLS: tuple[tuple[str, Mapping[str, object]], ...] = (
        ("read_serial", {"read_seconds": 3}),
        ("write_serial", {"timeout_seconds": 2}),
        ("serial_exchange", {"read_seconds": 2, "steps": [{"write": "a"}]}),
    )

    def test_provider_defaults_to_zero_counts_before_any_refresh(self) -> None:
        """The resolver must be safe to call during startup."""

        self.assertEqual(dict(ops._eligible_hook_counts()), {"probe": 0, "uart": 0})
        self.assertEqual(ops._hook_budget("probe", "uart"), 0.0)

    def test_uart_action_budgets_equal_todays_value_with_no_hooks(self) -> None:
        for tool, arguments in self.UART_CALLS:
            with self.subTest(tool=tool):
                self.assertEqual(
                    ops.operation_timeout_seconds(tool, arguments),
                    ops.DEFAULT_OPERATION_TIMEOUT_SECONDS,
                )

    def test_every_uart_action_gains_budget_once_a_uart_hook_is_loaded(self) -> None:
        before = {
            tool: ops.operation_timeout_seconds(tool, arguments)
            for tool, arguments in self.UART_CALLS
        }
        ops.set_eligible_hook_count_provider(self._counts(uart=1))

        for tool, arguments in self.UART_CALLS:
            with self.subTest(tool=tool):
                after = ops.operation_timeout_seconds(tool, arguments)
                self.assertAlmostEqual(after - before[tool], self.ONE_HOOK, places=6)
                self.assertGreater(
                    after,
                    discovery_hooks.MAX_HOOK_TIMEOUT_SECONDS,
                    "one hook could exceed the whole operation budget",
                )

    def test_read_serial_with_a_uart_finalizer_reserves_both_hook_budgets(self) -> None:
        """The action and the on_exit finalizer can each execute a hook."""

        arguments = {
            "read_seconds": 3,
            "on_exit": {"action": "uart_write", "data": "q", "timeout_seconds": 2},
        }
        before = ops.operation_timeout_seconds("read_serial", arguments)
        ops.set_eligible_hook_count_provider(self._counts(uart=1))

        after = ops.operation_timeout_seconds("read_serial", arguments)

        self.assertAlmostEqual(after - before, 2 * self.ONE_HOOK, places=6)

    def test_a_uart_finalizer_on_a_non_serial_tool_still_reserves_budget(self) -> None:
        arguments = {
            "on_exit": {"action": "uart_write", "data": "q", "timeout_seconds": 2},
        }
        before = ops.operation_timeout_seconds("reset_and_run", arguments)
        ops.set_eligible_hook_count_provider(self._counts(uart=1))

        after = ops.operation_timeout_seconds("reset_and_run", arguments)

        self.assertAlmostEqual(after - before, self.ONE_HOOK, places=6)

    def test_uart_actions_do_not_reserve_probe_hook_budget(self) -> None:
        before = ops.operation_timeout_seconds("read_serial", {"read_seconds": 3})
        ops.set_eligible_hook_count_provider(self._counts(probe=3))

        after = ops.operation_timeout_seconds("read_serial", {"read_seconds": 3})

        self.assertEqual(after, before, "a probe hook cannot run during a serial read")

    def test_probe_inventory_tools_reserve_both_kinds(self) -> None:
        before = ops.operation_timeout_seconds("setup_overview", {})
        ops.set_eligible_hook_count_provider(self._counts(probe=1, uart=1))

        after = ops.operation_timeout_seconds("setup_overview", {})

        self.assertAlmostEqual(after - before, 2 * self.ONE_HOOK, places=6)

    def test_refresh_discovery_hooks_reserves_both_kinds(self) -> None:
        before = ops.operation_timeout_seconds("refresh_discovery_hooks", {})
        ops.set_eligible_hook_count_provider(self._counts(probe=2, uart=1))

        after = ops.operation_timeout_seconds("refresh_discovery_hooks", {})

        self.assertGreater(after, before)
        self.assertGreaterEqual(after, 3 * self.ONE_HOOK)

    def test_get_discovery_hook_contract_keeps_the_default_budget(self) -> None:
        """It executes nothing, so it must not inflate its deadline."""

        before = ops.operation_timeout_seconds("get_discovery_hook_contract", {"kind": "probe"})
        ops.set_eligible_hook_count_provider(self._counts(probe=5, uart=5))

        after = ops.operation_timeout_seconds("get_discovery_hook_contract", {"kind": "probe"})

        self.assertEqual(after, before)
        self.assertEqual(after, ops.DEFAULT_OPERATION_TIMEOUT_SECONDS)

    def test_budget_scales_with_the_number_of_eligible_hooks(self) -> None:
        base = ops.operation_timeout_seconds("read_serial", {"read_seconds": 3})
        seen = []
        for count in (1, 2, 4):
            ops.set_eligible_hook_count_provider(self._counts(uart=count))
            seen.append(ops.operation_timeout_seconds("read_serial", {"read_seconds": 3}) - base)

        self.assertAlmostEqual(seen[0], self.ONE_HOOK, places=6)
        self.assertAlmostEqual(seen[1], 2 * self.ONE_HOOK, places=6)
        self.assertAlmostEqual(seen[2], 4 * self.ONE_HOOK, places=6)

    def test_a_failing_provider_never_breaks_a_deadline(self) -> None:
        def broken() -> Mapping[str, int]:
            raise RuntimeError("hook store unavailable")

        ops.set_eligible_hook_count_provider(broken)

        self.assertEqual(
            ops.operation_timeout_seconds("read_serial", {"read_seconds": 3}),
            ops.DEFAULT_OPERATION_TIMEOUT_SECONDS,
        )

    def test_malformed_provider_counts_are_ignored_not_fatal(self) -> None:
        ops.set_eligible_hook_count_provider(lambda: {"uart": "many"})  # type: ignore[dict-item]

        self.assertEqual(
            ops.operation_timeout_seconds("read_serial", {"read_seconds": 3}),
            ops.DEFAULT_OPERATION_TIMEOUT_SECONDS,
        )

    def test_the_snapshot_store_is_a_valid_provider_and_starts_at_zero(self) -> None:
        store = HookSnapshotStore()
        ops.set_eligible_hook_count_provider(store.eligible_counts)

        self.assertEqual(
            ops.operation_timeout_seconds("read_serial", {"read_seconds": 3}),
            ops.DEFAULT_OPERATION_TIMEOUT_SECONDS,
        )

    def test_loading_a_manifest_raises_the_budget_through_the_store(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        store = HookSnapshotStore()
        ops.set_eligible_hook_count_provider(store.eligible_counts)
        before = ops.operation_timeout_seconds("read_serial", {"read_seconds": 3})

        store.replace(snapshot_for(root, [hook_entry("uart-hook", "uart", argv=["uart"])]))
        after = ops.operation_timeout_seconds("read_serial", {"read_seconds": 3})

        self.assertAlmostEqual(after - before, self.ONE_HOOK, places=6)
        store.clear()
        self.assertEqual(
            ops.operation_timeout_seconds("read_serial", {"read_seconds": 3}), before
        )

    def test_uart_action_tools_are_not_in_the_probe_inventory_set(self) -> None:
        """The whole point of the new group: these were never covered."""

        self.assertTrue(ops._UART_ACTION_TOOLS.isdisjoint(ops._PROBE_INVENTORY_TOOLS))
        self.assertEqual(
            ops._UART_ACTION_TOOLS, {"read_serial", "write_serial", "serial_exchange"}
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
