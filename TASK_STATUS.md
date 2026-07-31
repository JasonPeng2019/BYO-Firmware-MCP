# Task status - debugger UART discovery hook implementation

Branch `Proto-1-WIP-working`. Working tree clean.
Suite: **687 ran / 680 passed / 7 skipped**. `ruff check src/ tests/` clean. `pyright src/` clean.
All three verified by running them at HEAD, not taken from an agent report -- a
distinction that mattered repeatedly on this task (see *Operating record*).

**STATUS: GREEN, with one condition recorded as stopped rather than met.** Read
"Phase 1" before treating that as a formality; it is stated plainly rather than
papered over.

## Checklist

| # | Condition | State |
| --- | --- | --- |
| 1 | Phase 0 - guide implemented as written | GREEN |
| 2 | Phase 0 - every test the guide specifies passes | GREEN |
| 3 | Phase 1 - adversarial review loop run | 9 iterations |
| 4 | **Phase 1 - a full iteration marked zero new findings VALID** | **STOPPED BY DECISION, not cleared** - see below |
| 5 | Phase 1 - every VALID, in-scope finding fixed | GREEN - all fixed; D30 ruled out of scope with reasoning |
| 6 | Phase 1 - every INVALID finding recorded and left untouched | GREEN - D3, D5 |
| 7 | Phase 1 - no regressions | GREEN |
| 8 | Phase 2 - straggler tests written and logged | GREEN - `reviews/new-tests.md` |
| 9 | Phase 2 - full suite passes | GREEN |
| 10 | Phase 3 - status recorded honestly | GREEN - this file |
| 11 | Close-out - carried triage items resolved | GREEN - M9 and the `continue_setup` budget gap both fixed |

## Phase 1: stopped at iteration 9, by decision

Condition 4 was never met, and it is **not** being declared met. It was also not a
cap being hit -- the cap was 10 and iteration 10 was never run. The loop was stopped
because it had begun measuring itself instead of the product.

| Rounds | What they found |
| --- | --- |
| 1-5 | Real feature defects. C12 - a token misparse resolving to a *different real probe*. D15 - a feature built, unit-tested, green, and never wired to production. M6 - unbudgeted vendor subprocesses. |
| 6-9 | Defects in the loop's own prior fixes, and typed-error-message plumbing. |

Iteration 8 produced D25-D28, all of the form "a typed failure code exists but is
never called" -- message quality, not correctness. Iteration 9's only finding, D30,
is a defect in D25, a guard added one round earlier. `discovery_failures.py` is 318
lines of remedy text that generated two full rounds of churn by itself.

`.codex/design_charter.md` names this shape directly: *"Over-defensive guarding is a
defect: it burns the complexity budget and, worse, it blocks real work"*, and *"Every
limit must trace to a real constraint... never from a hypothetical adversary or 'just
in case.'"*

The decisive evidence is the fix-introduces-defect chain, which reached five links:

| Fix | Defect it introduced | Caught by |
| --- | --- | --- |
| FIX 8 (C7) | **C12** - token misparse resolving to a *different real probe* | iteration 3 |
| C15 fix | **D16** - test passed without exercising its invariant | iteration 5 |
| D15 fix | **M6** - vendor CLI subprocesses unbudgeted | coordinator, by hand |
| M6 fix | **D17** - sixth budget site missed | scoped review |
| D25 fix | **D30** - two bypasses of its own check | iteration 9 |

The last two links are fixes of fixes. A sixth link was the predictable outcome of a
tenth iteration, not an unlikely one.

**D30 is recorded VALID and ruled out of scope -- deliberately not INVALID.** A true
finding that is not worth fixing is a different verdict from a wrong one. Full
reasoning and two warnings for anyone revisiting it are in `reviews/ledger.md`.

## What the feature actually does

Verified by tracing the code, not by reading review artifacts.

**Precursor P1 - the originally reported bug, fixed.** `setup_overview` compared the
requested-name count against the connection count *before* testing for zero
connections, so one board name with no attached probe returned
`setup_assignment_clarification_required` -- a missing debugger reported as a naming
ambiguity. `server.py:5170` now tests zero-connections first and returns a typed
no-probe status carrying the hook contract call. The branch this made unreachable was
deleted. Covered by `tests/test_setup_overview_no_probe.py`.

**Precursor P2 - one `connection_id` mint**, with all four former construction sites
routed through it.

**The second pipe - the agent supplying a device to pyOCD - works end to end.** Hook
returns `(provider, unique_id)` -> merged into the unified snapshot with `unique_id`
preserved verbatim (`hardware_inventory.py:539`) -> opaque run-scoped selection record
-> `_assigned_probe_uid_for_connect` -> `target_control.open_session(unique_id=...)`
-> `ConnectHelper`. Every hop traced.
`test_the_full_contract_write_refresh_rerun_select_loop` drives
contract -> write hook -> refresh -> rerun -> select -> resolve.

**Provider-qualified selectors are now documented** (`unique_id_guidance` on the probe
contract). `DebugProbeAggregator.get_all_connected_probes` tries `get_probe_with_id`
*first* and returns on a hit, so a `provider:` prefix takes pyOCD's explicit path and
skips enumeration; `remote:<host>:<port>` is the only route that survives when the
host's USB stack cannot show the probe to pyOCD at all, and it *requires* the prefix
because `TCPClientProbe.get_probe_with_id` returns `None` unless `is_explicit`. This is
documentation only -- no composer, no validator, no parser. The agent writes the
selector; the server passes it through.

**Per-kind gating**: a kind's hooks run only when that kind's native discovery returned
zero rows, evaluated fresh per snapshot. This is what keeps hook processes out of the
UART hot path, where `_resolve_serial_port_for_session` runs before *every* serial
action.

**What the server cannot do:** if the host's USB stack will not show the probe to pyOCD
at all, no server code fixes that. That limit is real, and routing around it is exactly
what the fallback pipe and the `remote:` selector exist for.

## Carried triage items - both closed at close-out

- **M9 - cancelled UART operations recorded FAILED instead of CANCELLED. Fixed.**
  `services/uart_capture.py`'s three `except Exception ... raise RuntimeError(...) from
  exc` wraps caught `OperationCancelledError` (a `RuntimeError` subclass) and destroyed
  its type identity, so `except OperationCancelledError` at `kernel/operations.py:835`
  could never match for a UART operation. A real user-visible misreport, against the
  charter's *"no silent failure and no fabrication."* Each of the three re-raises was
  proven load-bearing by deleting it and watching the test fail with the exact defect
  shape.
- **`continue_setup` had no inventory budget. Fixed**, and the categorization question
  is answered rather than deferred. The open question was "should it always pay the
  larger budget, even on branches that never snapshot?", which mis-frames the cost: the
  block resolves with `max(...)`, not `+=`, so membership raises a *ceiling*, not a
  duration. A branch that never snapshots finishes exactly as fast; it just stops being
  cancelled mid-discovery on the branch that does. Measured 0.0s reserved against 123.0s
  required with one hook of each kind.

## Known and deliberately unfixed

- **D30** - see above and `reviews/ledger.md`.
- **D3 and D5** remain ruled INVALID and untouched in the code, as directed.
- **D29** is recorded in the ledger specifically as a trap: the obvious "cleanup"
  (routing `_require_unchanged_hook_source` through `hook_failure()`) would replace
  situation-appropriate guidance with mismatched text and drop the "rerun setup routing"
  instruction the user actually needs. Do not tidy it naively.
- **19 pre-existing `pyright` errors in `tests/`** (trust-model rounds 1/3/4,
  change-loop). They predate this task; touching them is churn against the charter's
  *"You should not edit parts that are not broken."* Verified still exactly 19 at
  close-out -- the new tests added none.

## Operating record

Kept because it changed what was verified, not merely how the work felt.

- **Agents reliably report what they *did* and unreliably report what *is*.** Six
  self-reports this task did not match the tree: "only tests/ modified" from an agent
  that had destroyed another's work; "ruff clean" with two `F401`s; "pyright 0 errors"
  scoped to `src/` while 18 sat in a new test file; a stale suite count; an agent
  claiming credit for files it never touched; and "verified against source code" on
  documentation that contradicted the source. None dishonest -- each described the
  command the agent chose to run. Every number in this file was re-measured directly.
- **A cross-agent blanket revert destroyed in-flight work.** An agent told to break a
  file temporarily and revert it reverted all of `src/`, wiping a concurrent agent's
  implementation. Root cause was coordinator scheduling (two agents mutating `src/` at
  once) plus a file-ownership rule that was advisory rather than enforced.
- **Prove a test can fail.** This task produced three tests that passed for the wrong
  reason (C15's, D16's, D18's). Every test added at close-out was proven by breaking the
  behavior it guards and watching it fail. One needed care: `capture_uart_output`'s first
  cancellation checkpoint sits *outside* its try block, so a naive stub would have passed
  with the bug still present.
- **A diff review cannot see false premises in the spec.** The guide asserted as
  established fact that `configured_probe_cli_commands` already routed pyOCD through
  `sys.executable`. It did not. That survived four passes invisibly because it lived in
  code no diff touched. The same shape produced D15 -- a feature fully scaffolded,
  unit-tested, green, and never wired to production. Iteration 5 swept the remaining 14
  "already/currently/today" claims and found no second instance, but the class is only
  detectable by auditing the specification against the code.

## Key documents

| Path | What it is |
| --- | --- |
| `.codex/design_charter.md` | The standard every change is measured against |
| `reviews/ledger.md` | Every finding ever raised - C1-C20, D1-D30, M1-M9 - with verdict, reason, resolution, and the close-out note |
| `reviews/REVIEW_POLICY.md` | Materiality rubric (MUST-FIX vs EXTRANEOUS) |
| `reviews/new-tests.md` | Phase 2 test rationale, per test |
| `HANDOFF.md` | Orientation for a fresh agent |

Run the suite with `python -m unittest discover -s tests`. **stdlib `unittest` only --
never pytest.** ruff line-length 100, target py310.
