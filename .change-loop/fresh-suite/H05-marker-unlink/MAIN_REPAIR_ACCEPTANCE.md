# Main-model repair acceptance — H05 retained-marker cleanup

- Accepted at: `2026-07-25T23:41:55-07:00`
- Server HEAD under test: `4e1393775167166146c6ee1a0ce310c9747ca3bf`
- Main-authored plan SHA-256:
  `2b1107796f7a5437304e4a440279b589fbdd2f7c8933ac599d08aa5f5a82186a`
- Production diff:
  `src/pyocd_debug_mcp/adapters/swd_process.py::_WorkerClient.close`
- Test-agent retest remains required before H05 can be green.

## Accepted behavior

The repair makes the existing actionable cleanup error escape when a nested
`close -> call("close") -> _invalidate` path confirms worker termination but cannot remove the
ownership marker. The retained marker and original `OSError` cause remain available for recovery.
A later `close()` retries only marker removal. Ordinary graceful-close diagnostics remain
suppressed after complete process and marker cleanup.

The implementation is the charter-preferred smallest local distinction: no retry loop, public
interface, timeout, platform/board/provider branch, or new cleanup abstraction was introduced.

## Independent acceptance evidence

1. Correct-runtime neutral gate:
   - spec: 5 passed;
   - regression: 3 passed;
   - report:
     `.change-loop/fresh-suite/H05-marker-unlink/state/test_report.md`.
2. Manager verification:
   - `git diff --check` on the production file and both focused tests: passed;
   - Ruff check and format check on both focused tests: passed;
   - targeted Ruff `PIE790` check on the production file: passed;
   - Pyright on `swd_process.py`: 0 errors/warnings;
   - focused plus adjacent unittest selection: 49 passed.
3. Independent post-repair reproducer:
   - healthy control returned normally, removed its case-local marker, and issued one worker
     request:
     `main_verification/post_repair/healthy/result.json`;
   - injected unlink failure raised
     `pyocd_debug_mcp.target_errors.TargetConnectionError`, retained the marker, preserved
     `OSError("H05_MARKER_UNLINK")` as its direct cause, and a second close removed the marker
     without another worker request:
     `main_verification/post_repair/fault/result.json`.
4. The accepted H05 wait-cancellation production/test slice remained in the adjacent 49-test gate.

No hardware action, commit, push, deploy, or flash was performed.
