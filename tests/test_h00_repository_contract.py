"""Adversarial repository-contract tests for H00 CL-001 through CL-005."""

from __future__ import annotations

import ctypes as real_ctypes
import importlib.util
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import psutil
from pyocd_debug_mcp.kernel import hygiene as canonical_hygiene
from pyocd_debug_mcp.kernel import processes as canonical_processes

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: pytest brings the locked tomli dependency.
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "uv.lock"
README = ROOT / "README.md"
CANDIDATE_MANIFEST_NAME = "H00_FINAL_CANDIDATE_MANIFEST.json"
CANDIDATE_MANIFEST_SHA256_NAME = "H00_FINAL_CANDIDATE_MANIFEST.sha256"
INNER_CANDIDATE_ENV = "H00_SPEC_INNER_CANDIDATE"
FIXED_CANDIDATE_PATHS = (
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "src/pyocd_debug_mcp/kernel/processes.py",
    "tests/test_h00_repository_contract.py",
    "tests/test_h00_repository_regressions.py",
)

_PROCESSES_SPEC = importlib.util.spec_from_file_location(
    "h00_processes", ROOT / "src" / "pyocd_debug_mcp" / "kernel" / "processes.py"
)
assert _PROCESSES_SPEC is not None and _PROCESSES_SPEC.loader is not None
processes = importlib.util.module_from_spec(_PROCESSES_SPEC)
sys.modules[_PROCESSES_SPEC.name] = processes
_PROCESSES_SPEC.loader.exec_module(processes)


class H00RepositoryContractTests(unittest.TestCase):
    def _sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _candidate_hashes(self, root: Path) -> dict[str, str]:
        return {relative: self._sha256(root / relative) for relative in FIXED_CANDIDATE_PATHS}

    def _run(
        self, command: list[str], *, cwd: Path, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command, cwd=cwd, env=env, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _remove_candidate_tree(self, path: Path) -> None:
        """Remove transient Windows-native locks rather than leaking a test environment."""
        def clear_readonly(
            function: Callable[[str], object],
            failed_path: str,
            _error: tuple[type[BaseException], BaseException, types.TracebackType | None],
        ) -> None:
            os.chmod(failed_path, stat.S_IWRITE)
            function(failed_path)

        last_error: PermissionError | None = None
        deadline = time.monotonic() + 5.0
        while path.exists():
            try:
                shutil.rmtree(path, onerror=clear_readonly)
                return
            except PermissionError as error:
                last_error = error
                if not path.exists():
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
        if not path.exists():
            return
        self.fail(f"candidate cleanup failed after bounded retries: {last_error}")

    def _fake_unavailable_ctypes(self) -> types.ModuleType:
        fake = types.ModuleType("ctypes")
        fake.__dict__.update(real_ctypes.__dict__)
        fake.windll = types.SimpleNamespace()
        fake.__dict__.pop("get_last_error", None)
        return fake

    def _stop_candidate_descendants(self, candidate: Path) -> None:
        candidate_text = str(candidate).lower()
        descendants: list[psutil.Process] = []
        for process in psutil.process_iter(["cmdline", "cwd", "exe"]):
            try:
                locations = [
                    process.info["exe"] or "", process.info["cwd"] or "",
                    *(process.info["cmdline"] or []),
                ]
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
            if any(candidate_text in location.lower() for location in locations):
                descendants.append(process)
        for process in descendants:
            process.terminate()
        _, alive = psutil.wait_procs(descendants, timeout=10)
        for process in alive:
            process.kill()
        _, alive = psutil.wait_procs(alive, timeout=10)
        self.assertFalse(alive, f"candidate processes survived verification: {alive}")

    def test_cl001_pytest_is_declared_and_locked_as_a_dev_tool(self) -> None:
        """The locked default dev environment must contain the actual test runner."""
        project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        dev = project["dependency-groups"]["dev"]
        self.assertIn("pytest>=8", dev)
        self.assertIn("pyright>=1.1", dev)
        self.assertIn("ruff>=0.6", dev)

        lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
        package_by_name = {package["name"]: package for package in lock["package"]}
        self.assertIn("pytest", package_by_name)
        root_package = package_by_name[project["project"]["name"]]
        self.assertEqual(
            {entry["name"] for entry in root_package["dev-dependencies"]["dev"]},
            {"pyright", "pytest", "ruff"},
        )
        self.assertIn(
            {"name": "pytest", "specifier": ">=8"},
            root_package["metadata"]["requires-dev"]["dev"],
        )

    def test_cl001_lockfile_agrees_with_metadata_without_relocking(self) -> None:
        before = LOCK.read_bytes()
        result = subprocess.run(
            ["uv", "lock", "--check"], cwd=ROOT, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(LOCK.read_bytes(), before, "lock validation must not rewrite uv.lock")

    def test_cl001_spec_suite_executes_its_tomli_fallback_on_python_310(self) -> None:
        project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["requires-python"], ">=3.10")
        self._run(
            [
                "uv", "run", "--locked", "--isolated", "--python", "3.10", "python", "-c",
                "import runpy; scope = runpy.run_path(r'tests/test_h00_repository_contract.py', "
                "run_name='h00_python310_probe'); assert scope['tomllib'].__name__ == 'tomli'",
            ],
            cwd=ROOT,
        )

    def test_cl002_pyright_has_only_the_explicit_shipped_source_scope(self) -> None:
        project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        self.assertEqual(project.get("tool", {}).get("pyright"), {"include": ["src"]})

        shipped = ROOT / "src" / "pyocd_debug_mcp"
        self.assertTrue(shipped.is_dir(), "the configured source scope must contain the shipped package")
        self.assertTrue(any(shipped.rglob("*.py")), "no production Python modules found in Pyright scope")

    def test_cl002_default_locked_pyright_command_checks_the_declared_scope(self) -> None:
        with tempfile.TemporaryDirectory(prefix="h00 pyright candidate ") as directory:
            candidate = Path(directory)
            for relative_path in ("pyproject.toml", "uv.lock", "README.md"):
                shutil.copy2(ROOT / relative_path, candidate / relative_path)
            shutil.copytree(ROOT / "src", candidate / "src")
            candidate_environment = {**os.environ, "UV_LINK_MODE": "copy"}
            sync = subprocess.run(
                ["uv", "sync", "--locked"],
                cwd=candidate,
                env=candidate_environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(sync.returncode, 0, sync.stdout + sync.stderr)
            result = subprocess.run(
                ["uv", "run", "--locked", "--no-sync", "pyright"],
                cwd=candidate,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cl002_pyright_detects_an_injected_source_type_error_outside_the_checkout(self) -> None:
        """A no-argument scope must not hide genuine errors below ``src``."""
        with tempfile.TemporaryDirectory(prefix="h00 pyright candidate ") as directory:
            candidate = Path(directory)
            shutil.copy2(ROOT / "README.md", candidate / "README.md")
            shutil.copy2(ROOT / "pyproject.toml", candidate / "pyproject.toml")
            shutil.copy2(ROOT / "uv.lock", candidate / "uv.lock")
            shutil.copytree(ROOT / "src", candidate / "src")
            injected = candidate / "src" / "pyocd_debug_mcp" / "h00_pyright_negative_control.py"
            injected.write_text('value: int = "not an int"\n', encoding="utf-8")
            candidate_environment = {**os.environ, "UV_LINK_MODE": "copy"}
            sync = subprocess.run(
                ["uv", "sync", "--locked"],
                cwd=candidate,
                env=candidate_environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(sync.returncode, 0, sync.stdout + sync.stderr)
            result = subprocess.run(
                ["uv", "run", "--locked", "--no-sync", "pyright"],
                cwd=candidate,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(injected.name, result.stdout + result.stderr)

    def test_cl004_windows_creationflags_fall_back_without_subprocess_names(self) -> None:
        with patch.dict(
            processes.process_group_options.__globals__,
            {"subprocess": types.SimpleNamespace()},
        ):
            self.assertEqual(
                processes.process_group_options(platform="nt"),
                {"creationflags": 0x00000204},
            )

    def test_cl001_windows_identity_normalizes_native_access_failures_but_jobs_do_not(
        self,
    ) -> None:
        fake_ctypes = self._fake_unavailable_ctypes()
        process = types.SimpleNamespace(pid=42, _handle=99)
        with patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            for label, callback in (
                ("create_job", lambda: processes._create_windows_kill_job(process)),
                ("resume", lambda: processes._resume_windows_process(process)),
                (
                    "close_job",
                    lambda: processes._close_windows_job(42, 7, terminate=True, deadline=0.0),
                ),
            ):
                with self.subTest(helper=label):
                    with self.assertRaises(OSError) as raised:
                        callback()
                    self.assertIsNotNone(raised.exception.__cause__)
                    self.assertRegex(str(raised.exception).lower(), r"windows|api|loader")

            with self.assertRaises(processes.ProcessIdentityUnavailable) as raised:
                processes._windows_start_token(42)
            self.assertIsInstance(raised.exception.__cause__, OSError)

    def test_cl001_windows_identity_wraps_each_native_access_phase(self) -> None:
        class MissingOpenProcess:
            def __getattr__(self, name: str) -> object:
                raise AttributeError(name)

        class OpenProcessRaises:
            def OpenProcess(self, *_args: object) -> int:
                raise OSError("OpenProcess unavailable")

        class MissingLastError:
            def OpenProcess(self, *_args: object) -> int:
                return 0

            def __getattr__(self, name: str) -> object:
                raise AttributeError(name)

        class CloseRaises:
            def OpenProcess(self, *_args: object) -> int:
                return 123

            def GetExitCodeProcess(self, _handle: int, exit_code: object) -> bool:
                getattr(exit_code, "_obj").value = 259
                return True

            def GetProcessTimes(self, _handle: int, creation: object, *_args: object) -> bool:
                getattr(creation, "_obj").dwHighDateTime = 1
                getattr(creation, "_obj").dwLowDateTime = 2
                return True

            def CloseHandle(self, _handle: int) -> None:
                raise OSError("CloseHandle unavailable")

        with patch.object(processes, "_windows_library", side_effect=OSError("loader unavailable")):
            with self.assertRaises(processes.ProcessIdentityUnavailable) as raised:
                processes._windows_start_token(42)
        self.assertIsInstance(raised.exception.__cause__, OSError)

        for phase, kernel32 in (
            ("member lookup", MissingOpenProcess()),
            ("native call", OpenProcessRaises()),
            ("last-error lookup", MissingLastError()),
            ("close", CloseRaises()),
        ):
            with self.subTest(phase=phase):
                with self.assertRaises(processes.ProcessIdentityUnavailable) as raised:
                    processes._windows_start_token(42, kernel32=kernel32)
                self.assertIsInstance(raised.exception.__cause__, (AttributeError, OSError))

    def test_cl001_specific_identity_failure_is_not_masked_by_close_failure(self) -> None:
        class Kernel32:
            def OpenProcess(self, *_args: object) -> int:
                return 123

            def GetExitCodeProcess(self, _handle: int, _exit_code: object) -> bool:
                return False

            def GetLastError(self) -> int:
                return 5

            def CloseHandle(self, _handle: int) -> None:
                raise OSError("close must not replace identity failure")

        with self.assertRaises(processes.ProcessIdentityUnavailable) as raised:
            processes._windows_start_token(42, kernel32=Kernel32())
        self.assertIn("liveness", str(raised.exception))
        self.assertIn("Windows error 5", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_cl001_native_identity_failure_remains_primary_when_close_also_fails(self) -> None:
        class Kernel32:
            close_attempted = False

            def OpenProcess(self, *_args: object) -> int:
                return 123

            def GetExitCodeProcess(self, _handle: int, _exit_code: object) -> bool:
                raise OSError("body failure")

            def CloseHandle(self, _handle: int) -> None:
                self.close_attempted = True
                raise OSError("close failure")

        kernel32 = Kernel32()
        with self.assertRaises(processes.ProcessIdentityUnavailable) as raised:
            processes._windows_start_token(42, kernel32=kernel32)
        self.assertTrue(kernel32.close_attempted, "a successful OpenProcess handle must be closed")
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertEqual(str(raised.exception.__cause__), "body failure")

    def test_cl001_hygiene_and_marked_cleanup_fail_closed_for_unavailable_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="h00 unavailable identity ") as directory:
            root = Path(directory)
            marker = canonical_processes.ProcessMarker(
                schema_version=2,
                marker_id="identity-unavailable",
                owner_pid=101,
                owner_start_token="owner-token",
                pid=202,
                start_token="child-token",
                argv_sha256="0" * 64,
                executable="python",
                created_at="2026-01-01T00:00:00+00:00",
            )
            marker_path = root / "identity-unavailable.json"
            marker_path.write_text(json.dumps(asdict(marker)), encoding="utf-8")
            unavailable = canonical_processes.ProcessIdentityUnavailable("native identity unavailable")
            with patch.object(canonical_hygiene, "_start_token", side_effect=unavailable):
                result = canonical_hygiene.cleanup_stale_owned_processes(root)
                self.assertEqual(result.unresolved, 1)
                self.assertTrue(marker_path.exists(), "unresolved marker must be retained")
                with self.assertRaisesRegex(RuntimeError, "1 unresolved marker"):
                    canonical_hygiene.require_clean_startup(root)
            with patch.object(canonical_processes, "_start_token", side_effect=unavailable):
                self.assertFalse(canonical_processes.terminate_marked_group(202, "child-token"))

    def test_cl003_candidate_cleanup_returns_immediately_when_removal_succeeds(self) -> None:
        with tempfile.TemporaryDirectory(prefix="h00 cleanup immediate ") as directory:
            path = Path(directory) / "candidate"
            path.mkdir()
            with patch.object(time, "sleep") as sleep:
                self._remove_candidate_tree(path)
            self.assertFalse(path.exists())
            sleep.assert_not_called()

    def test_cl003_candidate_cleanup_returns_when_tree_is_already_absent(self) -> None:
        path = Path("h00 absent cleanup sentinel")
        with (
            patch.object(Path, "exists", return_value=False),
            patch.object(shutil, "rmtree") as remove,
            patch.object(time, "sleep") as sleep,
        ):
            self._remove_candidate_tree(path)
        remove.assert_not_called()
        sleep.assert_not_called()

    def test_cl003_candidate_cleanup_returns_when_permission_failure_already_removed_tree(self) -> None:
        path = Path("h00 permission cleanup sentinel")
        with (
            patch.object(Path, "exists", side_effect=(True, False)),
            patch.object(shutil, "rmtree", side_effect=PermissionError("released after attempt")) as remove,
            patch.object(time, "monotonic", side_effect=(10.0, 10.1)),
            patch.object(time, "sleep") as sleep,
        ):
            self._remove_candidate_tree(path)
        remove.assert_called_once()
        sleep.assert_not_called()

    def test_cl003_candidate_cleanup_bounds_persistent_permission_errors(self) -> None:
        path = Path("h00 persistent cleanup sentinel")
        error = PermissionError("still locked")
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(shutil, "rmtree", side_effect=error) as remove,
            patch.object(time, "monotonic", side_effect=(10.0, 10.1, 15.1)) as monotonic,
            patch.object(time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(AssertionError, "still locked"):
                self._remove_candidate_tree(path)
        self.assertEqual(remove.call_count, 2)
        self.assertGreaterEqual(monotonic.call_count, 3)
        sleep.assert_called_once_with(0.05)

    @unittest.skipUnless(os.name != "nt", "real POSIX process-group behavior")
    def test_cl005_posix_owned_child_gets_a_new_session_and_real_group_cleanup(self) -> None:
        temp_root = Path(tempfile.mkdtemp(prefix="h00 posix owned child "))
        store = processes.ProcessMarkerStore(temp_root / "markers")
        process, marker = processes.popen_owned(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            marker_store=store,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertEqual(os.getpgid(process.pid), process.pid)
            self.assertTrue(processes.terminate_process_group(process))
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            store.remove(marker)
            self._remove_candidate_tree(temp_root)

    @unittest.skipIf(
        os.environ.get(INNER_CANDIDATE_ENV) == "1",
        "the candidate suite is already being verified by its outer clean-clone transaction",
    )
    def test_cl001_cl002_cl003_cl005_clean_candidate_clone_runs_the_documented_contract(self) -> None:
        """Exercise the uncommitted repair, never a pre-existing local environment."""
        temporary_root = Path(tempfile.mkdtemp(prefix="h00 candidate clone "))
        candidate = temporary_root / "candidate checkout with spaces"
        try:
            unrelated = temporary_root / "unrelated working directory"
            self._run(["git", "clone", str(ROOT), str(candidate)], cwd=temporary_root)
            head = self._run(["git", "rev-parse", "HEAD"], cwd=candidate)
            self.assertEqual(
                head.stdout.strip(),
                "6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876",
                "candidate must be cloned from the exact approved baseline commit",
            )
            self._remove_candidate_tree(candidate / ".git")
            unrelated.mkdir()
            self.assertFalse((candidate / ".venv").exists())
            self.assertFalse((candidate / "dist").exists())

            expected_hashes = self._candidate_hashes(ROOT)
            for relative_path in FIXED_CANDIDATE_PATHS:
                destination = candidate / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative_path, destination)
            self.assertEqual(self._candidate_hashes(candidate), expected_hashes)

            candidate_manifest = candidate / CANDIDATE_MANIFEST_NAME
            candidate_manifest_sha256 = candidate / CANDIDATE_MANIFEST_SHA256_NAME
            has_manifest = candidate_manifest.exists()
            has_manifest_sha256 = candidate_manifest_sha256.exists()
            self.assertEqual(
                has_manifest,
                has_manifest_sha256,
                "candidate manifest controls must appear together or not at all",
            )
            if has_manifest:
                final_manifest = json.loads(candidate_manifest.read_text(encoding="utf-8"))
                self.assertEqual(final_manifest["files"], expected_hashes)
                self.assertEqual(
                    self._sha256(candidate_manifest),
                    candidate_manifest_sha256.read_text(encoding="utf-8").strip(),
                )

            lock = candidate / "uv.lock"
            original_lock = lock.read_bytes()

            def run_locked(
                command: list[str], *, cwd: Path = candidate, env: dict[str, str] | None = None
            ) -> subprocess.CompletedProcess[str]:
                result = self._run(command, cwd=cwd, env=env)
                self.assertEqual(lock.read_bytes(), original_lock, f"command changed uv.lock: {command}")
                self.assertEqual(self._candidate_hashes(candidate), expected_hashes)
                return result

            candidate_environment = {**os.environ, "UV_LINK_MODE": "copy"}
            run_locked(["uv", "sync", "--locked"], env=candidate_environment)
            run_locked(["uv", "lock", "--check"])
            run_locked(["uv", "build"])
            run_locked(
                [
                    "uv", "run", "--project", str(candidate), "--locked", "--no-sync", "python", "-c",
                    "import pyocd_debug_mcp; import pyocd_debug_mcp.server",
                ],
                cwd=unrelated,
            )
            run_locked(["uv", "run", "--locked", "--no-sync", "ruff", "check", "."])
            run_locked(["uv", "run", "--locked", "--no-sync", "pyright"])

            injected = candidate / "src" / "pyocd_debug_mcp" / "h00_pyright_negative_control.py"
            injected.write_text('value: int = "not an int"\n', encoding="utf-8")
            try:
                result = subprocess.run(
                    ["uv", "run", "--locked", "--no-sync", "pyright"],
                    cwd=candidate,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(injected.name, result.stdout + result.stderr)
                self.assertEqual(lock.read_bytes(), original_lock, "negative control changed uv.lock")
            finally:
                injected.unlink(missing_ok=True)

            collected = run_locked(
                ["uv", "run", "--locked", "--no-sync", "pytest", "--collect-only", "-q"]
            )
            count = re.search(r"(\d+) tests collected", collected.stdout + collected.stderr)
            self.assertIsNotNone(count, collected.stdout + collected.stderr)
            self.assertGreater(int(count.group(1)), 0)
            inner_environment = {**os.environ, INNER_CANDIDATE_ENV: "1"}
            run_locked(["uv", "run", "--locked", "--no-sync", "pytest"], env=inner_environment)
        finally:
            try:
                self._stop_candidate_descendants(candidate)
            finally:
                self._remove_candidate_tree(temporary_root)
                self.assertFalse(temporary_root.exists(), "candidate cleanup left a temporary root")

    def test_cl003_readme_has_one_ordered_portable_verifier_contract(self) -> None:
        readme = README.read_text(encoding="utf-8").replace("\r\n", "\n")
        heading = "## Contributor and verifier checks"
        self.assertIn(heading, readme)
        section = readme.split(heading, 1)[1].split("\n## ", 1)[0]
        commands = [
            "uv sync --locked",
            "uv lock --check",
            "uv build",
            'uv run --project "<absolute-path-to-checkout>" --locked --no-sync python -c "import pyocd_debug_mcp; import pyocd_debug_mcp.server"',
            "uv run --locked --no-sync ruff check .",
            "uv run --locked --no-sync pyright",
            "uv run --locked --no-sync pytest --collect-only -q",
            "uv run --locked --no-sync pytest",
        ]
        positions = []
        for command in commands:
            matches = list(re.finditer(rf"(?m)^{re.escape(command)}$", section))
            self.assertEqual(len(matches), 1, f"missing or duplicated verifier command: {command}")
            positions.append(matches[0].start())
        self.assertEqual(positions, sorted(positions), "verifier commands must be in executable order")

        normalized = " ".join(section.lower().split())
        for required_phrase in (
            "default pyright command checks the shipped `src` package",
            "test code is outside that typecheck gate",
            "complete pytest suite",
            "missing tool",
            "rerun `uv sync --locked`",
            "lock and metadata disagree",
            "intentional dependency change",
            "project location",
            "replace the quoted path placeholder",
        ):
            self.assertIn(" ".join(required_phrase.split()), normalized)

        forbidden_path_markers = ("c:\\\\users\\", "/users/", "/home/", "mcp-trial-3")
        self.assertFalse(
            any(marker in section.lower() for marker in forbidden_path_markers),
            "the verifier section must not contain a machine-specific checkout path",
        )


if __name__ == "__main__":
    unittest.main()
