# Main-model final review after the first green gate

The neutral gate is green, but the high-level final diff review found two unresolved
plan/charter conformance gaps. This is implementation/test feedback under the existing
reviewed plan, not a plan amendment.

## 1. CL-001 still loses a primary native identity error

Current `_windows_start_token()` sets `identity_failure = True` only for an already-normalized
`ProcessIdentityUnavailable`. If a native liveness or birth-time accessor raises
`AttributeError`/`OSError` and `CloseHandle` also raises, the `finally` close exception replaces
the original accessor exception. The outer boundary then reports the close exception as the
cause and silently loses the primary identity failure.

Minimal observed control:

- `GetExitCodeProcess` raises `OSError("body failure")`.
- `CloseHandle` raises `OSError("close failure")`.
- The final `ProcessIdentityUnavailable.__cause__` is `"close failure"` rather than
  `"body failure"`.

This violates the plan's requirement to preserve primary failure identity and the design
charter's no-swallowed-error rule. Add a focused adversarial test that proves the primary native
access failure remains the chained cause while close is still attempted. Do not weaken the
existing close-only or specific-identity-failure controls.

## 2. CL-003's cleanup contract is not fully or honestly represented

The cleanup callback's third argument is annotated as `BaseException`, but Python's
`shutil.rmtree(..., onerror=...)` contract supplies an exception-info tuple. Correct the
annotation rather than suppressing it.

Also add deterministic controls for the exact promised absence semantics:

- An already-absent candidate tree returns successfully without sleeping or removing.
- If a removal attempt raises `PermissionError` but the tree is already absent when checked,
  cleanup returns successfully rather than falling through to `self.fail(...)`.

Keep the five-second monotonic retry bound, last concrete persistent `PermissionError`,
descendants-before-removal ordering, and no-suppression rules unchanged.

## Charter checkpoint

The main model reread `../.codex/design_charter.md` before this final review. The requested
follow-up preserves simple flat failure handling, truthful primary-error reporting, a real
thrash bound, and no environment-specific production behavior.
