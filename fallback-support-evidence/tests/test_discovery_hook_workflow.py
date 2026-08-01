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

from pyocd_debug_mcp import discovery_hooks
from pyocd_debug_mcp.discovery_failures import (
    CONTRACT_TOOL,
    DISCOVERY_UNSUPPORTED_PROVIDER,
    PROBE_OPEN_FAILED,
    REFRESH_TOOL,
    UART_OPEN_FAILED,
    carries_hook_contract,
    open_failure_payload,
    unsupported_provider_failure,
)
from pyocd_debug_mcp.discovery_hooks import (
    DiscoveryHookSnapshot,
    HookExecution,
    HookSnapshotStore,
    execute_eligible_hooks,
    load_hook_snapshot,
)
from pyocd_debug_mcp.hardware_inventory import (
    HardwareInventoryService,
    InventorySnapshot,
    ProbeSelection,
    ProbeSelectionStore,
    SelectionDisappeared,
    SessionUartSelection,
    SessionUartSelectionStore,
    UnsupportedProvider,
)
from pyocd_debug_mcp.probe_inventory import (
    EMPTY_NATIVE_PROBE_LISTING,
    NativeProbeListing,
    ProbeInfo,
    registered_provider_ids,
)
from pyocd_debug_mcp.services.connections import probe_connection_id
from pyocd_debug_mcp.tools.discovery import (
    DiscoveryRetryStore,
    DiscoveryToolServices,
    build_discovery_handlers,
)
from tests.discovery_hook_fixtures import FAKE_HOOK, hook_entry, write_manifest


def _listing(*uids: str) -> NativeProbeListing:
    if not uids:
        return EMPTY_NATIVE_PROBE_LISTING
    return NativeProbeListing(
        probes=tuple(
            ProbeInfo(uid=uid, description=f"J-Link {uid}", raw=uid, family="jlink")
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

        def run_hooks(
            snapshot: DiscoveryHookSnapshot, kind: str
        ) -> Sequence[HookExecution]:
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
                    self.selections.clear(),
                    self.session_uarts.clear(),
                )
                and None,
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
    def test_native_empty_then_a_probe_hook_returns_a_usable_uid(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("probe-hook", "probe", argv=["probe"])])
        self.flow.refresh()

        snapshot = self.flow.snapshot()

        self.assertEqual(self.flow.launches.count("probe"), 2)  # refresh + snapshot
        self.assertEqual(len(snapshot.probes), 1)
        row = snapshot.probes[0]
        self.assertEqual(row.unique_id, "066EFF505057717867163251")
        self.assertEqual(row.provider, "cmsisdap")
        self.assertTrue(row.from_hook)
        self.assertEqual(row.identity_scope, "stable")

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

    def test_a_provider_qualified_remote_selector_survives_the_whole_pipe(self) -> None:
        """The route that still works when the local USB stack is blind to the probe.

        `remote:<host>:<port>` is the one selector form that can succeed when pyOCD
        cannot enumerate the device locally at all, and the contract's
        `unique_id_guidance` now tells agents to write it. That guidance is only
        honest if the selector survives ingestion, tokenizing, and re-derivation
        byte-for-byte -- two colons and a non-USB provider are exactly what a token
        layer is most likely to mangle (C12 was a colon misparse that resolved to a
        *different real probe*), and `remote` must clear D25's provider check.
        """

        self.flow.native_ports = [_port("COM3")]
        self.flow.install(
            [hook_entry("probe-hook", "probe", argv=["probe_remote", "bench.local:5555"])]
        )
        self.flow.refresh()

        snapshot = self.flow.snapshot()
        self.assertEqual(snapshot.probes[0].provider, "remote")

        token = self.flow.select(snapshot)
        selection = self.flow.selections.resolve(token, self.flow.snapshot())

        # Verbatim: the host:port half must not be eaten by the provider split, and
        # resolve() must not have raised UnsupportedProvider on a registered provider.
        self.assertEqual(selection.unique_id, "remote:bench.local:5555")
        self.assertEqual(selection.provider, "remote")
        self.assertIn("remote", registered_provider_ids())

    def test_a_hook_discovered_probe_survives_reselection_across_snapshots(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("probe-hook", "probe", argv=["probe"])])
        self.flow.refresh()
        token = self.flow.select(self.flow.snapshot())

        for _repeat in range(3):
            selection = self.flow.selections.resolve(token, self.flow.snapshot())
            self.assertEqual(selection.unique_id, "066EFF505057717867163251")

    def test_a_device_that_becomes_natively_visible_still_resolves(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("probe-hook", "probe", argv=["probe"])])
        self.flow.refresh()
        token = self.flow.select(self.flow.snapshot())

        # The driver is fixed; pyOCD now sees the same physical probe natively.
        self.flow.native_probes = NativeProbeListing(
            probes=(
                ProbeInfo(
                    uid="066EFF505057717867163251",
                    description="ST-LINK",
                    raw="x",
                    family="cmsisdap",
                ),
            ),
            command=("pyocd",),
            exit_code=0,
            timed_out=False,
            stdout_summary="",
            stderr_summary="",
        )
        before = len(self.flow.launches)
        snapshot = self.flow.snapshot()

        self.assertEqual(len(self.flow.launches), before, "a hook ran despite native rows")
        selection = self.flow.selections.resolve(token, snapshot)
        self.assertEqual(selection.unique_id, "066EFF505057717867163251")
        self.assertTrue(snapshot.probes[0].native)

    def test_a_disappeared_hook_probe_refuses_instead_of_substituting(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("probe-hook", "probe", argv=["probe"])])
        self.flow.refresh()
        token = self.flow.select(self.flow.snapshot())

        # The hook now reports a *different* probe.
        self.flow.install(
            [hook_entry("probe-hook", "probe", argv=["probe_uid", "SOMETHINGELSE"])]
        )
        self.flow.refresh()
        self.flow.selections.record(
            ProbeSelection(
                connection_id=token,
                provider="cmsisdap",
                unique_id="066EFF505057717867163251",
                stable_identity="066EFF505057717867163251",
                provenance=("hook:probe-hook",),
                hook_source_sha256=None,
                identity_scope="stable",
            )
        )

        with self.assertRaises(SelectionDisappeared):
            self.flow.selections.resolve(token, self.flow.snapshot())

    def test_an_identity_change_after_a_hook_edit_is_refused(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("probe-hook", "probe", argv=["probe"])])
        self.flow.refresh()
        snapshot = self.flow.snapshot()
        token = self.flow.select(snapshot)
        recorded = self.flow.selections.recorded(token)
        assert recorded is not None
        self.assertIsNotNone(recorded.hook_source_sha256)

        # Edit the hook file. The bytes now differ from what the refresh admitted.
        (self.flow.root / "hook.py").write_text(
            'import json,sys;json.dump({"schema_version":1,"kind":"probe",'
            '"probes":[{"provider":"cmsisdap","unique_id":"066EFF505057717867163251",'
            '"description":"Edited"}]},sys.stdout)',
            encoding="utf-8",
        )
        after_edit = self.flow.snapshot()

        self.assertEqual(after_edit.probes, (), "a drifted hook still produced rows")
        self.assertEqual(
            after_edit.hook_failures[0].failure_code, "discovery/hook-source-changed"
        )
        with self.assertRaises(SelectionDisappeared):
            self.flow.selections.resolve(token, after_edit)

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
    def test_a_hook_timeout_is_reported_with_the_configured_deadline(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install(
            [hook_entry("slow", "probe", argv=["hang"], timeout_seconds=1.0)]
        )

        payload = self.flow.refresh()
        snapshot = self.flow.snapshot()

        self.assertEqual(payload["status"], "discovery_hooks_partial")
        self.assertEqual(payload["hooks"][0]["outcome"], "timeout")
        self.assertEqual(payload["hooks"][0]["code"], "discovery/hook-timeout")
        self.assertEqual(payload["hooks"][0]["timeout_seconds"], 1.0)
        self.assertEqual(snapshot.probes, ())

    def test_malformed_hook_output_is_reported_as_invalid_not_failed(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("bad", "probe", argv=["bad_json"])])

        payload = self.flow.refresh()

        self.assertEqual(payload["hooks"][0]["code"], "discovery/hook-output-invalid")
        self.assertIn("stdout_excerpt", payload["hooks"][0])

    def test_a_nonzero_hook_exit_is_reported_as_failed(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("bad", "probe", argv=["nonzero"])])

        payload = self.flow.refresh()

        self.assertEqual(payload["hooks"][0]["code"], "discovery/hook-failed")
        self.assertEqual(payload["hooks"][0]["exit_code"], 3)

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

    def test_a_successful_hook_that_finds_nothing_says_so_plainly(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("empty", "probe", argv=["probe_empty"])])

        payload = self.flow.refresh()

        self.assertEqual(payload["status"], "discovery_hooks_refreshed")
        self.assertEqual(payload["discovered_probes"], [])
        self.assertIn("none reported any hardware", payload["agent_prompt"])

    def test_pyocd_open_failure_after_successful_discovery_is_terminal(self) -> None:
        """Discovery worked. Never loop back to discovery."""

        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("probe-hook", "probe", argv=["probe"])])
        self.flow.refresh()
        snapshot = self.flow.snapshot()
        selection = self.flow.selections.resolve(
            self.flow.select(snapshot), self.flow.snapshot()
        )

        payload = open_failure_payload(
            PROBE_OPEN_FAILED,
            detail="pyOCD could not open the probe.",
            identity=selection.unique_id,
        )

        self.assertEqual(payload["code"], PROBE_OPEN_FAILED)
        self.assertFalse(carries_hook_contract(payload))
        self.assertNotIn("gate", payload)
        self.assertNotIn("validation_id", payload)
        self.assertIn("do not call the discovery hook tools", str(payload["agent_prompt"]))

    def test_an_unsupported_provider_is_diagnostic_only(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install(
            [hook_entry("odd", "probe", argv=["probe_provider", "nosuchprovider"])]
        )
        self.flow.refresh()

        snapshot = self.flow.snapshot()

        # Diagnostic only: the row itself stays visible. A hook found real hardware,
        # and hiding the row from an agent would throw away the one thing the hook
        # exists to report.
        self.assertEqual(snapshot.probes[0].provider, "nosuchprovider")
        self.assertNotIn("nosuchprovider", registered_provider_ids())

        # But resolving the selection for actual use is refused -- it must never fall
        # through to pyOCD's own open attempt, which would raise a generic
        # ProbeNotFoundError that reads as a cabling problem instead of a missing
        # pyOCD plug-in.
        token = self.flow.select(snapshot)
        with self.assertRaises(UnsupportedProvider) as ctx:
            self.flow.selections.resolve(token, self.flow.snapshot())

        exc = ctx.exception
        self.assertEqual(exc.provider, "nosuchprovider")
        payload = unsupported_provider_failure(
            exc.provider, registered_providers=exc.registered_providers
        ).to_payload()
        self.assertEqual(payload["code"], DISCOVERY_UNSUPPORTED_PROVIDER)
        self.assertNotIn("hook_contract_call", payload, "a hook cannot fix this")
        self.assertIn("nosuchprovider", str(payload["agent_prompt"]))
        self.assertIn(
            "install or enable a pyOCD probe plug-in",
            " ".join(cast("list[str]", payload["remedies"])),
        )


class UartDiscoveryWorkflowTests(_WorkflowCase):
    def test_native_pyserial_empty_then_a_uart_hook_returns_an_endpoint(self) -> None:
        self.flow.native_probes = _listing("U1")
        self.flow.install([hook_entry("uart-hook", "uart", argv=["uart"])])
        self.flow.refresh()

        snapshot = self.flow.snapshot()

        self.assertEqual(len(snapshot.uarts), 1)
        row = snapshot.uarts[0]
        self.assertEqual(row.port_path, "COM7")
        self.assertEqual(row.usb_serial, "066EFF505057717867163251")
        self.assertEqual(row.identity_scope, "stable")
        self.assertNotIn("probe", self.flow.launches)

    def test_a_stable_uart_survives_a_port_path_change(self) -> None:
        """The identity is (serial, vid, pid); the path is only reported."""

        self.flow.native_probes = _listing("U1")
        self.flow.install([hook_entry("uart-hook", "uart", argv=["uart"])])
        self.flow.refresh()
        first = self.flow.snapshot().uarts[0]

        self.flow.install(
            [hook_entry("uart-hook", "uart", argv=["uart_port", "COM42"])]
        )
        self.flow.refresh()
        second = self.flow.snapshot().uarts[0]

        self.assertNotEqual(first.port_path, second.port_path)
        self.assertEqual(first.stable_key(), second.stable_key())

    def test_a_session_local_uart_is_usable_this_run_and_never_cached(self) -> None:
        self.flow.native_probes = _listing("U1")
        self.flow.install(
            [hook_entry("uart-hook", "uart", argv=["uart_session_local", "COM9"])]
        )
        self.flow.refresh()
        snapshot = self.flow.snapshot()
        row = snapshot.uarts[0]

        self.assertEqual(row.identity_scope, "session")
        self.assertIsNone(row.stable_key(), "a session endpoint has no cache key")

        self.flow.session_uarts.record(
            SessionUartSelection("board-1", row.port_path, row.description, row.provenance)
        )
        resolved = self.flow.session_uarts.resolve("board-1", self.flow.snapshot())

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.port_path, "COM9")

    def test_a_session_local_uart_that_vanishes_is_forgotten(self) -> None:
        self.flow.native_probes = _listing("U1")
        self.flow.install(
            [hook_entry("uart-hook", "uart", argv=["uart_session_local", "COM9"])]
        )
        self.flow.refresh()
        row = self.flow.snapshot().uarts[0]
        self.flow.session_uarts.record(
            SessionUartSelection("board-1", row.port_path, row.description, row.provenance)
        )

        self.flow.install(
            [hook_entry("uart-hook", "uart", argv=["uart_session_local", "COM11"])]
        )
        self.flow.refresh()
        resolved = self.flow.session_uarts.resolve("board-1", self.flow.snapshot())

        self.assertIsNone(resolved)
        self.assertIsNone(self.flow.session_uarts.recorded("board-1"))

    def test_uart_open_failure_is_an_action_failure_not_a_discovery_failure(self) -> None:
        payload = open_failure_payload(UART_OPEN_FAILED, detail="Port is busy.")

        self.assertEqual(payload["code"], UART_OPEN_FAILED)
        self.assertFalse(carries_hook_contract(payload))
        self.assertIn("no other process", " ".join(cast("list[str]", payload["remedies"])))

    def test_a_uart_hook_never_runs_while_pyserial_reports_a_port(self) -> None:
        """The hot path: read/write/exchange and the finalizer all come through here."""

        self.flow.native_probes = _listing("U1")
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("uart-hook", "uart", argv=["uart"])])
        self.flow.refresh()
        self.flow.launches.clear()

        for _repeat in range(5):
            snapshot = self.flow.snapshot()
            self.assertEqual([row.port_path for row in snapshot.uarts], ["COM3"])

        self.assertEqual(self.flow.launches, [], "the serial hot path executed hooks")


class MultiBoardWorkflowTests(_WorkflowCase):
    def test_two_hook_discovered_probes_keep_one_to_one_selections(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("probe-hook", "probe", argv=["probe_two"])])
        self.flow.refresh()
        snapshot = self.flow.snapshot()

        self.assertEqual(len(snapshot.probes), 2)
        tokens = {}
        for row in snapshot.probes:
            token = (
                probe_connection_id(row.provider, row.stable_identity)
                if row.stable_identity
                else row.probe_id
            )
            self.flow.selections.record(ProbeSelection.from_row(token, row))
            tokens[token] = row.unique_id

        fresh = self.flow.snapshot()
        for token, expected in tokens.items():
            with self.subTest(token=token):
                self.assertEqual(
                    self.flow.selections.resolve(token, fresh).unique_id, expected
                )
        self.assertEqual(len(set(tokens.values())), 2)

    def test_both_kinds_can_be_hook_discovered_in_one_snapshot(self) -> None:
        self.flow.install(
            [
                hook_entry("probe-hook", "probe", argv=["probe"]),
                hook_entry("uart-hook", "uart", argv=["uart"]),
            ]
        )
        self.flow.refresh()

        snapshot = self.flow.snapshot()

        self.assertEqual(len(snapshot.probes), 1)
        self.assertEqual(len(snapshot.uarts), 1)
        self.assertTrue(snapshot.probes[0].from_hook)
        self.assertTrue(snapshot.uarts[0].from_hook)
        ids = {row.snapshot_id for row in snapshot.probes}
        ids |= {row.snapshot_id for row in snapshot.uarts}
        self.assertEqual(ids, {snapshot.snapshot_id})


class RefreshInvalidationWorkflowTests(_WorkflowCase):
    def test_a_refresh_clears_selections_recorded_against_the_old_hook_set(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("probe-hook", "probe", argv=["probe"])])
        self.flow.refresh()
        token = self.flow.select(self.flow.snapshot())
        self.assertIsNotNone(self.flow.selections.recorded(token))

        self.flow.refresh()

        self.assertIsNone(
            self.flow.selections.recorded(token),
            "a refresh kept a selection recorded against the previous hook set",
        )

    def test_removing_the_manifest_stops_hook_discovery_entirely(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("probe-hook", "probe", argv=["probe"])])
        self.flow.refresh()
        self.assertEqual(len(self.flow.snapshot().probes), 1)

        (self.flow.root / discovery_hooks.MANIFEST_FILENAME).unlink()
        payload = self.flow.refresh()
        self.flow.launches.clear()
        snapshot = self.flow.snapshot()

        self.assertEqual(payload["status"], "discovery_hooks_absent")
        self.assertEqual(snapshot.probes, ())
        self.assertEqual(self.flow.launches, [])

    def test_a_broken_manifest_leaves_the_working_hooks_in_place(self) -> None:
        self.flow.native_ports = [_port("COM3")]
        self.flow.install([hook_entry("probe-hook", "probe", argv=["probe"])])
        self.flow.refresh()

        (self.flow.root / discovery_hooks.MANIFEST_FILENAME).write_text(
            "{not json", encoding="utf-8"
        )
        payload = self.flow.refresh()

        self.assertEqual(payload["code"], "discovery/manifest-invalid")
        # The previously admitted snapshot is untouched, so discovery still works.
        self.assertEqual(len(self.flow.snapshot().probes), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
