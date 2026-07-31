# Handoff — debugger UART discovery hook feature

Read this first, then `TASK_STATUS.md`, then `reviews/ledger.md`. Everything below
was verified at HEAD by running it, not taken from an agent report. That
distinction matters here — see "Operating lessons."

## Where things stand

| | |
| --- | --- |
| Repo | `BYO-Firmware-MCP` — **its own git repo**, nested inside `FirmCLI-WIP`, not a submodule (the outer repo lists it untracked) |
| Remote | `github.com/JasonPeng2019/BYO-Firmware-MCP` |
| Branch | `Proto-1-WIP` |
| HEAD | `820a559`, working tree clean |
| Base | `6f3da0a` (feature diff is `git diff 6f3da0a..HEAD`) |
| Suite | **664 passed / 7 skipped** |
| Lint | `ruff check src/ tests/` clean |
| Types | `pyright src/` clean |
| **Status** | **❌ RED** — see blockers below |

Run the suite with `python -m unittest discover -s tests`. **stdlib `unittest`
only — never pytest.** ruff line-length 100, target py310.

## The task being executed

Implement `../DEBUGGER_UART_DISCOVERY_HOOK_IMPLEMENTATION_GUIDE.md` (in the
**parent** directory, alongside `../DEBUGGER_UART_DISCOVERY_HOOK_PLAN.md`), then
subject it to an adversarial review loop, then straggler tests, then a status
file. Four phases, in order, no skipping:

- **Phase 0** — implement exactly what the guide describes, nothing more or less. ✅
- **Phase 1** — adversarial loop: a diff adversary and a code adversary each
  iteration, adjudicate VALID/INVALID, fix every VALID, log everything to
  `reviews/ledger.md`. Clears when a full iteration marks zero new findings VALID.
  **Safety cap: stop at iteration 5.** ⚠️ ran to the cap, never cleared
- **Phase 2** — straggler tests for untested edge cases and failure modes,
  logged to `reviews/new-tests.md`. ✅
- **Phase 3** — `TASK_STATUS.md`, GREEN only if all conditions hold. ✅ written RED

## Why it is RED

Two blockers. **Neither is a known defect** — both concern review coverage.

1. **Phase 1 never cleared.** All five permitted iterations found VALID findings,
   so "a full iteration marks zero new findings VALID" never occurred. Iteration 5
   was the cap, so the loop stopped as directed. Closing this requires a full
   iteration returning empty, which requires the **user** to extend the cap. Do not
   extend it yourself.
2. **The most recent fixes are unreviewed.** D17/D18/D19 landed after the last
   review pass. Reduced from where it was — the riskiest code (D15/M6) has now had
   one scoped pass — but moved one level down, not closed.

There are no known unfixed defects in scope at HEAD.

## The single most important pattern

**A fix has introduced the next defect four consecutive times:**

| Fix | Defect it introduced | Caught by |
| --- | --- | --- |
| FIX 8 (C7) | **C12** — token misparse resolving to a *different real probe* | iteration 3 |
| C15 fix | **D16** — test passed without exercising its invariant | iteration 5 |
| D15 fix | **M6** — vendor CLI subprocesses unbudgeted | coordinator, by hand |
| M6 fix | **D17** — sixth budget site missed | scoped review |

Three of the four were the *worst* finding of their round. Do not treat a green
suite as evidence a fix is safe. Every fix in this codebase deserves a pass over it.

## Key documents

| Path | What it is |
| --- | --- |
| `reviews/REVIEW_POLICY.md` | **Binding.** Materiality rubric (MUST-FIX vs EXTRANEOUS), the raised cap, the changed clearing condition, and the targeted-test-rerun rule. Read before reviewing, adjudicating, or fixing anything. |
| `TASK_STATUS.md` | Authoritative status, full checklist, blockers, deliberately-unfixed items |
| `reviews/ledger.md` | **Every finding ever raised** — C1–C16, D1–D19, M1–M6 — with VALID/INVALID verdict, one-line justification, and resolution. Read before filing anything. |
| `reviews/iter-1..5-diff.md` / `-code.md` | The ten adversary reports |
| `reviews/narrow-d15-m6.md` | Post-Phase-3 scoped review that found D17/D18/D19 |
| `reviews/new-tests.md` | Phase 2 test rationale, per test |
| `reviews/phase0-notes.md` | Implementation notes |
| `../DEBUGGER_UART_DISCOVERY_HOOK_IMPLEMENTATION_GUIDE.md` | The spec. **Treat its claims about pre-existing behavior as unverified** — see below. |

**Never re-raise a finding the ledger rules INVALID** (D3, D5) unless the code has
since changed to make it newly relevant.

## Key source

| File | What matters in it |
| --- | --- |
| `src/pyocd_debug_mcp/hardware_inventory.py` | `VendorUartRow` (~160), `vendor_uart_rows` (~221) and its two parser adapters, `HardwareInventoryService`, `_collect_uart_rows`/`_vendor_uart_rows`, `stable_identity_equal`, `derive_selection_from_token` |
| `src/pyocd_debug_mcp/kernel/operations.py` | `_vendor_uart_budget()` (~132), `include_finalizer` (~568), the `_PROBE_INVENTORY_TOOLS`/`_UART_ACTION_TOOLS` budget blocks and the three UART early-return branches (~630–680) |
| `src/pyocd_debug_mcp/server.py` | `_run_cmd` (~864, maps missing binary→127, timeout→124), `HardwareInventoryService(...)` construction site (~3010) — the **only** one |
| `src/pyocd_debug_mcp/serial_resolver.py` | `SERIAL_FALLBACKS` (~195, empty unless `PYOCD_SERIAL_FALLBACK_REGISTRY` set), `resolve_command_path` (~202), the two vendor parsers (~303, ~325) — **guide says keep the parsers exactly as they are** |
| `src/pyocd_debug_mcp/services/connections.py` | `probe_connection_id` / `parse_probe_connection_id`. The `probeid:` vs legacy `probe:` prefix split is the C12 fix — **structural discrimination, do not reintroduce colon-counting** |
| `src/pyocd_debug_mcp/discovery_hooks.py` | Hook manifests, `MAX_HOOKS_TOTAL`, `server-python` runner routed through `sys.executable` |
| `src/pyocd_debug_mcp/adapters/swd_process.py` | `_WorkerClient.__init__`/`call`/`_invalidate` (`NoReturn`) — the shared-deadline contract |
| `src/pyocd_debug_mcp/probe_families.py` | `configured_probe_cli_commands` — routes pyOCD through `sys.executable` (M4) |

Relevant tests: `tests/test_phase2_uncovered.py`, `tests/test_hook_gating_and_budget.py`
(`BudgetTests`), `tests/test_swd_process_isolation.py`,
`tests/test_unified_inventory.py::VendorProvenanceTests`,
`tests/test_probe_selection_records.py`, `tests/test_setup_overview_no_probe.py`.

## Open items

1. **Cap decision — the user's call, not yours.** Extending it to run a full
   iteration 6 is the only way to close blocker 1.
2. **`continue_setup` has no inventory budget at all.** `server.py`
   `_setup_continue` → `_resolved_probe_uid_for_connection` →
   `_hardware_inventory.snapshot()`, but `continue_setup` is not in
   `_PROBE_INVENTORY_TOOLS`, so it gets the flat default timeout with zero
   reservation for probe CLI, vendor CLI, *or* hooks. Found independently by two
   agents converging from opposite directions. **Predates this feature.** Left
   unfixed because it needs a categorization decision (should it join
   `_PROBE_INVENTORY_TOOLS` and always pay the larger budget, even on branches that
   never snapshot?). Genuine candidate for the next work item.
3. **D17/D18/D19 are unreviewed.**
4. **19 pre-existing `pyright` errors in `tests/`** (trust-model rounds 1/3/4,
   change-loop). Predate this task, deliberately untouched as out of scope.

## The blind spot to watch

**The review loop cannot see false premises in the guide.** The guide asserted as
established fact that `configured_probe_cli_commands` already routed pyOCD through
`sys.executable`. It did not. That survived four passes invisibly because it lived
in code no diff ever touched, and it took a deliberate audit of the spec against
the code to find. Iteration 5 swept the remaining 14 "already/currently/today"
claims and found no second instance — but the class is only detectable by
auditing the specification, never by reviewing the diff.

The same shape produced **D15**: a feature fully scaffolded, unit-tested, green,
and *never wired to production* — invisible from the tested unit, caught only by
checking whether the guide's requirement was actually satisfied end to end.

## Operating lessons

- **Verify everything yourself. Agents reliably report what they *did* and
  unreliably report what *is*.** Four self-reports this task did not match the tree:
  a "only tests/ modified" that had destroyed another agent's work; "ruff clean"
  with two `F401`s; "pyright 0 errors" scoped to `src/` while 18 sat in a new test
  file; and a suite count pasted stale from an earlier run. None dishonest — each
  described the command the agent chose to run. Ask for raw output verbatim, then
  run it yourself anyway.
- **Never `git checkout` / `restore` / `stash` a path you don't own.** One agent
  reverted all of `src/` to undo its own one-line experiment and wiped a
  concurrent agent's implementation. Undo only lines you wrote. File-ownership
  notes in a prompt are advisory; a blanket revert ignores them.
- **Don't schedule two agents to mutate `src/` at once.** That was the root cause
  of the above, and it was a coordinator error, not an agent error.
- **Prove a test can fail.** Break the guarded behavior, watch the test fail,
  revert exactly. This task produced three tests that passed for the wrong reason
  (C15's, D16's, D18's). A test that has never failed has never been verified.
- **Don't run `ruff format` on whole files.** The repo was formatted with an older
  ruff than the installed one; a full-file format produces large unrelated churn.
- Interrupting a turn kills subagents launched in it. Subagent output-file size is
  **not** a liveness signal — completed agents show 0 bytes. Use the notification.
- The Bash tool is Git Bash: **no PowerShell here-strings** (`@'...'@`). Use a
  heredoc or `git commit -F <file>`.
- Stage explicitly by path. Never `git add -A` / `git add .`.

## Agent structure used

Four roles: a manager (adjudicates, never implements), a Sonnet code writer
(`src/`), a Sonnet reviewer (all review passes, never edits code), and a Haiku
test writer (`tests/`). Agent IDs do not survive a new session — spawn fresh ones
and give them this file. Note the Agent tool exposes `model` but **no effort
parameter**; subagents inherit the session's effort setting, so effort
expectations have to be encoded as instructions about verification depth.
