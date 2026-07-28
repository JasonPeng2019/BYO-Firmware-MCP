# H05 marker-unlink repair — design-charter checkpoints

## Main model — post-spec / pre-plan

- Timestamp: `2026-07-26T06:07:00Z`
- Contemplated diff: only the `_WorkerClient.close` control-flow boundary that currently
  suppresses a typed marker-removal cleanup failure raised during graceful-close invalidation.
- Correctness: a retained ownership marker is incomplete cleanup and must not be reported as
  success; the existing typed/cause-preserving error is already the intended contract.
- Simplicity/neatness: distinguish that existing cleanup failure locally; do not add an exception
  framework, retry loop, polling, or duplicated marker logic.
- Generalizability/dynamism: the behavior is process/marker-state based and independent of board,
  OS, host path, toolchain, provider, or target.
- Usability/trusted-but-fallible boundary: keep the actionable retained-marker recovery message.
  This catches honest cleanup failure; it adds no hostile-input hardening or paternalistic gate.
- Rejected alternatives: same-call automatic unlink retry (would hide failure and defeat retained
  evidence), global cleanup redesign, board/Windows special cases, new arbitrary limits, or
  unrelated refactoring.
- Scope exclusions: preserve the accepted H05 wait repair and every unrelated server contract.

## Main model — post-plan / pre-review

- Timestamp: `2026-07-26T06:14:00Z`
- The main-authored plan contains one vertical production item, one local file, explicit
  failure/success predicates, retry semantics, adjacent controls, and objective neutral-gate
  coverage. The independent reproducer paths and 41-test dirty-baseline command were reverified
  after eliminating the duplicate orchestrator process.
- Charter verdict: the planned repair is the narrowest general correctness change. It removes a
  swallowed error without changing the ownership authority or inventing environment-specific
  behavior.

## Required role checkpoints

## Doer — pre-implementation

- Timestamp: `2026-07-25T23:21:58-07:00`
- Contemplated diff: narrow `_WorkerClient.close` diagnostic suppression so the existing typed marker-removal failure escapes when nested invalidation has confirmed process cleanup but retained `_marker`.
- Charter properties applied: correctness prohibits reporting complete cleanup while its recovery marker remains; simplicity and neatness reuse the existing state and error contracts; generalizability excludes board, OS, provider, and toolchain-specific behavior.
- Assumption/tie-breaker: graceful-close errors are diagnostic only after complete ownership cleanup. A retained marker is incomplete cleanup, so correctness wins over suppression.
- Rejected alternatives: same-call retry, process/marker subsystem redesign, exception wrapper, polling or timeout changes, and board/OS/toolchain branches.
- Scope exclusions: tests, gate commands, manifests, `tools/misc.py`, marker-store and termination implementations, and public interfaces remain unchanged.

## Doer — pre-verification

- Timestamp: `2026-07-25T23:22:15-07:00`
- Implemented diff: the existing caught close exception is re-raised only when `_cleanup_confirmed` is true and `_marker` remains; the direct exception and its cause are preserved.
- Charter properties applied: truthful cleanup reporting and retryable retained-marker state take precedence over diagnostic suppression; the diff is local, simple, and environment-independent.
- Assumption/tie-breaker: complete invalidation has no retained marker and therefore remains a successful outer close; only incomplete ownership cleanup is fatal.
- Rejected alternatives: catch only a new exception subtype, retry removal automatically, alter deadlines/termination, or add target-specific logic.
- Scope exclusions: verification will not modify tests, commands, manifests, other production modules, or existing dirty work.

## Doer — pre-final verdict

- Timestamp: `2026-07-25T23:23:07-07:00`
- Verified feature: nested `close -> call -> _invalidate` marker-removal failures now escape with their original typed exception/cause, while complete cleanup still suppresses graceful-close diagnostics.
- Charter properties applied: correctness prevents false success with retained recovery state; the single local condition is the simplest, neatest, and portable implementation.
- Assumption/tie-breaker: preserving truthful cleanup state is more important than universal graceful-close suppression; no behavior changes when cleanup is complete.
- Rejected alternatives: cleanup retry loop, special marker error class, broad error-policy change, formatter sweep, or OS/board/toolchain conditionals.
- Scope exclusions: only `swd_process.py` production logic changed; tests, gate commands, manifests, hardware, and pre-existing dirty work were not changed.

## Doer — iteration 2 pre-verification

- Timestamp: `2026-07-25T23:27:44-07:00`
- Test feature: rerun the tester-owned marker-unlink spec using the declared PowerShell syntax after the neutral report showed Bash misinterpreting that syntax.
- Charter properties applied: correctness distinguishes an execution-environment error from a production failure; the source remains unchanged because the report identifies no behavioral defect.
- Assumption/tie-breaker: the named local PowerShell workspace is the intended host shell, as evidenced by `$env:PYTHONPATH` and Windows executable paths in the command.
- Rejected alternatives: modifying the tester-owned command, test files, manifests, PATH/toolchain configuration, or adding platform-specific production logic.
- Scope exclusions: this is host-only verification; no board, firmware, source, test, or gate-command modifications are made.

## Doer — iteration 2 pre-final verdict

- Timestamp: `2026-07-25T23:27:57-07:00`
- Result assessed: the marker-unlink spec passes when executed in its declared PowerShell form; the neutral failure is confined to Bash parsing that form, while its regression suite already passed.
- Charter properties applied: honest reporting separates harness-shell failure from server behavior; no speculative platform workaround is added to production source.
- Assumption/tie-breaker: the test command's PowerShell syntax defines its required host shell, so rerunning it in that shell is the minimal correctness check.
- Rejected alternatives: source edits, tester-command edits, test modifications, new shell abstraction, or OS-specific runtime behavior.
- Scope exclusions: source, tests, manifests, tester command files, firmware, boards, and configuration remain unchanged in this iteration.

The doer, spec tester, and regression tester must append their own dated entries after rereading
`../.codex/design_charter.md` at their applicable pre-implementation/pre-test, pre-verification,
post-risky-diff, and pre-acceptance boundaries. Each entry must name the actual contemplated diff
or test surface, the charter properties preserved, rejected overbroad alternatives, and scope
exclusions.

## Regression tester — pre-test

- Timestamp: `2026-07-25T23:45:00-07:00`
- Contemplated test surface: a separate, host-only regression module for the public
  `ProcessIsolatedSWDInterface.close` delegate and the worker close lifecycle boundaries.
- Charter properties applied: correctness requires that a retained recovery marker reaches the
  production caller as the existing typed failure, while provider-close diagnostics remain
  non-fatal after complete cleanup and unconfirmed cleanup remains fail-closed; simplicity and
  neatness favor deterministic mocks of the existing worker boundary.
- Assumption/tie-breaker: delegation must preserve the worker error unchanged; truthful cleanup
  status wins over a successful-looking close result.
- Rejected alternatives: physical boards, real provider processes, timing sleeps, retry loops,
  OS/provider/board branches, production changes, and broad integration suites.
- Scope exclusions: only a new regression test module and the required tester state files are in
  scope; server source, spec-tester files, hardware, toolchains, and unrelated dirty changes are
  excluded.

## Regression tester — pre-verification

- Timestamp: `2026-07-25T23:48:00-07:00`
- Verified test surface: the regression module exercises the public close delegate through a real
  nested worker invalidation, then separately checks a typed provider diagnostic and unconfirmed
  termination with mocked ownership state.
- Charter properties applied: the tests assert truthful failure propagation, complete-cleanup
  usability, and retained-marker fail-closed behavior without fabricating provider, board, or OS
  facts; they remain narrow and portable.
- Assumption/tie-breaker: worker lifecycle state plus marker-removal and termination call counts
  are sufficient regression evidence for these control-flow edges, so no physical target is used.
- Rejected alternatives: sleep-based races, a real subprocess/provider, hardware smoke tests,
  extra product branches, or tests of unrelated deadline mechanics.
- Scope exclusions: verification is limited to this regression module; no production source,
  spec-tester module, hardware action, or unrelated working-tree file will be altered.

## Regression tester — pre-final verdict

- Timestamp: `2026-07-25T23:50:00-07:00`
- Verified result: the recorded host-only unittest command ran the three independent regression
  tests successfully, and the worktree has no whitespace errors from this test addition.
- Charter properties applied: the suite detects false success when a confirmed worker cleanup
  retains its marker, while preserving diagnostic-only provider failures after complete cleanup
  and the existing fail-closed state when termination is unconfirmed; no environment-specific
  assumptions or new behavior are introduced.
- Assumption/tie-breaker: targeted lifecycle coverage is proportionate to this one-condition
  production diff, and the neutral harness remains the acceptance authority.
- Rejected alternatives: claiming hardware validation, expanding to unrelated suites, testing
  real process timing, or changing production code to make tests easier.
- Scope exclusions: the final record owns only `tests/test_regression_h05_marker_unlink.py` and
  its required state records; all other modified or untracked files are left untouched.

## Regression tester — iteration 2 pre-analysis

- Timestamp: `2026-07-25T23:55:00-07:00`
- Contemplated test surface: re-audit the existing focused regression module against the current
  one-condition `_WorkerClient.close` diff and neutral-gate result, with no hardware action.
- Charter properties applied: correctness requires checking that the delegate cannot hide retained
  cleanup state, while simplicity retains the established deterministic lifecycle coverage unless
  a distinct credible edge is found; portability excludes board, OS, provider, and toolchain
  assumptions.
- Assumption/tie-breaker: the neutral spec command failure is an external shell-command mismatch,
  not evidence that the production behavior or this regression suite changed; neutral harness
  output remains authoritative for that separate suite.
- Rejected alternatives: editing the spec tester's command/file, changing production behavior,
  real-board validation, subprocess timing tests, or OS-specific workarounds.
- Scope exclusions: only tester-owned regression coverage and required state/checkpoint records
  may change; source, spec tests, hardware, and unrelated workspace files remain excluded.

## Regression tester — iteration 2 pre-verification

- Timestamp: `2026-07-25T23:58:00-07:00`
- Contemplated verification: run only the recorded regression command from the repository root
  after confirming the exact production diff and tester manifest remain unchanged.
- Charter properties applied: a focused, non-interactive host-only run verifies the truthful
  cleanup-status boundary without introducing board, OS, toolchain, or provider dependencies.
- Assumption/tie-breaker: no new credible risk warrants broadening the three-test suite; the
  plan's independent delegate, diagnostic, and fail-closed edges remain the proportionate target.
- Rejected alternatives: invoking a physical probe, adding environment-specific command logic,
  running unrelated suites, or modifying the separate spec gate.
- Scope exclusions: only the existing regression command and required state records are used;
  production source and spec-tester-owned artifacts remain untouched.

## Regression tester — iteration 2 pre-final verdict

- Timestamp: `2026-07-26T00:00:00-07:00`
- Verified result: the unchanged recorded regression command completed successfully with all three
  tests passing; its manifest still names only the tester-owned module.
- Charter properties applied: the retained-marker error remains observable through the public
  delegate, complete cleanup stays usable, and unconfirmed cleanup stays honest and fail-closed;
  validation is host-only and portable.
- Assumption/tie-breaker: no added test is preferable to duplicate coverage when the re-audit
  found no new credible behavior edge; focused evidence and neutral execution decide acceptance.
- Rejected alternatives: modifying the unrelated failing spec command, invoking hardware,
  introducing shell/OS conditionals, or expanding scope beyond the lifecycle boundary.
- Scope exclusions: no source or spec-tester file was changed in this iteration; required tester
  state records retain the same single regression command and single owned test path.

## Regression tester — manager format follow-up

- Timestamp: `2026-07-26T00:05:00-07:00`
- Contemplated diff: mechanical Ruff formatting of only
  `tests/test_regression_h05_marker_unlink.py`.
- Charter properties applied: neatness and simplicity require repository-standard formatting while
  preserving the existing assertions and behavior exactly; the update is host-only and has no
  board, OS, provider, or toolchain-specific logic.
- Assumption/tie-breaker: formatter output is mechanical and does not alter the established
  lifecycle contract, so no test-design expansion is warranted.
- Rejected alternatives: production edits, assertion changes, command or manifest changes,
  hardware validation, and formatting unrelated files.
- Scope exclusions: production behavior, assertions, commands, manifests, hardware, and unrelated
  files remain excluded.

## Regression tester — manager format follow-up pre-final

- Timestamp: `2026-07-26T00:08:00-07:00`
- Verified result: Ruff check passed, Ruff format check passed, and the unchanged recorded
  regression command passed all three tests; the manifest still has exactly the one owned path.
- Charter properties applied: mechanical formatting improves neatness without altering test
  behavior or introducing environment-specific logic; correctness remains guarded by the unchanged
  lifecycle assertions.
- Assumption/tie-breaker: the formatter made only mechanical layout changes, so preserving the
  existing command, manifest, and assertion surface is the narrowest correct outcome.
- Rejected alternatives: any production/spec/plan/state edit, assertion rewrite, hardware action,
  or unrelated formatting.
- Scope exclusions: only the owned regression test and the named checkpoint log changed; commands,
  manifests, hardware, and all unrelated files remain untouched.

## Independent plan reviewer — post-review

- Timestamp: `2026-07-26T06:16:44Z`
- Thread/model: `019f9d10-a2e0-7ae0-b4aa-6f4a2feeaea8`,
  `gpt-5.6-terra` medium on priority/Fast.
- The reviewer read the design charter, request, exact SHA-pinned plan, named production/test
  surfaces, and current diff in read-only mode.
- Charter verdict: `READY`; no conflict, unnecessary complexity, overbroad scope, missing
  preservation contract, or ambiguous state transition. The four numbered risks are recorded in
  `plan-review.md` as execution/test targets rather than a replanning loop.

## Main model — pre-implementation handoff

- Timestamp: `2026-07-26T06:18:00Z`
- The charter and this checkpoint log were reread after the one-time review and immediately before
  starting change-loop.
- Accepted implementation boundary: one local distinction in `_WorkerClient.close`; no same-call
  retry, no new exception type, no provider/process redesign, and no edit to the accepted
  `misc.wait` slice.
- The doer and both testers are required by the plan/prompt to reread the charter and append
  concrete checkpoints. Neutral gates, not role prose, decide acceptance.

## Doer — manager cleanup follow-up

- Timestamp: `2026-07-25T23:36:27-07:00`
- Contemplated cleanup: remove only the redundant `pass` immediately after the conditional bare `raise` in `_WorkerClient.close`.
- Charter properties applied: retained-marker cleanup failures still propagate honestly; falling out of the handler preserves ordinary graceful-close diagnostic suppression; the change is portable and remains within the plan's strict local scope.
- Assumption/tie-breaker: an empty exception handler is intentional for non-retained-marker graceful-close failures, so removing the `pass` changes no behavior.
- Rejected alternatives: exception-policy redesign, retries, OS/board/toolchain-specific behavior, reformatting, or unrelated cleanup.
- Scope exclusions: only this production line and this checkpoint log may change; tests, commands, manifests, hardware, and unrelated dirty files remain untouched.

## Doer — manager cleanup pre-final follow-up

- Timestamp: `2026-07-25T23:36:58-07:00`
- Verified cleanup: the redundant `pass` is absent; the conditional bare `raise` remains the sole retained-marker propagation path and non-retained-marker graceful-close errors still fall through as diagnostics.
- Charter properties applied: correctness preserves truthful cleanup failure reporting; simplicity/neatness remove dead syntax without new behavior; portability remains unaffected.
- Assumption/tie-breaker: the empty handler's fallthrough is the established diagnostic contract, making removal of the explicit no-op the narrowest change.
- Rejected alternatives: modifying exception semantics, retries, platform-specific paths, formatting, or unrelated changes.
- Scope exclusions: verification used host-only lint/diff/tests; no test, command, manifest, hardware, or unrelated worktree file was changed.

## Main model — pre-verification

- Timestamp: `2026-07-26T06:31:00Z`
- The complete charter, plan, one-time review, role identities, role checkpoint entries, source
  diff, and iteration-2 neutral report were reread after the sequential loop exited green.
- Verification target: the one two-line `_WorkerClient.close` state distinction, both tester-owned
  suites, the existing process/SWD and H05 wait suites, targeted lint/format/type checks, and an
  independent rerun of the exact marker-unlink reproducer.
- Charter boundary remains unchanged: honest retained-marker failure, no environment-specific
  behavior, no new retry/limit/framework, and no hardware action.

## Spec tester — pre-test

- Timestamp: `2026-07-25T23:30:00-07:00`
- Contemplated test surface: a focused host-only `_WorkerClient.close` suite that drives the real nested `close -> call("close") -> _invalidate` EOF route with a case-local mocked marker, then verifies retained-marker retry and the specified graceful/unconfirmed controls.
- Charter properties applied: correctness requires a typed, cause-preserving failure when confirmed process cleanup leaves an ownership marker; simplicity/neatness favor the existing worker test conventions and direct lifecycle observables; generalizability excludes board, port, OS, provider, and toolchain assumptions.
- Assumption/tie-breaker: process termination confirms worker death but does not make ownership cleanup successful while removal failed, so correctness outweighs diagnostic suppression; the later close is marker-only because the worker is already closed.
- Rejected alternatives: physical-board tests, external provider or filesystem-root dependencies, retry loops, timing sleeps, OS-specific assertions, and production edits.
- Scope exclusions: only a new repository test module plus the required state command/manifest will be changed; no server source, hardware, docs, dependencies, or unrelated dirty work is in scope.

## Spec tester — between test features

- Timestamp: `2026-07-25T23:31:00-07:00`
- Contemplated test feature: controls for harmless graceful-close failures after complete cleanup and for unconfirmed termination retaining its marker without an unlink attempt.
- Charter properties applied: honest status distinguishes complete cleanup from retained recovery state; the checks retain normal usable close behavior while failing closed on a known incomplete ownership cleanup; portable mocked process state avoids environment-specific behavior.
- Assumption/tie-breaker: an ordinary protocol/EOF diagnostic stays suppressible only after termination and marker removal are both confirmed; when termination cannot be confirmed, correctness requires the existing retained-marker error.
- Rejected alternatives: asserting provider internals, depending on real process timing, adding new production control paths, board-specific cases, or hostile-input scenarios.
- Scope exclusions: the feature adds test assertions only and does not alter the implementation, marker schema/root, worker protocol, hardware actions, or unrelated suites.

## Spec tester — pre-verification

- Timestamp: `2026-07-25T23:32:00-07:00`
- Verified test surface: `tests/test_h05_marker_unlink_spec.py` covers the real nested close/call/invalidate path, an exact `OSError("H05_MARKER_UNLINK")` cause, retained marker state, marker-only later retry, complete-cleanup diagnostics, and unconfirmed termination.
- Charter properties applied: the assertions reject false success while retaining clean graceful-close usability; they are local, deterministic, portable, and do not assert board, OS, toolchain, or physical-provider details.
- Assumption/tie-breaker: the worker client's own lifecycle fields and marker-store call boundary are its observable contract; timing is merely bounded with the existing absolute-deadline interface.
- Rejected alternatives: integration with a physical probe, sleeps/polling, mutation of production code, a new fake protocol mode, or broad suite changes.
- Scope exclusions: verification runs only the recorded H05 spec module and does not run hardware, deploy, flash, or remote commands.

## Spec tester — pre-final verdict

- Timestamp: `2026-07-25T23:33:00-07:00`
- Verified result: the isolated H05 spec command passed all four tests using the repository virtual environment. The suite proves the required typed/cause-preserving failure and retry boundary plus the clean and fail-closed controls.
- Charter properties applied: no retained marker is misreported as success; harmless diagnostics remain usable only after complete cleanup; the tests are concise, host-only, and cross-environment by construction.
- Assumption/tie-breaker: exact error typing, original cause, marker retention, request count, and termination count are sufficient external lifecycle evidence; no physical worker process is needed to establish this branch.
- Rejected alternatives: weakening the assertion to a generic failure, accepting a retrying provider request, broad integration execution, hardware validation, or any production modification.
- Scope exclusions: final handoff records only this focused test command and owned test path; all other working-tree changes remain untouched.

## Spec tester — iteration 2 pre-command repair

- Timestamp: `2026-07-25T23:28:26-07:00`
- Contemplated change: replace the required spec-suite state command's PowerShell-only environment syntax with its shell-neutral Bash form after the neutral harness reported exit 127; test assertions and production code remain unchanged.
- Charter properties applied: correctness requires the recorded neutral command to actually execute; simplicity keeps the same virtual environment, module, and scope with only portable environment assignment syntax; generalizability rejects host-shell assumptions.
- Assumption/tie-breaker: the neutral report's `bash` error is authoritative for its execution context, so Bash-compatible syntax is required even though the command succeeded interactively in PowerShell.
- Rejected alternatives: a hardcoded absolute path, OS/toolchain branch, changing test behavior, invoking a broad suite, or modifying production code.
- Scope exclusions: only the mandated state command and this checkpoint log change; no hardware, board, provider, source, test assertion, dependency, or unrelated work is touched.

## Spec tester — iteration 2 between test features

- Timestamp: `2026-07-25T23:28:48-07:00`
- Contemplated test feature: a healthy close control using the repository's existing run-owned fake provider worker, asserting one graceful request, confirmed process exit, and marker cleanup without a physical target.
- Charter properties applied: correctness preserves normal close success; simplicity reuses the existing test fixture/protocol; generalizability and neatness avoid board, port, OS, toolchain, and real-provider assumptions.
- Assumption/tie-breaker: the fake worker's `good` response is the project-established healthy provider contract, sufficient to prove request ordering and lifecycle preservation alongside the deterministic error tests.
- Rejected alternatives: hardware probe validation, an external provider service, board-specific fixtures, a new protocol mode, timing sleep, or production/source edits.
- Scope exclusions: only the existing H05 spec module gains this host-process control; marker implementation, protocol, test command scope, and all unrelated work stay unchanged.

## Spec tester — iteration 2 pre-verification

- Timestamp: `2026-07-25T23:29:05-07:00`
- Verified test surface: the H05 spec now combines a real run-owned healthy worker close with deterministic nested invalidation, retained-marker retry, ordinary diagnostic, and unconfirmed-cleanup cases; the recorded command uses Bash-compatible syntax.
- Charter properties applied: tests make incomplete cleanup visible, preserve normal usability, and do not rely on hardware or environment-specific configuration.
- Assumption/tie-breaker: the explicit deadline in the healthy test is the existing bounded worker API rather than a new arbitrary policy; deterministic mocked branches remain preferable for failure causality.
- Rejected alternatives: full hardware integration, sleeps/polls, shell-specific state commands, production changes, or weakening the retained-marker/cause assertions.
- Scope exclusions: verification remains limited to the H05 spec module and required state files; no board, firmware, external target, or unrelated suite is invoked.

## Spec tester — iteration 2 pre-final verdict

- Timestamp: `2026-07-25T23:29:30-07:00`
- Verified result: the neutral-compatible command `PYTHONPATH=src ./.venv/Scripts/python.exe -m unittest tests/test_h05_marker_unlink_spec.py` passed five tests under Bash; the manifest names only the owned H05 spec module.
- Charter properties applied: the recorded command is executable in the actual neutral shell, tests retain truthful cleanup reporting and normal healthy behavior, and no environment-specific production behavior was introduced.
- Assumption/tie-breaker: using the repository virtual environment via a relative path is portable within the named workspace and is confirmed by the neutral shell, while the PowerShell-only form was rejected by the report.
- Rejected alternatives: retaining an incompatible shell command, absolute host paths, hardware validation, a broader suite, source changes, or assertion relaxation.
- Scope exclusions: final result covers only CL-001's server-side lifecycle contract and host-only tests; all other dirty files and no physical boards remain outside this role.

## Spec tester — manager format follow-up

- Timestamp: `2026-07-25T23:37:38-07:00`
- Contemplated diff: only mechanical Ruff formatting of the owned `tests/test_h05_marker_unlink_spec.py` test module after the manager reported a format-check failure.
- Charter properties applied: neatness requires repository-standard formatting; correctness preserves every existing assertion and test contract; simplicity excludes any behavioral change.
- Assumption/tie-breaker: Ruff's repository-local formatter is authoritative for whitespace/layout, while test semantics, fixtures, and lifecycle coverage must remain byte-for-byte equivalent in intent.
- Rejected alternatives: source/production changes, assertion relaxation, command or manifest changes, board/OS/toolchain-specific formatting, and unrelated cleanup.
- Scope exclusions: production behavior, assertions, recorded command, manifest, hardware, plans, regression tests, and unrelated dirty files remain excluded.

## Spec tester — manager format follow-up pre-final

- Timestamp: `2026-07-25T23:38:01-07:00`
- Verified result: Ruff reformatted only the owned spec test; Ruff check and format check both pass, and the unchanged recorded Bash command passes all five focused H05 tests.
- Charter properties applied: neatness is restored without changing behavior; correctness is preserved by the same focused lifecycle assertions; portability remains confirmed by the recorded neutral-shell command.
- Assumption/tie-breaker: successful lint, format, test, and manifest checks establish that the mechanical change did not broaden scope or alter the H05 contract.
- Rejected alternatives: editing production code, changing assertions, changing the working command/manifest, hardware execution, or formatting unrelated files.
- Scope exclusions: the completed change is limited to the owned test's formatting and this checkpoint log; all other files, hardware, and external targets remain untouched.

## Main model — final repair acceptance

- Timestamp: `2026-07-25T23:41:55-07:00`
- The complete charter, accepted plan and review, final production diff, both neutral suites,
  targeted quality checks, adjacent tests, and independent post-repair reproducer were reread
  before acceptance.
- Charter judgment: the one local state distinction prevents silent incomplete cleanup, retains
  the existing actionable error/cause and marker-only recovery, preserves complete-cleanup
  usability, adds no environment-specific behavior, and introduces no speculative machinery.
- Verified result: correct-runtime neutral gate 5+3 passed; manager lint/format/type checks passed;
  49 focused/adjacent tests passed; healthy and injected-unlink post-repair cases matched every
  required observable.
- Assumption/tie-breaker: confirmed process death is insufficient for success while a known
  ownership marker remains; correctness therefore requires the existing cleanup exception to
  escape, while fully completed cleanup keeps graceful-close errors diagnostic-only.
- Rejected alternatives: retries in the initiating close, new exception types, cleanup-framework
  refactors, broad formatting, board/OS/provider cases, or weakening the retained-marker oracle.
- Scope exclusions: H05 is not green until the exact persistent test agent retests this accepted
  server diff; no hardware, firmware, unrelated source, commit, push, deploy, or flash is accepted
  by this checkpoint.
