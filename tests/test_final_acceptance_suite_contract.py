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
import re
import shutil
import tempfile
import subprocess
import sys
import time
import unittest
from unittest import mock
import urllib.error

from tests import final_acceptance_suite as acceptance
from tests.process_cleanup_observations import (
    CleanupTrace,
    descendant_facts,
    execution_relationship,
    marker_facts,
    popen_facts,
    process_facts,
    role_evidence,
    terminal_identity,
)

OBSERVATIONS = {}
# Windows' venv redirector delegates to a base child; a macOS framework stub can
# exec its app image in place after a parent's start snapshot; others are direct.
RELATIONSHIPS = (
    {"direct", "launcher-child"}
    if os.name == "nt"
    else {"direct", "framework-exec"}
    if sys.platform == "darwin"
    else {"direct"}
)


def same_process_refusals(start, interpreter, command, argv, expected_argv, parent, launch):
    """Alter only rejection inputs of a real same-process join; none may still join."""
    if start.get("pid") != interpreter.get("pid") or not interpreter.get("cmdline"):
        return {}
    tail = interpreter["cmdline"][1:]
    foreign = str(Path(tempfile.gettempdir(), "foreign-image", "python"))
    variants = {
        "foreign_image": ({**interpreter, "exe": foreign, "cmdline": [foreign, *tail]}, launch)
    }
    joined = execution_relationship(
        start, interpreter, command, argv, expected_argv, parent=parent, launch=launch
    )
    if joined["relationship"] == "framework-exec":
        image, birth = interpreter["exe"], interpreter["creation_time"]
        other = re.sub(r"/Versions/[^/]+/", "/Versions/0.0/", image, count=1)
        variants.update(
            no_launch_self_report=(interpreter, None),
            launch_through_other_path=(interpreter, {**launch, "executable": foreign}),
            launch_resolving_elsewhere=(interpreter, {**launch, "resolved": image}),
            other_version_image=({**interpreter, "exe": other, "cmdline": [other, *tail]}, launch),
            argv0_not_the_image=({**interpreter, "cmdline": [start["cmdline"][0], *tail]}, launch),
            changed_parent=({**interpreter, "ppid": interpreter["ppid"] + 1}, launch),
            changed_birth=(
                {**interpreter, "creation_time": birth + 1, "birth_after_queries": birth + 1},
                launch,
            ),
        )
    return {
        label: execution_relationship(
            start, facts, command, argv, expected_argv, parent=parent, launch=self_report
        )
        for label, (facts, self_report) in variants.items()
    }


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

    def test_execution_relationship_requires_stable_direct_identity_and_module_argv(self):
        launcher = process_facts(os.getpid())
        interpreter = process_facts(os.getpid(), launcher["creation_time"])
        command = launcher["cmdline"]
        direct = execution_relationship(launcher, interpreter, command, sys.argv, sys.argv)
        OBSERVATIONS[self.id()] = {"direct": direct, "negative_controls": {}}
        self.assertFalse(direct["evidence_gaps"], direct)
        self.assertEqual(direct["relationship"], "direct")

        # Only rejection inputs are altered; no invented facts establish a
        # positive identity. Reuse is queried with a deliberately wrong birth.
        invalid = {
            "missing": {**interpreter, "identity": "missing"},
            "unavailable": {**interpreter, "identity": "unavailable"},
            "reused": process_facts(os.getpid(), launcher["creation_time"] + 1),
            "unknown_birth": {k: v for k, v in interpreter.items() if k != "creation_time"},
            "changed_during_observation": {**interpreter, "identity": "changed_during_observation"},
            "gone_during_observation": {**interpreter, "identity": "gone_during_observation"},
            "unknown_parent": {k: v for k, v in interpreter.items() if k != "ppid"},
            "unknown_command": {k: v for k, v in interpreter.items() if k != "cmdline"},
            "unknown_executable": {k: v for k, v in interpreter.items() if k != "exe"},
        }
        for label, facts in invalid.items():
            with self.subTest(label=label):
                refused = execution_relationship(launcher, facts, command, sys.argv, sys.argv)
                OBSERVATIONS[self.id()]["negative_controls"][label] = refused
                self.assertEqual(refused["relationship"], "unknown")
                self.assertTrue(refused["evidence_gaps"])
        for argv in (None, [*sys.argv, "not-the-executed-module"]):
            with self.subTest(argv=argv):
                refused = execution_relationship(launcher, interpreter, command, argv, sys.argv)
                self.assertEqual(refused["relationship"], "unknown")
                self.assertTrue(refused["evidence_gaps"])
        refused = execution_relationship(
            launcher, interpreter, [command[0], "not-the-dispatched-module"], sys.argv, sys.argv
        )
        self.assertEqual(refused["relationship"], "unknown")
        self.assertTrue(refused["evidence_gaps"])

    def test_execution_relationship_binds_a_real_portable_launcher_child(self):
        # Run the same command through a real Python wrapper and its actual
        # interpreter child on every OS, keeping the locked environment the way
        # that OS selects it: POSIX by the invoked venv path; Windows' venv
        # redirector by naming itself in __PYVENV_LAUNCHER__, which the base
        # image consumes at startup. The wrapper's sole wait is its normal
        # subprocess.run lifecycle; the identity observer never waits/signals.
        script = r"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tests.process_cleanup_observations import process_facts

directory = Path(sys.argv[1])
identity = process_facts(os.getpid())
psutil = importlib.util.find_spec("psutil")
context = {"prefix": sys.prefix, "psutil": psutil and psutil.origin}
if os.environ.get("BYO_FR11_LAYER_CONTROL") == "child":
    (directory / "executed.json").write_text(json.dumps({
        "identity": identity, "parent": process_facts(os.getppid()),
        "argv": sys.argv, "module_name": __name__, **context,
    }), encoding="utf-8")
else:
    environment = dict(os.environ)
    environment["BYO_FR11_LAYER_CONTROL"] = "child"
    if os.name == "nt":
        command = [identity["exe"], *identity["cmdline"][1:]]
        environment["__PYVENV_LAUNCHER__"] = sys.executable
    else:
        command = [sys.executable, *identity["cmdline"][1:]]
    (directory / "launcher.json").write_text(json.dumps({
        "identity": identity, "command": command, **context,
    }), encoding="utf-8")
    raise SystemExit(subprocess.run(command, env=environment).returncode)
"""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            command = [sys.executable, "-c", script, str(directory)]
            environment = dict(os.environ)
            environment.pop("BYO_FR11_LAYER_CONTROL", None)
            try:
                code, outcome = acceptance.run_module(command, environment, directory, 15)
            finally:
                OBSERVATIONS[self.id()] = {
                    name: marker_facts(directory / name)
                    for name in (
                        "launcher.json",
                        "executed.json",
                        "module-exit.json",
                        "stdout.log",
                        "stderr.log",
                    )
                }
            launcher = json.loads((directory / "launcher.json").read_bytes())
            executed = json.loads((directory / "executed.json").read_bytes())
            exit_record = json.loads((directory / "module-exit.json").read_bytes())
            relationship = execution_relationship(
                launcher["identity"],
                executed["identity"],
                command,
                executed["argv"],
                ["-c", str(directory)],
                parent=executed["parent"],
            )
            OBSERVATIONS[self.id()]["execution_relationship"] = relationship
            self.assertEqual((code, outcome), (0, "PASS"))
            self.assertTrue(exit_record["wait_completed"])
            self.assertEqual(exit_record["popen"]["cached_returncode"], 0)
            self.assertEqual(launcher["command"][1:], command[1:])
            self.assertEqual(executed["module_name"], "__main__")
            # Both layers really execute in this locked environment and load its
            # dependency; a base interpreter outside it is not this chain.
            self.assertEqual((launcher["prefix"], executed["prefix"]), (sys.prefix, sys.prefix))
            self.assertIsNotNone(executed["psutil"])
            self.assertEqual(executed["psutil"], launcher["psutil"])
            self.assertTrue(
                Path(executed["psutil"]).resolve().is_relative_to(Path(sys.prefix).resolve())
            )
            self.assertFalse(relationship["evidence_gaps"], relationship)
            self.assertEqual(relationship["relationship"], "launcher-child")
            # Missing/reused/unknown parent observations and a different real
            # parent PID cannot become ownership merely by matching argv.
            for parent in (
                None,
                {**executed["parent"], "identity": "reused"},
                {k: v for k, v in executed["parent"].items() if k != "creation_time"},
                executed["identity"],
            ):
                with self.subTest(parent=parent):
                    refused = execution_relationship(
                        launcher["identity"],
                        executed["identity"],
                        command,
                        executed["argv"],
                        ["-c", str(directory)],
                        parent=parent,
                    )
                    self.assertEqual(refused["relationship"], "unknown")
                    self.assertTrue(refused["evidence_gaps"])
            unrelated = {
                **executed["identity"],
                "ppid": launcher["identity"]["ppid"],
            }
            refused = execution_relationship(
                launcher["identity"],
                unrelated,
                command,
                executed["argv"],
                ["-c", str(directory)],
                parent=executed["parent"],
            )
            self.assertEqual(refused["relationship"], "unknown")
            self.assertTrue(refused["evidence_gaps"])

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
from tests.process_cleanup_observations import CleanupTrace, launch_facts, process_facts

directory = Path(sys.argv[1])
trace = CleanupTrace("natural-interpreter-exit-control", directory / "cleanup-observations")
event = multiprocessing.get_context("spawn").Event()
pid = getattr(resource_tracker._resource_tracker, "_pid", None)
trace.record("actual-interpreter-helper-slot", helper_pid=pid, identity=process_facts(os.getpid()))
if pid is not None:
    trace.role(pid, "multiprocessing-resource-tracker", "actual Event control resource_tracker._resource_tracker._pid")
module_identity = process_facts(os.getpid())
(directory / "control.json").write_text(json.dumps({"module_pid": os.getpid(), "helper_pid": pid,
    "module_identity": module_identity, "parent_identity": process_facts(os.getppid()),
    "module_argv": sys.argv, "module_name": __name__, "launch": launch_facts(),
    "boundary": "before normal interpreter exit; no emergency cleanup"}), encoding="utf-8")
"""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            command = [sys.executable, "-c", script, str(directory)]
            try:
                code, outcome = acceptance.run_module(command, dict(os.environ), directory, 15)
            finally:
                # Retain actual raw receipts before temp cleanup, even when the
                # observed command or a subsequent contract assertion fails.
                OBSERVATIONS[self.id()] = {
                    name: marker_facts(directory / name)
                    for name in ("control.json", "module-exit.json", "stdout.log", "stderr.log")
                }
            control = json.loads((directory / "control.json").read_text(encoding="utf-8"))
            exit_record = json.loads((directory / "module-exit.json").read_text(encoding="utf-8"))
            joined = (
                exit_record["module_identity_at_start"],
                control["module_identity"],
                command,
                control["module_argv"],
                ["-c", str(directory)],
                control["parent_identity"],
                control["launch"],
            )
            relationship = execution_relationship(*joined[:5], parent=joined[5], launch=joined[6])
            refusals = same_process_refusals(*joined)
            OBSERVATIONS[self.id()]["module_execution_relationship"] = relationship
            OBSERVATIONS[self.id()]["same_process_refusals"] = refusals
        self.assertEqual((code, outcome), (0, "PASS"))
        self.assertTrue(exit_record["wait_completed"])
        self.assertEqual(exit_record["popen"]["cached_returncode"], 0)
        self.assertEqual(control["module_name"], "__main__")
        self.assertEqual(control["module_identity"]["pid"], control["module_pid"])
        self.assertFalse(relationship["evidence_gaps"], relationship)
        self.assertIn(relationship["relationship"], RELATIONSHIPS)
        for label, refused in refusals.items():
            with self.subTest(refusal=label):
                self.assertEqual(refused["relationship"], "unknown")
                self.assertTrue(refused["evidence_gaps"])
        if control["helper_pid"] is not None:
            observations = exit_record["descendants"]["seen_identities"]
            helpers = [r for r in observations if r["pid"] == control["helper_pid"]]
            self.assertEqual(len(helpers), 1)
            self.assertIn("after_module_exit", helpers[0])
            self.assertIn(helpers[0]["state"], ("gone", "terminal"))
        self.assertTrue(exit_record["normal_wait_completed"])
        self.assertTrue(exit_record["natural_eof0"])
        self.assertFalse(exit_record["interventions"])
        self.assertEqual(exit_record["descendants"]["gate"], "PASS")
        # This control intentionally has no suite cleanup ledger; the observer
        # must expose that gap, rather than fabricate an empty cleanup success.
        self.assertTrue(exit_record["descendants"]["evidence_gaps"])

    def real_child(self):
        """Start an actual child this test owns; it exits once its stdin closes."""
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
        )
        self.addCleanup(child.wait, timeout=15)
        self.addCleanup(child.stdin.close)
        facts = process_facts(child.pid)
        self.assertIsNotNone(facts.get("creation_time"), facts)
        return child, facts

    def exited_child(self):
        # The owner's own wait reaps it. The Popen stays referenced by the
        # caller, so Windows keeps that PID's handle and cannot reuse it.
        child, facts = self.real_child()
        child.stdin.close()
        child.wait(timeout=15)
        return child, facts

    def test_capture_gap_resolves_only_by_its_exact_identity_exit(self):
        # A capture gap is retained and resolved only by its own exact identity's
        # positive post-exit state; any other identity or state keeps it an error.
        exited_process, exited = self.exited_child()
        live_process, live = self.real_child()

        def gap(facts, **override):
            child = {"pid": facts["pid"], "creation_time": facts["creation_time"], **override}
            return {
                "gap": "Child cmdline unavailable during capture",
                "identity": process_facts(child["pid"], child.get("creation_time")),
                "child": child,
            }

        def captured(facts, **override):
            key = (facts["pid"], override.get("creation_time", facts["creation_time"]))
            entry = {"source": "contract capture", "prior": facts, "owned_by_command": True}
            return [(key, entry)]

        changed = {"creation_time": live["creation_time"] + 1}
        cases = {
            "exact_exit": (captured(exited), gap(exited), "PASS"),
            "live_owned": (captured(live), gap(live), "FAIL"),
            "changed_birth": (captured(live, **changed), gap(live, **changed), "ERROR"),
            "unseen_identity": (
                captured(exited),
                gap(exited, creation_time=exited["creation_time"] + 1),
                "ERROR",
            ),
            "birthless": (
                captured(exited),
                {
                    "pid": exited["pid"],
                    "error": {"type": "contract"},
                    "child": {"pid": exited["pid"]},
                },
                "ERROR",
            ),
            "no_child_identity": (
                captured(exited),
                {"gap": "Root changed during tree capture", "identity": exited},
                "ERROR",
            ),
        }
        results = {}
        for label, (seen, raw_gap, expected) in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                observed = acceptance.module_exit_observations(Path(temporary), seen, [raw_gap])
                results[label] = observed
                self.assertEqual(observed["gate"], expected, observed)
                if label == "exact_exit":
                    self.assertEqual(observed["identity_gaps"], [])
                    [resolved] = observed["resolved_identity_gaps"]
                    self.assertEqual({k: resolved[k] for k in raw_gap}, raw_gap)
                    self.assertEqual(terminal_identity(resolved["resolved_by"]), "gone")
                else:
                    self.assertIn(raw_gap, observed["identity_gaps"])
                    self.assertEqual(observed["resolved_identity_gaps"], [])
        OBSERVATIONS[self.id()] = results
        self.assertEqual(
            terminal_identity(process_facts(live["pid"], live["creation_time"])), "live"
        )
        self.assertIsNotNone(exited_process.returncode)
        self.assertIsNone(live_process.returncode)

    def test_exact_child_gap_stays_error_when_later_facts_are_unavailable(self):
        child, facts = self.exited_child()
        key = (facts["pid"], facts["creation_time"])
        captured = [(key, {"source": "contract", "prior": facts, "owned_by_command": True})]
        gap = {
            "gap": "Child cmdline unavailable during capture",
            "child": {"pid": key[0], "creation_time": key[1]},
        }
        results = {}
        for reason in ("AccessDenied", "SystemError", "missed-birth", "malformed"):
            unavailable = {
                "pid": key[0],
                "expected_creation_time": key[1],
                "identity": "unavailable",
                "error": {"type": reason},
            }
            if reason == "missed-birth":
                unavailable["expected_creation_time"] = None
            if reason == "malformed":
                unavailable["identity"] = "same_birth"
                unavailable["status"] = "dead"
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                with mock.patch.object(acceptance, "process_facts", return_value=unavailable):
                    observed = acceptance.module_exit_observations(Path(temporary), captured, [gap])
                results[reason] = observed
                self.assertEqual(observed["gate"], "ERROR", observed)
                self.assertIn(gap, observed["identity_gaps"])
                self.assertFalse(observed["resolved_identity_gaps"])
        OBSERVATIONS[self.id()] = results
        self.assertIsNotNone(child.returncode)

    def test_real_zombie_capture_gap_resolves_by_its_terminal_identity(self):
        if os.name == "nt":
            self.skipTest("Windows has no zombie process state")
        child, facts = self.real_child()
        child.stdin.close()
        birth = facts["creation_time"]
        deadline = time.monotonic() + 15
        # Waiting only for this fixture's exit, never as a completeness guarantee.
        while terminal_identity(process_facts(child.pid, birth)) != "terminal":
            self.assertLess(time.monotonic(), deadline, "the exited child never became a zombie")
            time.sleep(0.05)
        own = process_facts(os.getpid())
        snapshot = descendant_facts(os.getpid(), own["creation_time"], "zombie capture control")
        rows = [row for row in snapshot["descendants"] if row["pid"] == child.pid]
        gaps = [
            gap for gap in snapshot["evidence_gaps"] if gap.get("child", {}).get("pid") == child.pid
        ]
        self.assertEqual(len(rows), 1, snapshot)
        self.assertTrue(gaps, "a zombie's unavailable fields must be capture gaps")
        seen = [
            ((child.pid, birth), {"source": "contract", "prior": rows[0], "owned_by_command": True})
        ]
        with tempfile.TemporaryDirectory() as temporary:
            observed = acceptance.module_exit_observations(Path(temporary), seen, gaps)
        OBSERVATIONS[self.id()] = {"snapshot": snapshot, "observed": observed}
        self.assertIsNone(child.returncode, "the observer must not reap the zombie")
        self.assertEqual(observed["gate"], "PASS", observed)
        self.assertEqual(observed["identity_gaps"], [])
        self.assertEqual(len(observed["resolved_identity_gaps"]), len(gaps))
        for resolved in observed["resolved_identity_gaps"]:
            self.assertEqual(terminal_identity(resolved["resolved_by"]), "terminal")

    def test_root_exit_is_marked_only_on_positive_exact_absence(self):
        import psutil

        exited_process, exited = self.exited_child()
        own = process_facts(os.getpid())
        birth = own["creation_time"]
        gone = descendant_facts(exited["pid"], exited["creation_time"], "exited root control")
        birthless = descendant_facts(exited["pid"], None, "birthless root control")
        changed = descendant_facts(os.getpid(), birth + 1, "changed root control")
        with mock.patch("psutil.Process.children", side_effect=psutil.NoSuchProcess(os.getpid())):
            raced = descendant_facts(os.getpid(), birth, "live root, failed tree capture")
        with mock.patch("psutil.Process.children", side_effect=psutil.AccessDenied(os.getpid())):
            denied = descendant_facts(os.getpid(), birth, "live root, denied tree capture")
        OBSERVATIONS[self.id()] = {
            "gone": gone,
            "birthless": birthless,
            "changed": changed,
            "raced": raced,
            "denied": denied,
        }
        self.assertEqual([gap.get("root_exit") for gap in gone["evidence_gaps"]], [True])
        self.assertEqual(terminal_identity(gone["root_identity"]), "gone")
        for label, snapshot in (
            ("birthless", birthless),
            ("changed", changed),
            ("raced", raced),
            ("denied", denied),
        ):
            with self.subTest(root=label):
                self.assertTrue(snapshot["evidence_gaps"], snapshot)
                self.assertFalse(any(gap.get("root_exit") for gap in snapshot["evidence_gaps"]))
        self.assertEqual(raced["root_after_capture"]["identity"], "same_birth")
        self.assertEqual(
            terminal_identity(
                {"identity": "gone_during_observation", "expected_creation_time": birth}
            ),
            "gone",
        )
        self.assertEqual(terminal_identity({"identity": "gone_during_observation"}), "unknown")
        self.assertIsNotNone(exited_process.returncode)

    def run_with_first_root_observation(self, pid, birth, directory):
        """Run a real command whose first tree observation is of another real root."""
        real = acceptance.descendant_facts
        first = []

        def observe(*args, **kwargs):
            if first:
                return real(*args, **kwargs)
            first.append(real(pid, birth, "contract root observation"))
            return first[0]

        command = [sys.executable, "-B", "-c", "pass"]
        with mock.patch.object(acceptance, "descendant_facts", observe):
            code, outcome = acceptance.run_module(command, dict(os.environ), directory, 15)
        exit_record = json.loads((directory / "module-exit.json").read_text(encoding="utf-8"))
        return code, outcome, exit_record, first[0]

    def test_module_gate_excludes_only_a_positive_root_exit(self):
        # The raw gap is always retained; only an exact root's positive exit
        # leaves the module's real wait to decide without a descendant error.
        exited_process, exited = self.exited_child()
        results = {}
        for label, birth, expected in (
            ("exact_root_exit", exited["creation_time"], "PASS"),
            ("birthless_root", None, "ERROR"),
        ):
            with self.subTest(root=label), tempfile.TemporaryDirectory() as temporary:
                code, outcome, exit_record, first = self.run_with_first_root_observation(
                    exited["pid"], birth, Path(temporary)
                )
                results[label] = exit_record
                [raw] = first["evidence_gaps"]
                self.assertEqual(bool(raw.get("root_exit")), expected == "PASS")
                self.assertIn(raw, exit_record["tree_observation_gaps"])
                self.assertEqual((code, outcome), (0, expected))
                self.assertEqual(exit_record["descendants"]["gate"], expected)
                self.assertFalse(exit_record["interventions"])
        OBSERVATIONS[self.id()] = results
        self.assertIsNotNone(exited_process.returncode)


FINALIZATION_PROBE = r"""
import atexit
import json
import multiprocessing
from multiprocessing import resource_tracker
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from tests.process_cleanup_observations import CleanupTrace, launch_facts, process_facts

root = Path(os.environ["BYO_FR12_PROBE_ROOT"])
event = writer = leaked = None

def record(name, data):
    (root / name).write_text(json.dumps(data), encoding="utf-8")

def finish_writer(release):
    identity = process_facts(os.getpid())
    record("writer-ready.json", identity)
    released = release.wait(20)
    record("writer-finished.json", {
        "released": released, "identity": process_facts(os.getpid(), identity["creation_time"]),
        "at_monotonic": time.monotonic(),
    })

class OrdinaryFinalization(unittest.TestCase):
    def test_body(self):
        global event, writer, leaked
        trace = CleanupTrace("fr12-actual-body")
        mode = os.environ["BYO_FR12_MODE"]
        if mode == "leak":
            script = (
                "import json,os,sys,time; from pathlib import Path; "
                "from tests.process_cleanup_observations import process_facts; "
                "Path(sys.argv[1]).write_text(json.dumps(process_facts(os.getpid())), "
                "encoding='utf-8'); time.sleep(60)"
            )
            leaked = subprocess.Popen(
                [sys.executable, "-B", "-c", script, str(root / "leak-ready.json"),
                 "multiprocessing.resource_tracker"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            trace.role(leaked.pid, "resource-tracker-shaped-deliberate-leak",
                       "actual subprocess fixture; deliberately survives interpreter exit")
            ready = root / "leak-ready.json"
        else:
            context = multiprocessing.get_context("spawn")
            event = context.Event()
            writer = context.Process(target=finish_writer, args=(event,))
            writer.start()
            trace.role(writer.pid, "actual-spawn-writer",
                       "actual Process.start; no body join or termination")
            helper = getattr(resource_tracker._resource_tracker, "_pid", None)
            if helper is not None:
                trace.role(helper, "multiprocessing-resource-tracker", "actual Event helper slot")
            def finalize():
                record("ordinary-finalizer.json", {
                    "at_monotonic": time.monotonic(), "identity": process_facts(os.getpid()),
                })
                event.set()
            atexit.register(finalize)
            ready = root / "writer-ready.json"
        deadline = time.monotonic() + 10
        while not ready.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.is_file(), "actual child must be ready before body completion")
        record("body.json", {
            "identity": process_facts(os.getpid()), "parent": process_facts(os.getppid()),
            "argv": sys.argv, "launch": launch_facts(), "at_monotonic": time.monotonic(),
            "mode": mode,
            "writer_pid": None if writer is None else writer.pid,
            "leak_pid": None if leaked is None else leaked.pid,
        })
        if mode == "error":
            raise RuntimeError("deliberate body error survives natural finalization")
        for value in (1, 2):
            with self.subTest(value=value):
                self.assertEqual(value, 1 if mode == "failure" else value)
"""


class OrdinaryInterpreterExitContracts(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.environ.get("GITHUB_ACTIONS"), "true", "Actions-only controls")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tests = self.root / "tests"
        self.tests.mkdir()
        (self.tests / "__init__.py").touch()
        for name in ("final_acceptance_suite.py", "process_cleanup_observations.py"):
            shutil.copyfile(Path(__file__).with_name(name), self.tests / name)
        (self.tests / "fr12_finalization_probe.py").write_text(FINALIZATION_PROBE, encoding="utf-8")
        OBSERVATIONS[self.id()] = {}
        self.addCleanup(self.emergency_fixture_cleanup)

    def emergency_fixture_cleanup(self):
        # Explicit fixture-owner fallback only on failed controls. Never evidence
        # of natural cleanup, and needing it must itself fail the control.
        import psutil

        actions = []
        for name in ("leak-ready.json", "writer-ready.json", "body.json"):
            for path in self.root.glob("*/" + name):
                try:
                    data = json.loads(path.read_bytes())
                    facts = data["identity"] if isinstance(data.get("identity"), dict) else data
                    pid, birth = facts["pid"], facts["creation_time"]
                    current = process_facts(pid, birth)
                    if terminal_identity(current) == "live":
                        action = {"source": str(path), "before": current}
                        actions.append(action)
                        process = psutil.Process(pid)
                        if process.create_time() == birth:
                            process.kill()
                            process.wait(timeout=15)
                        action["after"] = process_facts(pid, birth)
                except (OSError, ValueError, KeyError, psutil.Error) as exc:
                    actions.append({"source": str(path), "error": repr(exc)})
        OBSERVATIONS[self.id()]["fixture_emergency_cleanup"] = actions
        self.assertFalse(
            actions, "fixture emergency intervention is a failed control: " + repr(actions)
        )

    def run_probe(self, mode, timeout=20):
        directory = self.root / mode
        directory.mkdir()
        environment = dict(
            os.environ,
            BYO_FR12_PROBE_ROOT=str(directory),
            BYO_FR12_MODE=mode,
            PYTHONPATH=str(self.root),
        )
        command = [
            sys.executable,
            "-B",
            str(self.tests / "final_acceptance_suite.py"),
            "--worker",
            "fr12_finalization_probe",
            "--evidence",
            str(directory),
        ]
        try:
            code, outcome = acceptance.run_module(command, environment, directory, timeout)
        finally:
            # Retain raw ledgers/streams on failed premises/assertions as well.
            OBSERVATIONS[self.id()][mode] = {
                path.name: path.read_text(encoding="utf-8", errors="replace")
                for path in directory.glob("*")
                if path.is_file()
            }
        exit_record = json.loads((directory / "module-exit.json").read_bytes())
        result = json.loads((directory / "result.json").read_bytes())
        pre = json.loads((directory / "cleanup.json").read_bytes())
        events = [
            json.loads(line) for line in (directory / "events.jsonl").read_bytes().splitlines()
        ]
        self.assertTrue(exit_record["normal_wait_completed"])
        self.assertEqual(exit_record["normal_wait_exit"], code)
        self.assertEqual(exit_record["popen"]["cached_returncode"], code)
        self.assertLessEqual(
            exit_record["normal_wait_returned_monotonic"],
            exit_record["descendants"]["at_monotonic"],
        )
        self.assertFalse(pre["errors"], pre)
        self.assertIn("before ordinary interpreter exit", pre["boundary"])
        self.assertTrue(pre["pre_exit_descendants"], "must observe an actual pre-exit child")
        self.assertFalse(
            any(
                "emergency" in str(row.get("operation", ""))
                for row in pre["observations"]["events"]
            )
        )
        self.assertEqual(result["testsRun"], 1)
        self.assertEqual([e["id"] for e in events if e["kind"] == "start"], result["loaded"])
        self.assertEqual([e["id"] for e in events if e["kind"] == "stop"], result["loaded"])
        return directory, code, outcome, exit_record, result, pre, events

    def test_actual_event_writer_finalizes_after_body_before_authoritative_judgment(self):
        directory, code, outcome, exit_record, result, pre, events = self.run_probe("pass")
        self.assertEqual((code, outcome), (0, "PASS"))
        self.assertTrue(result["successful"])
        self.assertEqual(set(result["methods"].values()), {"PASS"})
        self.assertEqual([e["outcome"] for e in events if e["kind"] == "subtest"], ["PASS", "PASS"])
        self.assertTrue(exit_record["natural_eof0"])
        self.assertFalse(exit_record["interventions"])
        self.assertEqual(exit_record["descendants"]["gate"], "PASS")
        self.assertFalse(exit_record["descendants"]["identity_gaps"])
        self.assertFalse(exit_record["descendants"]["evidence_gaps"])
        body = json.loads((directory / "body.json").read_bytes())
        finished = json.loads((directory / "writer-finished.json").read_bytes())
        finalizer = json.loads((directory / "ordinary-finalizer.json").read_bytes())
        joined = (
            exit_record["module_identity_at_start"],
            body["identity"],
            exit_record["popen"]["argv"],
            body["argv"],
            [
                str(self.tests / "final_acceptance_suite.py"),
                "--worker",
                "fr12_finalization_probe",
                "--evidence",
                str(directory),
            ],
            body["parent"],
            body["launch"],
        )
        relationship = execution_relationship(*joined[:5], parent=joined[5], launch=joined[6])
        refusals = same_process_refusals(*joined)
        OBSERVATIONS[self.id()]["module_execution_relationship"] = relationship
        OBSERVATIONS[self.id()]["same_process_refusals"] = refusals
        self.assertFalse(relationship["evidence_gaps"], relationship)
        self.assertIn(relationship["relationship"], RELATIONSHIPS)
        for label, refused in refusals.items():
            with self.subTest(refusal=label):
                self.assertEqual(refused["relationship"], "unknown")
                self.assertTrue(refused["evidence_gaps"])
        self.assertTrue(finished["released"], finished)
        writers = [row for row in pre["pre_exit_descendants"] if row["pid"] == body["writer_pid"]]
        self.assertEqual(len(writers), 1)
        self.assertEqual(terminal_identity(writers[0]["identity"]), "live")
        self.assertLessEqual(writers[0]["at_monotonic"], finalizer["at_monotonic"])
        self.assertLessEqual(finalizer["at_monotonic"], finished["at_monotonic"])
        self.assertLessEqual(
            finished["at_monotonic"], exit_record["normal_wait_returned_monotonic"]
        )
        self.assertTrue(
            all(
                row["state"] in ("gone", "terminal")
                for row in exit_record["descendants"]["seen_identities"]
            )
        )

    def test_deliberate_helper_shaped_leak_fails_after_real_zero_exit_before_emergency(self):
        directory, code, outcome, exit_record, result, pre, _ = self.run_probe("leak")
        self.assertEqual(code, 0, "retain the actual successful test-body command exit")
        self.assertEqual(outcome, "FAIL")
        self.assertTrue(result["successful"])
        self.assertEqual(set(result["methods"].values()), {"PASS"})
        self.assertFalse(exit_record["natural_eof0"])
        self.assertEqual(exit_record["descendants"]["gate"], "FAIL")
        ready = json.loads((directory / "leak-ready.json").read_bytes())
        survivors = exit_record["descendants"]["survivors"]
        exact = [
            row
            for row in survivors
            if row["pid"] == ready["pid"] and row["creation_time"] == ready["creation_time"]
        ]
        self.assertEqual(len(exact), 1, survivors)
        self.assertEqual(exact[0]["state"], "live")
        self.assertIn("multiprocessing.resource_tracker", exact[0]["after_module_exit"]["cmdline"])
        self.assertTrue(exit_record["interventions"])
        emergency = exact[0]["emergency_cleanup"]
        self.assertLessEqual(exit_record["descendants"]["at_monotonic"], emergency["at_monotonic"])
        self.assertEqual(terminal_identity(emergency["before"]), "live")
        self.assertIn(terminal_identity(emergency["after"]), ("gone", "terminal"))
        self.assertTrue(any(row["pid"] == ready["pid"] for row in pre["pre_exit_descendants"]))

    def test_real_body_failure_and_error_survive_unintervened_finalization(self):
        for mode, expected in (("failure", "FAIL"), ("error", "ERROR")):
            with self.subTest(mode=mode):
                directory, code, outcome, exit_record, result, _, events = self.run_probe(mode)
                self.assertEqual((code, outcome), (1, "FAIL"))
                self.assertFalse(result["successful"])
                self.assertEqual(set(result["methods"].values()), {expected})
                self.assertEqual(exit_record["descendants"]["gate"], "PASS")
                self.assertFalse(exit_record["natural_eof0"])
                self.assertFalse(exit_record["interventions"])
                self.assertTrue(
                    json.loads((directory / "writer-finished.json").read_bytes())["released"]
                )
                if mode == "failure":
                    self.assertEqual(
                        [e["outcome"] for e in events if e["kind"] == "subtest"], ["PASS", "FAIL"]
                    )
                else:
                    self.assertTrue(any(e["kind"] == "error" for e in events))

    def test_actual_interpreter_timeout_never_claims_normal_eof0(self):
        directory = self.root / "timeout"
        directory.mkdir()
        command = [sys.executable, "-B", "-c", "import time; time.sleep(60)"]
        code, outcome = acceptance.run_module(command, dict(os.environ), directory, 1)
        exit_record = json.loads((directory / "module-exit.json").read_bytes())
        OBSERVATIONS[self.id()]["timeout"] = exit_record
        self.assertEqual(outcome, "TIMEOUT")
        self.assertNotEqual(code, 0)
        self.assertFalse(exit_record["normal_wait_completed"])
        self.assertIsNone(exit_record["normal_wait_exit"])
        self.assertFalse(exit_record["natural_eof0"])
        self.assertTrue(exit_record["interventions"])
        self.assertTrue(exit_record["wait_completed"])
        self.assertEqual(exit_record["popen"]["cached_returncode"], code)
        self.assertTrue(
            all(
                row["state"] in ("gone", "terminal")
                for row in exit_record["descendants"]["seen_identities"]
            )
        )

    def test_descendant_snapshot_cannot_poll_wait_or_signal(self):
        identity = process_facts(os.getpid())
        with (
            mock.patch("psutil.Process.wait", side_effect=AssertionError("observer wait")),
            mock.patch("psutil.Process.kill", side_effect=AssertionError("observer kill")),
            mock.patch(
                "psutil.Process.terminate", side_effect=AssertionError("observer terminate")
            ),
            mock.patch("os.waitpid", create=True, side_effect=AssertionError("observer reap")),
        ):
            snapshot = descendant_facts(os.getpid(), identity["creation_time"], "observer control")
            reused = process_facts(os.getpid(), identity["creation_time"] + 1)
            self.assertEqual(terminal_identity(reused), "unknown")
            self.assertEqual(terminal_identity({"identity": "unavailable"}), "unknown")
            self.assertEqual(terminal_identity({"identity": "missing"}), "unknown")
            self.assertEqual(
                terminal_identity({"identity": "same_birth", "status": "running"}), "unknown"
            )
            directory = self.root / "role-only"
            directory.mkdir()
            trace = CleanupTrace(
                "role-observation-is-not-ownership", directory / "cleanup-observations"
            )
            trace.role(os.getpid(), "actual observer", "actual self PID, outside the command tree")
            observed = acceptance.module_exit_observations(directory)
            self.assertEqual(observed["gate"], "ERROR")
            self.assertFalse(
                observed["survivors"], "role labels must not authorize emergency signals"
            )
            self.assertTrue(observed["identity_gaps"])
            self.assertFalse(observed["seen_identities"][0]["owned_by_command"])
        OBSERVATIONS[self.id()] = {"snapshot": snapshot, "reused": reused}
        self.assertFalse(snapshot["evidence_gaps"], snapshot)


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
            )
            == (system, arch)
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
            sys.executable,
            "-I",
            "-B",
            "-c",
            script,
            str(Path(acceptance.__file__).resolve()),
            str(bundle),
            str(self.installer),
            str(self.evidence),
        ]
        completed = subprocess.run(
            command, cwd=self.root, env=environment, capture_output=True, text=True, timeout=120
        )
        self.evidence.mkdir(parents=True, exist_ok=True)
        (self.evidence / "stdout.log").write_text(completed.stdout, encoding="utf-8")
        (self.evidence / "stderr.log").write_text(completed.stderr, encoding="utf-8")
        OBSERVATIONS[self.id()] = {
            "argv": command,
            "exit": completed.returncode,
            "evidence": str(self.evidence),
            "pythonpath_removed": True,
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
            "BYO_TEST_CLANGD",
            "BYO_MCP_TEST_CLANGD_EXECUTABLE",
            "BYO_MANAGED_TEST_INPUT_RECORD",
            "BYO_MANAGED_TEST_INPUT_RECORD_SHA256",
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
            "Analysis runtime mapping differs from pinned selection",
            result["facts"]["bundle"]["error"],
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
            "Analysis SBOM source/license/recipe mismatch: clangd",
            result["facts"]["bundle"]["error"],
        )


if __name__ == "__main__":
    unittest.main()
