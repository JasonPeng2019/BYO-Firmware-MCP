"""Runner contracts for real failures, subtests and missing pinned prerequisites.

The small unittest/transport fixtures below test the observer. They are not native
analyzer provenance, real-model coverage, or a substitute for the complete pass.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
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


class FinalAcceptanceNativeInputContracts(unittest.TestCase):
    """Real Actions-built bundle admission; never fabricate native payloads."""

    def setUp(self):
        self.assertEqual(os.environ.get("GITHUB_ACTIONS"), "true", "Actions-only control")
        source = os.environ.get("BYO_FINAL_INSTALLER_SOURCE")
        if not source:
            self.skipTest("Actual frozen installer source is required for native-input contracts")
        self.installer = Path(source).resolve(strict=True)
        system = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[platform.system()]
        arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}[
            platform.machine().casefold()
        ]
        # Discover the real build independently of the selectors being tested:
        # a broken admission must fail this control, rather than cause a skip.
        manifests = [
            path
            for path in (self.installer / "release/dist").glob("*/release-manifest.json")
            if (
                (document := json.loads(path.read_bytes()))["platform"],
                document["architecture"],
            ) == (system, arch)
        ]
        if not manifests:
            self.skipTest(
                "Actual host-native built bundle is unavailable; native admission remains UNRUN"
            )
        self.assertEqual(len(manifests), 1, "Native-input control requires one exact host bundle")
        self.bundle = manifests[0].parent.resolve(strict=True)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        retained = Path(os.environ.get("BYO_FINAL_CASE_EVIDENCE", str(self.root)))
        self.evidence = retained / "fr08-native-inputs" / self._testMethodName

    def copied_bundle(self):
        copied = self.root / "native bundle"
        copied.mkdir()
        shutil.copytree(self.bundle / "analysis", copied / "analysis")
        for name in ("release-manifest.json", "sbom.cdx.json"):
            shutil.copyfile(self.bundle / name, copied / name)
        return copied

    def rewrite_inventoried_json(self, bundle, relative, document):
        path = bundle / relative
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        manifest = json.loads((bundle / "release-manifest.json").read_bytes())
        row = next(row for row in manifest["files"] if row["path"] == relative)
        row.update(size=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        (bundle / "release-manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    def prepare_in_clean_interpreter(self, bundle):
        script = r"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

source, bundle, installer, evidence = map(Path, sys.argv[1:])
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("fr08_actual_native_preparation", source)
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)
environment = {}
args = argparse.Namespace(native_bundle=bundle, installer_source=installer,
    arm_receipt=evidence / "missing-arm", official_pack_receipt=evidence / "missing-pack")
facts = acceptance.prepare_native_inputs(args, evidence, environment)
record = environment.get("BYO_MANAGED_TEST_INPUT_RECORD")
acceptance.write(evidence / "control-result.json", {
    "facts": facts, "selectors": environment,
    "native_record": json.loads(Path(record).read_bytes()) if record else None,
})
"""
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        command = [
            sys.executable, "-I", "-B", "-c", script, str(Path(acceptance.__file__).resolve()),
            str(bundle), str(self.installer), str(self.evidence),
        ]
        completed = subprocess.run(
            command, cwd=self.root, env=environment, capture_output=True, text=True, timeout=120
        )
        self.evidence.mkdir(parents=True, exist_ok=True)
        (self.evidence / "stdout.log").write_text(completed.stdout, encoding="utf-8")
        (self.evidence / "stderr.log").write_text(completed.stderr, encoding="utf-8")
        OBSERVATIONS[self.id()] = {
            "argv": command, "exit": completed.returncode,
            "evidence": str(self.evidence), "pythonpath_removed": True,
        }
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads((self.evidence / "control-result.json").read_bytes())
        OBSERVATIONS[self.id()]["result"] = result
        # These deliberately absent independent receipts must remain errors,
        # even when the bundle itself admits and publishes its selectors.
        self.assertIn("arm", result["facts"]["errors"])
        self.assertIn("official_pack", result["facts"]["errors"])
        self.assertEqual(result["facts"]["outcome"], "ERROR")
        return result

    def assert_bundle_refused(self, result):
        self.assertEqual(result["facts"]["bundle"]["outcome"], "ERROR")
        self.assertEqual(set(result["facts"]["errors"]), {"bundle", "arm", "official_pack"})
        for name in (
            "BYO_TEST_CLANGD", "BYO_MCP_TEST_CLANGD_EXECUTABLE",
            "BYO_MANAGED_TEST_INPUT_RECORD", "BYO_MANAGED_TEST_INPUT_RECORD_SHA256",
        ):
            self.assertNotIn(name, result["selectors"])
        self.assertIsNone(result["native_record"])

    def test_real_built_bundle_publishes_exact_native_selectors(self):
        result = self.prepare_in_clean_interpreter(self.bundle)
        self.assertEqual(result["facts"]["bundle"]["outcome"], "PASS")
        self.assertEqual(set(result["facts"]["errors"]), {"arm", "official_pack"})
        clangd = result["facts"]["bundle"]["clangd"]
        self.assertEqual(result["selectors"]["BYO_TEST_CLANGD"], clangd)
        self.assertEqual(result["selectors"]["BYO_MCP_TEST_CLANGD_EXECUTABLE"], clangd)
        self.assertEqual(result["facts"]["bundle"]["clangd_sha256"], acceptance.digest(clangd))
        self.assertIn(
            str(self.bundle / "analysis/runtime.json"), result["facts"]["protected_inputs"]
        )
        if os.name == "nt":
            record = Path(result["selectors"]["BYO_MANAGED_TEST_INPUT_RECORD"])
            self.assertEqual(
                result["selectors"]["BYO_MANAGED_TEST_INPUT_RECORD_SHA256"],
                acceptance.digest(record),
            )
            self.assertTrue(result["native_record"]["payload"])
            self.assertEqual(set(result["native_record"]["provenance"]), {"clangd", "cppcheck"})
            for row in result["native_record"]["payload"]:
                self.assertEqual(row["sha256"], acceptance.digest(row["source"]))
                self.assertTrue(row["upstream_path"])
                self.assertTrue(row["upstream_path_origin"])
        else:
            self.assertNotIn("BYO_MANAGED_TEST_INPUT_RECORD", result["selectors"])

    def test_missing_real_native_resource_publishes_no_selectors(self):
        bundle = self.copied_bundle()
        manifest = json.loads((bundle / "release-manifest.json").read_bytes())
        resource = next(
            row["path"]
            for row in manifest["files"]
            if row["path"].startswith("analysis/")
            and not row["executable"]
            and row["path"] != "analysis/runtime.json"
        )
        (bundle / resource).unlink()
        result = self.prepare_in_clean_interpreter(bundle)
        self.assert_bundle_refused(result)
        self.assertIn("FileNotFoundError", result["facts"]["bundle"]["error"])

    def test_hash_consistent_mapping_tamper_reaches_real_validator_and_is_refused(self):
        bundle = self.copied_bundle()
        mapping = json.loads((bundle / "analysis/runtime.json").read_bytes())
        mapping["schema_version"] = -1
        self.rewrite_inventoried_json(bundle, "analysis/runtime.json", mapping)
        result = self.prepare_in_clean_interpreter(bundle)
        self.assert_bundle_refused(result)
        self.assertIn(
            "Analysis runtime mapping differs from pinned selection", result["facts"]["bundle"]["error"]
        )

    def test_hash_consistent_sbom_provenance_tamper_is_refused(self):
        bundle = self.copied_bundle()
        sbom = json.loads((bundle / "sbom.cdx.json").read_bytes())
        component = next(c for c in sbom["components"] if c["bom-ref"] == "byo-analysis:clangd")
        upstream = next(p for p in component["properties"] if p["name"] == "byo:analysis:upstream")
        upstream["value"] = "{}"
        self.rewrite_inventoried_json(bundle, "sbom.cdx.json", sbom)
        result = self.prepare_in_clean_interpreter(bundle)
        self.assert_bundle_refused(result)
        self.assertIn(
            "Analysis SBOM source/license/recipe mismatch: clangd", result["facts"]["bundle"]["error"]
        )


if __name__ == "__main__":
    unittest.main()
