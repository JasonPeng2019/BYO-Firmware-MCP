"""Run independent M11 oracles on actual archived pre-M11 source.

Extracts files into an external evidence directory; never edits the candidate
or creates a Git worktree. Assertion failures must accompany a passing public
generic-access control. Raw test errors cannot satisfy sensitivity.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    product = Path(__file__).resolve().parents[1]
    repo = product.parents[1]
    base = "4245212ff4d04314012788473cb4b038a78d7084"
    source_path = "MCP_Server_Dev/BYO-Firmware-MCP/src"
    raw = subprocess.run(
        ["git", "archive", "--format=zip", base, source_path],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    archive = args.output / "pre-m11-source.zip"
    archive.write_bytes(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as content:
        content.extractall(args.output / "archive")
    root = args.output / "archive" / "MCP_Server_Dev" / "BYO-Firmware-MCP"
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    for name in ("tiered_acceptance_support.py", "test_m11_independent_public_selection.py"):
        shutil.copyfile(product / "tests" / name, root / "tests" / name)
    shutil.copytree(
        product / "tests" / "tiered_acceptance_fixtures",
        root / "tests" / "tiered_acceptance_fixtures",
    )
    runner = root / "run_oracle.py"
    runner.write_text(
        """import hashlib, json, pathlib, sys, unittest
import pyocd_debug_mcp.server as server
path = pathlib.Path(server.__file__).resolve()
assert path.is_relative_to(pathlib.Path.cwd() / "src"), path
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[2:])
result = unittest.TextTestRunner(verbosity=2).run(suite)
pathlib.Path(sys.argv[1]).write_text(json.dumps({"source": str(path),
 "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
 "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
 "skips": len(result.skipped)}) + "\\n")
sys.exit(0 if result.wasSuccessful() else 1)
""",
        encoding="utf-8",
    )
    prefix = "tests.test_m11_independent_public_selection.PublicSelectionProof."
    cases = {
        "negative": [
            prefix + name
            for name in (
                "test_unknown_label_needs_intent_even_with_single_probe_or_target",
                "test_known_stm_missing_probe_preserves_profile_binding_and_validation",
                "test_provable_catalog_mismatch_refuses_before_open_or_reset",
            )
        ],
        "control": [
            prefix + "test_catalog_generic_core_and_same_vendor_variants_keep_useful_raw_access"
        ],
    }
    report = {
        "base": base,
        "started_at": now(),
        "archive_sha256": hashlib.sha256(raw).hexdigest(),
        "runs": {},
    }
    for label, names in cases.items():
        result_path = args.output / (label + "-result.json")
        argv = [sys.executable, str(runner), str(result_path), *names]
        env = dict(
            os.environ,
            PYTHONPATH=str(root / "src"),
            BYO_MCP_ARTIFACT_ROOT=str(args.output / (label + "-import")),
            BYO_MCP_MONITOR_ROOT=str(args.output / (label + "-monitor")),
            M11_JOURNAL=str(args.output / (label + "-journal.jsonl")),
        )
        started = now()
        completed = subprocess.run(argv, cwd=root, env=env, capture_output=True)
        (args.output / (label + ".log")).write_bytes(completed.stdout + completed.stderr)
        report["runs"][label] = {
            "argv": argv,
            "cwd": str(root),
            "started_at": started,
            "completed_at": now(),
            "exit_code": completed.returncode,
            "environment": {
                k: env[k]
                for k in (
                    "PYTHONPATH",
                    "BYO_MCP_ARTIFACT_ROOT",
                    "BYO_MCP_MONITOR_ROOT",
                    "M11_JOURNAL",
                )
            },
            "result": json.loads(result_path.read_text()) if result_path.exists() else None,
        }
    negative = report["runs"]["negative"]
    control = report["runs"]["control"]
    report["sensitive"] = (
        negative["exit_code"] == 1
        and negative["result"] is not None
        and negative["result"]["failures"] >= 3
        and negative["result"]["errors"] == 0
        and negative["result"]["skips"] == 0
        and control["exit_code"] == 0
        and control["result"]["tests"] == 1
        and control["result"]["errors"] == 0
    )
    report["completed_at"] = now()
    (args.output / "sensitivity.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report))
    return 0 if report["sensitive"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
