"""Compare independent M04 tests against disposable current and frozen source.

Neither checkout source nor Git state is changed. Only source is taken from the
old revision; both children run the same current public-dispatch regressions.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


BASELINE = "0174ad36b485a20aadffd179fba1b3b35f2a0420"
SOURCE_PATH = "MCP_Server_Dev/BYO-Firmware-MCP/src"
PREFIX = "tests.test_b01_setup_public.SetupPublicTests."
EXPECTED_FAILURES = {
    PREFIX + "test_incompatible_installed_choice_is_absent",
    PREFIX + "test_geometry_and_shared_cpuid_do_not_admit_incompatible_installed_support",
    PREFIX + "test_same_family_device_prefix_does_not_establish_installed_support",
}
CHILD = """
import json, pathlib, sys, unittest
import pyocd_debug_mcp
source = pathlib.Path(pyocd_debug_mcp.__file__).resolve()
assert source.is_relative_to(pathlib.Path.cwd() / 'src'), source
suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_b01_setup_public')
result = unittest.TextTestRunner(verbosity=2).run(suite)
pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(
    imported_source=str(source), tests_run=result.testsRun,
    failures=[test.id() for test, _ in result.failures],
    errors=[test.id() for test, _ in result.errors],
    skipped=[test.id() for test, _ in result.skipped],
), indent=2) + '\\n', encoding='utf-8')
raise SystemExit(0 if result.wasSuccessful() else 1)
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    server = Path(__file__).resolve().parents[1]

    def git(*arguments):
        # Git archive limits paths to cwd, even when passed a subtree tree-ish.
        return subprocess.check_output(["git", *arguments], cwd=server.parents[1])

    entries = []
    for variant in ("current", "pre-repair"):
        with tempfile.TemporaryDirectory(prefix="b01-m04-baseline-") as temporary:
            isolated = Path(temporary).resolve()
            shutil.copytree(
                server / "tests", isolated / "tests",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            archive_hash = None
            if variant == "current":
                shutil.copytree(
                    server / "src", isolated / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                revision = git("rev-parse", "HEAD").decode().strip()
            else:
                revision = BASELINE
                # Trusted Git archive, containing just the tracked frozen src.
                archive = git("archive", "--format=zip", f"{BASELINE}:{SOURCE_PATH}")
                archive_hash = hashlib.sha256(archive).hexdigest()
                with zipfile.ZipFile(io.BytesIO(archive)) as source:
                    assert "pyocd_debug_mcp/__init__.py" in source.namelist()
                    source.extractall(isolated / "src")
            summary = output / f"{variant}-tests.json"
            command = [sys.executable, "-B", "-c", CHILD, str(summary)]
            environment = {
                **os.environ,
                "PYTHONPATH": str(isolated / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "BYO_MANAGED_TEST_EVIDENCE": str(output / "managed-analysis" / variant),
            }
            started = now()
            completed = subprocess.run(
                command, cwd=isolated, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=120, check=False,
            )
            log = output / f"{variant}.log"
            log.write_text(completed.stdout, encoding="utf-8")
            result = json.loads(summary.read_text(encoding="utf-8"))
            entry = dict(
                variant=variant, revision=revision,
                source_tree=git("rev-parse", f"{revision}:{SOURCE_PATH}").decode().strip(),
                source_archive_sha256=archive_hash,
                test_sha256=hashlib.sha256(
                    (isolated / "tests" / "test_b01_setup_public.py").read_bytes()
                ).hexdigest(),
                command=command, cwd=str(isolated), started_at=started,
                completed_at=now(), exit_code=completed.returncode, log=str(log),
                **result,
            )
            entries.append(entry)
            (output / "baseline.json").write_text(
                json.dumps(entries, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(entry), flush=True)
            assert not result["errors"] and not result["skipped"], entry
            assert result["tests_run"] == 8, entry
            assert completed.returncode == (0 if variant == "current" else 1), entry
            assert set(result["failures"]) == (
                set() if variant == "current" else EXPECTED_FAILURES
            ), entry
            # The five unaffected tests include both installed controls, the
            # live-CPUID refusal, nested LITE, and usable synthetic pack route.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
