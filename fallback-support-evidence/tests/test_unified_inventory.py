"""Merge, dedupe, and provenance rules for the one unified inventory service."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Sequence, cast

from pyocd_debug_mcp import discovery_hooks
from pyocd_debug_mcp.discovery_hooks import (
    DiscoveryHookSnapshot,
    DiscoveryHookSpec,
    HookExecution,
    HookOutput,
    HookProbeRow,
    HookUartRow,
)
from pyocd_debug_mcp.hardware_inventory import (
    ActiveConnectionRow,
    HardwareInventoryService,
    ProbeRow,
    UartRow,
    VendorUartRow,
    snapshot_from_validation_inventory,
    stable_identity_equal,
    validation_inventory_from,
)
from pyocd_debug_mcp.probe_inventory import (
    EMPTY_NATIVE_PROBE_LISTING,
    NativeProbeListing,
    ProbeInfo,
    registered_provider_ids,
)
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe

SHA_A = "a" * 64
SHA_B = "b" * 64


def _listing(*probes: tuple[str, str]) -> NativeProbeListing:
    if not probes:
        return EMPTY_NATIVE_PROBE_LISTING
    return NativeProbeListing(
        probes=tuple(
            ProbeInfo(uid=uid, description=f"{family} {uid}", raw=uid, family=family)
            for uid, family in probes
        ),
        command=("pyocd", "list", "--probes"),
        exit_code=0,
        timed_out=False,
        stdout_summary="",
        stderr_summary="",
    )


def _port(device: str, serial: str | None = "SER1", vid: int | None = 0x1234,
          pid: int | None = 0x5678) -> SimpleNamespace:
    return SimpleNamespace(
        device=device,
        description=f"UART {device}",
        product="",
        serial_number=serial or "",
        manufacturer="",
        interface="",
        hwid="",
        location="",
        vid=vid,
        pid=pid,
    )


def _hook_execution(
    hook_id: str,
    kind: str,
    *,
    probes: Sequence[HookProbeRow] = (),
    uarts: Sequence[HookUartRow] = (),
    sha: str = SHA_A,
) -> HookExecution:
    return HookExecution(
        hook_id=hook_id,
        kind=cast(Any, kind),
        source="project",
        outcome="exited",
        exit_code=0,
        timeout_seconds=10.0,
        output=HookOutput(cast(Any, kind), tuple(probes), tuple(uarts)),
        stdout_excerpt="",
        stderr_excerpt="",
        failure_detail="",
        stdout_truncated=False,
        file_sha256=sha,
    )


def _hook_snapshot(*kinds: str) -> DiscoveryHookSnapshot:
    from pathlib import Path

    return DiscoveryHookSnapshot(
        manifest_sha256="m" * 64,
        hooks=tuple(
            DiscoveryHookSpec(
                hook_id=f"{kind}-hook",
                kind=cast(Any, kind),
                platforms=frozenset(discovery_hooks.SUPPORTED_PLATFORMS),
                runner="server-python",
                entrypoint=Path("hook.py"),
                argv=(),
                timeout_seconds=10.0,
                source="project",
                file_sha256=SHA_A,
            )
            for kind in kinds
        ),
        loaded_at="now",
    )


def _service(
    native_probes: NativeProbeListing,
    ports: Sequence[SimpleNamespace] | None,
    *,
    active: Sequence[ActiveConnectionRow] = (),
    vendor: Sequence[VendorUartRow] = (),
    hooks: DiscoveryHookSnapshot | None = None,
    hook_results: dict[str, Sequence[HookExecution]] | None = None,
) -> HardwareInventoryService:
    results = hook_results or {}
    return HardwareInventoryService(
        native_probes=lambda: native_probes,
        native_uarts=lambda: cast("Any", None if ports is None else list(ports)),
        active_connections=lambda: tuple(active),
        vendor_uarts=lambda: tuple(vendor),
        hook_snapshot=lambda: hooks or discovery_hooks.EMPTY_SNAPSHOT,
        run_hooks=lambda snapshot, kind: results.get(kind, ()),
    )


class NativeOnlyTests(unittest.TestCase):
    def test_native_probe_rows_carry_native_provenance_and_stable_scope(self) -> None:
        snapshot = _service(_listing(("U1", "cmsisdap")), []).snapshot()

        row = snapshot.probes[0]
        self.assertEqual(row.provenance, ("native",))
        self.assertTrue(row.native)
        self.assertFalse(row.from_hook)
        self.assertEqual(row.identity_scope, "stable")
        self.assertEqual(row.unique_id, "U1")
        self.assertEqual(row.stable_identity, "U1")
        self.assertIsNone(row.hook_source_sha256)

    def test_duplicate_native_uids_are_collapsed(self) -> None:
        listing = _listing(("U1", "cmsisdap"), ("U1", "cmsisdap"))

        snapshot = _service(listing, []).snapshot()

        self.assertEqual(len(snapshot.probes), 1)

    def test_native_uart_scope_follows_stable_identity(self) -> None:
        ports = [
            _port("COM3", "SER1", 0x1234, 0x5678),
            _port("COM4", None, 0x1234, 0x5678),
            _port("COM5", "SER2", None, 0x5678),
        ]

        snapshot = _service(_listing(("U1", "cmsisdap")), ports).snapshot()

        self.assertEqual(
            [(row.port_path, row.identity_scope) for row in snapshot.uarts],
            [("COM3", "stable"), ("COM4", "session"), ("COM5", "session")],
        )

    def test_boolean_usb_ids_are_not_accepted_as_integers(self) -> None:
        ports = [_port("COM3", "SER1", cast(Any, True), 0x5678)]

        snapshot = _service(_listing(("U1", "cmsisdap")), ports).snapshot()

        self.assertEqual(snapshot.uarts[0].identity_scope, "session")

    def test_out_of_range_usb_ids_are_treated_as_session_local(self) -> None:
        ports = [_port("COM3", "SER1", 0x10000, 0x5678)]

        snapshot = _service(_listing(("U1", "cmsisdap")), ports).snapshot()

        self.assertEqual(snapshot.uarts[0].identity_scope, "session")


class ActiveConnectionTests(unittest.TestCase):
    """pyOCD omits probes this process already holds open; validation still needs them."""

    def test_an_open_probe_with_a_uid_appears_as_a_stable_row(self) -> None:
        active = (ActiveConnectionRow("U9", "U9", "Open board", "jlink"),)

        snapshot = _service(EMPTY_NATIVE_PROBE_LISTING, [], active=active).snapshot()

        row = snapshot.probes[0]
        self.assertEqual(row.probe_id, "U9")
        self.assertEqual(row.unique_id, "U9")
        self.assertEqual(row.identity_scope, "stable")
        self.assertTrue(row.native)

    def test_a_uid_less_open_probe_is_session_scoped_with_no_selector(self) -> None:
        active = (ActiveConnectionRow("session:tok", None, "Open board", "jlink"),)

        snapshot = _service(EMPTY_NATIVE_PROBE_LISTING, [], active=active).snapshot()

        row = snapshot.probes[0]
        self.assertEqual(row.probe_id, "session:tok")
        self.assertIsNone(row.unique_id)
        self.assertIsNone(row.stable_identity)
        self.assertEqual(row.identity_scope, "session")

    def test_an_open_probe_already_in_the_cli_listing_is_not_duplicated(self) -> None:
        active = (ActiveConnectionRow("U1", "U1", "Open board", "cmsisdap"),)

        snapshot = _service(_listing(("U1", "cmsisdap")), [], active=active).snapshot()

        self.assertEqual(len(snapshot.probes), 1)
        self.assertEqual(snapshot.probes[0].description, "cmsisdap U1")

    def test_active_probes_remain_visible_to_validation(self) -> None:
        active = (ActiveConnectionRow("U9", "U9", "Open board", "jlink"),)

        inventory = _service(
            EMPTY_NATIVE_PROBE_LISTING, [], active=active
        ).validation_inventory()

        self.assertEqual([probe.probe_id for probe in inventory.probes], ["U9"])
        self.assertEqual(inventory.probes[0].usb_serial, "U9")

    def test_a_uid_less_active_probe_warns_the_agent_it_is_session_local(self) -> None:
        active = (ActiveConnectionRow("session:tok", None, "Open board", "jlink"),)

        inventory = _service(
            EMPTY_NATIVE_PROBE_LISTING, [], active=active
        ).validation_inventory()

        self.assertIsNone(inventory.probes[0].usb_serial)
        self.assertIn("session-local", inventory.probes[0].choice().label)


class ProbeMergeTests(unittest.TestCase):
    MERGE = staticmethod(HardwareInventoryService._merge_probe_rows)

    @staticmethod
    def _row(provider: str, uid: str, provenance: tuple[str, ...], sha: str | None = None) -> ProbeRow:
        return ProbeRow(
            provider=provider,
            probe_id=uid,
            unique_id=uid,
            row_id=f"row-{provider}-{uid}",
            description=f"{provider} {uid}",
            stable_identity=uid,
            provenance=provenance,
            hook_source_sha256=sha,
            identity_scope="stable",
            snapshot_id="snap",
        )

    def test_the_same_device_from_both_sources_becomes_one_row(self) -> None:
        merged = self.MERGE(
            [self._row("jlink", "683710208", ("native",))],
            [self._row("jlink", "683710208", ("hook:h",), SHA_A)],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].provenance, ("native", "hook:h"))
        self.assertEqual(merged[0].hook_source_sha256, SHA_A)
        self.assertTrue(merged[0].native)
        self.assertTrue(merged[0].from_hook)

    def test_the_native_description_is_not_overwritten_by_a_hook(self) -> None:
        native = self._row("jlink", "1", ("native",))
        hook = ProbeRow(
            provider="jlink",
            probe_id="1",
            unique_id="1",
            row_id="row-hook",
            description="HOOK DESCRIPTION",
            stable_identity="1",
            provenance=("hook:h",),
            hook_source_sha256=SHA_A,
            identity_scope="stable",
            snapshot_id="snap",
        )

        merged = self.MERGE([native], [hook])

        self.assertEqual(merged[0].description, native.description)
        self.assertEqual(merged[0].row_id, native.row_id)

    def test_decimal_uids_merge_with_leading_zeros_stripped(self) -> None:
        merged = self.MERGE(
            [self._row("jlink", "683710208", ("native",))],
            [self._row("jlink", "000683710208", ("hook:h",), SHA_A)],
        )

        self.assertEqual(len(merged), 1)

    def test_two_providers_with_identical_uid_text_stay_distinct(self) -> None:
        merged = self.MERGE(
            [self._row("jlink", "12345", ("native",))],
            [self._row("cmsisdap", "12345", ("hook:h",), SHA_A)],
        )

        self.assertEqual(len(merged), 2)
        self.assertEqual({row.provider for row in merged}, {"jlink", "cmsisdap"})

    def test_hex_uids_are_not_renumbered_like_decimals(self) -> None:
        merged = self.MERGE(
            [self._row("cmsisdap", "0abc", ("native",))],
            [self._row("cmsisdap", "abc", ("hook:h",), SHA_A)],
        )

        self.assertEqual(len(merged), 2)

    def test_conflicting_devices_in_one_provider_stay_separate(self) -> None:
        merged = self.MERGE(
            [self._row("jlink", "111", ("native",))],
            [self._row("jlink", "222", ("hook:h",), SHA_A)],
        )

        self.assertEqual(len(merged), 2)

    def test_a_hook_row_never_deletes_a_native_row(self) -> None:
        native = [
            self._row("jlink", "111", ("native",)),
            self._row("jlink", "222", ("native",)),
        ]

        merged = self.MERGE(native, [self._row("jlink", "999", ("hook:h",), SHA_A)])

        self.assertEqual(len(merged), 3)
        self.assertEqual([row.provenance[0] for row in merged[:2]], ["native", "native"])

    def test_stable_identity_equality_is_deliberately_narrow(self) -> None:
        self.assertTrue(stable_identity_equal("ABC", "abc"))
        self.assertTrue(stable_identity_equal("0", "0000"))
        self.assertTrue(stable_identity_equal(" 12 ", "12"))
        self.assertFalse(stable_identity_equal(None, "abc"))
        self.assertFalse(stable_identity_equal("abc", None))
        self.assertFalse(stable_identity_equal("", "abc"))
        # Punctuation is never stripped broadly: that would conflate vendor formats.
        self.assertFalse(stable_identity_equal("ab-c", "abc"))
        self.assertFalse(stable_identity_equal("0abc", "abc"))


class UartMergeTests(unittest.TestCase):
    MERGE = staticmethod(HardwareInventoryService._merge_uart_rows)

    @staticmethod
    def _row(
        path: str,
        provenance: tuple[str, ...],
        *,
        serial: str | None = "SER1",
        vid: int | None = 0x1234,
        pid: int | None = 0x5678,
    ) -> UartRow:
        scope = "stable" if serial and vid is not None and pid is not None else "session"
        return UartRow(
            port_path=path,
            description=f"UART {path}",
            usb_serial=serial,
            vid=vid,
            pid=pid,
            provenance=provenance,
            identity_scope=cast(Any, scope),
            row_id=f"row-{path}",
            snapshot_id="snap",
        )

    def test_stable_endpoints_dedupe_on_the_attachment_cache_key(self) -> None:
        merged = self.MERGE(
            [self._row("COM3", ("native",))],
            [self._row("COM9", ("hook:h",))],
        )

        self.assertEqual(len(merged), 1, "the same (serial, vid, pid) is one endpoint")
        self.assertEqual(merged[0].provenance, ("native", "hook:h"))
        self.assertEqual(merged[0].port_path, "COM3", "native path must win")

    def test_stable_endpoints_with_different_keys_stay_separate(self) -> None:
        merged = self.MERGE(
            [self._row("COM3", ("native",), serial="SER1")],
            [self._row("COM9", ("hook:h",), serial="SER2")],
        )

        self.assertEqual(len(merged), 2)

    def test_session_endpoints_dedupe_by_normalized_path_and_source(self) -> None:
        merged = self.MERGE(
            [self._row("COM3", ("hook:h",), serial=None)],
            [self._row(r"\\.\COM3", ("hook:h",), serial=None)],
        )

        self.assertEqual(len(merged), 1, "the Windows device prefix is not a new port")

    def test_session_endpoints_from_different_sources_stay_separate(self) -> None:
        merged = self.MERGE(
            [self._row("COM3", ("vendor:nrfjprog",), serial=None)],
            [self._row("COM3", ("hook:h",), serial=None)],
        )

        self.assertEqual(len(merged), 2)

    def test_a_session_row_never_merges_into_a_stable_row(self) -> None:
        merged = self.MERGE(
            [self._row("COM3", ("native",))],
            [self._row("COM3", ("hook:h",), serial=None)],
        )

        self.assertEqual(len(merged), 2)

    def test_stable_key_matches_serial_endpoint_semantics(self) -> None:
        from pyocd_debug_mcp.firmstore.cache import SerialEndpoint

        row = self._row("COM3", ("native",), serial="SER1", vid=1, pid=2)
        endpoint = SerialEndpoint("COM3", "SER1", 1, 2)

        self.assertEqual(row.stable_key(), endpoint.stable_key())
        self.assertIsNone(self._row("COM3", ("native",), serial=None).stable_key())


class HookRowTests(unittest.TestCase):
    def test_hook_probe_rows_carry_the_hook_provenance_and_source_digest(self) -> None:
        execution = _hook_execution(
            "local-fallback",
            "probe",
            probes=[HookProbeRow("cmsisdap", "HOOKUID", "Hook probe")],
        )
        service = _service(
            EMPTY_NATIVE_PROBE_LISTING,
            [_port("COM3")],
            hooks=_hook_snapshot("probe"),
            hook_results={"probe": (execution,)},
        )

        snapshot = service.snapshot()

        row = snapshot.probes[0]
        self.assertEqual(row.provenance, ("hook:local-fallback",))
        self.assertEqual(row.hook_source_sha256, SHA_A)
        self.assertTrue(row.from_hook)
        self.assertFalse(row.native)
        self.assertEqual(row.unique_id, "HOOKUID")

    def test_a_failed_hook_contributes_no_rows_but_is_reported(self) -> None:
        failed = HookExecution(
            hook_id="broken",
            kind="probe",
            source="project",
            outcome="parse_failed",
            exit_code=0,
            timeout_seconds=10.0,
            output=None,
            stdout_excerpt="junk",
            stderr_excerpt="",
            failure_detail="not JSON",
            stdout_truncated=False,
            file_sha256=SHA_A,
        )
        service = _service(
            EMPTY_NATIVE_PROBE_LISTING,
            [_port("COM3")],
            hooks=_hook_snapshot("probe"),
            hook_results={"probe": (failed,)},
        )

        snapshot = service.snapshot()

        self.assertEqual(snapshot.probes, ())
        self.assertEqual(len(snapshot.hook_diagnostics), 1)
        self.assertEqual(len(snapshot.hook_failures), 1)
        self.assertEqual(
            snapshot.hook_diagnostic_rows()[0]["code"], "discovery/hook-output-invalid"
        )

    def test_hook_uart_rows_split_stable_from_session_local(self) -> None:
        execution = _hook_execution(
            "uart-fallback",
            "uart",
            uarts=[
                HookUartRow("COM7", "Stable UART", "SER7", 1, 2),
                HookUartRow("COM8", "Session UART", None, None, None),
            ],
        )
        service = _service(
            _listing(("U1", "cmsisdap")),
            [],
            hooks=_hook_snapshot("uart"),
            hook_results={"uart": (execution,)},
        )

        snapshot = service.snapshot()

        self.assertEqual(
            [(row.port_path, row.identity_scope) for row in snapshot.uarts],
            [("COM7", "stable"), ("COM8", "session")],
        )
        for row in snapshot.uarts:
            self.assertEqual(row.hook_source_sha256, SHA_A)


class VendorProvenanceTests(unittest.TestCase):
    def test_vendor_rows_fill_an_empty_native_uart_inventory(self) -> None:
        vendor = (VendorUartRow("nrfjprog", "COM9", "nRF UART", "SER9", 1, 2),)

        snapshot = _service(_listing(("U1", "cmsisdap")), [], vendor=vendor).snapshot()

        self.assertEqual([row.port_path for row in snapshot.uarts], ["COM9"])
        self.assertEqual(snapshot.uarts[0].provenance, ("vendor:nrfjprog",))
        self.assertFalse(snapshot.uarts[0].native)
        self.assertFalse(snapshot.uarts[0].from_hook)

    def test_vendor_rows_are_not_consulted_when_native_uarts_exist(self) -> None:
        consulted: list[int] = []

        def vendor_uarts() -> tuple[VendorUartRow, ...]:
            consulted.append(1)
            return (VendorUartRow("nrfjprog", "COM9", "nRF UART"),)

        service = HardwareInventoryService(
            native_probes=lambda: _listing(("U1", "cmsisdap")),
            native_uarts=lambda: cast("Any", [_port("COM3")]),
            vendor_uarts=vendor_uarts,
            hook_snapshot=lambda: discovery_hooks.EMPTY_SNAPSHOT,
        )

        snapshot = service.snapshot()

        self.assertEqual(consulted, [], "a vendor helper ran while pyserial saw a port")
        self.assertEqual([row.port_path for row in snapshot.uarts], ["COM3"])

    def test_a_vendor_row_and_a_hook_row_for_one_device_become_one_row(self) -> None:
        vendor = (VendorUartRow("nrfjprog", "COM9", "nRF UART", "SER9", 1, 2),)
        execution = _hook_execution(
            "uart-hook", "uart", uarts=[HookUartRow("COM9", "Hook UART", "SER9", 1, 2)]
        )
        service = _service(
            _listing(("U1", "cmsisdap")),
            [],
            vendor=vendor,
            hooks=_hook_snapshot("uart"),
            hook_results={"uart": (execution,)},
        )

        snapshot = service.snapshot()

        self.assertEqual(len(snapshot.uarts), 1)
        self.assertEqual(
            snapshot.uarts[0].provenance, ("vendor:nrfjprog", "hook:uart-hook")
        )


class ProviderTruthTests(unittest.TestCase):
    def test_registered_providers_come_from_pyocd_probe_classes(self) -> None:
        providers = registered_provider_ids()

        self.assertTrue(providers)
        self.assertEqual(list(providers), sorted(providers))
        for provider in providers:
            self.assertEqual(provider, provider.casefold())

    def test_a_plug_in_provider_injected_into_probe_classes_is_accepted(self) -> None:
        """A registered plug-in is openable whether probe_families.json knows it."""

        from pyocd.probe.aggregator import PROBE_CLASSES  # type: ignore[import-untyped]

        class _FakePlugInProbe:
            pass

        PROBE_CLASSES["fakeplugin"] = _FakePlugInProbe
        try:
            self.assertIn("fakeplugin", registered_provider_ids())
        finally:
            PROBE_CLASSES.pop("fakeplugin", None)

        self.assertNotIn("fakeplugin", registered_provider_ids())

    def test_an_unregistered_provider_is_still_carried_as_a_row(self) -> None:
        """Discovery worked; whether pyOCD can drive it is a separate, typed answer."""

        execution = _hook_execution(
            "hook", "probe", probes=[HookProbeRow("nosuchprovider", "U1", "Odd probe")]
        )
        service = _service(
            EMPTY_NATIVE_PROBE_LISTING,
            [_port("COM3")],
            hooks=_hook_snapshot("probe"),
            hook_results={"probe": (execution,)},
        )

        snapshot = service.snapshot()

        self.assertEqual(snapshot.probes[0].provider, "nosuchprovider")
        self.assertNotIn("nosuchprovider", registered_provider_ids())


class AdapterTests(unittest.TestCase):
    def test_validation_inventory_sorts_probes_by_probe_id(self) -> None:
        listing = _listing(("zzz", "jlink"), ("aaa", "jlink"), ("mmm", "jlink"))

        inventory = _service(listing, []).validation_inventory()

        self.assertEqual(
            [probe.probe_id for probe in inventory.probes], ["aaa", "mmm", "zzz"]
        )

    def test_validation_serial_id_falls_back_to_the_port_path(self) -> None:
        ports = [_port("COM3", "SER1"), _port("COM4", None)]

        inventory = _service(_listing(("U1", "cmsisdap")), ports).validation_inventory()

        self.assertEqual(
            [item.serial_id for item in inventory.serial_ports], ["SER1", "COM4"]
        )

    def test_the_snapshot_adapter_round_trips_the_legacy_shape(self) -> None:
        inventory = ValidationInventory(
            probes=(ValidationProbe("U1", "Probe one", "jlink", "U1"),)
        )

        snapshot = snapshot_from_validation_inventory(inventory)
        again = validation_inventory_from(snapshot)

        self.assertEqual(again.probes[0].probe_id, "U1")
        self.assertEqual(again.probes[0].probe_family, "jlink")
        self.assertEqual(again.probes[0].usb_serial, "U1")

    def test_the_snapshot_adapter_marks_a_uid_less_probe_session_scoped(self) -> None:
        inventory = ValidationInventory(
            probes=(ValidationProbe("session:tok", "Live", "jlink", None),)
        )

        snapshot = snapshot_from_validation_inventory(inventory)

        self.assertEqual(snapshot.probes[0].identity_scope, "session")
        self.assertIsNone(snapshot.probes[0].unique_id)

    def test_every_row_in_one_snapshot_shares_its_snapshot_id(self) -> None:
        service = _service(
            _listing(("U1", "cmsisdap"), ("U2", "jlink")),
            [_port("COM3"), _port("COM4", "SER2")],
        )

        snapshot = service.snapshot()

        ids = {row.snapshot_id for row in snapshot.probes}
        ids |= {row.snapshot_id for row in snapshot.uarts}
        self.assertEqual(ids, {snapshot.snapshot_id})

    def test_row_ids_are_unique_within_one_snapshot(self) -> None:
        service = _service(
            _listing(("U1", "cmsisdap"), ("U2", "jlink")),
            [_port("COM3"), _port("COM4", "SER2")],
        )

        snapshot = service.snapshot()

        row_ids = [row.row_id for row in snapshot.probes]
        row_ids += [row.row_id for row in snapshot.uarts]
        self.assertEqual(len(row_ids), len(set(row_ids)))

    def test_two_snapshots_have_different_snapshot_ids(self) -> None:
        service = _service(_listing(("U1", "cmsisdap")), [])

        self.assertNotEqual(service.snapshot().snapshot_id, service.snapshot().snapshot_id)

    def test_probe_by_row_id_finds_only_its_own_row(self) -> None:
        snapshot = _service(_listing(("U1", "cmsisdap"), ("U2", "jlink")), []).snapshot()

        found = snapshot.probe_by_row_id(snapshot.probes[1].row_id)

        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.probe_id, "U2")
        self.assertIsNone(snapshot.probe_by_row_id("not-a-row"))


class JLinkRetryIsolationTests(unittest.TestCase):
    """Trap 7: a hook row must not change the UID-less J-Link retry verdict."""

    def test_the_retry_condition_consults_native_discovery_only(self) -> None:
        import inspect

        from pyocd_debug_mcp.adapters import swd_pyocd

        source = inspect.getsource(swd_pyocd._single_matching_probe_visible_for_board_family)

        self.assertIn("list_connected_probes_cli", source)
        self.assertIn("get_all_connected_probes", source)
        for forbidden in (
            "hardware_inventory",
            "HardwareInventoryService",
            "snapshot",
            "hook",
        ):
            self.assertNotIn(forbidden, source, f"{forbidden} reached the J-Link retry")

    def test_the_adapter_module_does_not_import_the_inventory_service(self) -> None:
        from pyocd_debug_mcp.adapters import swd_pyocd

        self.assertFalse(hasattr(swd_pyocd, "HardwareInventoryService"))
        self.assertFalse(hasattr(swd_pyocd, "_hardware_inventory"))

    def test_a_hook_discovered_matching_probe_row_does_not_change_the_retry_verdict(
        self,
    ) -> None:
        """FIX 5 (D4): the genuine behavioral counterpart to the static source scan above.

        Constructs a scenario with a real, executed hook that reports exactly one
        `jlink` probe -- which, if `_single_matching_probe_visible_for_board_family`
        consulted it, would make `matching == 1` true and flip the verdict. Native
        pyOCD/CLI discovery is held empty throughout (via the same mocking pattern
        `test_swd_process_isolation.py` uses), so the *only* difference between the
        two calls below is whether a hook-discovered row exists in the unified
        inventory at all. The verdict must be identical either way.
        """

        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from pyocd_debug_mcp.adapters import swd_pyocd
        from pyocd_debug_mcp.board_config import BoardConfig
        from pyocd_debug_mcp.target_errors import TargetConnectionError
        from tests.discovery_hook_fixtures import hook_entry, snapshot_for

        board = BoardConfig(
            board_id="board",
            display_name="Board",
            mcu_family="family",
            probe_family="jlink",
            pyocd_target="part",
            probe_type="jlink",
            probe_hint_terms=(),
            serial_hint_terms=(),
            test_addr=0,
        )

        def verdicts() -> tuple[bool, bool]:
            with (
                patch.object(
                    swd_pyocd.ConnectHelper,
                    "get_all_connected_probes",
                    side_effect=RuntimeError("no probes"),
                ),
                patch.object(swd_pyocd, "list_connected_probes_cli", return_value=[]),
            ):
                visible = swd_pyocd._single_matching_probe_visible_for_board_family(board)
                retry = swd_pyocd._should_retry_without_uid(
                    board, "683710208", TargetConnectionError("No emulator with serial number X")
                )
            return visible, retry

        # Control: no hook infrastructure touched at all.
        control_visible, control_retry = verdicts()

        self.assertFalse(control_visible)
        self.assertFalse(control_retry)

        # Same board, same native mocks, but this time a real hook actually ran and
        # discovered exactly one matching "jlink" probe through the unified service.
        with tempfile.TemporaryDirectory() as tmp:
            hooks = snapshot_for(
                Path(tmp), [hook_entry("jlink-hook", "probe", argv=["probe_provider", "jlink"])]
            )
            service = HardwareInventoryService(
                native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
                native_uarts=lambda: [],
                hook_snapshot=lambda: hooks,
            )
            snapshot = service.snapshot()

        # Confirm the hook genuinely ran and produced the row that -- if it were fed
        # into the retry logic -- would flip `matching == 1` to true.
        self.assertEqual([row.provider for row in snapshot.probes], ["jlink"])
        self.assertTrue(snapshot.probes[0].from_hook)

        with_hook_visible, with_hook_retry = verdicts()

        self.assertEqual(
            control_visible,
            with_hook_visible,
            "a hook-discovered probe row changed the J-Link retry visibility verdict",
        )
        self.assertEqual(
            control_retry,
            with_hook_retry,
            "a hook-discovered probe row changed the J-Link UID-less retry decision",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
