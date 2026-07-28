# Plan amendments

## PA-001 — Specify the Python 3.10 exception graph and cancellation semantics

- Evidence: the one-time adversarial review of plan SHA-256
  `852f427df6c33852a4a680543f0db598f9e85d596a7a5cdda8d1e8410f7fbab5` returned `BLOCK`
  because CL-001 left the exception graph, cancellation identity, exact strings, and traversal
  oracle ambiguous.
- Genuine plan mistake: Python 3.10 has no `ExceptionGroup`; without explicit cause/context edges,
  cleanup raised while handling a primary failure can invert the principal error or form an
  implicit context cycle. `OperationCancelledError` is also a `RuntimeError`, so a generic
  `except Exception` would incorrectly UART-normalize cancellation.
- Amended requirement: CL-001 now defines exact close-only and combined strings, preserves the same
  normalized primary object and original traceback, exempts the exact cancellation object from
  UART normalization, specifies every required `__cause__`/`__context__` edge and suppression
  flag, clears close-side implicit contexts to prevent cycles, and defines cause-before-context
  identity traversal sequences for neutral tests.
- Preserved contracts: exactly-one close, no implicit reopen/retry, healthy result equivalence,
  primary-only text/cause compatibility, explicit capture reopen behavior, public interfaces,
  backend neutrality, accepted H05 repairs, and all original scope exclusions remain unchanged.
- Review scope: review PA-001 and the corresponding changed CL-001 clauses only. Do not re-review
  unchanged plan items.

### Targeted adversarial review

- Reviewer session: `019f9d3d-c36c-7493-b53e-9ea95eecb75d`
- Reviewed amended-plan SHA-256:
  `a183fa3ec85b888d76b96d6bdac6fceb17b0843c7d81656244ae236f3d47044b`
- Verdict: `AMENDMENT_READY`
- Execution risks:
  1. Preserve the active primary/cancellation object and original traceback through cleanup; do not
     accidentally replace it with a new post-cleanup wrapper.
  2. Explicitly clear the raw close exception's implicit context before linking the specified
     close graph, preventing a primary -> close -> primary cycle.
- Neutral targets: all four exact traversal sequences across capture/write/exchange; object
  identities, strings, cause/context edges, suppression flags, cycle absence, original traceback,
  cancellation-only/plus-close, expected-text and `max_bytes` early returns, exactly-one close,
  no retry/reopen, and the named accepted-H05 regressions.

## PA-002 - Correct the traceback-preservation test oracle

- Evidence: iteration 3 proved that `unittest.TestCase.assertRaises` calls
  `exc_value.with_traceback(None)` when its context exits. The same helper invocation, caught by a
  direct `try`/`except`, showed that the exact principal exception escaping the UART helper
  contains the traceback object that was active when `backend.close()` ran; the `assertRaises`
  context then erased that traceback before the assertion inspected it.
- Genuine execution mistake: the tester implemented the plan's traceback oracle with a standard
  helper that destroys the evidence being asserted. Requiring the production code to recreate a
  traceback after the test framework clears it would either replace/forge the principal exception
  or add test-specific behavior, violating identity, correctness, simplicity, and scope.
- Amended verification requirement: retain the runtime behavior requirement unchanged. For
  primary-plus-close, cancellation-plus-close, and cancellation-only cases, catch the escaping
  exception with a direct `try`/`except BaseException` helper that preserves the caught object's
  traceback. Assert that it is the exact expected principal object and that its traceback chain
  contains the traceback object recorded by the fake adapter at close time. Do not use
  `assertRaises`, `assertRaisesRegex`, or any helper that clears or replaces the caught traceback
  for these assertions.
- Preserved contracts: the exact principal object, original active traceback frame, strings,
  cause/context graph, cycle absence, exactly-once close, no retry/reopen, healthy behavior, and
  every scope exclusion in CL-001 remain unchanged. This amendment permits only the tester-owned
  traceback-capture method to change; it does not relax the production requirement or authorize
  production code outside `uart_capture.py`.
- Review scope: review PA-002 only against the iteration-3 reproducer and charter. Do not re-review
  PA-001 or unchanged CL-001 behavior.

### PA-002 targeted adversarial review

- Reviewer session: `019f9d3d-c36c-7493-b53e-9ea95eecb75d`
- Verdict: `AMENDMENT_READY`
- The reviewer reread the complete design charter and confirmed that `assertRaises` destroys the
  traceback evidence after the helper returns.
- Execution constraint: change only tester-owned traceback capture to a direct
  `try`/`except BaseException` helper; retain every identity, exact-string, graph, cycle,
  close-count, and no-retry/reopen assertion.
- Neutral targets: the three combined/cancellation traceback cases across read/write/exchange and
  the focused regression traceback test, followed by both recorded neutral suites. PA-002 requires
  no production change.
