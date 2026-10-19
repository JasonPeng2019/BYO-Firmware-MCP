"""Run independent M10 tests on isolated source mutations, leaving candidate intact.

Each mutant must fail its targeted oracle and pass the unchanged short-wait
control. The copied import paths, exact changes, UTC/argv/raw exits and output
are retained. This is mutation sensitivity, not a frozen old-source claim.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


PROJECT = Path(__file__).resolve().parents[1]
PACKAGE = PROJECT / "src" / "pyocd_debug_mcp"
TEST = "tests.test_m10_independent_wait.IndependentWaitTests."
CONTROL = TEST + "test_short_wait_and_existing_integer_normalization_control"


def snapshot(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*.py")}


def run(root, env, label, test):
    command = [sys.executable, "-c", """
import hashlib, json, os, pathlib, unittest
from pyocd_debug_mcp.tools import misc
from pyocd_debug_mcp.kernel import operations, registry
for module in (misc, operations, registry):
    path = pathlib.Path(module.__file__).resolve()
    assert path.is_relative_to(pathlib.Path(os.environ['M10_MUTANT_ROOT']).resolve()), path
    print(json.dumps(dict(module=module.__name__, path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())), flush=True)
unittest.main(module=None, argv=['sensitivity', '-v', os.environ['M10_TEST']])
"""
    ]
    env = {**env, "M10_TEST": test}
    start = datetime.now(timezone.utc).isoformat()
    tick = time.perf_counter()
    with (root / (label + ".log")).open("wb") as log:
        process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            code = process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False)
            else:
                process.kill()
            process.wait(timeout=30)
            raise AssertionError("sensitivity exceeded 90-second cleanup bound") from None
    record = dict(argv=command, cwd=str(root), test=test, started_utc=start,
                  ended_utc=datetime.now(timezone.utc).isoformat(),
                  elapsed_seconds=time.perf_counter()-tick, exit_code=code,
                  environment={k: v for k, v in env.items() if k.startswith(("M10_", "BYO_", "PYTHONPATH"))})
    (root / (label + ".json")).write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record), flush=True)
    return code, (root / (label + ".log")).read_text(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    before = snapshot(PACKAGE)
    mutations = [
        ("no-admission-maximum", "tools/misc.py", "Field(ge=1, le=MAX_WAIT_DURATION_MS)",
         "Field(ge=1)", "test_oversized_and_nonpositive_refuse_before_operation_reservation_or_target"),
        ("no-wait-cancellation-wake", "kernel/operations.py", "            self.cancellation_requested.set()",
         '            if self.tool_name != "wait":\n                self.cancellation_requested.set()',
         "test_cancelled_handler_exit_and_cleanup_own_slot_until_immediate_successor"),
    ]
    results = []
    for label, relative, original, replacement, targeted in mutations:
        root = evidence / label
        copied = root / "src" / "pyocd_debug_mcp"
        shutil.copytree(PACKAGE, copied, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        path = copied / relative
        source = path.read_text(encoding="utf-8")
        assert source.count(original) == 1, (label, source.count(original))
        path.write_text(source.replace(original, replacement), encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(PROJECT)]),
               "M10_MUTANT_ROOT": str(root), "BYO_MCP_ARTIFACT_ROOT": str(root / "project"),
               "BYO_MCP_MONITOR_ROOT": str(root / "monitor"),
               "BYO_MANAGED_TEST_EVIDENCE": str(root / "managed-analysis")}
        (root / "project").mkdir()
        failing, log = run(root, env, "targeted", TEST + targeted)
        control, control_log = run(root, env, "control", CONTROL)
        assert failing == 1 and "FAIL:" in log and "ERROR:" not in log, log
        assert control == 0 and "OK" in control_log, control_log
        results.append(dict(mutation=label, path=relative, original=original, replacement=replacement,
                            target_exit=failing, control_exit=control, copied_files=snapshot(copied)))
    assert snapshot(PACKAGE) == before, "candidate source changed"
    (evidence / "summary.json").write_text(json.dumps(dict(
        outcome="PASS", candidate_source_unchanged=True, source_hashes=before, results=results,
    ), indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
