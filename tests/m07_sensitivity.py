"""Run independent tests against isolated current/pre-M07 adapters, never hardware."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1]
    old_commit = "478125c005853948a9bf166abfcb72b1ad8dfcf6"
    relative = "src/pyocd_debug_mcp/adapters/swd_pyocd.py"
    old = subprocess.check_output([
        "git", "show", f"{old_commit}:MCP_Server_Dev/BYO-Firmware-MCP/{relative}"], cwd=source)
    tests = ["tests.test_m07_independent_breakpoints.IndependentBreakpointTests." + name for name in (
        "test_already_full_flash_has_zero_attempted_code_writes",
        "test_failed_ram_transaction_proven_restoration_has_truthful_advice",
        "test_boundary_control_auto_really_patches_writable_flash_when_full",
    )]
    records = []
    with tempfile.TemporaryDirectory(prefix="m07-sensitivity-") as temporary:
        root = Path(temporary)
        for directory in ("src", "tests"):
            shutil.copytree(source / directory, root / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        path = root / relative
        for variant in ("current", "pre-M07"):
            if variant == "pre-M07":
                path.write_bytes(old)
            environment = {**os.environ, "PYTHONPATH": str(root / "src"),
                           "PYTHONDONTWRITEBYTECODE": "1",
                           "BYO_MANAGED_TEST_EVIDENCE": str(args.output_dir / "managed-analysis")}
            command = [sys.executable, "-B", "-m", "unittest", "-v", *tests]
            started = now()
            result = subprocess.run(command, cwd=root, env=environment,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=60, check=False)
            log = args.output_dir / f"{variant}.log"
            log.write_bytes(result.stdout)
            records.append({"variant": variant, "command": command, "cwd": str(root),
                            "started_at": started, "completed_at": now(),
                            "exit_code": result.returncode,
                            "adapter_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "old_commit": old_commit if variant == "pre-M07" else None,
                            "log": str(log), "log_sha256": hashlib.sha256(result.stdout).hexdigest()})
    (args.output_dir / "sensitivity.json").write_text(json.dumps(records, indent=2) + "\n")
    print(json.dumps(records, indent=2))
    return 0 if [row["exit_code"] for row in records] == [0, 1] else 1


if __name__ == "__main__":
    raise SystemExit(main())
