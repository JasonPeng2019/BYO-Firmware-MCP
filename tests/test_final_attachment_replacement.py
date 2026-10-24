"""VI05 production-callback regressions with persistent stores and real processes.

No probe, UART, compiler, or product build is invoked. Identity choices are
ordinary setup inputs; the cache, callback, transaction, and atomic writer are real.
"""

from __future__ import annotations

import errno
import functools
import json
import multiprocessing
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp.firmstore import cache as cache_module
from pyocd_debug_mcp.firmstore import store as store_module
from pyocd_debug_mcp.firmstore.cache import (
    AttachmentCache,
    AttachmentCacheError,
    ProbeIdentity,
    SerialEndpoint,
)
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.kernel import file_transactions
from pyocd_debug_mcp.setup_flow.preflight import (
    PreflightDecision,
    ProbeCandidate,
    SerialCandidate,
    SetupUserInput,
)
from tests.test_probe_selection_records import _snapshot, _uart_row
from tests.tiered_acceptance_support import tiered_test_server
from tests.process_cleanup_observations import CleanupTrace, process_facts, read_receipt

OBSERVATIONS = {}
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "pyocd_debug_mcp"
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-02-01T00:00:00Z"
P1 = ProbeIdentity("jlink", "probe1")
P2 = ProbeIdentity("jlink", "probe2")
C1 = SerialEndpoint("COM1", "cable1", 1, 2)
C2 = SerialEndpoint("COM2", "cable2", 1, 2)
C3 = SerialEndpoint("COM3", "cable3", 1, 2)


def confirm_setup(server, board, probe, cable):
    """Call the actual server callback with its real setup model types."""
    user = SetupUserInput(board, "chosen-probe", board, "fixture-mcu", 115200)
    decision = PreflightDecision(
        "preflight_ready",
        "preflight/ready",
        "confirmed fixture choice",
        selected_probe=ProbeCandidate(
            "chosen-probe", "probe", probe.probe_family, probe.usb_serial
        ),
        selected_serial=SerialCandidate(
            "chosen-uart",
            cable.port_path,
            "UART",
            cable.usb_serial,
            cable.vid,
            cable.pid,
        ),
        cache_confirmation_required=True,
    )
    server._confirm_setup_cache(user, decision)


def legacy_record(board, probe=P1, cable=C1, *, revoked=False):
    """Handwritten schema-v1 input; the current confirm writer cannot create conflicts."""
    return {
        "board_id": board,
        "probe_family": probe.probe_family,
        "probe_usb_serial": probe.usb_serial,
        "uart_usb_serial": cable.usb_serial,
        "uart_vid": cable.vid,
        "uart_pid": cable.pid,
        "confirmed": not revoked,
        "confirmed_at": T0,
        "revoked_at": T1 if revoked else None,
    }


def observe_writer(function):
    @functools.wraps(function)
    def observed(*args, **kwargs):
        trace = CleanupTrace("vi05-writer-" + args[1])
        trace.role(os.getpid(), "vi05-confirmation-writer", "executing process_confirm target")
        try:
            return trace.invoke("process_confirm", function, *args, **kwargs)
        finally:
            trace.record("writer-function-finally", identity=process_facts(os.getpid()),
                         evidence_gaps=trace.gaps,
                         boundary="target function exit; parent join records actual process exit")

    return observed


@observe_writer
def process_confirm(root, label, board, probe, cable, paused, release, contended, fail_publish):
    """Use real OS locking; hooks observe contention and stop inside the transaction."""
    root = Path(root)
    # Give each server its own monitor/lifecycle artifacts, while both real
    # attachment repositories intentionally address the same persistent file.
    with tiered_test_server(root / ("process-" + label)) as server:
        server._attachment_cache = AttachmentCache(FirmStore(root))
        cache = server._attachment_cache
        original_load = cache.load_records
        original_try = file_transactions._try_lock
        original_replace = store_module.os.replace
        reads = []
        failures = []

        def after_read():
            records = original_load()
            reads.append([record.to_document() for record in records])
            if paused is not None:
                paused.set()
                if not release.wait(20):
                    raise AssertionError("parent did not release the held transaction")
            return records

        def observe_lock(descriptor):
            try:
                original_try(descriptor)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    if contended is not None:
                        contended.set()
                raise

        def publication(source, destination):
            if fail_publish and Path(destination) == cache.path:
                failures.append(json.loads(Path(source).read_text(encoding="utf-8")))
                raise OSError("injected before atomic publication")
            return original_replace(source, destination)

        with (
            patch.object(cache, "load_records", after_read),
            patch.object(file_transactions, "_try_lock", observe_lock),
            patch.object(store_module.os, "replace", publication),
        ):
            try:
                confirm_setup(server, board, probe, cable)
            except OSError as exc:
                if not fail_publish or "injected before atomic publication" not in str(exc):
                    raise
            else:
                if fail_publish:
                    raise AssertionError("injected publication failure did not occur")
        proof = {
            "pid": os.getpid(),
            "test_module": __name__,
            "test_origin": str(Path(__file__).resolve()),
            "server_origin": server.__file__,
            "cache_origin": cache_module.__file__,
            "reads_under_transaction": reads,
            "failed_staged_documents": failures,
            "temporary_files_after": [p.name for p in cache.path.parent.glob("*.tmp")],
            "records_after_reload": [r.to_document() for r in original_load()],
        }
        (root / (label + "-process.json")).write_text(json.dumps(proof), encoding="utf-8")


class _StoreFixture(unittest.TestCase):
    def setUp(self):
        # The Actions suite supplies a private per-module evidence directory.
        # Direct unittest runs retain the original disposable-temp behavior.
        evidence = os.environ.get("BYO_FINAL_CASE_EVIDENCE")
        temporary_root = Path(evidence).resolve() if evidence else None
        if temporary_root is not None:
            temporary_root.mkdir(parents=True, exist_ok=True)
        self.root = Path(
            self.enterContext(
                tempfile.TemporaryDirectory(prefix="attachment-replacement-", dir=temporary_root)
            )
        ).resolve()
        self.server = self.enterContext(tiered_test_server(self.root))
        self.cache = self.server._attachment_cache
        self.assertEqual(Path(self.server.__file__).resolve(), SOURCE_ROOT / "server.py")
        self.assertEqual(
            Path(cache_module.__file__).resolve(), SOURCE_ROOT / "firmstore" / "cache.py"
        )

    def confirm(self, board, cable, probe=P1):
        confirm_setup(self.server, board, probe, cable)

    def reload(self):
        return AttachmentCache(FirmStore(self.root))

    def active(self):
        return [r for r in self.reload().load_records() if r.confirmed and r.revoked_at is None]

    def resolve(self, board, cables=(C1, C2, C3), probe=P1):
        return self.reload().resolve(board, probe, list(cables))

    def seed_legacy(self, *records):
        self.cache.store.atomic_write_json(
            self.cache.path, {"schema_version": 1, "records": list(records)}
        )

    def observe(self, name):
        OBSERVATIONS[self.id() + ":" + name] = [
            r.to_document() for r in self.reload().load_records()
        ]


class PersistentConfirmation(_StoreFixture):
    def test_moved_cable_retires_old_board_and_preserves_unrelated(self):
        self.confirm("board_a", C1)
        self.confirm("unrelated", C3, P2)
        unrelated = self.active()[1]
        self.confirm("board_b", C1, P2)
        old = self.resolve("board_a", (C1,))
        self.assertFalse(old.reused)
        self.assertEqual(old.reason, "revoked")
        self.assertEqual(self.resolve("board_b", probe=P2).port_path, "COM1")
        rows = self.reload().load_records()
        self.assertIn(unrelated, rows)
        retired = next(r for r in rows if r.board_id == "board_a")
        self.assertFalse(retired.confirmed)
        self.assertIsNotNone(retired.revoked_at)
        self.assertEqual(
            json.loads(self.cache.path.read_text(encoding="utf-8"))["schema_version"], 1
        )
        self.observe("move")

    def test_replacement_and_two_repeated_production_confirmations_are_idempotent(self):
        self.confirm("board_a", C1)
        self.confirm("board_a", C2)
        before = self.cache.path.read_bytes()
        with patch.object(self.cache, "_write", wraps=self.cache._write) as writer:
            for _ in range(2):
                self.confirm("board_a", C2)
                self.assertEqual(self.resolve("board_a").port_path, "COM2")
                self.assertEqual(self.cache.path.read_bytes(), before)
            writer.assert_not_called()
        rows = self.reload().load_records()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r.uart_usb_serial for r in rows if r.confirmed], ["cable2"])
        self.observe("repeated")

    def test_saved_stable_identity_survives_port_renumbering_without_write(self):
        self.confirm("board_a", C1)
        before = self.cache.path.read_bytes()
        renumbered = replace(C1, port_path="COM81")
        self.assertEqual(self.resolve("board_a", (renumbered, C2)).port_path, "COM81")
        self.confirm("board_a", renumbered)
        self.assertEqual(self.cache.path.read_bytes(), before)
        self.assertNotIn("COM", self.cache.path.read_text(encoding="utf-8"))

    def test_discovery_does_not_retire_an_absent_cable_or_create_new_association(self):
        self.confirm("board_a", C1)
        before = self.cache.path.read_bytes()
        for board, cables, expected in (
            ("board_a", (C2,), "hardware_changed"),
            ("board_b", (C1,), "no_record"),
            ("board_a", (C1, C2), "exact_match"),
        ):
            with self.subTest(board=board, expected=expected):
                self.assertEqual(self.resolve(board, cables).reason, expected)
                self.assertEqual(self.cache.path.read_bytes(), before)

    def test_retired_cable_never_reactivates_when_current_cable_disappears(self):
        self.confirm("board_a", C1)
        self.confirm("board_a", C2)
        before = self.cache.path.read_bytes()
        for cables in ((C1,), (), (C1, C2), (C1,)):
            result = self.resolve("board_a", cables)
            if C2 in cables:
                self.assertEqual(result.port_path, "COM2")
            else:
                self.assertFalse(result.reused)
            self.assertEqual(self.cache.path.read_bytes(), before)

    def test_explicit_confirmation_can_reclaim_retired_cable(self):
        self.confirm("board_a", C1)
        self.confirm("board_a", C2)
        self.confirm("board_a", C1)
        self.assertEqual(self.resolve("board_a").port_path, "COM1")
        self.assertEqual(len(self.reload().load_records()), 2)
        self.assertEqual([r.uart_usb_serial for r in self.active()], ["cable1"])

    def test_previously_revoked_unrelated_history_is_preserved(self):
        self.confirm("history", C3, P2)
        self.cache.revoke("history", P2, C3, revoked_at=T1)
        history = self.reload().load_records()[0]
        self.confirm("board_a", C1)
        self.confirm("board_a", C2)
        self.confirm("board_b", C1, P2)
        self.assertIn(history, self.reload().load_records())
        self.assertEqual(self.resolve("history", (C3,), P2).reason, "revoked")

    def test_different_probe_pair_and_different_uart_usb_ids_remain_unrelated(self):
        distinct_uart = replace(C1, port_path="COM91", vid=99)
        self.confirm("board_a", C1)
        self.confirm("board_a", C3, P2)
        self.confirm("other_board", distinct_uart, P2)
        preserved = self.reload().load_records()[1:]
        self.confirm("board_a", C2)
        for record in preserved:
            self.assertIn(record, self.active())
        self.assertEqual(self.resolve("board_a", probe=P2).port_path, "COM3")
        self.assertEqual(self.resolve("other_board", (distinct_uart,), P2).port_path, "COM91")

    def test_same_uart_moved_to_new_probe_for_same_board_retires_previous_probe(self):
        self.confirm("board_a", C1)
        self.confirm("board_a", C1, P2)
        self.assertEqual(self.resolve("board_a", probe=P1).reason, "revoked")
        self.assertEqual(self.resolve("board_a", probe=P2).port_path, "COM1")

    def test_legacy_multi_uart_refuses_even_with_only_one_visible_until_confirmation(
        self,
    ):
        self.seed_legacy(legacy_record("board_a"), legacy_record("board_a", cable=C2))
        before = self.cache.path.read_bytes()
        for cables in ((C1,), (C2,), (C1, C2), ()):
            with self.subTest(cables=cables):
                result = self.resolve("board_a", cables)
                self.assertFalse(result.reused)
                self.assertEqual(result.reason, "multiple_matches")
                self.assertEqual(self.cache.path.read_bytes(), before)
        self.confirm("board_a", C2)
        self.assertEqual(self.resolve("board_a").port_path, "COM2")
        self.assertEqual(self.resolve("board_a", (C1,)).reason, "revoked")
        self.observe("legacy-replacement")

    def test_legacy_cross_board_uart_claims_refuse_until_explicit_existing_choice(self):
        self.seed_legacy(legacy_record("board_a"), legacy_record("board_b", P2))
        before = self.cache.path.read_bytes()
        for board, probe in (("board_a", P1), ("board_b", P2)):
            self.assertEqual(self.resolve(board, (C1,), probe).reason, "multiple_matches")
        self.assertEqual(self.cache.path.read_bytes(), before)
        self.confirm("board_b", C1, P2)
        self.assertEqual(self.resolve("board_b", probe=P2).port_path, "COM1")
        self.assertEqual(self.resolve("board_a").reason, "revoked")
        self.observe("legacy-move")

    def test_legacy_duplicate_exact_rows_refuse_until_confirmation_deduplicates(self):
        self.seed_legacy(legacy_record("board_a"), legacy_record("board_a"))
        self.assertEqual(self.resolve("board_a").reason, "multiple_matches")
        self.confirm("board_a", C1)
        self.assertEqual(len(self.reload().load_records()), 1)
        self.assertEqual(self.resolve("board_a").port_path, "COM1")

    def test_legacy_existing_exact_choice_still_retires_conflicting_uart(self):
        self.seed_legacy(legacy_record("board_a"), legacy_record("board_a", cable=C2))
        self.confirm("board_a", C1)
        self.assertEqual(self.resolve("board_a").port_path, "COM1")
        self.assertEqual(self.resolve("board_a", (C2,)).reason, "revoked")

    def test_production_resolver_refuses_retired_association_and_resolves_new_pair(
        self,
    ):
        self.confirm("board_a", C1)
        self.confirm("board_b", C1, P2)
        snapshot = _snapshot(uarts=(_uart_row("COM81", scope="stable", serial="cable1"),))
        with self.assertRaisesRegex(RuntimeError, "saved-adapter-unavailable.*revoked"):
            self.server._resolve_recorded_uart("board_a", P1, snapshot)
        self.assertEqual(
            self.server._resolve_recorded_uart("board_b", P2, snapshot).device, "COM81"
        )

    def test_invalid_or_missing_stable_setup_selection_cannot_retire_records(self):
        self.confirm("board_a", C1)
        before = self.cache.path.read_bytes()
        for probe, cable in (
            (ProbeIdentity("jlink", None), C2),
            (P1, replace(C2, vid=None)),
        ):
            confirm_setup(self.server, "board_a", probe, cable)
            self.assertEqual(self.cache.path.read_bytes(), before)
        user = SetupUserInput("board_a", "chosen", "board", "fixture", 115200)
        self.server._confirm_setup_cache(user, PreflightDecision("preflight_ready", "ready", ""))
        self.assertEqual(self.cache.path.read_bytes(), before)
        with self.assertRaises(AttachmentCacheError):
            self.cache.confirm("board_a", P1, replace(C2, usb_serial=None))
        self.assertEqual(self.cache.path.read_bytes(), before)

    def test_failure_before_atomic_publication_preserves_old_bytes_and_cleans_staging(
        self,
    ):
        self.confirm("board_a", C1)
        self.confirm("unrelated", C3, P2)
        before = self.cache.path.read_bytes()
        observed = []
        original_replace = store_module.os.replace

        def fail(source, destination):
            if Path(destination) == self.cache.path:
                staged = json.loads(Path(source).read_text(encoding="utf-8"))
                observed.append(staged)
                # A separate cache reader still sees the complete old state.
                self.assertEqual(self.reload().path.read_bytes(), before)
                self.assertEqual(self.resolve("board_a").port_path, "COM1")
                raise OSError("injected before atomic publication")
            return original_replace(source, destination)

        with patch.object(store_module.os, "replace", fail):
            with self.assertRaisesRegex(OSError, "injected before atomic publication"):
                self.confirm("board_b", C1, P2)
        self.assertEqual(len(observed), 1)
        staged = {r["board_id"]: r for r in observed[0]["records"]}
        self.assertFalse(staged["board_a"]["confirmed"])
        self.assertTrue(staged["board_b"]["confirmed"])
        self.assertEqual(self.cache.path.read_bytes(), before)
        self.assertEqual(list(self.cache.path.parent.glob("*.tmp")), [])
        self.confirm("board_b", C1, P2)
        self.assertEqual(self.resolve("board_a").reason, "revoked")
        self.assertEqual(self.resolve("board_b", probe=P2).port_path, "COM1")
        OBSERVATIONS[self.id()] = {"staged": observed, "original_bytes_unchanged": True}

    def test_failed_initial_publication_does_not_leave_an_active_record(self):
        with patch.object(store_module.os, "replace", side_effect=OSError("publication failed")):
            with self.assertRaisesRegex(OSError, "publication failed"):
                self.confirm("board_a", C1)
        self.assertEqual(self.reload().load_records(), [])
        self.assertFalse(self.cache.path.exists())
        self.assertEqual(list(self.cache.path.parent.glob("*.tmp")), [])
        self.confirm("board_a", C1)
        self.assertEqual(self.resolve("board_a").port_path, "COM1")

    def test_explicit_confirmation_timestamp_is_retained_and_repeat_is_stable(self):
        first = self.cache.confirm("board_a", P1, C1, confirmed_at=T0)
        self.assertEqual(first.confirmed_at, T0)
        before = self.cache.path.read_bytes()
        self.assertEqual(self.cache.confirm("board_a", P1, C1, confirmed_at=T0), first)
        self.confirm("board_a", C1)
        self.assertEqual(self.cache.path.read_bytes(), before)
        refreshed = self.cache.confirm("board_a", P1, C1, confirmed_at=T1)
        self.assertEqual(refreshed.confirmed_at, T1)


class TwoProcessConfirmation(_StoreFixture):
    def setUp(self):
        super().setUp()
        self.context = multiprocessing.get_context("spawn")
        self.children = []
        self.releases = []
        self.cleanup_trace = CleanupTrace(self.id())
        self.child_identities = {}
        self.helper_identity = None
        # LIFO: preserve the final observations after finish_children, even if
        # a writer assertion or one of its existing cleanup operations raises.
        self.addCleanup(self.retain_cleanup_observations)
        self.addCleanup(self.finish_children)

    def retain_cleanup_observations(self):
        self.observe_interpreter_helper("after writer cleanup")
        retained = OBSERVATIONS.setdefault(self.id(), {})
        retained["writer_cleanup"] = self.cleanup_trace.summary()
        retained["writer_receipts_even_on_failure"] = [
            read_receipt(self.root / (label + "-process.json")) for label in ("first", "second")
        ]

    def observe_interpreter_helper(self, boundary):
        # This exact CPython object slot is evidence of a role. A Python argv
        # or an arbitrary descendant PID is never enough to assign that role.
        from multiprocessing import resource_tracker

        tracker = getattr(resource_tracker, "_resource_tracker", None)
        pid = getattr(tracker, "_pid", None)
        self.cleanup_trace.record("interpreter-helper-slot", boundary=boundary, pid_observed=pid,
                                  source="multiprocessing.resource_tracker._resource_tracker._pid")
        if pid is not None:
            expected = (self.helper_identity.get("creation_time") if self.helper_identity is not None
                        and self.helper_identity["pid"] == pid else None)
            facts = self.cleanup_trace.role(pid, "multiprocessing-resource-tracker",
                                            "actual resource_tracker._resource_tracker._pid at " + boundary,
                                            expected_birth=expected)
            if self.helper_identity is None and facts.get("creation_time") is not None:
                self.helper_identity = {"pid": pid, "creation_time": facts["creation_time"]}

    def child_boundary(self, child, boundary):
        known = self.child_identities.get(id(child), {})
        popen = child.__dict__.get("_popen")
        self.cleanup_trace.record(
            "writer-parent-boundary", boundary=boundary, known_identity=known,
            cached_returncode=None if popen is None else popen.returncode,
            process_object_closed=child.__dict__.get("_closed"),
            identity=(process_facts(known["pid"], known["creation_time"])
                      if known.get("creation_time") is not None else {"gap": "Writer birth unavailable; no PID-only attribution"}),
        )

    def child_call(self, child, name, *args):
        self.child_boundary(child, name + ":enter")
        try:
            return self.cleanup_trace.invoke("writer." + name, getattr(child, name), *args)
        finally:
            self.child_boundary(child, name + ":finally")

    def finish_children(self):
        for event in self.releases:
            self.cleanup_trace.invoke("teardown.release.set", event.set)
        for child in self.children:
            self.child_call(child, "join", 5)
            if self.child_call(child, "is_alive"):
                self.child_call(child, "terminate")
                self.child_call(child, "join", 5)
            if self.child_call(child, "is_alive"):
                self.child_call(child, "kill")
                self.child_call(child, "join", 5)
            self.child_call(child, "close")

    def run_pair(self, first, second, *, failure=False):
        paused = self.context.Event()
        release = self.context.Event()
        contended = self.context.Event()
        self.observe_interpreter_helper("after actual Event construction")
        self.releases.append(release)
        before = self.cache.path.read_bytes()
        for label, choice, pause, contention, fail in (
            ("first", first, paused, None, failure),
            ("second", second, None, contended, False),
        ):
            child = self.context.Process(
                target=process_confirm,
                args=(str(self.root), label, *choice, pause, release, contention, fail),
            )
            self.cleanup_trace.invoke("writer.start", child.start)
            self.children.append(child)
            identity = self.cleanup_trace.role(child.pid, "vi05-confirmation-writer",
                                               "actual spawned Process.pid; label=" + label)
            self.child_identities[id(child)] = {"pid": child.pid, "creation_time": identity.get("creation_time"),
                                                "label": label, "target": "process_confirm"}
            self.child_boundary(child, "after start")
            if label == "first":
                self.assertTrue(paused.wait(20), "first process did not read inside transaction")
        self.assertTrue(contended.wait(20), "second process did not hit the held native lock")
        self.assertEqual(self.cache.path.read_bytes(), before)
        self.cleanup_trace.invoke("transaction.release.set", release.set)
        for child in self.children:
            self.child_call(child, "join", 25)
            self.assertEqual(child.exitcode, 0, f"writer {child.pid} did not finish cleanly")
        proof = [
            json.loads((self.root / (label + "-process.json")).read_text(encoding="utf-8"))
            for label in ("first", "second")
        ]
        self.assertEqual(len({record["pid"] for record in proof}), 2)
        self.assertNotIn(os.getpid(), [record["pid"] for record in proof])
        expected_module = "__mp_main__" if __name__ == "__main__" else __name__
        for record in proof:
            self.assertEqual(record["temporary_files_after"], [])
            self.assertEqual(record["test_module"], expected_module)
            self.assertEqual(Path(record["test_origin"]).resolve(), Path(__file__).resolve())
            self.assertEqual(Path(record["server_origin"]).resolve(), SOURCE_ROOT / "server.py")
            self.assertEqual(
                Path(record["cache_origin"]).resolve(),
                SOURCE_ROOT / "firmstore" / "cache.py",
            )
        OBSERVATIONS[self.id()] = {
            "os_lock_contention_observed": True,
            "processes": proof,
        }
        return proof

    def seed_unrelated(self):
        self.confirm("unrelated", C3, P2)
        self.unrelated = self.active()[0]

    def test_two_process_board_move_serializes_and_retires_first_owner(self):
        self.seed_unrelated()
        self.run_pair(("board_a", P1, C1), ("board_b", P2, C1))
        self.assertEqual(self.resolve("board_a").reason, "revoked")
        self.assertEqual(self.resolve("board_b", probe=P2).port_path, "COM1")
        self.assertIn(self.unrelated, self.active())
        self.assertEqual(len(self.active()), 2)

    def test_two_process_same_pair_replacement_keeps_only_last_uart(self):
        self.seed_unrelated()
        self.run_pair(("board_a", P1, C1), ("board_a", P1, C2))
        self.assertEqual(self.resolve("board_a").port_path, "COM2")
        self.assertEqual(self.resolve("board_a", (C1,)).reason, "revoked")
        self.assertIn(self.unrelated, self.active())
        before = self.cache.path.read_bytes()
        for _ in range(2):
            self.confirm("board_a", C2)
        self.assertEqual(self.cache.path.read_bytes(), before)

    def test_two_process_nonconflicting_updates_and_revoked_history_survive(self):
        self.seed_unrelated()
        self.confirm("history", replace(C3, usb_serial="retired"), P2)
        self.cache.revoke("history", P2, replace(C3, usb_serial="retired"), revoked_at=T1)
        history = self.reload().load_records()[-1]
        self.run_pair(("board_a", P1, C1), ("board_b", P2, C2))
        self.assertEqual(self.resolve("board_a").port_path, "COM1")
        self.assertEqual(self.resolve("board_b", probe=P2).port_path, "COM2")
        self.assertIn(self.unrelated, self.active())
        self.assertIn(history, self.reload().load_records())
        self.assertEqual(len(self.active()), 3)

    def test_two_process_publication_failure_releases_lock_without_partial_retirement(
        self,
    ):
        self.seed_unrelated()
        self.confirm("board_a", C1)
        proof = self.run_pair(("board_a", P1, C2), ("board_b", P2, C1), failure=True)
        self.assertEqual(len(proof[0]["failed_staged_documents"]), 1)
        second_read = proof[1]["reads_under_transaction"][0]
        old = next(r for r in second_read if r["board_id"] == "board_a")
        self.assertTrue(old["confirmed"])
        self.assertEqual(old["uart_usb_serial"], "cable1")
        self.assertEqual(self.resolve("board_a").reason, "revoked")
        self.assertEqual(self.resolve("board_b", probe=P2).port_path, "COM1")
        self.assertFalse(any(r.uart_usb_serial == "cable2" for r in self.reload().load_records()))
        self.assertIn(self.unrelated, self.active())


if __name__ == "__main__":
    unittest.main(verbosity=2)
