"""End-to-end simulations: contract -> write hook -> refresh -> rerun -> select -> use.

Real manifests on disk and real child processes throughout. The point is to exercise the
whole loop an agent actually walks, including the failure branches, rather than each
piece in isolation.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence, cast

from pyocd_debug_mcp.discovery_failures import CONTRACT_TOOL, REFRESH_TOOL
from pyocd_debug_mcp.discovery_hooks import (
    DiscoveryHookSnapshot,
    EMPTY_SNAPSHOT,
    HookExecution,
    HookSnapshotStore,
    execute_eligible_hooks,
    load_hook_snapshot,
)
from pyocd_debug_mcp.hardware_inventory import (
    HardwareInventoryService,
    InventorySnapshot,
    VendorUartRow,
    ProbeSelection,
    ProbeSelectionStore,
    SessionUartSelectionStore,
)
from pyocd_debug_mcp.probe_inventory import (
    EMPTY_NATIVE_PROBE_LISTING,
    NativeProbeListing,
    registered_provider_ids,
)
from pyocd_debug_mcp.services.connections import probe_connection_id
from pyocd_debug_mcp.tools.discovery import (
    DiscoveryRetryStore,
    DiscoveryToolServices,
    build_discovery_handlers,
)
from tests.discovery_hook_fixtures import FAKE_HOOK, hook_entry, write_manifest


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


class _Workflow:
    """A whole server-side discovery world: hook root, stores, tools, inventory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        shutil.copy(FAKE_HOOK, root / "hook.py")
        self.native_probes: NativeProbeListing = EMPTY_NATIVE_PROBE_LISTING
        self.native_ports: list[SimpleNamespace] = []
        self.hook_store = HookSnapshotStore()
        self.retry_store = DiscoveryRetryStore("run-1")
        self.selections = ProbeSelectionStore()
        self.session_uarts = SessionUartSelectionStore()
        self.launches: list[str] = []

        def run_hooks(snapshot: DiscoveryHookSnapshot, kind: str) -> Sequence[HookExecution]:
            self.launches.append(kind)
            return execute_eligible_hooks(snapshot, kind)

        self.inventory = HardwareInventoryService(
            native_probes=lambda: self.native_probes,
            native_uarts=lambda: cast("Any", list(self.native_ports)),
            hook_snapshot=self.hook_store.current,
            run_hooks=run_hooks,
        )
        self.handlers = build_discovery_handlers(
            DiscoveryToolServices(
                hook_root=lambda: self.root,
                load_snapshot=lambda: load_hook_snapshot(self.root, environ={}),
                current_snapshot=self.hook_store.current,
                replace_snapshot=self.hook_store.replace,
                retry_store=self.retry_store,
                registered_providers=registered_provider_ids,
                run_hooks=run_hooks,
                on_refresh=lambda snapshot: (
                    (
                        self.selections.clear(),
                        self.session_uarts.clear(),
                    )
                    and None
                ),
            )
        )

    def contract(self, kind: str, retry_id: str | None = None) -> dict[str, Any]:
        return json.loads(self.handlers[CONTRACT_TOOL](kind, retry_id))

    def refresh(self, retry_id: str | None = None) -> dict[str, Any]:
        return json.loads(self.handlers[REFRESH_TOOL](retry_id))

    def install(self, entries: Sequence[dict[str, Any]]) -> None:
        write_manifest(self.root, list(entries))

    def snapshot(self) -> InventorySnapshot:
        return self.inventory.snapshot()

    def select(self, snapshot: InventorySnapshot) -> str:
        """Mint and record a connection token the way setup_overview does."""

        row = snapshot.probes[0]
        token = (
            probe_connection_id(row.provider, row.stable_identity)
            if row.stable_identity
            else row.probe_id
        )
        self.selections.record(ProbeSelection.from_row(token, row))
        return token


class _WorkflowCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.flow = _Workflow(Path(self._directory.name))


class ProbeDiscoveryWorkflowTests(_WorkflowCase):
    def test_native_uart_precedes_vendor_fallback(self) -> None:
        consulted = []

        def vendor_rows():
            consulted.append(True)
            return [VendorUartRow("nrfjprog", "COM9", "nRF UART", "SER9", 1, 2)]

        ports = [_port("COM3")]
        inventory = HardwareInventoryService(
            native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
            native_uarts=lambda: cast("Any", ports),
            vendor_uarts=vendor_rows,
            hook_snapshot=lambda: EMPTY_SNAPSHOT,
        )
        native = inventory.snapshot()
        self.assertEqual([row.port_path for row in native.uarts], ["COM3"])
        self.assertEqual(consulted, [])
        ports.clear()
        fallback = inventory.snapshot()
        self.assertEqual([row.port_path for row in fallback.uarts], ["COM9"])
        self.assertEqual(fallback.uarts[0].provenance, ("vendor:nrfjprog",))
        self.assertFalse(fallback.uarts[0].native)
        self.assertFalse(fallback.uarts[0].from_hook)
        self.assertTrue(consulted)

    def test_the_full_contract_write_refresh_rerun_select_loop(self) -> None:
        self.flow.native_ports = [_port("COM3")]

        # 1. Native discovery is empty, so the agent gets a ticket and the contract.
        ticket = self.flow.retry_store.issue(
            "probe", retry_tool="setup_overview", retry_arguments={"board_names": ["N"]}
        )
        contract = self.flow.contract("probe", ticket.retry_id)
        self.assertTrue(contract["executable"])
        self.assertEqual(contract["hook_root"], str(self.flow.root))

        # 2. The agent writes the hook and manifest the contract described.
        self.flow.install([hook_entry("probe-hook", "probe", argv=["probe"])])

        # 3. Refresh loads and runs it, then hands back the original call.
        refreshed = self.flow.refresh(ticket.retry_id)
        self.assertEqual(refreshed["status"], "discovery_hooks_refreshed")
        self.assertEqual(refreshed["retry_call"]["tool"], "setup_overview")
        self.assertEqual(len(refreshed["discovered_probes"]), 1)

        # 4. Rerunning the operation now sees the device and can select it.
        snapshot = self.flow.snapshot()
        token = self.flow.select(snapshot)
        selection = self.flow.selections.resolve(token, self.flow.snapshot())

        self.assertEqual(selection.unique_id, "066EFF505057717867163251")
        self.assertFalse(self.flow.retry_store.known(ticket.retry_id))

    def test_refreshing_again_after_an_edit_restores_service(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("probe-hook", "probe", argv=["probe"])])
        self.flow.refresh()
        (self.flow.root / "hook.py").write_text(
            'import json,sys;json.dump({"schema_version":1,"kind":"probe",'
            '"probes":[{"provider":"cmsisdap","unique_id":"NEWUID","description":"Edited"}]},'
            "sys.stdout)",
            encoding="utf-8",
        )
        self.assertEqual(self.flow.snapshot().probes, ())

        self.flow.refresh()
        snapshot = self.flow.snapshot()

        self.assertEqual(snapshot.probes[0].unique_id, "NEWUID")


class ProbeFailureBranchTests(_WorkflowCase):
    def test_a_partial_refresh_still_reports_the_hooks_that_worked(self) -> None:
        self.flow.native_ports = []
        self.flow.install(
            [
                hook_entry("good", "probe", argv=["probe"]),
                hook_entry("bad", "uart", argv=["nonzero"]),
            ]
        )

        payload = self.flow.refresh()

        self.assertEqual(payload["status"], "discovery_hooks_partial")
        self.assertEqual(len(payload["discovered_probes"]), 1)
        self.assertEqual(payload["discovered_serial_ports"], [])
        outcomes = {row["hook_id"]: row["ok"] for row in payload["hooks"]}
        self.assertEqual(outcomes, {"good": True, "bad": False})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
