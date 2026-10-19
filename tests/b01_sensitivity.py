"""Opt-in isolated source mutations with paired controls for B01 regressions.

No live checkout files are changed. Each run imports a disposable source copy
and uses isolated projects and the controlled worker/serial boundaries.
"""

from __future__ import annotations

import argparse
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


CASES = (
    (
        "M04-returned-schema",
        "capabilities/lite.py",
        '"response_schema": exact_object(',
        '"missing_response_schema": exact_object(',
        (
            "tests.test_b01_setup_public.SetupPublicTests.test_full_nested_lite_form_and_each_required_field_error",
            "tests.test_b01_setup_public.SetupPublicTests.test_eligible_installed_form_hits_live_cpuid_boundary_without_claiming_package",
        ),
    ),
    (
        "M09-raw-input-category",
        "tools/raw.py",
        'raise TargetInputError("value must be non-negative")',
        'raise ValueError("value must be non-negative")',
        (
            "tests.test_b01_register_public.RegisterPublicTests.test_both_public_setters_refuse_without_worker_write_and_keep_exact_session",
            "tests.test_b01_register_public.RegisterPublicTests.test_genuine_termination_and_broken_pipe_invalidate_the_proxy",
        ),
    ),
    (
        "M09-worker-input-category",
        "adapters/provider_worker_runtime.py",
        "raise TargetInputError(str(exc)) from exc",
        "raise TargetConnectionError(str(exc)) from exc",
        (
            "tests.test_b01_register_public.RegisterPublicTests.test_actual_worker_argument_refusal_is_nonfatal_and_wide_values_survive",
            "tests.test_b01_register_public.RegisterPublicTests.test_both_public_setters_refuse_without_worker_write_and_keep_exact_session",
        ),
    ),
    (
        "M18-payload-as-prose",
        "guardrails/plan_defs.py",
        '"real command.",\n                allow_empty=True,',
        '"real command.",\n                allow_empty=False,',
        (
            "tests.test_b01_uart_public.UARTPublicTests.test_public_write_plan_execute_receives_exact_bytes",
            "tests.test_b01_uart_public.UARTPublicTests.test_public_normal_command_control",
        ),
    ),
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1]
    results = []
    for name, relative, before, after, tests in CASES:
        with tempfile.TemporaryDirectory(prefix="b01-sensitivity-") as temporary:
            root = Path(temporary)
            for directory in ("src", "tests"):
                shutil.copytree(
                    source / directory,
                    root / directory,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            path = root / "src" / "pyocd_debug_mcp" / relative
            original = path.read_text(encoding="utf-8")
            assert original.count(before) == 1, (name, "mutation anchor must be unique")
            for variant in ("control", "mutant"):
                if variant == "mutant":
                    path.write_text(original.replace(before, after), encoding="utf-8")
                    # The same-size TEXT boolean mutation must not reuse a pyc.
                    for cache in (root / "src").rglob("__pycache__"):
                        shutil.rmtree(cache)
                environment = {
                    **os.environ,
                    "PYTHONPATH": str(root / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
                command = [sys.executable, "-B", "-m", "unittest", "-v", *tests]
                started = now()
                completed = subprocess.run(
                    command,
                    cwd=root,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=120,
                    check=False,
                )
                entry = {
                    "case": name,
                    "variant": variant,
                    "started_at": started,
                    "completed_at": now(),
                    "command": command,
                    "cwd": str(root),
                    "source_file": str(path),
                    "exit_code": completed.returncode,
                    "mutation": {"before": before, "after": after} if variant == "mutant" else None,
                }
                log = args.output_dir / f"{name}-{variant}.log"
                log.write_text(completed.stdout, encoding="utf-8")
                entry["log"] = str(log)
                results.append(entry)
                (args.output_dir / "sensitivity.json").write_text(
                    json.dumps(results, indent=2) + "\n", encoding="utf-8"
                )
                print(json.dumps(entry), flush=True)
                assert completed.returncode == (0 if variant == "control" else 1), entry
                # Each mutant must preserve the second selected control test.
                assert f"{tests[1].rsplit('.', 1)[1]} (" in completed.stdout
                assert " ... ok" in completed.stdout, (
                    "the paired unaffected control must still pass"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
