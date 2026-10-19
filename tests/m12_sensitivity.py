"""Execute independent public oracles against frozen pre-M12 source externally."""

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


PRIOR = "1e1a26286b301591c4e50bc9a7f822d35b380be3"


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prior", default=PRIOR)
    parser.add_argument(
        "--required-case",
        default="tests.test_m12_independent_recovery.IndependentRecoveryTests.test_disconnected_instructions_and_backend_then_fresh_validation",
    )
    parser.add_argument(
        "--control-case",
        default="tests.test_m12_independent_recovery.IndependentRecoveryTests.test_normal_profile_control_requires_new_validation_and_keeps_other_guidance",
    )
    parser.add_argument("--required-text", default="Use while disconnected")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    product = Path(__file__).resolve().parents[1]
    raw = subprocess.run(
        ["git", "archive", "--format=zip", args.prior, "MCP_Server_Dev/BYO-Firmware-MCP/src"],
        cwd=product.parents[1],
        capture_output=True,
        check=True,
    )
    (output / "git-archive.stderr").write_bytes(raw.stderr)
    (output / "prior-source.zip").write_bytes(raw.stdout)
    with zipfile.ZipFile(io.BytesIO(raw.stdout)) as archive:
        archive.extractall(output / "archive")
    root = output / "archive/MCP_Server_Dev/BYO-Firmware-MCP"
    shutil.copytree(
        product / "tests", root / "tests", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    runner = root / "oracle.py"
    runner.write_text(
        "import json, pathlib, sys, unittest\n"
        "suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[2:])\n"
        "result=unittest.TextTestRunner(verbosity=2).run(suite)\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(tests=result.testsRun,"
        "failures=[dict(test=t.id(),traceback=tb) for t,tb in result.failures],"
        "errors=[dict(test=t.id(),traceback=tb) for t,tb in result.errors],skips=result.skipped)))\n"
        "sys.exit(0 if result.wasSuccessful() else 1)\n",
        encoding="utf-8",
    )
    report = dict(
        started_at=now(),
        prior=args.prior,
        prior_archive_sha256=hashlib.sha256(raw.stdout).hexdigest(),
        archive_argv=raw.args,
        archive_raw_exit=raw.returncode,
        runs={},
    )
    cases = {
        "required-guidance": args.required_case,
        "unaffected-control": args.control_case,
    }
    for label, case in cases.items():
        destination = output / label
        destination.mkdir()
        env = dict(os.environ)
        for variable, name in (
            ("BYO_MCP_ARTIFACT_ROOT", "project"),
            ("BYO_MCP_MONITOR_ROOT", "monitor"),
            ("BYO_MANAGED_TEST_EVIDENCE", "managed"),
            ("PYOCD_MCP_RUNS_ROOT", "runs"),
        ):
            env[variable] = str(destination / name)
            Path(env[variable]).mkdir()
        observer = getattr(sys.modules.get("sitecustomize"), "__file__", None)
        env["PYTHONPATH"] = os.pathsep.join(
            ([str(Path(observer).parent)] if observer else []) + [str(root / "src"), str(root)]
        )
        argv = [
            sys.executable,
            "-B",
            str(runner),
            str(destination / "unittest.json"),
            case,
        ]
        record = dict(
            argv=argv,
            cwd=str(root),
            started_at=now(),
            environment={
                k: env[k]
                for k in (
                    "PYTHONPATH",
                    "BYO_MCP_ARTIFACT_ROOT",
                    "BYO_MCP_MONITOR_ROOT",
                    "BYO_MANAGED_TEST_EVIDENCE",
                )
            },
        )
        result = subprocess.run(argv, cwd=root, env=env, capture_output=True)
        (destination / "stdout.log").write_bytes(result.stdout)
        (destination / "stderr.log").write_bytes(result.stderr)
        record.update(
            finished_at=now(),
            raw_exit=result.returncode,
            unittest=json.loads((destination / "unittest.json").read_text()),
        )
        report["runs"][label] = record
    failed = report["runs"]["required-guidance"]
    control = report["runs"]["unaffected-control"]
    report["sensitive"] = (
        failed["raw_exit"] == 1
        and len(failed["unittest"]["failures"]) == 1
        and not failed["unittest"]["errors"]
        and not failed["unittest"]["skips"]
        and args.required_text in failed["unittest"]["failures"][0]["traceback"]
        and control["raw_exit"] == 0
        and control["unittest"]["tests"] == 1
        and not control["unittest"]["errors"]
        and not control["unittest"]["skips"]
    )
    report["finished_at"] = now()
    (output / "sensitivity.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "sensitive": report["sensitive"],
                "prior": args.prior,
                "runs": {
                    k: {
                        "raw_exit": v["raw_exit"],
                        "counts": {
                            "tests": v["unittest"]["tests"],
                            "failures": len(v["unittest"]["failures"]),
                            "errors": len(v["unittest"]["errors"]),
                            "skips": len(v["unittest"]["skips"]),
                        },
                    }
                    for k, v in report["runs"].items()
                },
            }
        )
    )
    return 0 if report["sensitive"] else 1


if __name__ == "__main__":
    sys.exit(main())
