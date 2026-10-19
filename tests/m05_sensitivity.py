"""Isolated close-on-paired-prompt mutant, with an unchanged FULL/pending control."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--defect", choices=("lifetime", "c01"), default="lifetime")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    cases = [
        "tests.test_m05_r01_independent.IndependentR01Tests.test_c01_fresh_process_live_binding_partial_policy_and_operations"
        if args.defect == "c01"
        else "tests.test_m05_public_lifecycle.PublicLifecycleTests.test_new_lite_prompt_is_answerable_after_research_fix",
        "tests.test_m05_public_lifecycle.PublicLifecycleTests.test_full_installed_control_and_m03_pending_stop",
    ]
    results = []
    for mode in ("candidate", "mutant"):
        isolated = output / mode
        for directory in ("src", "tests"):
            shutil.copytree(
                root / directory,
                isolated / directory,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        target = (
            isolated
            / "src"
            / "pyocd_debug_mcp"
            / ("server.py" if args.defect == "c01" else "setup_flow/setup.py")
        )
        before = sha(target)
        if mode == "mutant":
            source = target.read_text(encoding="utf-8")
            old = (
                '    state = _tier_router.require_safe("board_validate", board_id)\n'
                if args.defect == "c01"
                else "            if terminal not in PENDING_SETUP_STATUSES:\n"
            )
            assert source.count(old) == 1
            replacement = (
                old
                + '    if state.tier is Tier.SETUP_LITE:\n        raise TargetConnectionError("C01 reproduction: board_validate requires FULL identity policy")\n'
                if args.defect == "c01"
                else "            if repair or terminal not in PENDING_SETUP_STATUSES:\n"
            )
            target.write_text(
                source.replace(old, replacement),
                encoding="utf-8",
                newline="\n",
            )
        env = {
            **os.environ,
            "PYTHONPATH": str(isolated / "src"),
            "BYO_MANAGED_TEST_EVIDENCE": str(isolated / "evidence"),
            "BYO_MCP_ARTIFACT_ROOT": str(isolated / "project"),
            "BYO_MCP_MONITOR_ROOT": str(isolated / "monitor"),
        }
        argv = [sys.executable, "-m", "unittest", "-v", *cases]
        record = {
            "mode": mode,
            "started_at": now(),
            "argv": argv,
            "cwd": str(isolated),
            "source_before_sha256": before,
            "source_after_sha256": sha(target),
            "environment_overrides": {
                key: env[key]
                for key in (
                    "PYTHONPATH",
                    "BYO_MANAGED_TEST_EVIDENCE",
                    "BYO_MCP_ARTIFACT_ROOT",
                    "BYO_MCP_MONITOR_ROOT",
                )
            },
        }
        with (
            (isolated / "stdout.log").open("wb") as stdout,
            (isolated / "stderr.log").open("wb") as stderr,
        ):
            child = subprocess.run(
                argv, cwd=isolated, env=env, stdout=stdout, stderr=stderr, timeout=120
            )
        record.update(
            finished_at=now(),
            exit_code=child.returncode,
            stdout_sha256=sha(isolated / "stdout.log"),
            stderr_sha256=sha(isolated / "stderr.log"),
        )
        text = (isolated / "stderr.log").read_text()
        expected = child.returncode == (0 if mode == "candidate" else 1)
        expected &= "Ran 2 tests" in text and "ERROR:" not in text and "skipped" not in text
        expected &= "test_full_installed_control_and_m03_pending_stop) ... ok" in text
        if mode == "mutant":
            expected &= (
                "FAILED (failures=1)" in text
                and (
                    "C01 advertised LITE validation must be usable"
                    if args.defect == "c01"
                    else "The new advertised LITE prompt must remain answerable"
                )
                in text
            )
        imported = []
        for manifest in (isolated / "evidence").glob("*.imports.json"):
            for item in json.loads(manifest.read_text()):
                if item["module"].startswith("pyocd_debug_mcp"):
                    path = Path(item["path"])
                    path.relative_to(isolated / "src")
                    assert sha(path) == item["sha256"]
                    imported.append(item)
        assert imported and any(i["module"] == "pyocd_debug_mcp.setup_flow.setup" for i in imported)
        record.update(expected_outcome=bool(expected), imports=imported)
        results.append(record)
        (isolated / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    (output / "sensitivity.json").write_text(
        json.dumps({"helper_sha256": sha(__file__), "results": results}, indent=2) + "\n"
    )
    print(
        json.dumps(
            [
                {
                    key: r[key]
                    for key in (
                        "mode",
                        "started_at",
                        "finished_at",
                        "exit_code",
                        "expected_outcome",
                    )
                }
                for r in results
            ]
        )
    )
    return 0 if all(r["expected_outcome"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
