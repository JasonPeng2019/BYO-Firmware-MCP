"""Runner contracts for real failures, subtests and missing pinned prerequisites.

The small unittest/transport fixtures below test the observer. They are not native
analyzer provenance, real-model coverage, or a substitute for the complete pass.
"""

from __future__ import annotations

import argparse
import errno
import io
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock
import urllib.error

from tests import final_acceptance_suite as acceptance
from tests.process_cleanup_observations import (
    CleanupTrace,
    marker_facts,
    popen_facts,
    process_facts,
    role_evidence,
)

OBSERVATIONS = {}


class FinalAcceptanceLedgerContracts(unittest.TestCase):
    def release_observer(self):
        source = os.environ.get("BYO_FINAL_INSTALLER_SOURCE")
        if not source:
            self.skipTest("Actual frozen installer source is required for release-ledger contracts")
        path = Path(source) / "release/scripts/final_acceptance_checks.py"
        spec = importlib.util.spec_from_file_location("fr05_release_observer_contract", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def run_observed(self, case, directory):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
        loaded = list(acceptance.flatten(suite))
        events = directory / "events.jsonl"
        result = unittest.TextTestRunner(
            stream=io.StringIO(),
            resultclass=lambda *a, **kw: acceptance.LedgerResult(*a, ledger=events, **kw),
        ).run(suite)
        records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
        return loaded, result, records

    def test_subtest_failure_retains_parent_failure_and_actual_boundaries(self):
        class Case(unittest.TestCase):
            def test_body(self):
                for value in (1, 2):
                    with self.subTest(value=value):
                        self.assertEqual(value, 1)

        with tempfile.TemporaryDirectory() as temporary:
            loaded, result, events = self.run_observed(Case, Path(temporary))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(result.outcomes[loaded[0]], "FAIL")
        self.assertEqual([e["outcome"] for e in events if e["kind"] == "subtest"], ["PASS", "FAIL"])
        self.assertEqual([e["id"] for e in events if e["kind"] == "start"], loaded)
        self.assertEqual([e["id"] for e in events if e["kind"] == "stop"], loaded)

    def test_skipped_subtest_does_not_become_an_extra_method(self):
        class Case(unittest.TestCase):
            def test_body(self):
                with self.subTest(reason="fixture"):
                    self.skipTest("explicit subtest prerequisite")

        with tempfile.TemporaryDirectory() as temporary:
            loaded, result, events = self.run_observed(Case, Path(temporary))
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(set(result.outcomes), set(loaded))
        skips = [e for e in events if e["kind"] == "subtest"]
        self.assertEqual(skips[0]["outcome"], "SKIP")
        self.assertEqual(skips[0]["parent"], loaded[0])

    def test_class_setup_error_never_fabricates_method_success(self):
        class Case(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError("actual setup failure")

            def test_body(self):
                raise AssertionError("body must never run")

        with tempfile.TemporaryDirectory() as temporary:
            loaded, result, events = self.run_observed(Case, Path(temporary))
        self.assertFalse(result.wasSuccessful())
        self.assertTrue(loaded)
        self.assertFalse(set(loaded) & set(result.outcomes))
        self.assertFalse(any(e["kind"] == "start" for e in events))
        self.assertTrue(
            any(e["kind"] == "error" and "actual setup failure" in e["traceback"] for e in events)
        )

    def test_pack_network_error_retains_source_and_error_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(
                acceptance.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("fixture outage"),
            ):
                code = acceptance.acquire_official_pack(
                    root / acceptance.U5_FILENAME, root / "evidence"
                )
            receipt = json.loads(
                (root / "evidence/official-pack-acquisition.json").read_text(encoding="utf-8")
            )
        self.assertNotEqual(code, 0)
        self.assertEqual(receipt["source"], acceptance.U5_URL)
        self.assertEqual(receipt["expected_sha256"], acceptance.U5_SHA256)
        self.assertEqual(receipt["outcome"], "ERROR")
        self.assertIn("fixture outage", receipt["error"])

    def test_missing_inputs_record_each_independent_prerequisite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = argparse.Namespace(
                native_bundle=root / "missing-bundle",
                installer_source=root,
                arm_receipt=root / "missing-arm",
                official_pack_receipt=root / "missing-pack",
            )
            environment = {}
            facts = acceptance.prepare_native_inputs(args, root / "evidence", environment)
            retained = json.loads(
                (root / "evidence/native-prerequisites.json").read_text(encoding="utf-8")
            )
        self.assertEqual(set(facts["errors"]), {"bundle", "arm", "official_pack"})
        self.assertEqual(retained["outcome"], "ERROR")
        self.assertNotIn("BYO_TEST_CLANGD", environment)
        self.assertNotIn("BYO_MCP_TEST_CLANGD_EXECUTABLE", environment)
        self.assertNotIn("BYO_MCP_OFFICIAL_U5_PACK", environment)

    def test_libtest_multiple_targets_exclude_captured_output_from_method_records(self):
        observer = self.release_observer()
        # Observer fixture only: these names are not acceptance execution claims.
        raw = (
            "running 2 tests\ntest first::a ... ok\ntest first::b ... ignored, helper\n"
            "successes:\n---- first::a stdout ----\ntest fabricated::name ... ok\n"
            "test result: ok. 1 passed; 0 failed; 1 ignored\n"
            "running 1 test\ntest second::c ... FAILED\nfailures:\n"
            "test result: FAILED. 0 passed; 1 failed\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stdout.log"
            path.write_text(raw, encoding="utf-8")
            actual = observer.rust_ledger(path)
        self.assertEqual(
            [item["id"] for item in actual["starts"]], ["first::a", "first::b", "second::c"]
        )
        self.assertEqual([item["outcome"] for item in actual["stops"]], ["PASS", "SKIP", "FAIL"])
        self.assertFalse(actual["evidence_gaps"])

    def test_outer_libtest_success_cannot_hide_missing_b05_child_evidence(self):
        observer = self.release_observer()
        name = "project::b05_lifecycle_tests::legacy_archive_interruption_recovers_real_journal_in_fresh_process"
        children = {"records": [], "errors": []}
        gaps = observer.reconcile_rust_children({"starts": [{"id": name}]}, children)
        self.assertTrue(gaps)
        self.assertTrue(all("Missing actual child dispatch" in gap for gap in gaps))

    def test_cleanup_delegate_preserves_real_result_arguments_and_os_error(self):
        # Observer contract only, not evidence that any product cleanup passed.
        with tempfile.TemporaryDirectory() as temporary:
            trace = CleanupTrace(self.id(), temporary)
            self.addCleanup(
                lambda: OBSERVATIONS.setdefault(self.id(), {}).update(
                    observer_contract=trace.summary()
                )
            )
            buffer = io.BytesIO()
            payload = b"actual bytes\n"
            self.assertEqual(trace.invoke("stream.write", buffer.write, payload), len(payload))
            self.assertEqual(buffer.getvalue(), payload)
            caught = []

            def read_closed_descriptor(descriptor, length):
                try:
                    return os.read(descriptor, length)
                except OSError as exc:
                    caught.append(exc)
                    raise

            with self.assertRaises(OSError) as raised:
                trace.invoke("real.os.read", read_closed_descriptor, -1, 1)
            self.assertIs(raised.exception, caught[0])
            self.assertEqual(raised.exception.errno, errno.EBADF)
            rows = [
                json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [r["kind"] for r in rows],
                ["operation-start", "operation-return", "operation-start", "operation-error"],
            )
            self.assertEqual(rows[2]["args"], [-1, 1])
            self.assertEqual(rows[3]["error"]["errno"], errno.EBADF)
            self.assertIn("read_closed_descriptor", rows[3]["error"]["traceback"])
            self.assertFalse(trace.gaps)

    def test_process_and_marker_snapshots_do_not_reap_or_consume_protocol(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.readline()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        try:
            with tempfile.TemporaryDirectory() as temporary:
                marker = Path(temporary) / "marker.json"
                raw = b'{"exact":"marker bytes"}\n'
                marker.write_bytes(raw)
                with (
                    mock.patch.object(
                        child, "poll", side_effect=AssertionError("extra poll")
                    ) as poll,
                    mock.patch.object(
                        child, "wait", side_effect=AssertionError("extra wait")
                    ) as wait,
                    mock.patch.object(
                        child.stdout, "read", side_effect=AssertionError("extra read")
                    ) as read,
                ):
                    state = popen_facts(child)
                    identity = process_facts(child.pid)
                    observed_marker = marker_facts(marker)
                OBSERVATIONS[self.id()] = {
                    "process_snapshot": state,
                    "identity": identity,
                    "marker_snapshot": observed_marker,
                }
                poll.assert_not_called()
                wait.assert_not_called()
                read.assert_not_called()
                self.assertIsNone(state["cached_returncode"])
                self.assertEqual(identity["pid"], child.pid)
                self.assertEqual(identity["identity"], "observed")
                self.assertEqual(bytes.fromhex(observed_marker["prefix_hex"]), raw)
                self.assertEqual(marker.read_bytes(), raw)
        finally:
            child.stdin.write(b"finish\n")
            child.stdin.flush()
            child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()

    def test_role_join_requires_actual_registered_pid_and_birth(self):
        with tempfile.TemporaryDirectory() as temporary:
            trace = CleanupTrace(self.id(), temporary)
            facts = trace.role(
                os.getpid(), "observer-contract-process", "actual os.getpid in this contract"
            )
            exact = role_evidence(os.getpid(), facts["creation_time"], temporary)
            changed = role_evidence(os.getpid(), facts["creation_time"] + 1, temporary)
            missing_birth = role_evidence(os.getpid(), None, temporary)
            OBSERVATIONS[self.id()] = {
                "role_trace": trace.summary(),
                "exact": exact,
                "changed_birth": changed,
                "missing_birth": missing_birth,
            }
        self.assertEqual(len(exact["matches"]), 1)
        self.assertEqual(exact["matches"][0]["role"], "observer-contract-process")
        self.assertEqual(changed["matches"], [])
        self.assertEqual(missing_birth["matches"], [])
        self.assertIn("unknown", changed["role"])

    def test_real_interpreter_helper_and_module_exit_have_separate_boundaries(self):
        # Genuine spawn-context Event starts CPython's helper where that OS uses
        # one. This control measures its own lifecycle, never a historical Mac PID.
        script = r"""
import json
import multiprocessing
from multiprocessing import resource_tracker
import os
from pathlib import Path
import sys
from tests.process_cleanup_observations import CleanupTrace, process_facts

directory = Path(sys.argv[1])
trace = CleanupTrace("natural-interpreter-exit-control", directory / "cleanup-observations")
event = multiprocessing.get_context("spawn").Event()
pid = getattr(resource_tracker._resource_tracker, "_pid", None)
trace.record("actual-interpreter-helper-slot", helper_pid=pid, identity=process_facts(os.getpid()))
if pid is not None:
    trace.role(pid, "multiprocessing-resource-tracker", "actual Event control resource_tracker._resource_tracker._pid")
(directory / "control.json").write_text(json.dumps({"module_pid": os.getpid(), "helper_pid": pid,
    "boundary": "before normal interpreter exit; no emergency cleanup"}), encoding="utf-8")
"""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            try:
                code, outcome = acceptance.run_module(
                    [sys.executable, "-c", script, str(directory)], dict(os.environ), directory, 15
                )
            finally:
                # Retain actual raw receipts before temp cleanup, even when the
                # observed command or a subsequent contract assertion fails.
                OBSERVATIONS[self.id()] = {
                    name: marker_facts(directory / name)
                    for name in ("control.json", "module-exit.json", "stdout.log", "stderr.log")
                }
            control = json.loads((directory / "control.json").read_text(encoding="utf-8"))
            exit_record = json.loads((directory / "module-exit.json").read_text(encoding="utf-8"))
        self.assertEqual((code, outcome), (0, "PASS"))
        self.assertTrue(exit_record["wait_completed"])
        self.assertEqual(exit_record["popen"]["cached_returncode"], 0)
        self.assertEqual(exit_record["module_identity_at_start"]["pid"], control["module_pid"])
        if control["helper_pid"] is not None:
            observations = exit_record["descendants"]["seen_identities"]
            self.assertEqual([r["pid"] for r in observations], [control["helper_pid"]])
            self.assertIn("after_module_exit", observations[0])
        # This control intentionally has no suite cleanup ledger; the observer
        # must expose that gap, rather than fabricate an empty cleanup success.
        self.assertTrue(exit_record["descendants"]["evidence_gaps"])


if __name__ == "__main__":
    unittest.main()
