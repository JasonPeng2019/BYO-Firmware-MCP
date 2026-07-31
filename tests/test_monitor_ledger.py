"""The activity ledger: durability, the hash chain, hardening, and verification.

The ledger records *occasions* -- boot, a cumulative usage snapshot, a check-in, a
problem report, close -- and never one entry per tool call. These tests assert that
shape as well as the chain's behaviour.

The chain's guarantee is narrow and the tests say so: it detects localized
modification, and it is defeated entirely by recomputing the chain offline. These
tests assert both the detection it does provide and the limit it does not exceed.
"""

from __future__ import annotations

import inspect
import json
import sys
import unittest

from pyocd_debug_mcp.monitor.counters import USAGE_SNAPSHOT_CADENCE
from pyocd_debug_mcp.monitor.ledger import (
    Hardening,
    SegmentLedger,
    VerificationOutcome,
    file_identity,
    verify_file,
    verify_prior_runs,
)
from pyocd_debug_mcp.monitor.paths import resolve_store_root
from tests.monitor_support import MonitorTestCase


def add_snapshot(ledger: SegmentLedger, total: int = USAGE_SNAPSHOT_CADENCE, **extra) -> bool:
    """Append one usage-snapshot record, the ledger's most common occasion.

    There is no per-call form to append instead: the counts a snapshot carries are
    what answer "how much was used", so a test that wants a record in the file
    writes one of these.
    """

    detail = {"cumulative": True, "total_calls": total}
    detail.update(extra)
    return ledger.append("usage_snapshot", detail=detail)


def unharden(path) -> None:
    """Remove the append-only ACL so a test can rewrite the file.

    This is the threat model stated honestly: the file's owner can always undo
    any protection the server applied, because the server runs as the owner. The
    hardening stops stray scripts and accidental clobbering, not the owner.
    """

    if sys.platform != "win32":
        return
    import os
    import subprocess

    user = os.environ.get("USERNAME") or ""
    subprocess.run(
        ["icacls", str(path), "/remove:d", user, "/grant", f"{user}:(F)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )


class RecordsOccasionsNotCalls(MonitorTestCase):
    """The ledger has no per-call record, and structurally cannot grow one."""

    def setUp(self) -> None:
        super().setUp()
        self.store = resolve_store_root()
        self.ledger = SegmentLedger(self.store, "ws-a", "run-1")
        self.addCleanup(self.ledger.seal)

    def test_append_accepts_no_per_call_columns(self) -> None:
        """A generic envelope plus a kind-specific detail, and nothing else.

        Asserted against the signature rather than against behaviour because the
        absence is the requirement: if a ``tool=`` or ``outcome=`` parameter comes
        back, a per-call record has been reintroduced.
        """

        params = set(inspect.signature(self.ledger.append).parameters)
        self.assertEqual(params, {"kind", "detail"})
        for column in ("tool", "board", "args_fp", "outcome", "error_class", "duration_ms"):
            self.assertNotIn(column, params)

    def test_snapshot_carries_cumulative_counts(self) -> None:
        add_snapshot(
            self.ledger,
            total=200,
            per_tool={"get_state": 120, "read_memory": 80},
            per_outcome={"success": 190, "policy_refusal": 10},
            never_exercised=["flash"],
        )
        record = json.loads(self.ledger.resident_files()[0].read_text().splitlines()[0])
        self.assertEqual(record["kind"], "usage_snapshot")
        detail = record["detail"]
        self.assertTrue(detail["cumulative"])
        self.assertEqual(detail["total_calls"], 200)
        self.assertEqual(detail["per_tool"]["get_state"], 120)
        self.assertEqual(detail["per_outcome"]["policy_refusal"], 10)
        self.assertEqual(detail["never_exercised"], ["flash"])

    def test_a_later_snapshot_never_reports_less_than_an_earlier_one(self) -> None:
        """The anti-under-report property, at the record level.

        Counts are running totals, not per-window deltas, so a snapshot that is
        dropped or withheld cannot lower what the next one carries.
        """

        for total in (100, 200, 300):
            add_snapshot(self.ledger, total=total)
        totals = [
            json.loads(line)["detail"]["total_calls"]
            for line in self.ledger.resident_files()[0]
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(totals, [100, 200, 300])
        self.assertEqual(totals, sorted(totals))

    def test_all_five_occasion_kinds_append(self) -> None:
        for kind in ("boot", "usage_snapshot", "checkin", "report", "close"):
            self.assertTrue(self.ledger.append(kind, detail={"marker": kind}))
        kinds = [
            json.loads(line)["kind"]
            for line in self.ledger.resident_files()[0]
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(kinds, ["boot", "usage_snapshot", "checkin", "report", "close"])


class LedgerAppend(MonitorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = resolve_store_root()
        self.ledger = SegmentLedger(self.store, "ws-a", "run-1")
        self.addCleanup(self.ledger.seal)

    def test_records_are_appended(self) -> None:
        for index in range(10):
            self.assertTrue(add_snapshot(self.ledger, total=(index + 1) * 100))
        self.assertEqual(self.ledger.total_appended, 10)
        lines = self.ledger.resident_files()[0].read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 10)

    def test_refusals_are_visible_in_the_outcome_distribution(self) -> None:
        """Refused calls are counted, not individually logged.

        A policy refusal is normal output here, so it never earned its own durable
        record. It still has to be *countable*, which is what the snapshot's
        outcome distribution provides.
        """

        add_snapshot(self.ledger, per_outcome={"success": 90, "policy_refusal": 10})
        record = json.loads(self.ledger.resident_files()[0].read_text().splitlines()[0])
        self.assertEqual(record["detail"]["per_outcome"]["policy_refusal"], 10)

    def test_durability_comes_from_the_append(self) -> None:
        # Nothing is flushed at shutdown: the record must already be on disk with
        # no seal, roll, or close having run.
        add_snapshot(self.ledger, per_tool={"get_state": 100})
        text = self.ledger.resident_files()[0].read_text(encoding="utf-8")
        self.assertIn("get_state", text)

    def test_records_carry_their_run_identity(self) -> None:
        add_snapshot(self.ledger)
        record = json.loads(self.ledger.resident_files()[0].read_text().splitlines()[0])
        self.assertEqual(record["run_id"], "run-1")
        self.assertEqual(record["workspace"], "ws-a")

    def test_hardening_state_is_observable(self) -> None:
        add_snapshot(self.ledger)
        expected = (
            Hardening.APPLIED.value
            if sys.platform == "win32"
            else Hardening.UNSUPPORTED.value
        )
        self.assertEqual(self.ledger.hardening, expected)

    @unittest.skipUnless(sys.platform == "win32", "append-only ACL is Windows-only")
    def test_append_survives_hardening(self) -> None:
        # Hardening is applied after the handle is open. If it were applied first
        # the ledger could not write at all.
        for index in range(5):
            self.assertTrue(add_snapshot(self.ledger, total=(index + 1) * 100))
        self.assertEqual(self.ledger.hardening, Hardening.APPLIED.value)
        self.assertEqual(self.ledger.total_appended, 5)


class ChainIntegrity(MonitorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = resolve_store_root()
        self.ledger = SegmentLedger(self.store, "ws-a", "run-1")
        for index in range(6):
            add_snapshot(self.ledger, total=(index + 1) * 100)
        self.ledger.seal()
        self.path = self.ledger.sealed_segments()[0].path

    def test_untouched_chain_is_internally_consistent(self) -> None:
        # Reported as "impossible" rather than "verified": with no head published
        # off-box we cannot rule out a wholesale offline rewrite.
        self.assertIs(verify_file(self.path), VerificationOutcome.IMPOSSIBLE)

    def test_in_place_overwrite_is_denied(self) -> None:
        """Existing bytes cannot be rewritten without first changing permissions.

        Note the exact shape of the guarantee. Windows grants access at open
        time, so the handle the ledger already holds keeps appending (see
        ``test_appends_continue_after_hardening``), while any attempt to reopen
        the file for writing -- to overwrite it or to append to it -- is denied.
        The server never reopens a segment: roll and seal close it permanently
        and the next segment is a new file.
        """

        if sys.platform != "win32":
            self.skipTest("append-only ACL is Windows-only")
        with self.assertRaises(PermissionError):
            self.path.write_text("clobbered\n", encoding="utf-8")
        with self.assertRaises(PermissionError):
            self.path.open("a", encoding="utf-8")
        # Reading is unaffected, so verification and delivery still work.
        self.assertTrue(self.path.read_text(encoding="utf-8"))

    def test_appends_continue_after_hardening(self) -> None:
        """The live segment keeps accepting records once the ACL is applied."""

        ledger = SegmentLedger(self.store, "ws-live", "run-live")
        self.assertTrue(ledger.append("boot", detail={"marker": "first"}))
        applied = ledger.hardening
        for index in range(5):
            self.assertTrue(add_snapshot(ledger, total=(index + 1) * 100))
        self.assertEqual(ledger.total_appended, 6)
        if sys.platform == "win32":
            self.assertEqual(applied, Hardening.APPLIED.value)
        ledger.seal()

    def test_acked_file_can_still_be_deleted(self) -> None:
        """Delete-on-acknowledgement needs DELETE, not WRITE_DATA."""

        ledger = SegmentLedger(self.store, "ws-del", "run-del")
        add_snapshot(ledger)
        sealed = ledger.seal()
        assert sealed is not None
        sealed.path.unlink()
        self.assertFalse(sealed.path.exists())

    def test_edited_record_is_detected(self) -> None:
        unharden(self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[2])
        record["detail"]["total_calls"] = 1  # understate the usage count
        lines[2] = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertIs(verify_file(self.path), VerificationOutcome.CHAIN_INVALID)

    def test_deleted_record_is_detected(self) -> None:
        """A snapshot removed from the sequence leaves a detectable gap."""

        unharden(self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        del lines[3]
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertIs(verify_file(self.path), VerificationOutcome.CHAIN_INVALID)

    def test_appending_a_valid_record_is_not_flagged(self) -> None:
        ledger = SegmentLedger(self.store, "ws-b", "run-2")
        add_snapshot(ledger, total=100)
        add_snapshot(ledger, total=200)
        path = ledger.resident_files()[0]
        self.assertIs(verify_file(path), VerificationOutcome.IMPOSSIBLE)

    def test_offline_rewrite_with_recomputed_chain_is_not_detected(self) -> None:
        """The honest limit, asserted so nobody over-claims it.

        With the server stopped, a public hash, and no secret, an editor can
        understate a usage count and recompute every link after it. The result
        verifies. This is tier 2 of the under-report bound: detection requires an
        off-box witness, and none exists yet.
        """

        from pyocd_debug_mcp.monitor.ledger import _link

        unharden(self.path)
        lines = [json.loads(line) for line in self.path.read_text().splitlines()]
        lines[2]["detail"]["total_calls"] = 1
        prev = lines[0]["prev"]
        for record in lines:
            record["prev"] = prev
            record.pop("hash", None)
            record["hash"] = _link(prev, record)
            prev = record["hash"]
        self.path.write_text(
            "\n".join(
                json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for r in lines
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertIs(verify_file(self.path), VerificationOutcome.IMPOSSIBLE)


class Segments(MonitorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = resolve_store_root()
        self.ledger = SegmentLedger(self.store, "ws-a", "run-1")
        self.addCleanup(self.ledger.seal)

    def test_roll_seals_and_opens_a_successor(self) -> None:
        add_snapshot(self.ledger, total=100)
        sealed = self.ledger.roll()
        self.assertIsNotNone(sealed)
        add_snapshot(self.ledger, total=200)
        self.assertEqual(self.ledger.current_segment, 2)
        self.assertEqual(len(self.ledger.resident_files()), 2)

    def test_segment_carries_predecessor_head(self) -> None:
        add_snapshot(self.ledger, total=100)
        head_before = self.ledger.head
        self.ledger.roll()
        add_snapshot(self.ledger, total=200)
        second = self.ledger.resident_files()[1]
        first_record = json.loads(second.read_text().splitlines()[0])
        self.assertEqual(first_record["prev"], head_before)

    def test_a_segment_whose_predecessor_was_delivered_still_verifies(self) -> None:
        add_snapshot(self.ledger, total=100)
        first = self.ledger.roll()
        add_snapshot(self.ledger, total=200)
        self.ledger.seal()
        assert first is not None
        first.path.unlink()  # delivered and drained
        second = self.ledger.sealed_segments()[1].path
        self.assertIs(verify_file(second), VerificationOutcome.IMPOSSIBLE)

    def test_identity_is_stable_and_sortable(self) -> None:
        self.assertEqual(file_identity("ws", "run-1", 2), "ws:run-1:0002")
        self.assertLess(file_identity("ws", "r", 2), file_identity("ws", "r", 10))


class Verification(MonitorTestCase):
    def test_absent_run_is_never_a_finding(self) -> None:
        # Deletion after acknowledgement is the only cleanup mechanism, so an
        # empty store is the expected steady state rather than evidence of tamper.
        store = resolve_store_root()
        outcomes = verify_prior_runs(store, "ws-empty", "run-current")
        self.assertEqual(outcomes, {})

    def test_current_run_is_not_verified_against_itself(self) -> None:
        store = resolve_store_root()
        ledger = SegmentLedger(store, "ws-a", "run-current")
        add_snapshot(ledger)
        ledger.seal()
        self.assertEqual(verify_prior_runs(store, "ws-a", "run-current"), {})

    def test_prior_run_is_verified(self) -> None:
        store = resolve_store_root()
        old = SegmentLedger(store, "ws-a", "run-old")
        add_snapshot(old)
        old.seal()
        outcomes = verify_prior_runs(store, "ws-a", "run-new")
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(set(outcomes.values()), {VerificationOutcome.IMPOSSIBLE.value})


class BufferingStore(MonitorTestCase):
    def test_unavailable_store_never_raises(self) -> None:
        from pyocd_debug_mcp.monitor.paths import StoreRoot, StoreState

        buffering = StoreRoot(StoreState.BUFFERING, None)
        ledger = SegmentLedger(buffering, "ws", "run")
        self.assertFalse(add_snapshot(ledger))
        self.assertEqual(ledger.total_appended, 0)
        self.assertIsNone(ledger.seal())


class NoOverclaiming(unittest.TestCase):
    def test_untamperable_never_appears_in_the_source(self) -> None:
        """The word is banned: the property it implies is not achievable here."""

        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "src"
        offenders = [
            path.name
            for path in root.rglob("*.py")
            if "untamperable" in path.read_text(encoding="utf-8", errors="replace").lower()
        ]
        self.assertEqual(offenders, [])

    def test_the_under_report_ceiling_is_documented(self) -> None:
        """Tier 3 must be stated: owner-level forgery is not caught at all.

        A document that claims cumulative counts make usage unforgeable would be
        wrong, and this is where that claim would be made.
        """

        from pyocd_debug_mcp.monitor import ledger as ledger_module

        text = (ledger_module.__doc__ or "").lower()
        self.assertIn("under-report", text)
        self.assertIn("neither prevented nor detected", text)
        self.assertIn("not** unforgeable", text)


if __name__ == "__main__":
    unittest.main()
