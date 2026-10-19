"""Isolated behavioral mutants for M02; candidate source bytes are never edited.

Run each mode in a separate process. Mutation runs intentionally exit 1 when
the regressions fail and record exact failure/error/skip counts for review.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.adapters import swd_pyocd


PREFIX = "tests.test_m02_independent_flash."
CASES = {
    "control": (
        "IndependentRawFlash.test_programmed_bytes_control_and_healthy_followup",
        "IndependentRawFlash.test_already_identical_noop_is_a_legitimate_idempotency_control",
        "IndependentGuardedFlash.test_supported_public_guarded_flash_commits_real_bounded_allocation_and_symbol_binding",
    ),
    "skip-readback": (
        "IndependentRawFlash.test_normal_return_noop_on_different_bytes_cannot_succeed",
        "IndependentRawFlash.test_last_sparse_byte_corruption_short_and_failed_reads_never_prove_success",
        "IndependentGuardedFlash.test_actual_guarded_backend_exception_or_noop_records_failure_without_successful_deployment",
        "IndependentRawFlash.test_programmed_bytes_control_and_healthy_followup",
    ),
    "skip-support": (
        "IndependentRawFlash.test_every_region_is_checked_before_any_reset_program_or_write",
        "IndependentGuardedFlash.test_guarded_unsupported_second_region_clears_pending_without_allocation",
        "IndependentRawFlash.test_programmed_bytes_control_and_healthy_followup",
    ),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=CASES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = Path(swd_pyocd.__file__)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with contextlib.ExitStack() as stack:
        if args.mode == "skip-readback":
            stack.enter_context(patch.object(swd_pyocd, "verify_flash_image", lambda *a, **k: None))
        elif args.mode == "skip-support":
            stack.enter_context(
                patch.object(swd_pyocd, "_require_flash_support", lambda *a, **k: None)
            )
        suite = unittest.defaultTestLoader.loadTestsFromNames(
            [PREFIX + n for n in CASES[args.mode]]
        )
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    after = hashlib.sha256(source.read_bytes()).hexdigest()
    assert before == after, "the candidate source is immutable during sensitivity checks"
    args.output.write_text(
        json.dumps(
            {
                "mode": args.mode,
                "source": str(source.resolve()),
                "source_sha256_before": before,
                "source_sha256_after": after,
                "tests_run": result.testsRun,
                "skips": len(result.skipped),
                "failures": len(result.failures),
                "errors": len(result.errors),
                "failure_ids": [str(t) for t, _ in result.failures],
                "raw_exit": 0 if result.wasSuccessful() else 1,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
