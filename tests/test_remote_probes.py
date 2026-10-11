"""Tests for the remote-probe registry: module, inventory integration, and MCP tools.

Covers section 5 of REMOTE_PROBE_PLAN.md: the no-registration invariant, round-trip
registration through snapshot(), the selector surviving ProbeSelectionStore.resolve(),
re-registration updating in place rather than duplicating, unregistration, a malformed
registry file not crashing discovery, an unreachable endpoint still registering, host
and port validation, and remote rows staying visible alongside a native probe.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from pyocd_debug_mcp.firmstore.store import FirmLayout
from pyocd_debug_mcp.hardware_inventory import (
    HardwareInventoryService,
    ProbeSelection,
    ProbeSelectionStore,
)
from pyocd_debug_mcp.probe_inventory import (
    EMPTY_NATIVE_PROBE_LISTING,
    NativeProbeListing,
    ProbeInfo,
)
from pyocd_debug_mcp.remote_probes import (
    RemoteProbeEntry,
    RemoteProbeError,
    check_endpoint,
    load_remote_probes,
    normalize_host,
    normalize_port,
    register_entry,
    remove_remote_probe,
    save_remote_probes,
    upsert_remote_probe,
)
from pyocd_debug_mcp.services.connections import probe_connection_id
from pyocd_debug_mcp.tools.remote_probes import RemoteProbeToolServices, build_remote_probe_handlers


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


def _delayed_process_registration(
    registry: str,
    host: str,
    start: Any,
    failures: Any,
) -> None:
    """Widen the stale-snapshot window so a missing process lock loses writes."""

    from pyocd_debug_mcp import remote_probes

    original_upsert = remote_probes.upsert_remote_probe

    def delayed_upsert(*args, **kwargs):  # type: ignore[no-untyped-def]
        time.sleep(0.1)
        return original_upsert(*args, **kwargs)

    remote_probes.upsert_remote_probe = delayed_upsert
    try:
        if not start.wait(10.0):
            raise TimeoutError("process registration start signal was not received")
        register_entry(Path(registry), host, 5555, host)
    except BaseException as exc:  # noqa: BLE001 - returned to the parent assertion
        failures.put(repr(exc))


class _RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.registry_path = Path(self._directory.name) / "remote_probes.json"


# --------------------------------------------------------------------------------------
# remote_probes.py: normalization
# --------------------------------------------------------------------------------------


class NormalizationTests(unittest.TestCase):
    def test_an_empty_host_is_rejected(self) -> None:
        with self.assertRaises(RemoteProbeError):
            normalize_host("   ")
        for malformed in ("nul\0host", "localhost:5555", "bad host"):
            with self.subTest(host=malformed), self.assertRaises(RemoteProbeError):
                normalize_host(malformed)

    def test_a_real_host_is_stripped_of_surrounding_whitespace(self) -> None:
        self.assertEqual(normalize_host("  bench.local  "), "bench.local")

    def test_port_zero_is_rejected(self) -> None:
        with self.assertRaises(RemoteProbeError):
            normalize_port(0)

    def test_a_port_above_the_valid_range_is_rejected(self) -> None:
        with self.assertRaises(RemoteProbeError):
            normalize_port(65536)

    def test_a_non_integer_port_is_rejected(self) -> None:
        with self.assertRaises(RemoteProbeError):
            normalize_port("not-a-port")
        with self.assertRaises(RemoteProbeError):
            normalize_port(5555.9)
        with self.assertRaises(RemoteProbeError):
            normalize_port("9" * 5_000)

    def test_a_boolean_port_is_rejected(self) -> None:
        with self.assertRaises(RemoteProbeError):
            normalize_port(True)

    def test_boundary_ports_are_accepted(self) -> None:
        self.assertEqual(normalize_port(1), 1)
        self.assertEqual(normalize_port(65535), 65535)


# --------------------------------------------------------------------------------------
# remote_probes.py: load / save
# --------------------------------------------------------------------------------------


class LoadSaveTests(_RegistryCase):
    def test_a_missing_file_is_an_empty_tuple_not_an_error(self) -> None:
        self.assertEqual(load_remote_probes(self.registry_path), ())

    def test_round_trip_preserves_every_field(self) -> None:
        entries = (RemoteProbeEntry("bench.local", 5555, "bench ST-LINK", "2026-01-01T00:00:00Z"),)
        save_remote_probes(self.registry_path, entries)

        loaded = load_remote_probes(self.registry_path)

        self.assertEqual(loaded, entries)

    def test_malformed_json_loads_as_empty_rather_than_crashing(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text("{not json", encoding="utf-8")
        original = self.registry_path.read_bytes()

        self.assertEqual(load_remote_probes(self.registry_path), ())
        with self.assertRaisesRegex(RemoteProbeError, "refusing to overwrite"):
            register_entry(self.registry_path, "new.local", 5555, "new")
        self.assertEqual(self.registry_path.read_bytes(), original)

        self.registry_path.write_bytes(b"\xff")
        invalid_utf8 = self.registry_path.read_bytes()
        self.assertEqual(load_remote_probes(self.registry_path), ())
        with self.assertRaisesRegex(RemoteProbeError, "preserved"):
            register_entry(self.registry_path, "new.local", 5555, "new")
        self.assertEqual(self.registry_path.read_bytes(), invalid_utf8)

        invalid_documents = (
            '{"schema_version":true,"entries":[]}',
            '{"schema_version":1.0,"entries":[]}',
            '{"schema_version":1,"entries":[{"host":"a.local","port":"5555",'
            '"description":"","registered_at":""}]}',
            '{"schema_version":1,"schema_version":1,"entries":[]}',
            '{"schema_version":1,"entries":[{"host":"a.local","host":"b.local",'
            '"port":5555,"description":"","registered_at":""}]}',
            "[" * 20_000 + "]" * 20_000,
        )
        for invalid_document in invalid_documents:
            with self.subTest(invalid_document=invalid_document):
                self.registry_path.write_text(invalid_document, encoding="utf-8")
                original_invalid = self.registry_path.read_bytes()
                with self.assertRaisesRegex(RemoteProbeError, "refusing to overwrite"):
                    register_entry(self.registry_path, "new.local", 5555, "new")
                self.assertEqual(self.registry_path.read_bytes(), original_invalid)

    def test_a_bad_entry_is_skipped_but_a_good_sibling_entry_still_loads(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": 1,
            "entries": [
                {"host": "good.local", "port": 5555, "description": "", "registered_at": ""},
                {"host": "", "port": 5555},
                {"host": "bad-port.local", "port": 99999},
                "not-a-dict",
            ],
        }
        self.registry_path.write_text(json.dumps(document), encoding="utf-8")

        loaded = load_remote_probes(self.registry_path)

        self.assertEqual([entry.host for entry in loaded], ["good.local"])

    def test_save_creates_missing_parent_directories(self) -> None:
        nested = Path(self._directory.name) / "nested" / "dir" / "remote_probes.json"
        syncs: list[Path] = []

        with mock.patch(
            "pyocd_debug_mcp.remote_probes._fsync_directory",
            side_effect=syncs.append,
        ):
            save_remote_probes(nested, (RemoteProbeEntry("h", 1, "", ""),))

        self.assertTrue(nested.is_file())
        self.assertEqual(
            syncs,
            [
                Path(self._directory.name),
                Path(self._directory.name) / "nested",
                Path(self._directory.name) / "nested" / "dir",
            ],
        )

    def test_cross_process_registrations_do_not_lose_acknowledged_entries(self) -> None:
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        failures = context.Queue()
        hosts = [f"process-{index}.local" for index in range(6)]
        processes = [
            context.Process(
                target=_delayed_process_registration,
                args=(str(self.registry_path), host, start, failures),
            )
            for host in hosts
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(20.0)

        self.assertTrue(all(not process.is_alive() for process in processes))
        self.assertEqual([process.exitcode for process in processes], [0] * len(processes))
        reported_failures: list[str] = []
        while True:
            try:
                reported_failures.append(failures.get_nowait())
            except queue.Empty:
                break
        self.assertEqual(reported_failures, [])
        self.assertEqual(
            {entry.host for entry in load_remote_probes(self.registry_path)}, set(hosts)
        )

        # A rejected multiply-linked lock must not chmod or otherwise mutate its
        # external peer before validation.  (Hard links are unavailable on a few
        # filesystems, so retain the concurrency assertion above in that case.)
        hardlink_registry = Path(self._directory.name) / "hardlink.json"
        victim = Path(self._directory.name) / "external-lock-peer"
        victim.write_text("do not mutate\n", encoding="utf-8")
        victim.chmod(0o644)
        lock_path = hardlink_registry.with_name(f".{hardlink_registry.name}.lock")
        try:
            os.link(victim, lock_path)
        except OSError:
            return
        with self.assertRaisesRegex(OSError, "not a private regular file"):
            register_entry(hardlink_registry, "blocked.local", 5555, "blocked")
        self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o644)
        self.assertEqual(victim.read_text(encoding="utf-8"), "do not mutate\n")


# --------------------------------------------------------------------------------------
# remote_probes.py: upsert / remove
# --------------------------------------------------------------------------------------


class UpsertRemoveTests(unittest.TestCase):
    def test_registering_a_new_endpoint_appends_it(self) -> None:
        updated = upsert_remote_probe((), "bench.local", 5555, "first")

        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0].selector, "remote:bench.local:5555")

    def test_re_registering_the_same_endpoint_updates_rather_than_duplicates(self) -> None:
        first = upsert_remote_probe((), "bench.local", 5555, "first")
        second = upsert_remote_probe(first, "bench.local", 5555, "second")

        self.assertEqual(len(second), 1, "re-registering the same host:port duplicated the row")
        self.assertEqual(second[0].description, "second")

    def test_dedupe_is_case_insensitive_on_host(self) -> None:
        first = upsert_remote_probe((), "Bench.Local", 5555, "first")
        second = upsert_remote_probe(first, "bench.local", 5555, "second")

        self.assertEqual(len(second), 1)

    def test_different_ports_on_the_same_host_stay_distinct(self) -> None:
        first = upsert_remote_probe((), "bench.local", 5555, "a")
        second = upsert_remote_probe(first, "bench.local", 5556, "b")

        self.assertEqual(len(second), 2)

    def test_remove_deletes_the_matching_entry(self) -> None:
        entries = upsert_remote_probe((), "bench.local", 5555, "x")

        remaining, removed = remove_remote_probe(entries, "bench.local", 5555)

        self.assertTrue(removed)
        self.assertEqual(remaining, ())

    def test_remove_of_an_absent_entry_reports_false_and_changes_nothing(self) -> None:
        entries = upsert_remote_probe((), "other.local", 5555, "x")

        remaining, removed = remove_remote_probe(entries, "bench.local", 5555)

        self.assertFalse(removed)
        self.assertEqual(remaining, entries)


# --------------------------------------------------------------------------------------
# remote_probes.py: check_endpoint
# --------------------------------------------------------------------------------------


class CheckEndpointTests(unittest.TestCase):
    def test_a_listening_port_is_reachable(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        _, port = server.getsockname()

        self.assertTrue(check_endpoint("127.0.0.1", port, timeout_seconds=1.0))

    def test_a_closed_port_is_not_reachable(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        _, port = probe.getsockname()
        probe.close()  # closed immediately: very unlikely anything now listens on it

        self.assertFalse(check_endpoint("127.0.0.1", port, timeout_seconds=0.5))

        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")):
            for _ in range(12):
                self.assertFalse(check_endpoint("start-fails.example", 5555, 0.01))
        with mock.patch(
            "pyocd_debug_mcp.remote_probes.socket.create_connection",
            return_value=mock.MagicMock(),
        ):
            self.assertTrue(
                check_endpoint("slots-were-released.example", 5555, timeout_seconds=0.5)
            )

        def slow_resolution(*_args: Any, **_kwargs: Any) -> None:
            time.sleep(0.2)
            raise OSError("slow DNS")

        started = time.monotonic()
        with mock.patch(
            "pyocd_debug_mcp.remote_probes.socket.create_connection",
            side_effect=slow_resolution,
        ):
            self.assertFalse(check_endpoint("slow.example", 5555, timeout_seconds=0.01))
        self.assertLess(time.monotonic() - started, 0.1)


# --------------------------------------------------------------------------------------
# FirmLayout
# --------------------------------------------------------------------------------------


class FirmLayoutTests(unittest.TestCase):
    def test_remote_probes_is_a_plain_file_directly_under_the_firm_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = FirmLayout.for_project(Path(tmp))

            self.assertEqual(layout.remote_probes, layout.root / "remote_probes.json")
            # Not a hook: it must not live under the hook directory or be named as one.
            self.assertNotEqual(layout.remote_probes.parent, layout.discovery_hooks)


# --------------------------------------------------------------------------------------
# HardwareInventoryService integration
# --------------------------------------------------------------------------------------


class InventoryIntegrationTests(unittest.TestCase):
    def test_no_registration_invariant_no_remote_row_appears(self) -> None:
        """The single most important test: absent registration, zero behavior change."""

        service = HardwareInventoryService(
            native_probes=lambda: _listing(("U1", "cmsisdap")),
            native_uarts=lambda: [],
        )

        snapshot = service.snapshot()

        self.assertEqual(len(snapshot.probes), 1)
        self.assertNotIn("remote", {row.provider for row in snapshot.probes})

    def test_an_empty_registry_produces_identical_row_content_to_the_default(self) -> None:
        listing = _listing(("U1", "cmsisdap"))

        def _content(snapshot):
            return [
                (row.provider, row.probe_id, row.unique_id, row.description, row.provenance)
                for row in snapshot.probes
            ]

        default_service = HardwareInventoryService(
            native_probes=lambda: listing, native_uarts=lambda: []
        )
        explicit_empty_service = HardwareInventoryService(
            native_probes=lambda: listing, native_uarts=lambda: [], remote_probes=lambda: ()
        )

        self.assertEqual(
            _content(default_service.snapshot()), _content(explicit_empty_service.snapshot())
        )

    def test_a_registered_endpoint_appears_as_a_remote_provider_row(self) -> None:
        entries = upsert_remote_probe((), "bench.local", 5555, "bench ST-LINK")
        service = HardwareInventoryService(
            native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
            native_uarts=lambda: [],
            remote_probes=lambda: entries,
        )

        snapshot = service.snapshot()

        self.assertEqual(len(snapshot.probes), 1)
        row = snapshot.probes[0]
        self.assertEqual(row.provider, "remote")
        self.assertEqual(row.unique_id, "remote:bench.local:5555")
        self.assertEqual(row.stable_identity, "remote:bench.local:5555")
        self.assertEqual(row.identity_scope, "stable")

    def test_the_remote_selector_survives_selection_and_resolve_unmangled(self) -> None:
        """Extends the hook-based whole-pipe test: the direct-registration route must
        clear the same ProbeSelectionStore.resolve() choke point every real connect
        path goes through, with the two-colon selector intact byte-for-byte.
        """

        entries = upsert_remote_probe((), "bench.local", 5555, "")
        service = HardwareInventoryService(
            native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
            native_uarts=lambda: [],
            remote_probes=lambda: entries,
        )
        snapshot = service.snapshot()
        row = snapshot.probes[0]
        assert row.stable_identity is not None  # always set for a remote row
        token = probe_connection_id(row.provider, row.stable_identity)
        store = ProbeSelectionStore()
        store.record(ProbeSelection.from_row(token, row))

        resolved = store.resolve(token, service.snapshot())

        self.assertEqual(resolved.unique_id, "remote:bench.local:5555")
        self.assertEqual(resolved.provider, "remote")

    def test_a_corrupt_registry_file_does_not_crash_a_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "remote_probes.json"
            registry_path.write_text("{not json", encoding="utf-8")
            service = HardwareInventoryService(
                native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
                native_uarts=lambda: [],
                remote_probes=lambda: load_remote_probes(registry_path),
            )

            snapshot = service.snapshot()  # must not raise

            self.assertEqual(snapshot.probes, ())

    def test_remote_rows_stay_visible_alongside_a_native_probe(self) -> None:
        """The anti-gating test: a remote endpoint is an explicit registration, not
        fallback discovery, so it must not be hidden just because native discovery
        already found something.
        """

        entries = upsert_remote_probe((), "bench.local", 5555, "")
        service = HardwareInventoryService(
            native_probes=lambda: _listing(("U1", "cmsisdap")),
            native_uarts=lambda: [],
            remote_probes=lambda: entries,
        )

        snapshot = service.snapshot()

        self.assertEqual(len(snapshot.probes), 2)
        providers = {row.provider for row in snapshot.probes}
        self.assertEqual(providers, {"cmsisdap", "remote"})


# --------------------------------------------------------------------------------------
# MCP tool handlers
# --------------------------------------------------------------------------------------


class ToolHandlerTests(_RegistryCase):
    def _handlers(self, *, check_endpoint_result: bool = True):
        return build_remote_probe_handlers(
            RemoteProbeToolServices(
                registry_path=lambda: self.registry_path,
                check_endpoint=lambda host, port: check_endpoint_result,
            )
        )

    def test_register_writes_the_registry_and_reports_success(self) -> None:
        handlers = self._handlers()

        result = json.loads(handlers["register_remote_probe"]("bench.local", 5555, "bench"))

        self.assertEqual(result["status"], "remote_probe_registered")
        self.assertEqual(result["selector"], "remote:bench.local:5555")
        loaded = load_remote_probes(self.registry_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].host, "bench.local")

        with mock.patch(
            "pyocd_debug_mcp.remote_probes._fsync_directory",
            side_effect=OSError("directory sync failed"),
        ):
            uncertain = json.loads(
                handlers["register_remote_probe"]("uncertain.local", 5555, "uncertain")
            )
        self.assertEqual(uncertain["code"], "remote_probe/registry-durability-uncertain")
        self.assertNotIn("bytes were preserved", uncertain["agent_prompt"])
        self.assertIn(
            "uncertain.local", {entry.host for entry in load_remote_probes(self.registry_path)}
        )

    def test_a_reachable_endpoint_reports_reachable_true(self) -> None:
        handlers = self._handlers(check_endpoint_result=True)

        result = json.loads(handlers["register_remote_probe"]("bench.local", 5555))

        self.assertTrue(result["reachable"])

    def test_an_unreachable_endpoint_still_registers_and_says_so(self) -> None:
        handlers = self._handlers(check_endpoint_result=False)

        result = json.loads(handlers["register_remote_probe"]("bench.local", 5555, "x"))

        self.assertEqual(result["status"], "remote_probe_registered")
        self.assertFalse(result["reachable"])
        loaded = load_remote_probes(self.registry_path)
        self.assertEqual(len(loaded), 1, "an unreachable endpoint must still be registered")
        self.assertEqual(loaded[0].host, "bench.local")

    def test_re_registering_through_the_tool_updates_the_description_in_place(self) -> None:
        handlers = self._handlers()
        handlers["register_remote_probe"]("bench.local", 5555, "first")

        handlers["register_remote_probe"]("bench.local", 5555, "second")

        loaded = load_remote_probes(self.registry_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].description, "second")

    def test_register_rejects_an_empty_host_without_writing_the_registry(self) -> None:
        handlers = self._handlers()

        result = json.loads(handlers["register_remote_probe"](" ", 5555))

        self.assertEqual(result["status"], "remote_probe_rejected")
        self.assertEqual(load_remote_probes(self.registry_path), ())

    def test_register_rejects_an_out_of_range_port_without_writing_the_registry(self) -> None:
        handlers = self._handlers()

        result = json.loads(handlers["register_remote_probe"]("bench.local", 70000))

        self.assertEqual(result["status"], "remote_probe_rejected")
        self.assertEqual(load_remote_probes(self.registry_path), ())

    def test_unregister_rejects_an_empty_host(self) -> None:
        handlers = self._handlers()

        result = json.loads(handlers["unregister_remote_probe"](" ", 5555))

        self.assertEqual(result["status"], "remote_probe_rejected")

    def test_unregister_removes_a_registered_endpoint(self) -> None:
        handlers = self._handlers()
        handlers["register_remote_probe"]("bench.local", 5555, "bench")

        result = json.loads(handlers["unregister_remote_probe"]("bench.local", 5555))

        self.assertTrue(result["removed"])
        self.assertEqual(load_remote_probes(self.registry_path), ())

        handlers["register_remote_probe"]("uncertain.local", 5555, "bench")
        with mock.patch(
            "pyocd_debug_mcp.remote_probes._fsync_directory",
            side_effect=OSError("directory sync failed"),
        ):
            uncertain = json.loads(handlers["unregister_remote_probe"]("uncertain.local", 5555))
        self.assertEqual(uncertain["code"], "remote_probe/registry-durability-uncertain")
        self.assertNotIn("bytes were preserved", uncertain["agent_prompt"])
        self.assertEqual(load_remote_probes(self.registry_path), ())

    def test_unregistering_an_absent_endpoint_is_not_an_error(self) -> None:
        handlers = self._handlers()

        result = json.loads(handlers["unregister_remote_probe"]("bench.local", 5555))

        self.assertEqual(result["status"], "remote_probe_not_registered")
        self.assertFalse(result["removed"])

    def test_the_row_disappears_from_the_next_snapshot_after_unregistering(self) -> None:
        handlers = self._handlers()
        handlers["register_remote_probe"]("bench.local", 5555, "bench")
        service = HardwareInventoryService(
            native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
            native_uarts=lambda: [],
            remote_probes=lambda: load_remote_probes(self.registry_path),
        )
        self.assertEqual(len(service.snapshot().probes), 1)

        handlers["unregister_remote_probe"]("bench.local", 5555)

        self.assertEqual(service.snapshot().probes, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
