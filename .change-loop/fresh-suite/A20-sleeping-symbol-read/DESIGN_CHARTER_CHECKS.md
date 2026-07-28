# A20 coherent symbol-read repair — design-charter checks

Authoritative charter:
`../.codex/design_charter.md` relative to the `BYO-Firmware-MCP` repository root.

Reviewed charter SHA-256:
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`

Every repair role must reread the authoritative charter, not rely only on this summary, and append
its stage check here before changing behavior or tests. This file is runtime evidence, not
production documentation.

## Post-spec check — main/orchestrating model

- **Correctness:** The accepted defect is a successful but incoherent scalar read from a sleeping
  target, reproduced on two independent boards and contradicted by an immediate halted read. The
  repair must return a coherent value or an honest error; it must not reject legitimate zero.
- **Simplicity:** The narrow existing halt/read/finally-resume pattern is sufficient. No retry,
  polling, state machine, or transaction framework is justified.
- **Generalizability and dynamism:** State observation and halt/resume are generic provider
  capabilities. The request excludes all board, MCU, probe, OS, toolchain, application, and
  low-power-state branches.
- **Neatness and usability:** The memory tool remains the single owner of its scalar read contract,
  while the injected target-control operations keep it testable. Public help must disclose the
  brief halt and recovery behavior.
- **Trust boundary:** The operator's symbol and ELF remain trusted but correctness-validated. The
  repair adds no approval, refusal, hostility check, arbitrary cap, or paternalistic guard.
- **Scope discipline:** Raw reads/writes, flash, setup, permissions, plans, firmware, fixtures, and
  unrelated server behavior remain untouched.

## Post-plan check — main/orchestrating model

- **Correctness decision:** Only `HALTED` reads directly. Any other reported target state uses one
  halt, one read, and one guaranteed resume attempt. Read and restoration failures are both
  preserved when simultaneous; success is impossible while restoration is uncertain.
- **Simplicity decision:** Add three injected lifecycle callables plus one local helper rather than
  coupling the tool to a concrete backend or expanding the legacy fixed-width symbol helper.
- **Generalizability decision:** Normalize provider state text case-insensitively and use only the
  generic lifecycle interface. The plan deliberately contains no fixture-derived constant.
- **Usability decision:** The exact affected MCP help text is in scope because FastMCP publishes the
  handler docstring. No unrelated documentation sweep is allowed.
- **Rejected alternatives:** Do not retry a sleeping read, treat zero as invalid, special-case
  `SLEEPING`, inspect a particular counter twice, leave the target halted, silently ignore resume
  failure, or broaden coherent halting to raw memory operations.
- **Accepted interpretation:** `resume` restores execution eligibility, not an exact firmware
  low-power state. A failed halt is reported without a speculative resume because no successful
  inserted state transition was established.

## Pre-implementation check

### Main/orchestrating model — 2026-07-28

- Reread the complete charter at SHA-256
  `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`
  immediately before authorizing the implementation role.
- **Correctness over simplicity:** A coherent snapshot and truthful restoration failure are
  non-negotiable. The accepted plan remains the smallest explicit control flow that can provide
  them; no fabricated zero, swallowed error, or guessed state is permitted.
- **Generalizability/dynamism:** The doer must use only injected generic target operations and
  case-normalized provider state. No board, state-name exception, OS, probe, or toolchain branch
  is justified.
- **Neatness/usability:** Keep responsibility in `tools/memory.py` and wire it once in `server.py`.
  CL-002 is a distinct checkpoint because public help is part of the tool contract.
- **Trust/functionality:** The change corrects tool observation; it adds no user gate, approval,
  arbitrary limit, adversarial-input defense, or new refusal.
- **Scope exclusion:** The doer may edit only production files required by CL-001/CL-002 and may
  not touch tests, firmware, fixtures, setup, flash, permissions, raw-memory semantics, or
  unrelated existing dirty changes.

The implementation doer must append its own pre-edit check before changing production source.

### Implementation doer pre-edit check — 2026-07-27

- **Contemplated feature:** CL-001 only: inject generic `get_state`, `halt`, and `resume` operations into the memory service and use one local helper for scalar symbol reads after all existing validation.
- **Correctness and simplicity:** A read from any state other than case-normalized `HALTED` will halt once, read once, and resume once; read and restoration failures remain visible and no result is recorded before restoration succeeds. This is a local explicit helper, not a transaction framework.
- **Generalizability and usability:** The helper uses only provider-defined state text and generic lifecycle operations; no board, MCU, probe, OS, toolchain, low-power-state, or value-specific branch is allowed. The later CL-002 help update will disclose the lifecycle and recovery.
- **Assumption/tie-breaker:** `resume` restores execution eligibility for every successfully inserted halt; the precise low-power state is neither exposed nor inferable. Correctness wins over the smallest direct-read implementation.
- **Rejected alternatives and scope:** Do not special-case `SLEEPING`, reject zero, retry/poll, invoke lifecycle operations for raw reads or writes, modify safety/session/provider behavior, or change tests.

### Scoped sandbox recovery — main/orchestrating model

- The first persistent doer and spec-tester turns were launched with the documented
  `workspace-write` sandbox, but Codex enforced read-only filesystem access and rejected both the
  authorized production edit and required runtime/test evidence writes.
- No source or tests changed. The same persistent threads will be resumed with the change-loop
  skill's narrowly documented `danger-full-access` fallback, while retaining `--ignore-user-config`,
  medium reasoning, priority/Fast service, exact repository root, policy-bound prompts, role edit
  ownership, and no hardware authority.
- This is host-tool recovery, not a server design choice. It adds no production guard, branch,
  limit, approval bypass flag, or expanded repair scope and therefore preserves the charter's
  correctness, simplicity, generality, and trust boundary.

## Plan adversarial review — A20-plan-adversarial-reviewer-001

- The reviewer verified charter SHA-256
  `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`
  and plan SHA-256
  `a974e693dd8b16d03d993b27b0c16f1113891482de58e67160608b2cc6da0a07`
  before assessing the plan. A second reread was attempted before report persistence but denied by
  the review session's evidence-only policy; the original full read and matching hash remain in
  `plan-review.codex.jsonl`.
- The reviewer judged the plan sound and focused execution on lifecycle ordering, preservation of
  primary plus restoration failures, generic case-normalized state handling, production wiring,
  adjacent-tool isolation, and the public FastMCP help contract.
- These risks preserve charter correctness without adding retry/value heuristics, keep the repair
  to one simple local helper, exclude board/OS/toolchain branching, retain one responsibility in
  the memory tool, teach the caller the lifecycle, and add no hostile-input or paternalistic gate.
- The review did not edit the plan, production source, or tests. Because its permitted report write
  was rejected, the main model transcribed the numbered risks into `plan-review.md` from the
  immutable reviewer output instead of launching another review.

## Between implementation features

### Implementation doer review — 2026-07-27

- **Reviewed features:** CL-001 lifecycle helper/production wiring and CL-002 published handler help.
- **Charter application:** The helper is limited to scalar symbol reads after validation, has no environmental branches or retry/value heuristic, and delays success recording until the required resume succeeds. The help describes purpose, parameters, return, temporary halt/restoration, honest failures, and recovery.
- **Assumption/tie-breaker:** The provider supplies a state string and only case-normalized `HALTED` means no intervention. Correctness favors reporting a restoration failure over returning an otherwise valid value.
- **Rejected alternatives/scope:** No special `SLEEPING` path, raw-read/write lifecycle behavior, concrete backend coupling, target-specific logic, test edits, or changes to plan/session/safety/provider semantics.

## Pre-verification check

### Implementation doer — 2026-07-27

- **Tests contemplated:** focused symbol-memory lifecycle coverage through the existing test suite, plus Ruff, Pyright, and `git diff --check`.
- **Charter application:** Verification must demonstrate truthful errors and target restoration without relying on fixture-specific state names or a zero-value heuristic; adjacent raw paths remain isolated.
- **Assumption/tie-breaker:** Host-side tests are sufficient for this source-only change and do not authorize board activity. No board/OS/toolchain-specific alternative is selected.
- **Scope exclusions:** Do not modify tests, manifests, commands, hardware, firmware, or unrelated dirty changes while validating.

## Corrective lifecycle-flow check — 2026-07-27

- **Contemplated diff:** Remove the `return` from the helper's `try` body so a successful non-halted read always reaches the single `resume` call before returning.
- **Correctness and simplicity:** An in-memory call trace proved that Python's `try`/`else` does not execute the `else` suite after a `return`; the correction keeps explicit flat control flow, preserves read-error cleanup and dual-error reporting, and prevents false successful restoration.
- **Generalizability and tie-breaker:** The correction remains generic over all non-`HALTED` provider state strings and uses no board/OS/toolchain-specific branch. Correctness wins over preserving the superficially shorter form.
- **Scope exclusions:** No retry, value heuristic, raw read/write change, backend coupling, test edit, or hardware action is introduced.

## Post-correction pre-verification check — 2026-07-27

- **Tests contemplated:** direct lifecycle call-order/error probes; focused regressions; Ruff; lockfile-managed Pyright; and `git diff --check`.
- **Charter application:** Test successful and failing restoration separately so the tool cannot fabricate a completed scalar read; retain generic case-normalized handling and raw-path isolation.
- **Assumption/tie-breaker:** A local in-memory provider fake is adequate to test the helper's sequencing without a board. No target, OS, or toolchain-specific substitute is used.
- **Scope exclusions:** Tests remain unmodified; no fixture, firmware, permission, or unrelated worktree change is involved.

## Post-risky-diff check

### Implementation doer — 2026-07-27

- **Diff reviewed:** only `tools/memory.py` and `server.py` change production behavior. The helper observes state, directly reads only an already halted target, otherwise halts, reads, resumes, and returns only after resume; dual failures contain both error types/messages.
- **Charter application:** Correctness is preserved by no fabricated zero or success on restore failure; simplicity by one local helper; generality by case-normalized generic state only; usability by published lifecycle/recovery help.
- **Assumption/tie-breaker:** Existing test constructors retain optional lifecycle fields while production supplies all three operations. This avoids unrelated constructor breakage without changing the production contract.
- **Rejected alternatives/scope:** No state-specific, hardware-specific, OS/toolchain-specific, raw-operation, retry, or test-file change exists.

## Pre-acceptance check

### Implementation doer — 2026-07-27

- **Final review:** Focused regressions and direct lifecycle probes pass; Ruff, lockfile-managed Pyright, and diff whitespace checks pass. Repository-wide pytest exceeded the local 120-second command limit and therefore is not claimed green.
- **Charter application:** The final result reports that unresolved verification limitation honestly; no success is fabricated. The accepted source scope remains the smallest generic repair.
- **Scope exclusions:** Neutral harness results remain authoritative; no test, manifest, command, hardware, or unrelated dirty change was modified.

## Iteration 2 pre-verification check — 2026-07-27

- **Contemplated verification:** Rerun the tester-owned A20 spec and regression files through the existing Windows virtualenv after the neutral Bash launcher removed path separators.
- **Charter application:** This diagnoses the reported infrastructure invocation only; it neither changes source nor invents a green result. The generic target lifecycle implementation, including raw-path isolation, remains the only feature under test.
- **Assumption/tie-breaker:** The report's exit 127 occurred before pytest loaded, while the files and Windows virtualenv exist. Use the host-native executable path rather than changing tester commands or introducing OS-specific production behavior.
- **Scope exclusions:** No test, manifest, command, firmware, board, or unrelated production file will be modified.

## Iteration 2 final verdict check — 2026-07-27

- **Result reviewed:** The native Windows-virtualenv invocations of the exact A20 spec and regression files pass. The neutral report's exit-127 failures are retained as launcher-path evidence and were not rewritten.
- **Charter application:** Report the launcher distinction honestly; no source change is justified by a test process that never reached pytest. The existing repair remains generic, simple, and isolated to scalar symbol reads.
- **Scope exclusions:** This iteration changed only the required checkpoint log. No production source, tests, test commands, manifests, hardware, or unrelated worktree content was modified.

## Green neutral-gate final verdict check — 2026-07-27

- **Result reviewed:** The neutral A20 spec and regression gates both exit 0 with their recorded commands; no additional failure report exists.
- **Charter application:** The current repair remains a truthful, minimal, generic scalar-read lifecycle fix with no target-specific behavior or unrelated scope expansion.
- **Scope exclusions:** No production source, test, manifest, command, hardware, or unrelated worktree content was modified in this confirmation turn.

## Post-gate cancellation cleanup pre-edit — 2026-07-27

- **Charter SHA-256 verified:** `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.
- **Contemplated diff:** Expand the local scalar-read cleanup to attempt one resume after every `BaseException` from the read following a successful inserted halt.
- **Correctness/simplicity:** Preserve the original interruption when cleanup succeeds; expose both primary and restoration failure when cleanup also fails. A single local `BaseException` handler is simpler and more truthful than a special cancellation branch.
- **Assumption/tie-breaker:** `BaseException` failures after the inserted halt need the same known-state restoration attempt as ordinary read failures; no retry or swallowing is allowed.
- **Rejected alternatives/scope:** No target/OS/toolchain branch, raw-path lifecycle change, test edit, or provider/session change.

## Post-gate public-help pre-edit — 2026-07-27

- **Charter SHA-256 verified:** `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.
- **Contemplated diff:** Expand only the public `read_memory_symbol` docstring with explicit parameter meanings and one invocation example.
- **Usability/simplicity:** The published handler docstring is the existing MCP help surface; concise parameter text and an example eliminate guessing without changing schema or adding documentation machinery.
- **Assumption/tie-breaker:** `elf_artifact` remains optional and refers to the current local ELF; the existing lifecycle, failure, and recovery text stays intact.
- **Rejected alternatives/scope:** No schema/return change, board/toolchain-specific example, test edit, or unrelated documentation change.

## Post-gate corrections pre-verification — 2026-07-27

- **Charter SHA-256 verified:** `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.
- **Checks contemplated:** focused A20 spec/regression suites, Ruff, lockfile-managed Pyright, and `git diff --check`.
- **Charter application:** Validate cancellation-safe cleanup and published-help completeness without hardware. Correctness requires re-raising the original `BaseException` after successful cleanup and reporting both failures otherwise.
- **Scope exclusions:** No tests, commands, manifests, hardware, or unrelated files are changed.

## Post-gate corrections final verdict — 2026-07-27

- **Charter SHA-256 verified:** `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.
- **Final result:** The helper now performs exactly one cleanup attempt for every `BaseException` from a read following an inserted halt, preserving the primary failure or honestly combining it with cleanup failure. The published help documents all parameters and a generic invocation.
- **Checks:** A20 focused tests passed (10 tests, 6 subtests); Ruff and lockfile-managed Pyright passed; `git diff --check` passed.
- **Scope exclusions:** Only `tools/memory.py` production behavior/help changed in this correction. No tests, manifests, commands, hardware, or unrelated production files were modified.

## Spec tester pre-edit check - 2026-07-27

- **Contemplated test feature:** one pytest/unittest module that fakes the
  injected generic lifecycle callables and asserts CL-001 call ordering,
  cleanup/error truthfulness, validation isolation, and raw-operation isolation.
- **Charter properties:** correctness requires preserving real zero values and
  refusing to record success before restoration; simplicity and neatness keep
  the proof in the existing test framework with no production helper. Generic
  state strings and case variants uphold generalizability.
- **Assumption/tie-breaker:** documented generic provider operations suffice to
  prove host-side sequencing. Correctness outweighs minimizing test cases where
  state restoration can otherwise be misreported.
- **Rejected alternatives / scope:** no board, MCU, probe, OS, path, toolchain,
  firmware, external-equipment, or state-specific fixture; no production edits,
  permission changes, or raw behavior changes.

## Spec tester pre-verification check - 2026-07-27

- **Verification feature:** run only the new A20 pytest module non-interactively
  from the repository root, then inspect its diff and result.
- **Charter properties:** correctness is verified by exact lifecycle sequencing,
  truthful cleanup errors, valid-zero output, and no success event on failed
  restoration. Generality remains limited to generic provider state strings;
  simplicity avoids hardware or process-provider integration.
- **Assumption/tie-breaker:** host fakes are the narrow sufficient proof for
  service injection and handler behavior. The neutral harness remains the final
  authority for the recorded command.
- **Rejected alternatives / scope:** no board or external-equipment action, no
  OS/toolchain-specific test path, no production or unrelated test modification.

## Spec tester final-verdict check - 2026-07-27

- **Final test verdict:** the recorded focused suite passes through the
  repository virtual environment (8 tests and 6 subtests), with Ruff and
  whitespace validation also passing.
- **Charter properties:** tests enforce truthful coherent reads and restoration
  without fabricated values or target-specific behavior. The focused fake-based
  proof remains the simplest and most general evidence for this handler-level
  repair; public help is tested from the registered server handler.
- **Assumption/tie-breaker:** the neutral harness executes the recorded command
  as the authoritative verdict. No broader suite result is implied here.
- **Scope exclusions:** only the A20 tester-owned test and required state/log
  evidence were added; production, hardware, permissions, firmware, and
  unrelated dirty worktree changes remain outside this role's edits.

## Spec tester iteration-2 command repair check - 2026-07-27

- **Contemplated evidence edit:** replace the tester-owned command's Windows
  backslashes with a repository-relative forward-slash executable path so the
  neutral Bash harness does not erase its separators before invoking pytest.
- **Charter properties:** this is a test-harness portability correction only;
  it preserves the same focused assertions and supplies truthful executable
  evidence. It introduces no production behavior, target assumption, or
  environment-specific server branch.
- **Assumption/tie-breaker:** `./.venv/Scripts/python.exe` was executed under
  Bash from the repository root and reached the same project virtualenv. A
  repository-relative path is preferable to an absolute host-specific path.
- **Rejected alternatives / scope:** do not weaken tests, change production,
  alter the regression tester's owned command, or use hardware/board actions.

## Spec tester iteration-2 pre-verification check - 2026-07-27

- **Verification feature:** invoke the exact newly recorded command through
  Bash from the repository root, matching the neutral harness's shell.
- **Charter properties:** success must prove the test runner actually loaded
  pytest and ran the lifecycle assertions, rather than merely resolving an
  executable. No target-specific or production behavior is involved.
- **Assumption/tie-breaker:** a direct Bash run is the smallest faithful
  reproduction of the neutral exit-127 failure mode.
- **Scope exclusions:** no test assertion, production file, regression-owned
  command, hardware, or external resource is changed.

## Spec tester iteration-2 final-verdict check - 2026-07-27

- **Result:** the exact forward-slash recorded command passes under Bash with
  8 tests and 6 subtests; Ruff on the owned test and `git diff --check` pass.
- **Charter properties:** the neutral launcher now executes the intended
  correctness assertions rather than failing before pytest starts. The suite
  remains generic, host-only, and scoped to the public scalar-symbol contract.
- **Assumption/tie-breaker:** the independent regression tester owns its
  separate command and evidence; this role reports only its repaired command.
- **Scope exclusions:** no production, test assertion, regression-owned state,
  hardware, firmware, permission, or unrelated worktree change was made.

## Spec tester neutral-pass final check - 2026-07-27

- **Result reviewed:** the neutral gate reports the exact spec command and the
  independently owned regression command both passing; the spec command was
  rerun successfully under Bash in this turn.
- **Charter properties:** the existing tests continue to require coherent,
  truthful scalar reads without target-specific logic or broadened raw-memory
  behavior. No new feature is justified once the adversarial gate is green.
- **Assumption/tie-breaker:** neutral test evidence is authoritative, so retain
  the strict suite unchanged instead of adding redundant coverage.
- **Scope exclusions:** no production, test, command, manifest, hardware,
  firmware, permission, or unrelated dirty-worktree change occurred this turn.

## Spec tester BaseException/help pre-edit check - 2026-07-27

- **Contemplated test feature:** add assertions that a `KeyboardInterrupt`
  primary failure resumes once and re-raises the same object, that dual
  non-`Exception` failures retain both facts and primary chaining, and that the
  published help names every public parameter plus its concrete invocation.
- **Charter properties:** correctness forbids bypassing guaranteed restoration
  on cancellation or fabricating a success; usability requires agent-facing
  parameter and example guidance. The fake remains generic and host-only.
- **Assumption/tie-breaker:** `KeyboardInterrupt` and `SystemExit` are portable
  representatives of `BaseException` failures. Exact identity and chaining are
  stronger evidence than matching text alone.
- **Rejected alternatives / scope:** no production change, board/MCU/probe/OS/
  toolchain branch, hardware action, retry, raw-operation behavior, or change
  to another tester's file, manifest, or command.

## Spec tester BaseException/help pre-verification check - 2026-07-27

- **Verification feature:** run the unchanged recorded Bash command, Ruff on
  the owned test, and the repository whitespace check after confirming the
  charter SHA-256 remains the required value.
- **Charter properties:** verification must prove the exact primary object is
  preserved after cleanup and the public description remains usable before an
  agent invokes the tool; no environment-specific fixture is involved.
- **Assumption/tie-breaker:** test-local `BaseException` values are sufficient
  to exercise the host-side cleanup path without a provider or hardware action.
- **Scope exclusions:** no production, command/manifest, regression-owned file,
  board, firmware, or unrelated worktree change is included.

## Spec tester help-assertion correction check - 2026-07-27

- **Contemplated test correction:** assert the required concrete invocation
  itself, independent of sentence punctuation, after the first run showed the
  implementation supplies the example followed by a period rather than comma.
- **Charter properties:** usability requires the example text, not an arbitrary
  punctuation character; retaining the exact callable expression preserves a
  strict public-help contract without a false failure.
- **Assumption/tie-breaker:** the plan requires an example but does not prescribe
  punctuation. Correctness favors the semantic help requirement over that
  formatting accident.
- **Scope exclusions:** no production, lifecycle assertion, command/manifest,
  regression-owned work, hardware, or environment-specific change.

## Spec tester BaseException/help final pre-verification check - 2026-07-27

- **Verification feature:** rerun the exact Bash-recorded suite after the
  punctuation-independent help assertion, then run Ruff and `git diff --check`.
- **Charter properties:** retain strict lifecycle/error identity coverage and
  validate public help by its meaningful contract, with no target-specific
  conditions or behavior changes.
- **Assumption/tie-breaker:** the re-run must be green only after pytest reaches
  all new BaseException and help assertions.
- **Scope exclusions:** no production, command/manifest, regression tester,
  board, firmware, or external resource action is involved.

## Spec tester BaseException/help final-verdict check - 2026-07-27

- **Result:** the exact recorded Bash command passes with 10 tests and 6
  subtests; Ruff on the owned test and `git diff --check` also pass. The
  verified charter SHA-256 remains
  `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.
- **Charter properties:** the added tests make cleanup truthful even for
  non-ordinary failures and make published usage self-contained, while retaining
  generic, host-only, narrowly scoped behavior.
- **Assumption/tie-breaker:** a `KeyboardInterrupt` primary and `SystemExit`
  restoration error sufficiently prove the required `BaseException` paths and
  chaining without broadening source or tests to platform details.
- **Scope exclusions:** only the owned A20 spec test and required charter log
  changed; production, commands/manifests, regression-owned files, hardware,
  firmware, permissions, and unrelated worktree changes were untouched.

## Regression tester initial-analysis check - 2026-07-27

- **Contemplated diff/test feature:** focused regression tests for the public
  scalar-symbol read lifecycle and its caller/interface blast radius, limited to
  existing repository test conventions.
- **Charter properties applied:** correctness requires an honest failure when
  restoration fails and no fabricated scalar value; simplicity and neatness keep
  coverage local to the memory-tool contract. Generic provider state strings
  must remain case-normalized without board, MCU, probe, OS, or toolchain
  branches.
- **Assumption/tie-breaker:** host-side fakes are sufficient to prove exact
  lifecycle ordering and interface isolation. Correctness wins over shorter
  direct-read behavior where a running target otherwise yields an incoherent
  value.
- **Rejected alternatives and scope exclusions:** no hardware action, external
  equipment, production/spec-tester edits, raw-read/write behavior change,
  state-specific fixture, retry/polling heuristic, or environment-specific test
  assumption.

## Regression tester pre-edit check - 2026-07-27

- **Contemplated test feature:** prove that absent lifecycle dependencies cannot
  silently fall back to a direct scalar read, while legacy raw-address reads
  remain usable without those newly optional dependencies.
- **Charter properties applied:** correctness requires an explicit honest error
  before target I/O when coherent-read prerequisites are absent; simplicity
  preserves the existing raw-memory behavior rather than imposing lifecycle
  requirements on unrelated operations. The tests use no environment-specific
  fixture identity.
- **Assumption/tie-breaker:** optional dataclass fields preserve existing
  constructor callers, but only the newly changed scalar-symbol path requires
  the lifecycle contract. Correctness and compatibility therefore both favor
  this narrow boundary test.
- **Rejected alternatives and scope exclusions:** no production change, board
  action, OS/toolchain constant, retry, state-name special case, or expansion of
  coherent-stop behavior to raw reads/writes.

## Regression tester pre-verification check - 2026-07-27

- **Contemplated verification:** run the regression-owned A20 suite together
  with the existing focused A20 contract suite, then run lint and whitespace
  checks against the owned test file and current diff.
- **Charter properties applied:** the verification must prove both honest
  lifecycle-precondition failure and unchanged raw-memory isolation; host fakes
  avoid unsupported hardware, OS, or toolchain assumptions.
- **Assumption/tie-breaker:** a noninteractive repository-root pytest command
  is the sufficient neutral-harness handoff for this test-only role. No
  environment-specific substitute or board check is needed.
- **Scope exclusions:** no production/tester-file rewrite, firmware, fixture,
  permission, external-equipment, or live-board action is authorized.

## Regression tester final-verdict check - 2026-07-27

- **Verification result:** the regression-owned command passes (2 tests), and
  the combined owned-plus-existing focused A20 run passes (10 tests and 6
  subtests). Ruff on the owned test and `git diff --check` pass.
- **Charter properties applied:** the tests prove the changed scalar path does
  not fabricate a value through a missing-dependency fallback, while the
  adjacent raw-address path retains its original direct-read behavior. The
  coverage remains generic and host-only.
- **Assumption/tie-breaker:** the repository virtual environment is the
  project-owned noninteractive execution path after the host interpreter lacked
  pytest and Ruff. No board, OS, toolchain, or fixture-specific alternative was
  used.
- **Scope exclusions:** only the regression-owned test and required suite
  coordination evidence were added; production, spec-tester tests, firmware,
  permissions, and hardware remain untouched.

## Regression tester iteration-two command-repair check - 2026-07-27

- **Contemplated diff/test feature:** replace only the recorded regression
  command's Windows-style separators with a Bash-compatible relative command;
  the owned test code is unchanged.
- **Charter properties applied:** a test command must report its result
  truthfully in the neutral runner, while remaining simple, noninteractive, and
  independent of any board, OS-specific source branch, or toolchain-specific
  test behavior.
- **Assumption/tie-breaker:** the neutral report establishes Bash execution;
  `./.venv/Scripts/python.exe` is verified there and is the narrow compatible
  spelling. Correctness favors a working harness command over preserving a
  PowerShell-only invocation.
- **Rejected alternatives and scope exclusions:** no production or test logic
  change, no physical hardware action, no alternate interpreter installation,
  no environment-specific assertion, and no modification to spec-tester files.

## Regression tester iteration-two pre-verification and final-verdict check - 2026-07-27

- **Verification result:** under Bash, the recorded regression command passes
  (2 tests); the combined regression and existing focused A20 suite passes (10
  tests and 6 subtests). Ruff on the owned test and `git diff --check` pass.
- **Charter properties applied:** the Bash-proof command prevents a false
  infrastructure failure while test coverage remains focused on truthful
  lifecycle failure and raw-read isolation. No hardware, board, MCU, OS-source,
  or toolchain-specific behavior is asserted.
- **Assumption/tie-breaker:** the complete charter was reread before this
  verification. The neutral report's shell evidence is sufficient to choose a
  portable relative executable path; no source change is warranted.
- **Scope exclusions:** the spec suite's separately recorded command remains
  spec-tester-owned; this role changed only its required regression command,
  owned regression test, and checkpoint evidence.

## Regression tester neutral-pass final-verdict check - 2026-07-27

- **Diff/test review:** the latest neutral report passes both the spec suite
  (8 tests and 6 subtests) and the owned regression suite (2 tests). The A20
  production diff remains limited to scalar-symbol lifecycle injection and its
  helper; no new caller, public-interface, persistence, concurrency, or
  adjacent-operation risk warrants an additional test feature.
- **Charter properties applied:** retain the smallest focused test proof for
  truthful dependency failure and raw-address isolation; do not add speculative
  fixture, board, MCU, OS, or toolchain branches after the generic lifecycle
  behavior has passed neutral execution.
- **Assumption/tie-breaker:** the neutral report is authoritative for the
  recorded commands. Correctness favors preserving tested adjacent behavior over
  redundant tests of untouched execution-control code.
- **Scope exclusions:** no production/spec-tester edit, hardware action,
  external equipment, firmware change, or broad-suite claim is made by this
  role.

## Regression tester BaseException pre-edit check - 2026-07-27

- **Contemplated test feature:** a registry-level assertion that the public
  `read_memory_symbol` callable retains its established parameter order and
  defaults while its help text is expanded.
- **Charter properties applied:** correctness and usability require that public
  documentation improvements do not silently alter the tool contract;
  simplicity keeps the proof to one signature assertion, independent of board,
  MCU, probe, OS, or toolchain details.
- **Assumption/tie-breaker:** direct BaseException cleanup, dual failure, and
  help text are already spec-tester-owned. The distinct public-interface edge
  is the narrowest nonduplicative regression target.
- **Rejected alternatives and scope exclusions:** no duplicate cancellation or
  help test, production change, hardware action, fixture-specific branch, or
  change to raw-memory/execution behavior.

## Regression tester BaseException pre-verification check - 2026-07-27

- **Contemplated verification:** execute the exact owned regression command in
  Bash, the adjacent A20 spec suite, Ruff on the owned test, and whitespace
  validation.
- **Charter properties applied:** verification proves the public handler
  contract remains intact while the spec suite independently proves cancellation
  cleanup; all evidence is host-only and generic.
- **Assumption/tie-breaker:** the repository virtual-environment executable is
  the portable neutral-runner path established by the passing report. No
  board/OS/toolchain-specific substitute is selected.
- **Scope exclusions:** no source, hardware, firmware, fixture, or
  spec-tester-owned artifact is changed during verification.

## Regression tester BaseException final-verdict check - 2026-07-27

- **Verification result:** the exact Bash regression command passes with 3
  tests; the adjacent A20 spec suite passes with 10 tests and 6 subtests; Ruff
  on the owned test and `git diff --check` pass. The charter SHA-256 remains
  `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.
- **Charter properties applied:** the added assertion protects the unchanged
  public signature, while direct BaseException cleanup remains covered by the
  spec suite. This preserves truthful cancellation behavior, simple generic
  design, and raw-operation isolation without environment-specific branches.
- **Assumption/tie-breaker:** no remaining actionable caller, construction,
  Layer-2, or adjacent-operation defect was evidenced after focused testing;
  avoid speculative duplicate cancellation coverage.
- **Scope exclusions:** only the owned regression test and required checkpoint
  log changed in this follow-up; production, spec artifacts, hardware,
  firmware, and unrelated tests were untouched.

## Main-model pre-verification check - 2026-07-28

- The main/orchestrating model reread the complete authoritative charter at SHA-256
  `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`
  after the neutral gate reported both focused suites passing.
- **Correctness:** Verification must independently establish that a non-halted scalar-symbol read
  cannot return before execution restoration, that read and restoration failures remain truthful,
  and that already-halted and legitimate-zero reads are preserved.
- **Simplicity and scope:** The accepted production behavior remains one local lifecycle helper
  plus dependency wiring and public help. No retry, polling, state-specific case, raw-memory
  behavior change, or unrelated dirty production change is accepted as part of A20.
- **Generalizability and dynamism:** The review must reject any dependence on STM32, a particular
  sleep state, probe, OS, port, toolchain, or fixture. Only case-normalized generic target state
  and injected generic lifecycle operations are permitted.
- **Neatness and usability:** Responsibility stays in `tools/memory.py`, wiring stays in
  `server.py`, and the published handler help must explain when to use the operation, parameters,
  return, temporary halt/restoration, common failures, and recovery.
- **Trust boundary:** This repair adds no approval, refusal, arbitrary limit, hostile-input
  defense, or paternalistic guard. Verification is host-only and authorizes no hardware action.

## Main-model post-diff-review decision - 2026-07-28

- The main model reread the complete charter at the recorded SHA-256 and independently checked the
  diff review against CL-001, CL-002, `services/symbols.py`, and the affected public docstring.
- **Accepted correctness gap:** Catching only `Exception` does not provide guaranteed cleanup for
  every failure raised after the inserted halt. A narrow cleanup path must also preserve
  cancellation/interrupt semantics while ensuring restoration is attempted and dual failures
  remain truthful.
- **Accepted usability gap:** The exact public tool help must state the meaning of every parameter
  and include one concise invocation example, as required by the charter. This changes no schema
  and adds no new behavior.
- **Simplicity/generalizability:** Correct only the existing local helper, docstring, and focused
  tester-owned assertions. Do not add a transaction framework, provider/board special case, retry,
  value heuristic, OS/toolchain assumption, or broad documentation sweep.
- **Trust/scope:** No new guard, refusal, approval, arbitrary cap, hardware action, or unrelated
  production change is authorized. The existing plan remains valid and is not amended.

## Main-model post-correction pre-verification check - 2026-07-28

- The main model reread the complete charter at SHA-256
  `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`
  after the doer and both persistent testers completed their corrective turns.
- **Correctness:** The current helper now restores after every `BaseException` from the read,
  re-raises an unchanged primary when restoration succeeds, and reports both facts when
  restoration also fails. The neutral gate now directly exercises those cancellation paths.
- **Simplicity/generalizability:** The behavior remains one flat local helper using only generic
  injected lifecycle operations; no retry, value heuristic, state-specific case, or
  board/OS/probe/toolchain branch was added.
- **Neatness/usability:** The public handler's schema is unchanged and independently regression
  checked. Its docstring now names every parameter, gives an example, states its return and
  lifecycle, and provides honest failure/recovery guidance.
- **Trust/scope:** No permission, refusal, hostile-input defense, arbitrary cap, hardware action,
  or unrelated production edit was introduced. Verification remains host-only.

## Main-model pre-acceptance check - 2026-07-28

- The complete charter was reread at SHA-256
  `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`
  after the final neutral and main verification gates and after the same independent diff reviewer
  returned `VERDICT: ACCEPT`.
- **Correctness:** Every inserted halt now has a truthful restoration boundary across success,
  ordinary failure, and cancellation/interrupt-class failure. Success cannot be recorded before
  restoration, already-halted targets are not resumed, legitimate zero is preserved, and dual
  failures retain both facts.
- **Simplicity/generalizability:** One local helper and three injected generic operations remain
  the whole behavior change. There is no board, low-power-state, provider, OS, port, probe,
  toolchain, or value-specific branch and no speculative abstraction.
- **Neatness/usability/dynamism:** The memory tool owns the behavior, production wiring is singular,
  the schema is unchanged, and complete public help lets an agent use and recover from the tool
  without outside knowledge.
- **Trust/scope:** The accepted diff adds no paternalistic gate, hostile-input defense, arbitrary
  limit, retry loop, permission change, firmware change, or unrelated cleanup. Only
  `tools/memory.py` and `server.py` differ from the preceding production manifest.
- **Acceptance boundary:** Source repair is accepted for an exact reporting-run target retest; the
  production queue remains open until the two-board sleeping-state reproducer and directly
  affected cleanup controls pass on repaired snapshot diff `a520dfee72bb4aac5a0b9f53f1847bb834a73729`.
