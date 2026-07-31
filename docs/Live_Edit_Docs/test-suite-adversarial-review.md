# Adversarial review of the monitor test suite

**Role:** reviewer trying to show the suite does *not* prove what it claims.
**Subject:** `tests/test_monitor_*.py`, `tests/monitor_support.py`, `tests/codex_harness.py`.
**Standard:** does the suite actually test the spec's required behaviour, or only
the code's current behaviour?

Each finding carries the main agent's audit verdict.

---

## Round 1

### C-1. The suite never proves a *report actually reaches Sentry* — VALID

`test_monitor_delivery.py` asserts the report→envelope *mapping* and that the
filler writes a JSON file, but nothing asserts a Sentry envelope was really
produced by the SDK and landed in `simulated_remote/`. The spec requires every
triggered report to appear both in the sink and as a local file. A regression that
broke the SDK client entirely would leave every test green.

**Audit: valid.** The JSON copy is written unconditionally, so it masks a dead SDK
path. Added `test_report_produces_a_sentry_envelope` asserting an envelope file is
written, plus a test that a broken SDK still yields the local JSON copy.

### C-2. No test proves recording stays outside the board lock — VALID

The plan's central latency claim is that recording is *structurally* incapable of
running inside a board lock, because it happens after dispatch returns. Nothing
tests it. A future refactor moving the hook into `dispatch` would satisfy every
existing test while violating the requirement.

**Audit: valid and important.** Added `test_monitor_passivity.py` asserting the
hook is invoked after `dispatch` has returned and that no monitor call occurs
while an execution lock is held.

### C-3. The 500-call periodic tick is never exercised — VALID

`SUMMARY_EVERY_CALLS` is asserted to equal 500, which tests a constant, not
behaviour. Nothing proves a summary is produced at the boundary, that the ledger
rolls a segment, or that the check-in prompt is raised. The entire periodic
occasion is untested.

**Audit: valid.** Added `PeriodicTick` covering summary emission, segment roll,
prompt appearing once and then clearing, and the prompt's absence in a
professional build. Uses an injected small cadence so it does not need 500 real
calls.

> **Superseded by the snapshot-cadence spec change (F-129, F-165, N-11).** There
> is no longer one 500-call tick: the usage snapshot fires every
> `USAGE_SNAPSHOT_CADENCE` (100) calls and the check-in prompt every
> `CHECKIN_CADENCE` (500). `PeriodicTick` was split into `UsageSnapshotTick` and
> `CheckInTick` so each cadence is exercised on its own, plus
> `OneCadenceMovesEverythingItGoverns` for AC-106. The constant-equality
> assertion this finding criticised is gone; what remains asserts that the two
> cadences are *separate* constants from each other and from the trail buffer.

### C-4. "No plaintext path in the store" is asserted too weakly — VALID

`test_no_plaintext_project_path_is_stored` only checks the temp directory's
basename. A path leak of the *parent* directory, the user name, or a forward-slash
variant would pass.

**Audit: valid.** Strengthened to check every path component of the project
directory, both separator forms, and the user name, across file contents *and*
file/directory names.

### C-5. The block is never tested through real dispatch — VALID

`test_monitor_thrash_block.py` tests `evaluate()` in isolation. Nothing proves a
tripped block actually refuses guarded dispatch, that the refusal names a remedy,
or that the three monitoring tools stay callable while blocked. The spec's one
authority-bearing behaviour is untested end to end.

**Audit: valid.** Added `StalenessBlockThroughDispatch` driving a tripped block
through `_enforce_guarded_invocation`, asserting the refusal, its remedy, that it
is classified as a refusal rather than a defect, and that the monitor tools remain
callable.

### C-6. Thrash detection is never exercised through the monitor — PARTIALLY VALID

The detector is well covered in isolation, but nothing proves the monitor wires it
up: that a real repeated call produces exactly one S-2 report with the right
triage class and origin.

**Audit: valid.** Added a monitor-level test driving repeated observations and
asserting exactly one grouped thrash report with origin
`server-thrash-detector`.

### C-7. Rate limiting is asserted only at the `Deduper` — VALID

Nothing proves a storm of identical failures produces one report *on disk*.

**Audit: valid.** Added `test_report_storm_collapses_to_one_file`.

### C-8. `test_recovery_does_not_resend_acked_content` proves less than it claims — VALID

It asserts `transport.sent_files == []` after `enqueue_segments`, but the delivery
thread was never started, so the queue was never drained. The assertion would hold
even if the acked-set logic were deleted.

**Audit: valid — a genuinely vacuous test.** Rewritten to start the service, wait
for the drain, and assert the acked file was skipped while an unacked one was sent.

### C-9. Codex tests assert the agent's prose, not the recorded truth — PARTIALLY VALID

Several e2e tests assert on `result.stdout` (what the model said). A model that
hallucinated the right answer without calling the tool would pass.

**Audit: partially valid.** `test_agent_can_read_live_counts_back` is the real
offender: it reads only the model's reply. Tightened to additionally require the
recorded ledger to contain the health-check calls. The others already assert on
recorded state; left as they are, since asserting the reply *in addition* is
useful evidence the tool output was usable.

### C-10. No test covers a professional build end to end — VALID

`ProfessionalBuild` patches the module constant *after* import, which does not
prove the build flag actually removes the tool at registration time in a real
server process.

**Audit: valid.** Added a subprocess test that flips the constant, launches the
real server, and asserts over the protocol that `submit_routine_checkin` is absent
from `tools/list` while `report_agent_issue` is present and refuses.

---

## Round 2

### C-11. `test_a_session_of_correct_refusals_files_no_server_defect` can pass vacuously — VALID

If the agent fails to call any tool at all (a refusal to comply, a timeout), there
are no reports, so the assertion "no defects" holds trivially. The primary gate
can pass while testing nothing.

**Audit: valid and serious — this is the gate that matters most.** Added a
precondition asserting the session actually produced refused calls in the ledger
before concluding no defects were filed.

### C-12. Nothing asserts the trail is bounded in a *report* — VALID

`TRAIL_MAX_EVENTS` is tested on the buffer, but not that an emitted report carries
at most that many entries.

**Audit: valid.** Added an assertion in the monitor-level report test.

### C-13. No test proves the monitor survives an unwritable store — VALID

The spec requires the server to run normally when logging cannot write. Only the
`BUFFERING` ledger path is tested, never the whole monitor.

**Audit: valid.** Added `test_monitor_degrades_when_the_store_is_unavailable`
asserting health still answers, counts still advance, and no exception escapes.

### C-14. The `NullMonitor` fallback is never tested — VALID

The import-time guard that prevents monitoring from bricking server startup has no
test.

**Audit: valid.** Added tests that `NullMonitor` satisfies the whole surface and
that its health output declares itself unavailable rather than healthy.

### C-15. Duplicate-tolerance is claimed but untested — PARTIALLY VALID

At-least-once delivery with stable identity is a spec requirement; no test resends
the same file twice.

**Audit: valid.** Added `test_resend_is_tolerated_and_identity_is_stable`.

---

## Round 3

### C-16. Coverage of "which advertised tools were never exercised" is shallow — NOT VALID

Reviewer claim: the coverage assertion only checks the list is non-empty.

**Audit: not valid.** `test_coverage_lists_unexercised_tools` plus the counters
unit test assert both directions (exercised and never-exercised) with exact sets.
The e2e test additionally proves it reflects a real session. No change.

### C-17. The salt should be tested for persistence across restarts — VALID

Cross-process stability is tested implicitly by the codex harness but never
directly, and this exact bug (a process-local salt when the store had not been
resolved) shipped and was caught only by accident.

**Audit: valid, and it is the bug that actually occurred.** Added
`test_salt_is_stable_across_processes` as an explicit regression test.

### C-18. Tests do not assert the ordering of closeout steps — VALID

The spec is explicit that hardware release precedes the close record, which
precedes the send. Nothing verifies the order.

**Audit: valid.** Added an ordering test using a recording transport to assert the
close record exists on disk before any delivery attempt is observed.

### C-19. `assertRaises(Exception)` in the wiring tests is too loose — VALID

Several integration tests accept any exception, so a wrong-but-still-raising
behaviour passes.

**Audit: valid.** Tightened to assert the exception type and that the message
carries the expected refusal text.

---

## Round 4

No further valid findings. Remaining reviewer comments were stylistic (test
ordering, naming) or restated earlier items.

**Residual limitations, accepted and recorded rather than fixed:**

1. **No hardware.** Every board-touching path is exercised through refusals and
   fakes. Real probe faults (S-3 with genuine USB/J-Link failures) cannot be
   covered here and are left to the existing HIL plan.
2. **The offline-rewrite limit is asserted as unfixable.** `test_offline_rewrite_
   with_recomputed_chain_is_not_detected` deliberately asserts a *negative*
   guarantee. If an external witness is ever added, that test must be inverted.
3. **Codex tests depend on a live login and a real model.** They skip -- loudly --
   when the CLI is absent or logged out, and they fail rather than downgrade if
   the pinned model is not honoured.
4. **The 14-day threshold is tested via injected anchors**, never by waiting.
