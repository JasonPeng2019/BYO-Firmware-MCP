"""Isolated historical-fault mutations. Candidate source is never modified."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("member", choices=("m06", "m13"))
    args = parser.parse_args()
    output = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / args.member
    output.mkdir(exist_ok=False)
    root = Path.cwd()
    isolated = output / "mutant-src"
    shutil.copytree(root / "src", isolated, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if args.member == "m06":
        source = isolated / "pyocd_debug_mcp/server.py"
        original = '"read_memory_symbol",\n    guard=_enforce_symbol_read_invocation,'
        replacement = '"read_memory_symbol",\n    guard=_enforce_guarded_invocation,'
        cases = [
            "tests.test_b02_m06_symbols.IndependentSymbols.test_registered_direct_batch_and_planned_address_match_independent_elf_bytes",
            "tests.test_tiered_acceptance_paths.TieredIndirectPathAcceptanceTests.test_no_setup_symbol_route_refuses_before_symbol_resolution_or_backend_io",
        ]
    else:
        source = isolated / "pyocd_debug_mcp/safety/enforce.py"
        original = 'and document.deployment_policy["kind"] == "none"'
        replacement = 'and False  # isolated pre-repair unconditional refresh behavior'
        cases = [
            "tests.test_b02_m13_workflow.IndependentAllocation.test_public_refusal_refresh_then_advertised_verified_deployment_and_same_breakpoint",
            "tests.test_b02_m13_workflow.IndependentAllocation.test_outside_allocation_and_changed_plan_elf_remain_refused",
        ]
    before = source.read_text(encoding="utf-8")
    assert before.count(original) == 1
    source.write_text(before.replace(original, replacement), encoding="utf-8")
    env = dict(os.environ)
    # Preserve the read-only import observer as the first entry, followed by
    # isolated source. The candidate package cannot win module resolution.
    observer = env["PYTHONPATH"].split(os.pathsep)[0]
    env["PYTHONPATH"] = os.pathsep.join((observer, str(isolated), str(root)))
    command = [sys.executable, "-B", "-m", "unittest", "-v", *cases]
    record = dict(started_at=datetime.now(timezone.utc).isoformat(), argv=command,
                  original=original, replacement=replacement, source=str(source),
                  source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  candidate_sha256=hashlib.sha256((root / "src" / source.relative_to(isolated)).read_bytes()).hexdigest(),
                  pythonpath=env["PYTHONPATH"])
    (output / "command.json").write_text(json.dumps(record, indent=2))
    result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    record.update(finished_at=datetime.now(timezone.utc).isoformat(), exit_code=result.returncode,
                  stdout=result.stdout, stderr=result.stderr)
    (output / "result.json").write_text(json.dumps(record, indent=2))
    print(result.stdout + result.stderr)
    # Propagate the actual failing mutant exit; it is evidence, not a passing test command.
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
