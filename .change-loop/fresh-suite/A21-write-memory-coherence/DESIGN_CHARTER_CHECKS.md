# Design charter checks — A21 write-memory coherence

Charter:
`C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\.codex\design_charter.md`

Whole-file SHA-256:
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`

## Before repair specification — 2026-07-28T19:03Z

The main model reread the entire charter before accepting and specifying this repair.

- **Correctness / no fabrication:** the defect is a successful mutation report contradicted by
  immediate coherent readback. The repair must verify before success and report lifecycle,
  mutation, verification, and restoration failures honestly.
- **Simplicity:** keep ownership in `tools/memory.py`; use existing generic target lifecycle and
  scalar read/write services. Do not add a provider feature, new public parameter, or unrelated
  abstraction.
- **Generalizability / dynamism:** key only on generic target state and existing memory services.
  No board, MCU, address, firmware, pyOCD, OS, or path constant is permitted.
- **Neatness:** one coherent scalar-write helper should own lifecycle, exact readback, and
  restoration semantics for both symbol and raw-address calls.
- **Usability:** public action and plan guidance must explain the automatic halt/verify/restore
  behavior, what success proves, later firmware overwrite, failures, and recovery.
- **Guard boundary:** exact readback and execution restoration are correctness checks for a
  fallible agent/tool, not adversarial hardening or paternalistic refusal.
- **Scope exclusion:** do not edit unrelated server behavior merely because the charter suggests
  broader improvements.

Accepted interpretation: a successful write proves the requested scalar value was coherently
observed at the exact address and width while execution was halted. It cannot promise that firmware
will not later overwrite its own variable after execution resumes.

## Required future rereads

- Before main-authored plan validation and one-time plan review.
- Before the implementation doer's first slice.
- Before any distinct follow-up implementation slice.
- Before neutral test acceptance and main verification.
- Before targeted A21 HIL retest and final repair acceptance.

Every repair-role prompt must direct that role to reread the same charter at its applicable
boundaries and stop if the requested work conflicts with it.

## Before plan validation and independent review — 2026-07-28T19:10Z

The main model reread the entire charter and verified the same SHA-256 immediately before
validating the directly authored plan. Validation passed with plan SHA-256
`d2a1d29a7b8932133fa959bf35733906086cee3121240aa0594e15c0615fc626`.
The sole independent reviewer was explicitly required to reread the charter and checked the plan
against correctness, simplicity, provider neutrality, coherent lifecycle ownership, truthful
failure reporting, usable public recovery guidance, and strict scope control. The review passed
with no blocking risk.

## Before plan validation and adversarial review — 2026-07-28T19:09Z

The main model reread the complete charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) immediately
before validating the directly authored plan. Deterministic validation passed for plan SHA-256
`d2a1d29a7b8932133fa959bf35733906086cee3121240aa0594e15c0615fc626`.

The plan keeps the charter boundary explicit: exact readback and lifecycle restoration are
correctness duties, not a new permission or paternalistic guard; one provider-neutral private
helper is the simple/general implementation; the public call shape and unrelated memory behavior
remain fixed; and the help contract explains exactly what success does and does not prove.

## Plan adversarial review — A21-write-memory-plan-reviewer-001

Immediately before this review record, the reviewer reread the complete charter and verified its
whole-file SHA-256 as
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.

The review confirms that exact same-address/same-width readback and restoration only after an
inserted halt preserve **correctness** and the charter's no-fabrication requirement. One private,
provider-neutral helper preserves **simplicity**, **generality**, and **dynamism** without a
board/provider state allowlist or public configuration. Testing unchanged success text, explicit
failure/recovery guidance, and both public routes preserves **neatness/usability**. Existing
symbol-first, mapped-RAM, plan/gate, and pre-I/O refusal checks remain the trust boundary; the new
verification is a correctness check for fallible-agent mistakes, not adversarial hardening or a
new permission barrier.

## Before implementation doer first slice — 2026-07-28T19:16Z

The main model reread the complete charter and verified SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`
immediately before authorizing the first implementation slice. The reviewed plan remains byte
stable at SHA-256
`d2a1d29a7b8932133fa959bf35733906086cee3121240aa0594e15c0615fc626`.
The runtime `changes.md` was reconciled to the A21 request without changing that plan. The doer
must use one provider-neutral helper, preserve every pre-I/O check and public permission boundary,
verify exact same-address/same-width value before success, restore only a halt it inserted, report
all failures honestly, publish recovery, and avoid unrelated edits.

The first role start failed before creating a thread or changing source because the Windows
workspace-write sandbox could not launch. The approved change-loop fallback removes only that
broken host sandbox layer; the role remains scoped to this repository, uses isolated configuration,
and is still bound to the same charter, plan, model, reasoning, and priority/Fast tier.

## Before implementation doer first slice â€” 2026-07-28T19:20Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
examining or editing the A21 implementation. The contemplated diff is one private scalar-write
lifecycle helper, routing the existing write handler through it after all existing pre-I/O checks,
and matching public write/plan guidance. Correctness requires same-address, same-width exact
readback before success and restoration only of an execution state this server interrupted;
primary and restoration failures must remain honest. Simplicity, generalizability, dynamism, and
neatness require reuse of generic existing lifecycle/scalar services in `tools/memory.py`, with no
provider, board, MCU, OS, toolchain, or address special case and no public API, permission, retry,
reset, or persistent-state addition. The exact readback/restoration is a correctness guard for a
fallible agent, not adversarial hardening or a paternalistic refusal. Assumption/tie-breaker: any
non-`HALTED` generic provider state is temporarily interruptible, matching the reviewed plan and
the adjacent coherent-read pattern. Rejected alternatives: provider-state allowlists, board/OS/toolchain
branches, pre-halt caller requirements, and unrelated memory/read refactors. Scope excludes tests,
manifests, plan/gate policy, containment, and unrelated server behavior.

## Before doer verification â€” 2026-07-28T19:27Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) immediately
before verification. The contemplated verification feature is the completed A21 scalar-write
coherence diff: a provider-neutral private helper owns state query, conditional halt, write,
same-address/same-width exact readback, and conditional restoration; the public handler records
success only after it returns; matching tool and plan help state the lifecycle, immediate-proof
limit, honest failures, and recovery. Applied properties: correctness/no fabrication, simple
single-responsibility ownership, generic lifecycle services without environment constants,
consistent documentation, and correctness guards for a fallible agent rather than adversarial or
paternalistic controls. Assumption/tie-breaker: all non-`HALTED` provider states are handled
uniformly, preserving the established coherent-read interpretation. Rejected alternatives:
provider/board/OS/toolchain-specific states, unconditional resume, caller pre-halt requirements,
durability claims after resume, retries/resets, and test/configuration changes. Scope excludes
tests, manifests, recorded test commands, plans, permissions, containment, and unrelated code.

## Before doer final verdict â€” 2026-07-28T19:30Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
recording this verdict. The implemented A21 diff satisfies the charter's correctness requirement
by withholding success until a generic lifecycle-coherent exact scalar readback completes and by
reporting write, verification, cancellation-class, and restoration failures honestly; it restores
only server-inserted interruptions. It remains simple and neat through one private helper in the
existing memory owner, general/dynamic through existing provider-neutral services, and usable
through aligned tool/plan recovery guidance. Assumption/tie-breaker: any non-`HALTED` provider
state is temporarily interrupted; no provider, board, MCU, OS, toolchain, or address variant was
introduced. Rejected alternatives: unconditional resume, success without exact readback,
provider-specific allowlists, pre-halt requirements, retries/resets, durability claims, or edits
to tests/configuration. Scope exclusions remain tests, manifests, recorded test commands, plans,
permissions, containment, and unrelated code. Verification found Ruff and diff whitespace clean;
the existing A20 test's raw-write/no-lifecycle assertion conflicts with the approved A21 raw-write
contract and is left for its tester owner rather than being weakened or modified here.

## Before spec-tester analysis — 2026-07-28T19:38Z

Reread the complete design charter before inspecting the A21 diff and test surface. The
contemplated test feature is a deterministic, hardware-free adversarial suite for coherent scalar
writes: it will assert provider-neutral target lifecycle ordering, exact same-address/same-width
readback, honest failure and restoration handling, delayed success-event recording, and unchanged
pre-I/O refusal behavior. Correctness/no-fabrication requires tests to fail if a backend write is
treated as success without readback or if an interrupted target is not restored; cancellation and
dual failure facts must not be swallowed. Simplicity/neatness require the tests to reuse the
repository's pytest conventions and service fakes rather than add a test framework. Generalizability
and dynamism require no board, MCU, OS, toolchain, provider-specific state, or hardware process;
all non-HALTED states are tested through the generic service boundary. Assumption/tie-breaker: the
existing coherent-read helper establishes that case-insensitive HALTED is the only non-interrupted
state. Rejected alternatives: HIL/board tests, provider-state allowlists, platform-specific paths,
and modifications to production code, permissions, containment, or unrelated read behavior. Scope
excludes production source and all non-A21 test ownership.

## Before spec-test edit — 2026-07-28T19:43Z

Reread the complete design charter immediately before adding the A21 spec suite. The test diff will
exercise only `tools/memory.py` through its public handler and private lifecycle primitive using
recording service fakes. Applied properties: correctness requires exact verification before success,
conditional restoration, no swallowed primary/cancellation failure, and no fabricated event;
simplicity and neatness require one focused pytest-compatible module; generalizability and dynamism
require provider-neutral fake states and no hardware, board, OS, or toolchain assumption; usability
requires assertions against the published help and plan guidance. Assumption/tie-breaker: all
non-HALTED provider strings share the temporary-interruption contract. Rejected alternatives:
hardware/pyOCD subprocess tests, provider-specific state branches, changing existing A20 tests, or
production edits. Scope excludes server implementation, permission/gate policy, containment logic,
and unrelated suites.

## Before spec-suite verification — 2026-07-28T19:48Z

Reread the complete design charter immediately before executing the focused A21 suite. Verification
will be non-mutating and hardware-free. It checks correctness/no-fabrication through lifecycle,
readback, failure, and event assertions; simplicity/neatness through the repository's existing
test runner; and generalizability through generic service fakes with no target/provider/platform
specific dependency. The same non-HALTED interpretation is retained; no board, OS, toolchain,
permission, containment, production-source, or unrelated-test change is contemplated. Rejected
alternatives remain HIL dependence, provider-specific behavior, and weakening existing assertions.

## Before spec assertion correction — 2026-07-28T19:51Z

Reread the complete design charter before correcting assertions revealed by the first focused run.
The only contemplated correction is to preserve the established Layer-2 response wrapper while
asserting its unchanged write-success content, and to normalize a documented hyphenation variant
without relaxing the lifecycle contract. Correctness requires asserting the exact existing success
sentence is present rather than falsely requiring it to be the entire wrapped response; usability
requires the same semantic help contract. No production, hardware, provider, board, OS, toolchain,
permission, containment, or unrelated-test change is contemplated. Rejected alternatives are
changing response behavior, deleting the assertion, or accepting missing lifecycle/recovery facts.

## Before corrected spec-suite verification — 2026-07-28T19:53Z

Reread the complete design charter immediately before rerunning the corrected focused suite. The
verification remains deterministic and non-mutating: it tests correctness/no-fabrication and
truthful restoration failure, uses the existing runner for simplicity/neatness, and has no
hardware/provider/board/OS/toolchain dependence for generality. The only adjustment retains the
existing wrapped response contract while checking its success sentence. No production or policy
surface is in scope; provider-specific and HIL alternatives remain rejected.

## Before spec harness record — 2026-07-28T19:56Z

Reread the complete design charter before recording the focused test command and tester manifest.
These are isolated test-role metadata, not a server change. They preserve simplicity and neatness by
using one existing non-interactive pytest invocation and one owned test path; correctness is
verified by the same deterministic fake-based suite. No hardware, board, provider, OS, toolchain,
permission, containment, or production implementation behavior is introduced or changed. Rejected
alternatives are broad/full-suite commands as the spec gate, HIL commands, and ownership of any
existing non-A21 test.

## Before spec lint correction — 2026-07-28T20:00Z

Reread the complete design charter before making a mechanical test-only lint correction. The
contemplated diff removes an unused private-helper import and an extraneous f-string prefix; it
does not alter assertions, lifecycle coverage, behavior, or scope. This preserves neatness and
simplicity while retaining correctness/no-fabrication coverage. No hardware, provider, board, OS,
toolchain, permission, containment, production source, or unrelated test is touched. Rejected
alternatives: suppressing lint, changing production code, or weakening any assertion.

## Before final spec verification — 2026-07-28T20:02Z

Reread the complete design charter immediately before the final focused command and lint check.
The verification remains a deterministic, fake-based test of coherent write correctness and honest
failure reporting, with the existing runner and no hardware/provider/platform dependency. The
mechanical lint correction changed no contract. No production, permissions, containment, board,
OS, toolchain, or unrelated-test scope is contemplated; HIL and provider-specific alternatives
remain rejected.

## Before spec-tester final verdict — 2026-07-28T20:04Z

Reread the complete design charter immediately before this final spec-test verdict. The owned A21
suite tests the approved implementation solely through deterministic service fakes and published
surfaces. It enforces correctness/no fabrication (same-address/same-width exact readback before
success, conditional state restoration, primary/cancellation and dual-failure visibility, and no
success event on failure); simplicity/neatness (one narrowly scoped existing-framework test file);
generalizability/dynamism (generic states and services with no hardware, provider, board, OS, or
toolchain assumption); and usability (published lifecycle, failure, later-overwrite, and recovery
guidance). Assumption/tie-breaker: all states except case-insensitive HALTED are temporarily
interrupted, per the reviewed plan. Rejected alternatives: HIL, provider/board-specific branches,
new public parameters/permissions, retries/resets, production edits, and assertion weakening.
Scope excludes production source, containment/policy changes, and all non-A21 tester-owned files.

## Iteration 2 before portable spec-command correction — 2026-07-28T20:12Z

Reread the complete design charter before correcting the tester-owned command after the neutral
gate revealed that it invokes commands through Bash. The contemplated change is metadata only:
replace Windows separators in the already verified focused pytest command with a POSIX-compatible
relative executable path. Correctness requires a runnable independent gate; simplicity/neatness
favor one direct existing pytest command; generalizability rejects shell-specific separators.
Assumption/tie-breaker: the neutral harness's recorded Bash error is authoritative evidence that
the command must be portable to that shell. Rejected alternatives: a board/HIL command, a
provider/OS-specific test branch, production changes, or changes to assertions. Scope excludes
production source, test semantics, permissions, containment, and all non-owned test files.

## Iteration 2 before spec-command verification — 2026-07-28T20:14Z

Reread the complete design charter immediately before verifying the corrected recorded command
under the neutral harness's Bash execution model. This is deterministic, host-only verification of
the focused service-fake suite: correctness/no-fabrication assertions remain unchanged, simplicity
uses the repository runner, and generalizability is improved by portable relative separators. No
board, provider, OS-specific behavior in the tests, production source, permissions, containment,
or unrelated test is in scope. Rejected alternatives remain HIL, shell-specific wrapper scripts,
and assertion or implementation changes.

## Iteration 2 before spec-tester final verdict — 2026-07-28T20:16Z

Reread the complete design charter immediately before this verdict. The only iteration-2 change is
the neutral-harness-compatible, repository-relative command path; it has been executed successfully
through Bash. The A21 test feature remains deterministic, hardware-free, provider-neutral, and
scoped to correctness/no-fabrication, lifecycle restoration, compatibility, and published-help
coverage. Applied charter properties are correctness, simplicity, generalizability, neatness, and
usability; the generic non-HALTED lifecycle interpretation remains the sole tie-breaker. Rejected
alternatives are board/HIL tests, OS/provider-specific test logic, production edits, and weakened
assertions. Scope excludes source, permissions, containment, and every non-owned test file.

## Iteration 2 before FastMCP write-docstring spec expansion — 2026-07-28T20:24Z

Reread the complete design charter immediately before extending the owned A21 test. The
contemplated feature asserts the registered `server.py::write_memory` docstring—not merely the
internal handler or plan guidance—teaches symbol-first versus justified raw-RAM use, every public
parameter and width units, an example, the returned immediate-success limit, common refusal and
failure facts, recovery, and the approved lifecycle. Correctness/usability require the actual MCP
discovery surface to be self-contained; simplicity and neatness keep this as one focused test;
generalizability excludes board, provider, OS, and toolchain details. Assumption/tie-breaker: the
decorated server function's docstring is the FastMCP-public description. Rejected alternatives:
testing only private handler text, HIL/board tests, provider-specific documentation, production
edits, and weakened assertions. Scope excludes all production code, permissions, containment, and
non-owned test files.

## Iteration 2 before FastMCP docstring-spec verification — 2026-07-28T20:27Z

Reread the complete design charter immediately before running the expanded owned spec suite. The
verification is deterministic and host-only. It validates the FastMCP public-doc usability contract
alongside the prior no-fabrication lifecycle assertions, using no board, hardware, provider, OS, or
toolchain dependency. The required public text is intentionally broader than the handler text,
because MCP discovery exposes the server docstring. No source, permission, containment, or
unrelated-test change is contemplated; rejected alternatives remain HIL and assertion weakening.

## Before regression-test edit — 2026-07-28T20:12Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) immediately
before adding regression coverage. The contemplated test feature exercises the new coherent-write
lifecycle boundary and its nearby public handler: missing lifecycle configuration must fail before
any mutation, a restoration failure after a verified write must not create a success event, and
the existing raw read remains independent of write-lifecycle requirements. Correctness/no
fabrication requires that failure paths neither mutate nor report success. Simplicity and neatness
require one focused pytest module using in-process recording fakes. Generalizability and dynamism
exclude hardware, board, MCU, provider, OS, toolchain, and path assumptions. Assumption/tie-breaker:
the plan's generic lifecycle service is required for every public scalar write, while unchanged raw
read behavior remains outside that new write contract. Rejected alternatives: HIL, provider-state
allowlists, modifying production or spec-tester files, weakening existing A20 assertions, and
broad unrelated test changes. Scope excludes all production, permissions, containment, plans, and
non-A21 test ownership.

## Before regression-suite verification — 2026-07-28T20:14Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) immediately
before verification. The contemplated command runs only the tester-owned deterministic A21
pytest module and its lint check. It validates correctness/no fabrication at the lifecycle
configuration, restoration, event-recording, and adjacent raw-read edges; it relies on the
existing test framework for simplicity and neatness; and it uses generic in-process fakes with no
hardware, board, provider, OS, toolchain, or environment assumption for generality. The retained
interpretation is that new lifecycle requirements apply to scalar writes only. Rejected alternatives:
HIL, full-suite execution as the recorded regression command, provider-specific assertions, or
production/configuration changes. Scope excludes implementation, permissions, containment, and
unrelated tests.

## Before regression-tester final verdict — 2026-07-28T20:16Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
recording this verdict. The owned regression module preserves correctness/no-fabrication by
requiring lifecycle configuration before mutation, preventing a verified write followed by failed
restoration from being recorded as success, and retaining the adjacent raw-read contract. It is
simple and neat as one focused pytest module, general and dynamic through provider-neutral
in-process fakes with no hardware, board, MCU, OS, or toolchain assumptions. Assumption/tie-breaker:
the A21 plan deliberately changes raw-write behavior, while raw reads remain unchanged. The directly
adjacent A20 suite's old raw-write assertion was observed to fail because its fake readback returns
an unrelated value; that assertion is outside this tester's ownership and contradicts the explicit
A21 verification contract, so it was not weakened or edited. Rejected alternatives: production
changes, provider-specific branches, HIL, modification of another tester's file, and altering
permissions/containment. Scope remains only the owned regression test and mandated metadata.

## Iteration 2 — before regression-command correction — 2026-07-28T20:19Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before correcting
the regression harness metadata. The neutral report shows the recorded PowerShell invocation is
not executable by its Bash runner. The contemplated change is only a shell-portable, repository-relative
test command; the owned pytest assertions remain unchanged. Correctness requires an executable
non-interactive command so the neutral gate can actually evaluate the deterministic regression suite.
Simplicity/neatness favor direct invocation of the existing workspace virtual-environment interpreter.
No board, MCU, provider, OS-specific production behavior, toolchain-specific test expectation, or
hardware access is introduced. Assumption/tie-breaker: the Windows virtual environment executable
is callable from the Bash harness through a forward-slash relative path, as the neutral harness itself
uses Bash. Rejected alternatives: requesting an operator shell selection, adding a wrapper script,
altering production/spec tests, or using a global interpreter. Scope excludes all server and test
source changes, permissions, containment, and non-regression metadata.

## Iteration 2 — before regression-command verification — 2026-07-28T20:20Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before executing
the corrected command through Bash. This verification tests only harness executability and the
existing deterministic fake-based regression suite. It applies correctness/no-fabrication by
ensuring the neutral evaluator can run the assertions, simplicity/neatness through a direct
single-command invocation, and generality through no hardware, board, provider, production OS
branch, or toolchain behavior. Assumption: the harness starts at the repository root. Rejected
alternatives are HIL, a shell-specific wrapper, global Python, production edits, and modifying
spec-tester ownership. Scope excludes server behavior, test assertions, permissions, and containment.

## Iteration 2 — before regression-tester final verdict — 2026-07-28T20:21Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before this
verdict. The only iteration-two change is the recorded regression command, now portable to the
neutral Bash harness and verified to run the same three deterministic tests. Correctness is served
by making the harness evaluable; simplicity/neatness by retaining one direct command; and
generality by avoiding board, MCU, provider, production OS, or toolchain-specific assertions.
Assumption/tie-breaker: a relative virtual-environment executable is the least invasive valid
cross-shell form in this repository. Rejected alternatives: requiring user intervention, a wrapper
script, global Python, HIL, or source/test-contract changes. Scope excludes production, spec-tester
files, tester assertions, permissions, containment, and all unrelated metadata.

## Iteration 2 — before MV-001 adjacent-test correction — 2026-07-28T20:25Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) immediately
before correcting MV-001. The contemplated test-only diff removes the stale A20 assertion that a
raw public scalar write bypasses lifecycle/readback, while retaining that test's refusal and raw
read isolation proof; the A21 regression suite remains the owner of write lifecycle proof. Applied
properties: correctness/no fabrication requires the fixture not expect success from a mismatched
readback; simplicity/neatness favor deleting the superseded assertion rather than reshaping the
A20 fake; generalizability/dynamism retain generic in-process services without hardware, board,
MCU, provider, OS, or toolchain assumptions. Assumption/tie-breaker: the A21 plan explicitly
changes both public write forms, but preserves raw reads and coherent symbol reads. Rejected
alternatives: production edits, provider-state branches, HIL, global tooling, or weakening either
A20 coherent-read assertions or A21 write assertions. Scope excludes all source outside the named
adjacent test, the owned regression test metadata, permissions, containment, and spec-tester files.

## Iteration 2 — before MV-001 regression verification — 2026-07-28T20:27Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) immediately before
running the combined recorded regression command. Verification covers the unchanged A20 coherent
symbol-read lifecycle/refusal/raw-read assertions and the focused A21 write regressions. It applies
correctness/no-fabrication by checking the updated fixture no longer expects the superseded raw-write
contract; simplicity/neatness by using the existing pytest runner; and generality by using only
deterministic generic fakes, with no board, MCU, provider, OS, toolchain, or hardware dependency.
Assumption: the neutral Bash harness invokes the command from repository root. Rejected alternatives:
HIL, production edits, new test frameworks, shell wrappers, provider-specific assertions, or changes
to permission/containment policy. Scope excludes all tests except the two manifest paths and all
server source.

## Iteration 2 — before MV-001 regression-tester final verdict — 2026-07-28T20:28Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before this final
verdict. MV-001 is resolved by retaining the A20 coherent-read, refusal, and raw-read proofs while
removing its stale raw-write bypass expectation, and by recording both owned paths in one
Bash-compatible command that passed. Correctness/no fabrication is the governing property: A21
write readback is no longer contradicted by the A20 fixture. The result stays simple/neat through
the existing tests, general/dynamic through generic fakes with no board, MCU, provider, OS,
toolchain, or hardware dependence. Assumption/tie-breaker: write semantics belong to A21 and raw
read semantics remain A20's adjacent compatibility boundary. Rejected alternatives: production
changes, test weakening outside that stale assertion, provider-specific logic, HIL, and changing
permissions or containment. Scope is limited to the two declared regression test paths and mandated
test metadata.

## Iteration 2 — before FastMCP discovery regression — 2026-07-28T20:33Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) immediately
before tightening the existing regression module. The independent review's public-help risk is
real: the registered FastMCP descriptor currently receives the handler docstring rather than the
expanded server-wrapper docstring, and that descriptor does not state the Layer-2 return shape or
the relevant refusal detail. The contemplated test directly inspects the registered descriptor,
not an unavailable locked-tool listing, and requires the charter's what/when/parameters/example,
return, and failure/recovery contract. Correctness and usability require discovery help not teach
an incomplete contract; simplicity/neatness keep this in the existing A21 regression module.
Assumption/tie-breaker: the FastMCP tool manager's registered `Tool` descriptor is the effective
discovery surface while the guarded tool is hidden from an unprepared `list_tools` response.
Rejected alternatives: changing production/help text, hardware, board/provider/OS/toolchain-specific
tests, unlocking a board merely to inspect help, or adding a third regression file. Scope excludes
production, the spec-tester file, permissions, containment, and all tests except the existing
regression-owned module.

## Iteration 2 — before FastMCP discovery regression verification — 2026-07-28T20:35Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) immediately before
running the retained two-file command. Verification intentionally checks the FastMCP registered
descriptor as well as the existing lifecycle and adjacent-read assertions. It applies correctness
and usability by detecting discovery help that omits return/failure information, simplicity through
the existing pytest suite, and generality through in-process introspection with no hardware, board,
MCU, provider, OS, or toolchain dependence. Assumption: hidden guarded tools remain registered and
their descriptor is the correct discovery metadata subject. Rejected alternatives: hardware,
unlocking/configuring a board, production fixes, a new third test file, or permission/containment
changes. Scope remains the two recorded test files and metadata.

## Iteration 2 — before FastMCP discovery regression final verdict — 2026-07-28T20:36Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before this verdict.
The retained two-file regression command now exposes one genuine public-help defect: FastMCP's
registered `write_memory` descriptor uses the handler docstring and lacks the promised Layer-2
return shape (with further refusal wording also not yet covered by that descriptor), even though
the server wrapper docstring was expanded. The failure is intentional evidence for the doer: it
protects correctness/usability so discovery cannot silently omit the contract. The test remains
simple and neat in the existing module and general through generic in-process metadata with no
board, MCU, provider, OS, toolchain, or hardware dependence. Assumption/tie-breaker: registered
descriptor metadata—not an unavailable hidden listing—is the public FastMCP help surface. Rejected
alternatives: production changes by this role, weakening the assertion, HIL, unlocking a board,
or new test files. Scope remains the two manifest paths and mandatory metadata only.

## Iteration 2 — before corrected FastMCP regression verification — 2026-07-28T20:40Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) immediately before
verification. The registered FastMCP descriptor now exposes the complete public write contract,
including parameter use/example, Layer-2 return shape, lifecycle/readback limit, refusal facts,
and recovery. The retained two-file command will verify that public descriptor together with the
A21 lifecycle and A20 adjacent-read assertions. Correctness/usability require complete discoverable
help; simplicity/neatness retain the existing test/command; generality remains generic in-process
with no board, MCU, provider, OS, toolchain, or hardware dependency. Assumption: descriptor metadata
is authoritative for guarded-tool discovery. Rejected alternatives: new tests/files, HIL, production
changes by this role, board unlocking, or permission/containment edits. Scope remains the two owned
test paths and mandatory metadata.

## Iteration 2 — before corrected FastMCP regression final verdict — 2026-07-28T20:41Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before this verdict.
The production correction makes the registered FastMCP `write_memory` descriptor satisfy the
complete discoverable contract, and the retained two-file regression command passes. The suite
covers correctness/no fabrication through lifecycle/readback/restoration and delayed success,
usability through actual registered FastMCP help, and adjacent behavior through A20 coherent-read,
refusal, and raw-read isolation. It remains simple/neat as two focused test files and general/dynamic
through hardware-free provider-neutral fakes and no board, MCU, OS, or toolchain special cases.
Assumption/tie-breaker: descriptor metadata is the public help contract. Rejected alternatives:
new test files, HIL, production edits by this role, board operations, provider-specific behavior,
or permission/containment changes. Scope remains the two manifest paths and required metadata.

## Before iteration-2 neutral-report analysis â€” 2026-07-28T20:22Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
analyzing the iteration-2 neutral report. The reported failures are protected tester-command
launch failures under Bash, before any test collection or production behavior executes. The
contemplated diagnostic is host-appropriate execution of the same owned test files without
altering production code, tests, manifests, or their recorded commands. Correctness requires
separating a launcher failure from a product failure; simplicity and neatness require only the
existing venv/test runner; generalizability excludes board, provider, MCU, OS, toolchain, or
hardware assumptions in the implementation. Assumption/tie-breaker: no source change is justified
unless the actual test behavior identifies a source defect. Rejected alternatives: changing the
protected commands, weakening tests, platform-specific source behavior, HIL, retries/resets, or
permission/containment changes. Scope excludes all production source, test files, manifests, and
gate configuration.

## Before iteration-2 doer final verdict â€” 2026-07-28T20:24Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
the iteration-2 verdict. The host-appropriate equivalents of both protected A21 commands passed:
the focused spec suite and regression suite verify the implemented generic coherent scalar-write
contract. Correctness/no fabrication is maintained by exact readback before success and honest
lifecycle/restoration failure behavior; simplicity/neatness use the existing venv and focused
tests; generalizability/dynamism retain no hardware, board, provider, OS, or toolchain source
branch. Assumption/tie-breaker: the neutral failures are Bash parsing failures because its protected
commands contain Windows/PowerShell syntax, not evidence of an implementation failure. Rejected
alternatives: changing the protected command files, weakening tests, introducing platform-specific
production code, or modifying permissions, containment, plans, or unrelated source. Scope excludes
all production and tester-owned edits this iteration.

## Before CL-003 public-help follow-up â€” 2026-07-28T20:28Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
addressing the accepted independent review finding. The contemplated diff is documentation only:
expand the registered FastMCP `server.py::write_memory` docstring with symbol-versus-raw-fallback
usage, every parameter and allowed bit width, one realistic invocation, the wrapped success return,
common refusal/failure modes, recovery, and the existing lifecycle/immediate-verification limit.
Correctness and usability require public discovery help to state the actual contract; simplicity and
neatness keep that explanation at the registered public entrypoint; generalizability forbids board,
provider, OS, toolchain, or address-specific guidance. Assumption/tie-breaker: the richer handler
help is a factual source, but it does not substitute for the registered public tool docstring.
Rejected alternatives: runtime changes, signature/permission/plan-schema changes, test or gate
edits, platform-specific examples, retries/resets, and unrelated refactoring. Scope excludes all
behavior, policy, containment, and tester-owned files.

## Before CL-003 public-help verification â€” 2026-07-28T20:31Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) immediately
before verifying the public-help-only follow-up. Verification will run the existing focused A21
spec/regression suites plus lint and diff whitespace checks. Correctness/usability require that
the registered docstring accurately expose the complete symbol/raw, parameter, return, lifecycle,
failure, and recovery contract; simplicity/neatness use only the existing tests and linter;
generality retains no hardware, board, provider, OS, or toolchain dependence. Assumption/tie-breaker:
documentation does not alter runtime behavior, so existing focused coverage is the appropriate
regression check. Rejected alternatives: modifying tests or gate commands, HIL, provider-specific
logic, platform-specific source, or permission/containment changes. Scope excludes all unrelated
source and test-owned files.

## Before CL-003 help phrasing correction â€” 2026-07-28T20:34Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
the one-line public-help correction. The focused spec found that the semantically complete
`later overwrite` statement was split by a docstring line break, so its established discovery-help
assertion could not find the phrase. The contemplated diff keeps that required caveat contiguous;
it changes no runtime behavior, signature, permission, plan, or test. Correctness/usability require
the documented success limit to be discoverable; simplicity/neatness favor one wording correction;
no board, provider, OS, toolchain, or address-specific behavior is introduced. Rejected
alternatives: modifying the assertion, weakening lifecycle documentation, platform branches, or
any policy/containment/tester-owned change. Scope is this one public docstring phrase only.

## Before CL-003 corrected-help verification â€” 2026-07-28T20:35Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) immediately
before rerunning focused verification after the documentation phrasing correction. The check will
confirm the public help remains complete and the existing coherent-write behavior remains intact.
Correctness/usability require visible immediate-verification and later-overwrite limits; simplicity
and neatness use the existing focused suites/linter; no board, provider, OS, toolchain, hardware,
permission, containment, or runtime implementation change is involved. Rejected alternatives:
modifying tests/gates, HIL, platform-specific source behavior, or unrelated refactoring. Scope is
the public-docstring-only CL-003 follow-up and its verification.

## Before CL-003 honest-failure phrasing correction â€” 2026-07-28T20:37Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
the second one-line public-help correction. The established focused help assertion found that the
already documented `reported honestly` failure contract was split over two lines. The contemplated
diff makes that truthful-failure phrase contiguous without changing behavior, interface, policy,
or tests. Correctness and usability require the public help to expose failure truthfully;
simplicity/neatness require only this wording adjustment; generality excludes every board,
provider, OS, toolchain, and hardware-specific branch. Rejected alternatives: changing tests or
gates, weakening the documentation, runtime changes, or permission/containment edits. Scope is
only the registered docstring phrase.

## Before CL-003 final public-help verification â€” 2026-07-28T20:38Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
the final focused verification of the public-help follow-up. The verification checks complete,
discoverable truthful-lifecycle guidance and unchanged coherent-write behavior via the existing
focused suites and linter. Correctness/usability require exact public failure and success-limit
phrases; simplicity/neatness use existing checks; no hardware, board, provider, OS, toolchain,
runtime, permission, containment, or test/gate alteration is contemplated. Rejected alternatives
remain platform behavior, HIL, assertion changes, and unrelated refactoring. Scope is the one
CL-003 docstring and verification only.

## Before CL-003 public-help final verdict â€” 2026-07-28T20:40Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
the final verdict on the accepted review finding. The registered FastMCP `write_memory` docstring
now documents when to use a symbol versus a justified raw mapped-RAM fallback; all parameters and
the 8/16/32-bit width; a realistic invocation; the existing Layer-2 success response and immediate
verification limit; non-success refusal/failure cases; and recovery, while preserving the existing
lifecycle contract. Correctness and usability are improved without runtime changes; simplicity and
neatness keep the contract at the public entrypoint; generality has no board/provider/OS/toolchain
specialization. Assumption/tie-breaker: the public docstring must be self-sufficient even though
the handler has richer help. Rejected alternatives: signature/behavior/policy changes, tests or
gate edits, HIL, platform-specific branches, retries/resets, and unrelated refactoring. Scope was
only `server.py::write_memory` documentation. Focused A21, regression, and A20 tests passed, along
with Ruff and diff whitespace checks.

## Before CL-003 registered-descriptor correction â€” 2026-07-28T20:44Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
addressing the neutral regression. The actual FastMCP descriptor is sourced from the nested
`memory_tool_handlers["write_memory"]` docstring, not the wrapper docstring. The contemplated
CL-003-only diff expands that handler description with the same public symbol/raw usage,
parameters/allowed width, invocation, Layer-2 return/success limit, refusal/failure details,
lifecycle caveat, and recovery. Correctness/usability require registered discovery to be
self-sufficient; simplicity/neatness keep documentation local to the registered handler;
generality forbids hardware, board, provider, OS, toolchain, and address-specific behavior.
Assumption/tie-breaker: duplicating the public contract is necessary because FastMCP registration
uses this implementation function. Rejected alternatives: runtime/signature/permission/plan/schema
changes, test/gate edits, platform branches, retries/resets, and unrelated refactors. Scope is the
handler docstring only.

## Before registered-descriptor verification â€” 2026-07-28T20:47Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) immediately
before verifying the registered-handler docstring correction. The focused tests will inspect the
actual FastMCP description and existing coherent-write behavior; lint and whitespace checks will
confirm the documentation-only diff is clean. Correctness/usability require the real discovery
descriptor to state return/failure/recovery facts; simplicity/neatness use existing checks;
generality retains no board/provider/OS/toolchain/hardware behavior. Rejected alternatives are
test/gate edits, runtime or interface changes, HIL, platform branches, and unrelated work. Scope
is only CL-003 handler documentation and verification.

## Before registered-descriptor phrase correction â€” 2026-07-28T20:50Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
the narrowly scoped registered-docstring phrase correction. Focused tests found that `reported
honestly` and `unmapped or prohibited memory` are semantically present but split across line
breaks in FastMCP's descriptor text. The contemplated documentation-only diff keeps both factual
failure phrases contiguous so discovery and established assertions can find them. Correctness and
usability require truthful failure/recovery facts to be discoverable; simplicity/neatness require
only wording layout; generality adds no board/provider/OS/toolchain/hardware behavior.
Rejected alternatives: test/gate changes, behavior/interface/policy modifications, platform
branches, retries/resets, or unrelated work. Scope is two phrases in the registered handler
docstring only.

## Before registered-descriptor corrected verification â€” 2026-07-28T20:52Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
rerunning focused verification after making required failure phrases contiguous in the registered
descriptor. The tests will inspect FastMCP discovery and existing coherent-write behavior; lint and
diff checks guard neatness. Correctness/usability require the complete truthful public contract;
simplicity uses existing suites; generality retains no hardware, board, provider, OS, or toolchain
behavior. Rejected alternatives remain test/gate edits, runtime/interface/policy changes, HIL,
platform branches, and unrelated refactoring. Scope is handler-docstring-only CL-003 verification.

## Before registered-descriptor final verdict â€” 2026-07-28T20:54Z

Reread the complete design charter (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`) before
the final verdict. The registered handler docstring, which FastMCP actually publishes, now contains
the complete CL-003 contract: symbol versus justified raw mapped-RAM use, all parameters and
8/16/32-bit width, invocation example, normal Layer-2 success shape and immediate verification
limit, lifecycle/later-overwrite caveat, exact common refusal/failure facts, and concrete recovery.
Correctness/usability require public discovery to be truthful and self-sufficient; simplicity and
neatness retain the documentation at the registered handler; generality introduced no board,
provider, OS, toolchain, or hardware behavior. Assumption/tie-breaker: this public descriptor,
rather than the wrapper docstring, is authoritative for discovery. Rejected alternatives: tests or
gate edits, any runtime/signature/permission/plan/policy/containment change, HIL, platform
branches, retries/resets, and unrelated refactoring. Scope was `tools/memory.py` handler
documentation only. The focused A21/regression/A20 tests, Ruff, and diff whitespace checks passed.

## Iteration 2 before FastMCP docstring semantic-assertion correction — 2026-07-28T20:31Z

Reread the complete design charter before correcting wording-only test assertions after the newly
expanded public docstring demonstrated the required semantics using `use` rather than `prefer` and
an explicit list of refusal/failure categories rather than the word `refusal`. The test remains
strict: it will require symbol guidance, raw opt-in and reason, and concrete invalid/containment/
lifecycle failure classes. Correctness and usability require semantic coverage, not an arbitrary
synonym; simplicity retains one focused test. Assumption/tie-breaker: an explicit documented list
of rejected conditions proves common-refusal coverage more strongly than the label alone. Rejected
alternatives are deleting the checks, accepting lifecycle-only help, HIL/provider/platform-specific
testing, or production edits. Scope excludes source, permissions, containment policy, and
non-owned tests.

## Iteration 2 before corrected FastMCP docstring-spec verification — 2026-07-28T20:34Z

Reread the complete design charter immediately before rerunning the owned A21 suite. Verification
remains host-only and deterministic; it checks the public FastMCP documentation semantic contract
plus lifecycle correctness with no board, provider, OS, or toolchain dependency. No production,
permission, containment, or unrelated-test edit is contemplated. Rejected alternatives remain HIL,
provider/platform-specific documentation, and weakening the newly added public-help proof.

## Iteration 2 before FastMCP docstring whitespace normalization — 2026-07-28T20:37Z

Reread the complete design charter before correcting a test-only whitespace normalization issue:
the public docstring contains the required containment-failure phrase across a source line wrap.
The contemplated change collapses whitespace before semantic phrase checks and retains every
requirement. Correctness/usability require the test to distinguish missing content from formatting;
simplicity keeps normalization local. Assumption/tie-breaker: FastMCP help preserves text semantics
across ordinary source wrapping. Rejected alternatives: deleting the containment assertion, changing
production documentation, HIL/provider/platform-specific tests, or relaxing any required contract
fact. Scope excludes source, permissions, containment policy, and non-owned test files.

## Iteration 2 before whitespace-normalized FastMCP docstring-spec verification — 2026-07-28T20:39Z

Reread the complete design charter immediately before final verification of the expanded public
help test. It remains deterministic and host-only, enforces semantic self-contained FastMCP
documentation plus the earlier lifecycle/no-fabrication proof, and adds no board/provider/OS/
toolchain dependency. No production, permission, containment, or unrelated-test edit is in scope;
HIL and assertion weakening remain rejected.

## Iteration 2 before FastMCP docstring-spec final verdict — 2026-07-28T20:41Z

Reread the complete design charter immediately before this final verdict. The owned A21 spec now
requires the registered FastMCP `server.write_memory` docstring to explain all public parameters,
8/16/32-bit width, symbol-first and justified raw-RAM usage, an invocation example, Layer-2 return
and immediate verification limit, concrete refusal/failure classes, recovery, and lifecycle state
restoration. This applies correctness/no-fabrication and usability to the actual discovery surface,
while preserving simple, deterministic, provider/board/OS/toolchain-neutral tests. Assumption:
whitespace wrapping does not alter documentation semantics. Rejected alternatives: testing only the
private handler, HIL, production edits, provider-specific wording, and weaker phrase coverage.
Scope excludes production source, permission/containment policy, and non-owned tests.

## Iteration 2 follow-up before no-change owned-spec verification — 2026-07-28T20:45Z

Reread the complete design charter before verifying the accepted FastMCP-docstring follow-up.
Inspection confirms the owned A21 test already requires the registered public `server.write_memory`
docstring to cover symbol/raw use, each parameter and width units, example, return/immediate-success
limit, concrete refusal/failure categories, recovery, and lifecycle. No new test diff is needed.
Correctness/usability remain applied at the public discovery surface; deterministic host-only tests
preserve simplicity and generality without board, provider, OS, or toolchain assumptions. The latest
neutral regression failure is in a separately owned regression test and is excluded by role scope.
Rejected alternatives: editing that file, production edits, HIL, or weakening the owned assertion.
Scope excludes all non-owned tests, source, permissions, and containment policy.

## Iteration 2 follow-up before no-change owned-spec final verdict — 2026-07-28T20:47Z

Reread the complete design charter immediately before this verdict. The accepted follow-up was
already satisfied by the owned A21 FastMCP-docstring test; no source or test amendment was needed
this turn. The recorded command passed with all nine tests and retains the public-help proof for
CL-003 plus the prior lifecycle/no-fabrication coverage. Applied properties are correctness,
usability, simplicity, and provider/board/OS/toolchain-neutral generality. The only external gate
failure observed is confined to a non-owned regression test, which this role did not edit. Rejected
alternatives: modifying another tester's file, production changes, HIL, or weakening assertions.
Scope excludes non-owned tests, all production source, permissions, and containment policy.
## 2026-07-28T19:34Z — main-model post-loop verification boundary

- Reread the complete `.codex/design_charter.md`; SHA-256
  `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.
- Contemplated diff: generic lifecycle-coherent public scalar memory writes plus public help and
  tester-owned focused proof.
- Applied properties: correctness/no fabricated success, minimal lifecycle-preserving control
  flow, provider/board/OS generality, one responsibility in the memory tool, and complete
  operator-facing lifecycle/recovery documentation.
- Assumption/tie-breaker: success means exact immediate same-address/same-width readback while the
  target is coherent; it does not promise persistence after resumed firmware runs. Correctness
  wins over the prior simpler but unverified write.
- Rejected alternatives: board/MCU/firmware-specific delays, retries, resets, caller-required
  manual halts, durable-write promises, permission changes, and unrelated memory refactors.
- Scope exclusions: firmware, fixtures, provider protocols, memory-map containment, plan
  permissions/budgets, and all unrelated server behavior.

## Main adjacent-suite finding — 2026-07-28T19:36Z

The required adjacent A20 suite exposed one stale test expectation that public raw writes bypass
lifecycle/readback. The reviewed A21 contract intentionally changes that behavior for both public
scalar-write forms. Correctness/no fabrication forbids relaxing production to satisfy a fake that
returns a value different from the requested write; simplicity and scope require only an
ownership-correct adjacent test update that preserves raw-read independence and asserts the new
coherent raw-write order. No board/provider/OS special case, new permission, or unrelated source
change is authorized. Neutral acceptance remains pending until the persistent regression tester
incorporates that adjacent proof and the gate plus main verification are green.

## 2026-07-28T20:22Z — main-model final repair-review boundary

- Reread the complete `.codex/design_charter.md`; SHA-256
  `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.
- The neutral gate is green: 9 A21 spec tests plus 12 subtests, and 14
  regression/adjacent tests plus 6 subtests.
- Independent focused verification is green: 83 tests passed, 1 skipped, 45 subtests; Ruff passed;
  Pyright passed with the repository virtual environment active; and `git diff --check` passed.
- Applied charter properties remain correctness/no fabricated success, immediate exact readback,
  lifecycle restoration, minimal provider-neutral control flow, and complete public discovery
  guidance. The public wrapper and the actual registered FastMCP descriptor now each teach symbol
  versus justified raw use, all parameters and supported widths, an example, return semantics,
  lifecycle limits, concrete failure classes, and recovery.
- Assumption/tie-breaker: a verified immediate coherent mutation is the truthful success contract;
  persistence after resumed firmware executes is explicitly not promised.
- Rejected alternatives remain retries, reset-based masking, board/MCU/firmware special cases,
  caller-required manual halts, broader permission changes, and unrelated memory refactors.
- No new production edit is authorized at this boundary. The next action is a read-only resumption
  of the exact independent reviewer, followed by a targeted live A21 reproducer if accepted.
