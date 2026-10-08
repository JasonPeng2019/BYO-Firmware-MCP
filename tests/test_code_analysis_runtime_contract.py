"""Program resolution: managed runtime versus PATH, bounded cached probes.

Contract "Runtime resolution": ``code_analysis_runtime.resolve_analysis_program(
context, name, deadline)`` is the single resolver.  Managed launches never
substitute PATH; source/development uses a supplied runtime first, then PATH,
and reports the origin.  ``--version`` probes are owned, bounded (5 s plus
confirmed cleanup), cached by canonical identity and changed bytes; a nonzero or
timed-out probe is unavailable evidence.  The physical manifest mapping is a
packaging prerequisite, so only its failure-closed behavior is asserted here.
"""

from __future__ import annotations

import contextlib
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
    field,
    isolated_path_environment,
    make_context,
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
                self.assertEqual(argv, [str(executable.resolve()), "--version"])
                return original(
                    [
                        sys.executable,
                        str(FAKE_CLANGD),
                        "--scenario",
                        str(fake.scenario_path),
                        "--version",
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
    def test_expired_caller_budget_does_not_poison_an_independent_probe(self) -> None:
        directory, fake = self.shim()
        context = make_context(self.project.root)
        with only_on_path(directory):
            expired = resolve(context, budget=0.0)
            self.assert_unavailable(expired)
            retry = resolve(context, budget=10.0)
        self.assertTrue(usable(retry), "caller deadline is not executable failure evidence")
        self.assertEqual(len(fake.events("version_probe")), 1)
        self.assertEqual(fake.starts(), [], "version probe must not initialize LSP")

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
            program = resolve(make_context(self.project.root), budget=0.0)
        self.assertLess(
            time.monotonic() - started, OWNED_CLEANUP_SECONDS + SCHEDULING_SLACK_SECONDS
        )
        self.assert_unavailable(program)
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
