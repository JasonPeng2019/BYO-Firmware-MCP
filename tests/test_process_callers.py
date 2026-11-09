"""Independent nonbuilding FR13 caller/authority controls for supported hosts.

Positive controls use the real caller, native processes and actual kernel reap.
Named fault controls replace only their stated observation/permission premise.
No production clock, budget, Windows policy or AnalysisPopen policy is replaced.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from pyocd_debug_mcp import discovery_hooks
from pyocd_debug_mcp.kernel import processes

CLOCK = time.monotonic  # Never consume a test-owned mocked clock sequence.
FIXTURE = Path(__file__).with_name("process_caller_fixture.py").resolve()
ARTIFACT = Path(os.environ.get("BYO_TEST_FAILURES", tempfile.gettempdir())) / "process-callers"


def facts(pid: int, birth: float | None = None) -> dict:
    record = {"pid": pid, "expected_birth": birth, "at": CLOCK()}
    try:
        process = psutil.Process(pid)
        actual = process.create_time()
        record["birth"] = actual
        if birth is not None and actual != birth:
            return {**record, "identity": "reused", "state": "unknown"}
        record["identity"] = "observed"
        for key in ("ppid", "exe", "cmdline", "status"):
            try:
                record[key] = getattr(process, key)()
            except (psutil.Error, OSError, SystemError) as exc:
                record[key] = {"unknown": repr(exc)}
        record["state"] = (
            "terminal"
            if record.get("status") in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
            else "live"
            if isinstance(record.get("status"), str)
            else "unknown"
        )
        if os.name == "posix":
            for key, query in (("pgid", os.getpgid), ("sid", os.getsid)):
                try:
                    record[key] = query(pid)
                except OSError as exc:
                    record[key] = {"unavailable": repr(exc)}
        if psutil.Process(pid).create_time() != actual:
            record.update(identity="changed", state="unknown")
    except psutil.ZombieProcess as exc:
        record.update(identity="unknown", state="unknown", error=repr(exc))
    except psutil.NoSuchProcess:
        record.update(identity="gone", state="gone")
    except (psutil.Error, OSError, SystemError) as exc:
        record.update(identity="unknown", state="unknown", error=repr(exc))
    return record


@unittest.skipUnless(os.name == "posix", "independent retained POSIX caller controls")
class IndependentOwnership(unittest.TestCase):
    def setUp(self) -> None:
        ARTIFACT.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="v-fr13-", dir=ARTIFACT))
        self.store = processes.ProcessMarkerStore(self.root / "markers")
        self.owned = []
        self.events = []
        self.gates = []
        self.real_spawn = processes.popen_owned
        self.peer = subprocess.Popen(
            [sys.executable, str(FIXTURE), "peer", str(self.root)],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.rescue)
        self.peer_identity = self.receipt("peer")

    def note(self, boundary: str, **extra) -> None:
        self.events.append({"boundary": boundary, "at": CLOCK(), **extra})

    def receipt(self, role: str) -> dict:
        path = self.root / (role + ".json")
        deadline = CLOCK() + 12
        while not path.is_file() and CLOCK() < deadline:
            time.sleep(0.002)
        self.assertTrue(path.is_file(), "missing fixture identity: " + role)
        return json.loads(path.read_text(encoding="utf-8"))

    def current(self, role: str) -> dict:
        identity = self.receipt(role)
        return {"role": role, **facts(identity["pid"], identity["birth"])}

    def peer_preserved(self) -> None:
        record = self.current("peer")
        self.note("peer preservation", facts=record)
        self.assertEqual(record["state"], "live", record)
        self.assertEqual(record["birth"], self.peer_identity["birth"])
        self.assertEqual(record["pgid"], self.peer.pid)
        self.assertEqual(record["sid"], self.peer.pid)
        self.assertIsNone(self.peer.poll())

    def capture_spawn(self, *args, **kwargs):
        result = self.real_spawn(*args, **kwargs)
        self.owned.append(result)
        self.note("spawn/admission", leader=facts(result[0].pid), marker=str(result[1]))
        return result

    def argv(self, mode="normal", status=0, payload="independent-stdout") -> list[str]:
        return [
            sys.executable,
            str(FIXTURE),
            "leader",
            str(self.root),
            mode,
            str(status),
            payload,
        ]

    def release_after_premise(self, mode="normal") -> threading.Thread:
        def release():
            try:
                leader, child = (
                    self.receipt("leader"),
                    self.receipt("branch" if mode == "race" else "leaf"),
                )
                self.note("pre-exit premise", leader=leader, child=child)
                self.assertEqual(child["pgid"], leader["pid"])
                self.assertEqual(child["sid"], leader["pid"])
                self.assertEqual(self.current(child["role"])["state"], "live")
                (self.root / "leader-exit").touch()
            except BaseException as exc:
                self.gate_errors.append(repr(exc))
                (self.root / "leader-exit").touch()

        if not hasattr(self, "gate_errors"):
            self.gate_errors = []
        thread = threading.Thread(target=release, daemon=True)
        self.gates.append(thread)
        thread.start()
        return thread

    def actual_reap(self, process, status=None) -> None:
        if os.name != "posix":
            raise self.failureException("actual kernel reap requires POSIX waitpid")
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        if status is not None:
            self.assertEqual(process.returncode, status)
        self.note("actual kernel leader reap", pid=process.pid, returncode=process.returncode)

    def checked_cleanup(self, real_cleanup, process):
        # psutil's fresh zombie state and parent prove a retained direct child;
        # final waitpid(ECHILD) checks actual reap without a production decoder.
        record = facts(process.pid)
        child = self.current("leaf")
        self.note("caller cleanup entry", leader=record, child=child)
        self.assertEqual(record["state"], "terminal", record)
        self.assertEqual(record["ppid"], os.getpid())
        self.assertEqual(child["state"], "live", child)
        self.assertEqual(child["pgid"], process.pid)
        self.peer_preserved()
        return real_cleanup(process)

    def test_normal_communicate_caller_preserves_output_status_and_peer(self) -> None:
        self.release_after_premise("kill")
        real_cleanup = processes.terminate_process_group
        with (
            patch.object(processes, "popen_owned", self.capture_spawn),
            patch.object(
                processes,
                "terminate_process_group",
                lambda process: self.checked_cleanup(real_cleanup, process),
            ),
        ):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                processes.run_owned(
                    self.argv("kill", 9),
                    timeout=15,
                    capture_output=True,
                    text=True,
                    check=True,
                    marker_store=self.store,
                )
        self.assertEqual(raised.exception.returncode, 9)
        self.assertEqual(raised.exception.stdout, "independent-stdout\n")
        self.assertEqual(raised.exception.stderr, "independent-stderr\n")
        self.assertIn(self.current("leaf")["state"], {"terminal", "gone"})
        self.actual_reap(self.owned[0][0], 9)
        self.assertFalse(list(self.store.root.glob("*.json")))
        self.peer_preserved()
        self.assertFalse(self.gate_errors, self.gate_errors)

    def test_normal_discovery_public_caller_preserves_output_status_and_peer(
        self,
    ) -> None:
        payload = json.dumps({"schema_version": 1, "kind": "probe", "probes": []})
        entrypoint = self.root / "hook.py"
        # An actually admitted server-python hook launches the exact fixture,
        # without inheriting the discovery runner's module-search assumptions.
        entrypoint.write_text(
            "import runpy,sys\n"
            + f"sys.argv={self.argv(payload=payload)[1:]!r}\n"
            + f"runpy.run_path({str(FIXTURE)!r},run_name='__main__')\n",
            encoding="utf-8",
        )
        document = {
            "schema_version": 1,
            "hooks": [
                {
                    "hook_id": "independent",
                    "kind": "probe",
                    "platforms": ["windows", "macos", "linux"],
                    "runner": "server-python",
                    "entrypoint": "hook.py",
                    "argv": [],
                    "timeout_seconds": 15,
                }
            ],
        }
        (self.root / discovery_hooks.MANIFEST_FILENAME).write_text(
            json.dumps(document), encoding="utf-8"
        )
        snapshot = discovery_hooks.load_hook_snapshot(self.root, environ={})
        self.assertEqual(len(snapshot.hooks), 1, snapshot)
        self.release_after_premise()
        raw = []
        real_execute = discovery_hooks._execute
        real_cleanup = discovery_hooks.terminate_process_group

        def execute(*args, **kwargs):
            result = real_execute(*args, **kwargs)
            raw.append(result)
            return result

        with (
            patch.object(discovery_hooks, "popen_owned", self.capture_spawn),
            patch.object(
                discovery_hooks,
                "terminate_process_group",
                lambda process: self.checked_cleanup(real_cleanup, process),
            ),
            patch.object(discovery_hooks, "_execute", execute),
        ):
            result = discovery_hooks.execute_hook(snapshot.hooks[0], marker_store=self.store)
        self.assertTrue(result.ok, result)
        self.assertEqual((result.outcome, result.exit_code), ("exited", 0))
        assert result.output is not None
        self.assertEqual(result.output.probes, ())
        self.assertEqual(raw[0].stdout, (payload + "\n").encode())
        self.assertEqual(raw[0].stderr_text, "independent-stderr\n")
        self.assertIn(self.current("leaf")["state"], {"terminal", "gone"})
        self.actual_reap(self.owned[0][0], 0)
        self.assertFalse(list(self.store.root.glob("*.json")))
        self.peer_preserved()
        self.assertFalse(self.gate_errors, self.gate_errors)

    def test_concurrent_poll_wait_never_releases_anchor_before_cleanup(self) -> None:
        process, marker = self.capture_spawn(
            self.argv(),
            marker_store=self.store,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.release_after_premise()
        errors = []
        stop = threading.Event()

        def waiter():
            try:
                self.assertEqual(process.wait(timeout=15), 0)
                while not stop.is_set():
                    self.assertEqual(process.poll(), 0)
                    time.sleep(0.001)
            except BaseException as exc:
                errors.append(repr(exc))

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        try:
            self.assertEqual(process.wait(timeout=15), 0)
            self.assertEqual(facts(process.pid)["state"], "terminal")
            self.assertTrue(processes.terminate_process_group(process))
        finally:
            stop.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.actual_reap(process, 0)
        self.assertTrue(processes.terminate_process_group(process))
        self.assertIn(self.current("leaf")["state"], {"terminal", "gone"})
        self.store.remove(marker)
        self.peer_preserved()
        self.assertFalse(self.gate_errors, self.gate_errors)

    def test_faulted_current_native_observer_cannot_clear_normal_marker(self) -> None:
        if os.name != "posix":
            raise self.failureException("POSIX group-signal control requires os.killpg")
        self.release_after_premise()
        with (
            patch.object(processes, "popen_owned", self.capture_spawn),
            patch.object(
                processes,
                "_current_group_members",
                side_effect=OSError("scoped observer fault"),
            ),
            patch.object(processes.os, "killpg", wraps=os.killpg) as signals,
        ):
            with self.assertRaisesRegex(RuntimeError, "descendants could not be cleared"):
                processes.run_owned(
                    self.argv(),
                    timeout=15,
                    capture_output=True,
                    text=True,
                    marker_store=self.store,
                )
        signals.assert_not_called()
        self.assertEqual(self.current("leaf")["state"], "live")
        self.assertEqual(len(list(self.store.root.glob("*.json"))), 1)
        self.assertEqual(facts(self.owned[0][0].pid)["state"], "terminal")
        self.peer_preserved()

    @unittest.skipUnless(sys.platform.startswith("linux"), "directed real Linux orphan race")
    def test_fork_after_last_membership_read_cannot_produce_false_success(self) -> None:
        # Scope subreaping to this test interpreter so the *real* exited branch
        # remains inspectable. It adds no production authority or synthetic PID.
        # This directed schedule is separate from undisturbed native acceptance.
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:  # GET_CHILD_SUBREAPER
            self.fail("native subreaper premise unavailable")
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # SET_CHILD_SUBREAPER
            self.fail("native subreaper premise unavailable")
        self.addCleanup(lambda: libc.prctl(36, previous.value, 0, 0, 0))
        self.release_after_premise("race")
        real_members = processes._current_group_members
        snapshots = []
        triggered = False

        def members(group, deadline):
            nonlocal triggered
            observed = real_members(group, deadline)
            snapshots.append(sorted(observed))
            if len(snapshots) == 2:
                triggered = True
                (self.root / "fork-now").touch()
                leaf = self.receipt("leaf")
                branch = self.receipt("branch")
                end = CLOCK() + 5
                while facts(branch["pid"], branch["birth"])["state"] == "live" and CLOCK() < end:
                    time.sleep(0.002)
                self.assertEqual(self.current("branch")["state"], "terminal")
                self.assertEqual(self.current("leaf")["state"], "live")
                self.assertLess(
                    CLOCK(),
                    deadline,
                    "directed scheduling consumed the production phase; fixture premise not reached in reserve",
                )
                self.note(
                    "after last membership read: real branch fork+exit",
                    returned_snapshot=sorted(observed),
                    branch=self.current("branch"),
                    leaf=self.current("leaf"),
                    native_new_group=sorted(real_members(group, CLOCK() + 1)),
                )
                self.assertNotIn(leaf["pid"], observed)
            return observed

        value = None
        error = None
        with (
            patch.object(processes, "popen_owned", self.capture_spawn),
            patch.object(processes, "_current_group_members", members),
        ):
            try:
                value = processes.run_owned(
                    self.argv("race"),
                    timeout=15,
                    capture_output=True,
                    text=True,
                    marker_store=self.store,
                )
            except RuntimeError as exc:
                error = str(exc)
        self.assertTrue(triggered, "directed schedule was not reached")
        child = self.current("leaf")
        markers = list(self.store.root.glob("*.json"))
        self.note(
            "natural caller result before emergency rescue",
            result=repr(value),
            error=error,
            leaf=child,
            markers=[str(p) for p in markers],
            snapshots=snapshots,
        )
        self.peer_preserved()
        if value is not None:
            self.assertEqual(value.stdout, "independent-stdout\n")
            self.assertIn(
                child["state"],
                {"terminal", "gone"},
                "caller reported success and removed its marker with a live owned descendant",
            )
            self.actual_reap(self.owned[0][0], 0)
            self.assertFalse(markers)
        else:
            self.assertIsNotNone(error)
            self.assertTrue(markers, "unconfirmed group lost recovery marker")

    def rescue(self) -> None:
        # Never a positive oracle. Preserve natural assertions and identities
        # first; kill only fixture PID/birth matches, never a numeric group.
        for thread in self.gates:
            thread.join(0.05)
        for role in ("leaf", "branch", "leader", "peer"):
            receipt = self.root / (role + ".json")
            if not receipt.is_file():
                continue
            identity = json.loads(receipt.read_text(encoding="utf-8"))
            before = facts(identity["pid"], identity["birth"])
            action = "none"
            if before.get("identity") == "observed" and before["state"] == "live":
                current = psutil.Process(identity["pid"])
                if current.create_time() == identity["birth"]:
                    current.kill()
                    action = "emergency exact identity kill"
            # Children adopted by the scoped Linux subreaper may be ours to reap.
            if os.name == "posix":
                try:
                    os.waitpid(identity["pid"], os.WNOHANG)
                except ChildProcessError:
                    pass
            self.note(
                "fixture rescue, excluded from behavioral result",
                role=role,
                before=before,
                action=action,
            )
        for process, _marker in self.owned:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        try:
            self.peer.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.note("emergency peer reap incomplete")
        path = ARTIFACT / (self._testMethodName + ".native.json")
        path.write_text(
            json.dumps(
                {
                    "test": self.id(),
                    "events": self.events,
                    "fixture_root": str(self.root),
                    "retained_for_raw_inspection": True,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        # Keep compact receipts, gates and markers in the uploaded evidence.


if __name__ == "__main__":
    unittest.main()
