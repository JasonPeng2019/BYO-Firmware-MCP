"""Program resolution: managed runtime versus PATH, bounded cached probes.

Contract "Runtime resolution": ``code_analysis_runtime.resolve_analysis_program(
context, name, deadline)`` is the single resolver.  Managed launches never
substitute PATH; source/development uses a supplied runtime first, then PATH,
and reports the origin.  ``--version`` probes are owned, bounded (5 s plus
confirmed cleanup), cached by canonical identity and changed bytes; a nonzero or
timed-out probe is unavailable evidence.  An expired caller deadline is not
executable evidence: internally it is a typed ``TimeoutError`` and publicly an
``incomplete``/``analysis/timeout`` envelope.  The physical manifest mapping is a
packaging prerequisite, so only its failure-closed behavior is asserted here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
import time
import unittest
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest import mock

from tests.code_analysis_support import (
    OWNED_CLEANUP_SECONDS,
    SCHEDULING_SLACK_SECONDS,
    FakeClangd,
    FAKE_CLANGD,
    analysis_module,
    assert_envelope,
    field,
    isolated_path_environment,
    make_context,
    make_service,
    real_clangd_or_skip,
    same_path,
    simple_project,
    temporary_directory,
    wait_pids_gone,
    write_shim,
)

PROBE_BOUND = 5.0 + OWNED_CLEANUP_SECONDS + SCHEDULING_SLACK_SECONDS


def resolve(context: Any, *, budget: float = 30.0, name: str = "clangd") -> Any:
    runtime = analysis_module("code_analysis_runtime")
    return runtime.resolve_analysis_program(context, name, time.monotonic() + budget)


def usable(program: Any) -> bool:
    return field(program, "executable") is not None and not tuple(field(program, "problems"))


@contextlib.contextmanager
def only_on_path(directory: Path) -> Generator[None, None, None]:
    with mock.patch.dict(os.environ, isolated_path_environment(directory)):
        yield


class RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = temporary_directory("byo-ca-runtime-")
        self.base = directory.__enter__()
        self.addCleanup(directory.__exit__, None, None, None)
        self.project = simple_project(self.base / "project")
        self.empty = self.base / "empty-path"
        self.empty.mkdir()

    def shim(
        self, name: str = "path-bin", scenario: dict[str, Any] | None = None
    ) -> tuple[Path, FakeClangd]:
        fake = FakeClangd(self.base / f"{name}-fake", scenario or {})
        directory = self.base / name
        directory.mkdir()
        if os.name == "nt":
            # Native discovery/identity stays real. Redirect only the owned spawn
            # argv to the framed Python peer; no batch file or shell is executed.
            executable = directory / "clangd.exe"
            shutil.copyfile(sys.executable, executable)
            runtime = analysis_module("code_analysis_runtime")
            original = runtime.popen_owned

            def launch(argv, *args, **kwargs):
                self.assertEqual(argv[0], str(executable.resolve()))
                if argv[1:] != ["--version"]:
                    # The public deadline control can reach LSP startup after a
                    # cached probe. Recognize that exact transport separately;
                    # unexpected executable/arguments must still fail.
                    self.assertEqual(
                        argv[1:],
                        [
                            "--enable-config",
                            "--background-index",
                            "--limit-references=0",
                            "--limit-results=0",
                            "--compile-commands-dir=" + str(self.project.root),
                            "--query-driver=",
                        ],
                    )
                return original(
                    [
                        sys.executable,
                        str(FAKE_CLANGD),
                        "--scenario",
                        str(fake.scenario_path),
                        *argv[1:],
                    ],
                    *args,
                    **kwargs,
                )

            replacement = mock.patch.object(runtime, "popen_owned", launch)
            replacement.start()
            self.addCleanup(replacement.stop)
        else:
            write_shim(directory, "clangd", fake.scenario_path)
        return directory, fake

    def assert_unavailable(self, program: Any) -> None:
        self.assertFalse(usable(program), program)
        self.assertTrue(tuple(field(program, "problems")), "unavailability must be explained")

    def assert_public_caller_expiry_is_incomplete_timeout(self) -> None:
        # Public control: caller expiry is an incomplete timeout, never a clean,
        # unavailable or executable-failure result.
        runtime = analysis_module("code_analysis_runtime")
        service = make_service(
            self.project.root, program_resolver=runtime.resolve_analysis_program, testcase=self
        )
        envelope = service.definition(
            str(self.project.root / "src" / "main.c"), 5, 25, timeout_seconds=0.001
        )
        assert_envelope(self, envelope, "definition", status="incomplete")
        self.assertEqual(envelope["code"], "analysis/timeout", envelope)


class PathResolution(RuntimeCase):
    def test_empty_path_is_unavailable_with_a_problem(self) -> None:
        with only_on_path(self.empty):
            self.assert_unavailable(resolve(make_context(self.project.root)))

    def test_development_path_program_reports_origin_and_probed_version(self) -> None:
        directory, fake = self.shim()
        with only_on_path(directory):
            program = resolve(make_context(self.project.root))
        self.assertTrue(usable(program), program)
        self.assertEqual(field(program, "origin"), "path")
        self.assertEqual(Path(field(program, "executable")).parent.resolve(), directory.resolve())
        self.assertIn("clangd version 23.1.0-fake", field(program, "version") or "")
        self.assertEqual(len(fake.events("version_probe")), 1)
        self.assertEqual(fake.starts(), [], "a version probe never starts the LSP")

    def test_real_clangd_found_through_an_isolated_path(self) -> None:
        real = real_clangd_or_skip(self)
        with only_on_path(real.parent):
            program = resolve(make_context(self.project.root))
        self.assertTrue(usable(program), program)
        self.assertTrue(same_path(field(program, "executable"), real))
        self.assertEqual(field(program, "origin"), "path")
        self.assertRegex(field(program, "version") or "", r"clangd version \d+")

    def test_current_directory_is_not_a_search_location(self) -> None:
        directory, fake = self.shim("cwd-bin")
        previous = Path.cwd()
        os.chdir(directory)
        self.addCleanup(os.chdir, previous)
        with only_on_path(self.empty):
            program = resolve(make_context(self.project.root))
        self.assert_unavailable(program)
        self.assertEqual(fake.events("version_probe"), [], "a cwd program was executed")


class ManagedRuntime(RuntimeCase):
    def test_managed_launch_never_substitutes_path(self) -> None:
        runtime_root = self.base / "runtime"
        runtime_root.mkdir()
        directory, fake = self.shim()
        with only_on_path(directory):
            program = resolve(
                make_context(self.project.root, runtime_root=runtime_root, managed=True)
            )
        self.assert_unavailable(program)
        self.assertNotEqual(field(program, "origin"), "path")
        self.assertEqual(
            fake.events("version_probe"), [], "managed resolution probed a PATH program"
        )


class BoundedProbes(RuntimeCase):
    @unittest.skipUnless(os.name == "posix", "POSIX scoped cleanup receipt; Windows Jobs unchanged")
    def test_cached_native_probe_retains_exact_scoped_cleanup_receipt(self) -> None:
        runtime = analysis_module("code_analysis_runtime")
        directory, fake = self.shim("scoped-probe")
        context = make_context(self.project.root)
        actual_spawn = runtime.popen_owned
        spawned = []

        def capture(*args, **kwargs):
            result = actual_spawn(*args, **kwargs)
            spawned.append(result)
            return result

        with (
            only_on_path(directory),
            mock.patch.object(runtime, "_probe_cache", {}),
            mock.patch.object(runtime, "popen_owned", side_effect=capture),
        ):
            first = resolve(context)
            self.assertTrue(usable(first), first)
            process = spawned[0][0]
            process.cleanup_receipt["uncertainties"].append("late fixture mutation")
            try:
                cached = resolve(context)
                self.assertTrue(usable(cached), cached)
                self.assertEqual(field(first, "cleanup_receipt")["uncertainties"], [])
                self.assertEqual(field(cached, "cleanup_receipt")["uncertainties"], [])
            finally:
                process.cleanup_receipt["uncertainties"].pop()
        self.assertEqual(len(spawned), 1, "Receipt retention must survive actual cache reuse")
        self.assertEqual(len(fake.events("version_probe")), 1)
        self.assertEqual(fake.starts(), [])
        process, marker = spawned[0]
        receipt = field(first, "cleanup_receipt")
        self.assertIsInstance(receipt, dict)
        self.assertEqual(receipt, process.cleanup_receipt)
        self.assertEqual(field(cached, "cleanup_receipt"), receipt)
        self.assertEqual(receipt["pid"], process.pid)
        self.assertEqual(receipt["coverage"], "retained_group_and_captured_descendants")
        self.assertIs(receipt["scope_confirmed"], True)
        self.assertEqual(receipt["unobserved_detached_descendants"], "not_proven")
        self.assertIs(receipt["leader_reaped"], True)
        self.assertEqual(receipt["leader_exit"], 0)
        self.assertEqual(receipt["uncertainties"], [])
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        self.assertTrue(marker is None or not marker.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX scoped cleanup receipt; Windows Jobs unchanged")
    def test_native_probe_refuses_corrupted_scope_and_retains_failure_receipt_and_marker(
        self,
    ) -> None:
        runtime = analysis_module("code_analysis_runtime")
        processes = analysis_module("kernel.processes")
        actual_cleanup = runtime.terminate_process_group
        actual_spawn = runtime.popen_owned
        for case in ("missing", "wrong-coverage", "false-scope", "string-scope"):
            with self.subTest(case=case):
                directory, fake = self.shim("scope-fault-" + case)
                store = processes.ProcessMarkerStore(self.base / ("markers-" + case))
                native = []
                gate = self.base / ("probe-release-" + case)

                def marked_probe(argv, **kwargs):
                    self.assertEqual(argv[1:], ["--version"])
                    result = actual_spawn(
                        [
                            sys.executable,
                            "-c",
                            "import os,pathlib,sys,time; gate=pathlib.Path(sys.argv[1]);\n"
                            "while not gate.exists(): time.sleep(.005)\n"
                            "os.execv(sys.executable,[sys.executable,sys.argv[2],'--scenario',sys.argv[3],'--version'])",
                            str(gate),
                            str(FAKE_CLANGD),
                            str(fake.scenario_path),
                        ],
                        **kwargs,
                    )
                    process, marker = result
                    self.assertIsNotNone(marker, "Live native startup must establish a real marker")
                    self.assertEqual(json.loads(marker.read_bytes())["pid"], process.pid)
                    gate.touch()
                    return result

                def corrupt_after_actual_cleanup(process, *args, **kwargs):
                    scope_confirmed = actual_cleanup(process, *args, **kwargs)
                    self.assertTrue(scope_confirmed, process.cleanup_receipt)
                    original = process.cleanup_receipt
                    self.assertIs(original["scope_confirmed"], True)
                    receipt = dict(original)
                    if case == "missing":
                        receipt.pop("coverage")
                    elif case == "wrong-coverage":
                        receipt["coverage"] = "all_descendants"
                    elif case == "false-scope":
                        receipt["scope_confirmed"] = False
                    else:
                        receipt["scope_confirmed"] = "true"
                    process.cleanup_receipt = receipt
                    native.append((process, receipt))
                    # An actual successful reap plus the compatibility boolean
                    # cannot bypass malformed scope evidence at this consumer.
                    return scope_confirmed

                with (
                    only_on_path(directory),
                    mock.patch.object(runtime, "_probe_cache", {}),
                    mock.patch.object(runtime, "ProcessMarkerStore", return_value=store),
                    mock.patch.object(runtime, "popen_owned", side_effect=marked_probe),
                    mock.patch.object(
                        runtime, "terminate_process_group", side_effect=corrupt_after_actual_cleanup
                    ),
                ):
                    failed = resolve(make_context(self.project.root))
                    self.assert_unavailable(failed)
                    cached = resolve(make_context(self.project.root))
                    self.assert_unavailable(cached)
                self.assertEqual(len(native), 1, "Cached failure must retain the original evidence")
                process, receipt = native[0]
                self.assertEqual(field(failed, "cleanup_receipt"), receipt)
                self.assertEqual(field(cached, "cleanup_receipt"), receipt)
                self.assertEqual(len(fake.events("version_probe")), 1)
                markers = list(store.root.glob("*.json"))
                self.assertEqual(len(markers), 1, "Unaccepted proof retains real recovery evidence")
                self.assertEqual(json.loads(markers[0].read_bytes())["pid"], process.pid)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(process.pid, os.WNOHANG)

    def test_status_first_hung_probe_is_identity_cached_and_does_not_start_lsp(self) -> None:
        """No-argument status owns the probe cap; it has no expiring caller budget."""
        runtime = analysis_module("code_analysis_runtime")
        processes = analysis_module("kernel.processes")
        directory, fake = self.shim(scenario={"version": {"sleep": 60}})
        executable = next(directory.iterdir()).resolve()
        store = processes.ProcessMarkerStore(self.base / "status-probe-markers")
        children: list[tuple[Any, Path]] = []
        identities: list[dict[str, Any]] = []
        original_spawn = runtime.popen_owned

        def observed_spawn(*args: Any, **kwargs: Any) -> Any:
            process, marker = original_spawn(*args, **kwargs)
            children.append((process, marker))
            identities.append(json.loads(marker.read_text(encoding="utf-8")))
            return process, marker

        def cleanup_children() -> None:
            for process, marker in children:
                if process.poll() is None:
                    self.assertTrue(processes.terminate_process_group(process))
                    store.remove(marker)

        self.addCleanup(cleanup_children)
        service = make_service(
            self.project.root, program_resolver=runtime.resolve_analysis_program, testcase=self
        )
        inputs = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (executable, self.project.root / "compile_commands.json")
        }
        calls = []
        with (
            only_on_path(directory),
            mock.patch.object(runtime, "_probe_cache", {}),
            mock.patch.object(runtime, "ProcessMarkerStore", return_value=store),
            mock.patch.object(runtime, "popen_owned", side_effect=observed_spawn),
        ):
            for _ in range(2):
                started = time.monotonic()
                envelope = service.status()
                calls.append(
                    {
                        "envelope": envelope,
                        "elapsed": time.monotonic() - started,
                        "probe_children": len(children),
                        "cache_entries": len(runtime._probe_cache),
                    }
                )
            # Record both public calls before asserting, so RED proves the repeat.
            print(
                "STATUS_FIRST_HUNG_PROBE "
                + json.dumps(
                    {
                        "calls": calls,
                        "owned_identities": identities,
                        "child_exits": [process.poll() for process, _ in children],
                        "remaining_markers": list(map(str, store.root.glob("*.json"))),
                    }
                )
            )
            for index, call in enumerate(calls):
                with self.subTest(call=index):
                    envelope = call["envelope"]
                    assert_envelope(self, envelope, "status", status="unavailable")
                    self.assertEqual(envelope["code"], "analysis/executable-unavailable")
                    self.assertIn("5-second", envelope["message"])
                    self.assertTrue(same_path(envelope["data"]["executable"], executable))
                    self.assertEqual(envelope["data"]["executable_origin"], "path")
                    self.assertLess(call["elapsed"], PROBE_BOUND)
                    self.assertEqual(call["probe_children"], 1, "unchanged status re-probed")
                    self.assertEqual(call["cache_entries"], 1, "own-cap failure was not cached")
            self.assertEqual(len(fake.events("version_probe")), 1)
            self.assertEqual(fake.starts(), [], "status must never initialize LSP")
            self.assertIsNone(service._adapter)
            self.assertTrue(all(process.poll() is not None for process, _ in children))
            self.assertEqual(list(store.root.glob("*.json")), [])
            self.assertEqual(
                {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}, inputs
            )

    def test_expired_caller_budget_does_not_poison_an_independent_probe(self) -> None:
        directory, fake = self.shim()
        context = make_context(self.project.root)
        with only_on_path(directory):
            with self.assertRaises(TimeoutError):
                resolve(context, budget=0.0)
            self.assertEqual(fake.events("version_probe"), [], "an expired caller probed")
            self.assertEqual(fake.starts(), [], "an expired resolver started LSP")
            retry = resolve(context, budget=10.0)
            self.assertTrue(usable(retry), "caller deadline is not executable failure evidence")
            self.assertEqual(len(fake.events("version_probe")), 1)
            self.assertEqual(fake.starts(), [], "version probe must not initialize LSP")
            # The public definition control may reach its legitimate transport
            # startup through the cached successful probe before caller expiry.
            self.assert_public_caller_expiry_is_incomplete_timeout()
            self.assertTrue(usable(resolve(context, budget=10.0)))
        self.assertEqual(len(fake.events("version_probe")), 1)
        starts = fake.starts()
        self.assertLessEqual(len(starts), 1, "Only the public definition may start one transport")
        for start in starts:
            self.assertEqual(
                start["argv"],
                [
                    "--enable-config",
                    "--background-index",
                    "--limit-references=0",
                    "--limit-results=0",
                    "--compile-commands-dir=" + str(self.project.root),
                    "--query-driver=",
                ],
            )
            self.assertTrue(same_path(Path(start["cwd"]), self.project.root))

    def test_nonzero_probe_is_unavailable(self) -> None:
        directory, fake = self.shim(
            scenario={"version": {"exit": 3, "banner": "clangd version 99"}}
        )
        with only_on_path(directory):
            program = resolve(make_context(self.project.root))
        self.assert_unavailable(program)
        self.assertEqual(len(fake.events("version_probe")), 1)

    def test_hung_probe_is_bounded_and_its_child_is_gone(self) -> None:
        directory, fake = self.shim(scenario={"version": {"sleep": 60}})
        started = time.monotonic()
        with only_on_path(directory):
            program = resolve(make_context(self.project.root), budget=60.0)
        elapsed = time.monotonic() - started
        self.assert_unavailable(program)
        self.assertLess(elapsed, PROBE_BOUND, "the probe budget is 5 s plus confirmed cleanup")
        pids = [event["pid"] for event in fake.events("version_probe")]
        self.assertTrue(pids, "the probe ran")
        self.assertEqual(
            wait_pids_gone(pids, OWNED_CLEANUP_SECONDS), [], "probe child outlived its budget"
        )

    def test_expired_deadline_returns_promptly(self) -> None:
        directory, fake = self.shim(scenario={"version": {"sleep": 60}})
        started = time.monotonic()
        with only_on_path(directory):
            with self.assertRaises(TimeoutError):
                resolve(make_context(self.project.root), budget=0.0)
            elapsed = time.monotonic() - started
            self.assert_public_caller_expiry_is_incomplete_timeout()
        self.assertLess(elapsed, OWNED_CLEANUP_SECONDS + SCHEDULING_SLACK_SECONDS)
        pids = [event["pid"] for event in fake.events("version_probe")]
        self.assertEqual(
            wait_pids_gone(pids, OWNED_CLEANUP_SECONDS), [], "an expired probe left a child"
        )

    def test_probe_results_are_cached_until_the_executable_changes(self) -> None:
        directory, fake = self.shim()
        context = make_context(self.project.root)
        with only_on_path(directory):
            first = resolve(context)
            second = resolve(context)
            self.assertEqual(
                len(fake.events("version_probe")), 1, "unchanged identity reuses the probe"
            )
            self.assertEqual(field(first, "version"), field(second, "version"))
            shim = next(directory.iterdir())
            shim.write_bytes(shim.read_bytes() + (b"\r\n" if os.name == "nt" else b"\n"))
            resolve(context)
        self.assertEqual(len(fake.events("version_probe")), 2, "changed bytes must be re-probed")


if __name__ == "__main__":
    unittest.main()
