# Handoff — debugger UART discovery hook feature

**Your job is to close this out, not to review it further.** The adversarial loop
was stopped deliberately at iteration 9. Read "Why the loop stopped" before you
consider running another pass — the reasoning matters more than the result.

Read this, then `reviews/REVIEW_POLICY.md`, then `.codex/design_charter.md`.

## Verified state

| | |
| --- | --- |
| Repo | `BYO-Firmware-MCP` — **its own git repo**, nested inside `FirmCLI-WIP`, not a submodule |
| Remote | `github.com/JasonPeng2019/BYO-Firmware-MCP` |
| Branch | **`Proto-1-WIP-working`** — not `Proto-1-WIP`, which is stranded at `8a51138` and is 10 commits behind. Everything from iteration 6 onward is on `-working`. Check you are on it before anything else. |
| HEAD | `e7f4409` (this handoff), working tree clean |
| Base | `6f3da0a` (feature diff is `git diff 6f3da0a..HEAD`) |
| Suite | **677 passed / 7 skipped** |
| Lint | `ruff check src/ tests/` clean |
| Types | `pyright src/` clean |

Measured at HEAD by running them, not taken from an agent report. That
distinction is load-bearing — see "Operating lessons."

Run the suite with `python -m unittest discover -s tests`. **stdlib `unittest`
only — never pytest.** ruff line-length 100, target py310.

`TASK_STATUS.md` is **stale** (cites HEAD `8ec6b02`, 657 tests, cap 5). Rewriting
it is close-out step 3.

## Why the loop stopped at 9

The user called it: the loop had started measuring itself instead of the product.

| Rounds | What they found |
| --- | --- |
| 1–5 | Real feature defects — C12 (token misparse resolving to a *different real probe*), D15 (vendor UART built, tested, never wired to production), M6 (unbudgeted subprocesses) |
| 6–9 | Defects in the loop's own prior fixes, and typed-error-message plumbing |

Iteration 8 produced D25–D28: "typed failure codes exist but are never called."
That is message quality, not correctness. Iteration 9's only finding, **D30, is a
defect in D25** — a guard added one round earlier. `discovery_failures.py` is 318
lines of remedy text that generated two full rounds of churn on its own.

`.codex/design_charter.md` names this directly: *"Over-defensive guarding is a
defect: it burns the complexity budget"*, and *"Every limit must trace to a real
constraint... never from a hypothetical adversary or 'just in case.'"*

**Phase 1 is stopped, not cleared.** It never met its termination condition. That
is recorded honestly rather than papered over, and it is a deliberate call, not a
cap being hit.

### D30 is cancelled — do not fix it

D25's provider-registration check lives in `ProbeSelectionStore.resolve()`. There
are five `open_session` call sites (server.py `1274`, `2879`, `4136`, `5547`,
`5601`) and two `resolve()` gates (`1020`, `1086`), so `board_validate` and
`board_setup` reach pyOCD without it. That finding is **factually correct** — I
verified the site counts myself.

It is cancelled anyway. An unregistered provider fails at `open_session` regardless,
and the open-failure path already returns an honest, actionable error. D25 only ever
bought a *better message*, and chasing it across five call sites buys a better
message in two more places at the cost of another guard layer. Leave it.

Two notes for whoever revisits this: the D25 commit justified `resolve()` as "the
one choke point both real production call sites already share," which is **false** —
do not inherit that claim. And I approved it without enumerating the sites, which is
how D30 came to exist at all.

## Close-out steps

1. **Record D30 in `reviews/ledger.md`** as raised, verified correct, and ruled
   out of scope with the reasoning above. Do not mark it INVALID — it is a true
   finding that is not worth fixing, which is a different verdict. Ledger rows are
   5 columns / 6 pipes; check table well-formedness before committing (line 34 is
   a pre-existing 7-pipe row — leave it).
2. **Add an iteration-9 close-out note** to the ledger stating the loop was stopped
   by decision at 9, with the rounds-1–5 vs 6–9 contrast as the justification.
3. **Rewrite `TASK_STATUS.md`** against the verified numbers above. Phase 1's
   checklist row becomes "stopped by decision, not cleared," not ✅ and not a bare
   ❌ — state the reasoning. Phases 0, 2, 3 are genuinely complete.
4. Commit. Stage explicitly by path.

## What is actually built (verified by tracing the code, not the artifacts)

**Precursor P1 — the original reported bug, fixed.** `setup_overview` used to
compare requested-name count against connection count *before* testing for zero
connections, so one board name with no attached probe returned
`setup_assignment_clarification_required` — a missing debugger reported as a
naming ambiguity. [`server.py:5170`](src/pyocd_debug_mcp/server.py#L5170) now
tests zero-connections first and returns a typed no-probe status carrying the hook
contract call; the branch this made unreachable was deleted. Covered by
`tests/test_setup_overview_no_probe.py`.

**Precursor P2 — one `connection_id` mint**, all four former construction sites
routed through it.

**The second pipe — the agent supplying a device to pyOCD — works end to end.**
Hook returns `(provider, unique_id)` → merged into the unified snapshot with
`unique_id` preserved verbatim
([`hardware_inventory.py:539`](src/pyocd_debug_mcp/hardware_inventory.py#L539)) →
opaque run-scoped selection record → `_assigned_probe_uid_for_connect` →
`target_control.open_session(unique_id=...)` → `ConnectHelper`. Every hop traced.
`test_the_full_contract_write_refresh_rerun_select_loop` drives
contract → write hook → refresh → rerun → select → resolve.

**Per-kind gating**: a kind's hooks run only when that kind's native discovery
returned zero rows, evaluated per snapshot. This is what keeps hook processes out
of the UART hot path (`_resolve_serial_port_for_session` runs before *every*
serial action).

**What the server cannot do:** if the host's USB stack won't show the probe to
pyOCD at all, no server code fixes that. That limit is real and is exactly what
the fallback pipe exists to route around — see the next section.

## Recommended next work item

### Teach the hook contract about provider-qualified selectors

This is the highest-value remaining change, it adds no guards, and it is squarely
the charter's dynamism goal (*"expose a tool for the agent to supply the missing
piece"*).

**The finding.** `DebugProbeAggregator.get_all_connected_probes(unique_id=X)` tries
`get_probe_with_id(X, is_explicit)` **first** and returns immediately on a hit,
skipping enumeration entirely. A `provider:uid` prefix in that string makes
`_get_probe_classes` set `is_explicit=True` and restrict the search to one provider
class. Checking each provider's direct lookup in the installed pyOCD:

```text
cmsisdap   DAPAccess.get_device()       targeted open, no bus enumeration
jlink      SEGGER DLL emulator list     own driver, not pyocd's libusb path
remote     TCPClientProbe(uid)          zero USB — pure constructor
stlink     get_all_connected_devices()  enumerates
picoprobe  enumerates
```

So for the hard case — pyOCD genuinely blind to the local device — **`remote:host:port`
is the only route that survives**, and it requires the explicit prefix, because
`TCPClientProbe.get_probe_with_id` returns `None` unless `is_explicit` is set.

**The gap.** The server passes the hook's bare `unique_id` straight through and never
composes `provider:uid`. `grep -n "remote\|provider:" src/pyocd_debug_mcp/tools/discovery.py`
returns **nothing** — the contract never tells the agent this route exists. A bare UID
still reaches `cmsisdap`/`jlink` direct lookups (all classes get tried), so the common
cases work by luck; `remote:` cannot be reached at all.

**The change is documentation and contract text, not new machinery.** In
`get_discovery_hook_contract`, state that `unique_id` may carry a `provider:` prefix
to force one provider and take pyOCD's explicit path, and that `remote:host:port` is
available when the local USB stack cannot see the probe. Verify a `remote:`-form
selector survives the token layer (colon-bearing UIDs are already covered — that was
the C12 fix) and passes D25's `PROBE_CLASSES` check (`remote` is registered).

**Resist scope creep here.** Do not build a provider-prefix composer, a `remote:`
validator, or a URL parser. The agent writes the selector; the server passes it
through. That is the whole change.

### Two ordinary triage items

- **M9 — cancelled UART operations record FAILED instead of CANCELLED.**
  `services/uart_capture.py` lines 161–162, 209–210, 310–312 each wrap in
  `except Exception as exc: raise RuntimeError(...) from exc`, destroying
  `OperationCancelledError`'s type identity, so the `except OperationCancelledError`
  handler at [`kernel/operations.py:835`](src/pyocd_debug_mcp/kernel/operations.py#L835)
  can never match for UART ops. Real misreporting — the charter's *"no silent failure
  and no fabrication."* Roughly a 3-line fix. Recommended first.
- **`continue_setup` has no inventory budget.** `_setup_continue` →
  `_resolved_probe_uid_for_connection` → `_hardware_inventory.snapshot()`, but
  `continue_setup` is not in `_PROBE_INVENTORY_TOOLS`, so it gets the flat default
  timeout with zero reservation for probe CLI, vendor CLI, or hooks. **Predates this
  feature.** Needs a categorization decision (should it always pay the larger budget,
  even on branches that never snapshot?).

Also outstanding: **19 pre-existing `pyright` errors in `tests/`** (trust-model rounds
1/3/4, change-loop). Predate this task, deliberately untouched.

## Key documents

| Path | What it is |
| --- | --- |
| `.codex/design_charter.md` | **The standard every change is measured against.** Read it before deciding anything is worth fixing. |
| `reviews/REVIEW_POLICY.md` | Materiality rubric (MUST-FIX vs EXTRANEOUS) and the targeted-test-rerun rule |
| `reviews/ledger.md` | **Every finding ever raised** — C1–C20, D1–D30, M1–M9 — verdict, one-line reason, resolution |
| `TASK_STATUS.md` | Stale; close-out step 3 |
| `reviews/iter-1..9-{diff,code}.md` | The adversary reports |
| `reviews/new-tests.md` | Phase 2 test rationale, per test |
| `../DEBUGGER_UART_DISCOVERY_HOOK_PLAN.md` | The design |
| `../DEBUGGER_UART_DISCOVERY_HOOK_IMPLEMENTATION_GUIDE.md` | The spec. **Treat its claims about pre-existing behavior as unverified** — see the blind spot below. |

**Never re-raise a finding the ledger rules INVALID** (D3, D5) unless the code has
since changed.

## Key source

| File | What matters in it |
| --- | --- |
| `src/pyocd_debug_mcp/hardware_inventory.py` | `HardwareInventoryService`, `_collect_uart_rows` (the per-kind gate), `vendor_uart_rows`, `stable_identity_equal`, `derive_selection_from_token` |
| `src/pyocd_debug_mcp/discovery_hooks.py` | Manifests, source hashing, capped execution, `server-python` routed through `sys.executable` |
| `src/pyocd_debug_mcp/tools/discovery.py` | The two MCP tools — **where the contract change goes** |
| `src/pyocd_debug_mcp/server.py` | `_no_native_probe_overview` (~4961), the zero-connection test (~5170), `_assigned_probe_uid_for_connect` (~1003), `_resolved_probe_uid_for_connection` (~1073), `_run_cmd` (~864) |
| `src/pyocd_debug_mcp/adapters/swd_pyocd.py` | `_choose_session` (~386) — the pyOCD boundary the second pipe terminates at |
| `src/pyocd_debug_mcp/kernel/operations.py` | `_vendor_uart_budget` (~132), `include_finalizer`, the budget blocks |
| `src/pyocd_debug_mcp/services/connections.py` | `probeid:` vs legacy `probe:` prefix split — the C12 fix, **do not reintroduce colon-counting** |
| `src/pyocd_debug_mcp/services/uart_capture.py` | The M9 site |

## The pattern that produced most of the real defects

**A fix introduced the next defect five consecutive times:**

| Fix | Defect it introduced | Caught by |
| --- | --- | --- |
| FIX 8 (C7) | **C12** — token misparse resolving to a *different real probe* | iteration 3 |
| C15 fix | **D16** — test passed without exercising its invariant | iteration 5 |
| D15 fix | **M6** — vendor CLI subprocesses unbudgeted | coordinator, by hand |
| M6 fix | **D17** — sixth budget site missed | scoped review |
| D25 fix | **D30** — two bypasses of its own check | iteration 9 |

Note the shape of the last two: a fix creates a defect, whose fix creates a defect.
That is the signature of guarding past the point of value, and it is why the loop
was stopped. **A green suite is not evidence a fix is safe** — but neither is an
endless review. Weigh the next finding against the charter, not against perfection.

## The blind spot to watch

**A diff review cannot see false premises in the spec.** The guide asserted as
established fact that `configured_probe_cli_commands` already routed pyOCD through
`sys.executable`. It did not. That survived four passes invisibly because it lived
in code no diff touched. Iteration 5 swept the remaining 14 "already/currently/today"
claims and found no second instance — but the class is only detectable by auditing
the specification against the code, never by reviewing the diff.

The same shape produced **D15**: a feature fully scaffolded, unit-tested, green, and
*never wired to production*.

## Operating lessons

- **Verify everything yourself. Agents reliably report what they *did* and
  unreliably report what *is*.** Six self-reports this task did not match the tree:
  "only tests/ modified" from an agent that had destroyed another's work; "ruff
  clean" with two `F401`s; "pyright 0 errors" scoped to `src/` while 18 sat in a new
  test file; a stale suite count; an agent claiming credit for files it never
  touched; and "verified against source code" on documentation that contradicted the
  source. None dishonest — each described the command the agent chose to run. Ask for
  raw output verbatim, then run it yourself anyway.
- **Never `git checkout` / `restore` / `stash` a path you don't own.** One agent
  reverted all of `src/` to undo its own one-line experiment and wiped a concurrent
  agent's implementation. Undo only lines you wrote.
- **Don't schedule two agents to mutate `src/` at once.** That was the root cause of
  the above — a coordinator error, not an agent error.
- **Prove a test can fail.** Break the guarded behavior, watch the test fail, revert
  exactly. This task produced three tests that passed for the wrong reason (C15's,
  D16's, D18's).
- **Don't run `ruff format` on whole files.** The repo was formatted with an older
  ruff than the installed one; a full-file format produces large unrelated churn.
- Interrupting a turn kills subagents launched in it. Subagent output-file size is
  **not** a liveness signal — completed agents show 0 bytes.
- The Bash tool is Git Bash: **no PowerShell here-strings** (`@'...'@`). Use a
  heredoc or `git commit -F <file>`.
- Stage explicitly by path. Never `git add -A` / `git add .`.

## Agent structure used

Four roles: a manager (adjudicates, never implements), a Sonnet code writer (`src/`),
a Sonnet reviewer (review passes only, never edits code), and a Haiku test writer
(`tests/`). Agent IDs do not survive a new session — spawn fresh ones and give them
this file. The Agent tool exposes `model` but **no effort parameter**; subagents
inherit the session's effort setting, so effort expectations have to be encoded as
instructions about verification depth.
