# Handoff — debugger UART discovery hook feature

**The close-out is done. This feature is finished and green.** The adversarial
loop was stopped deliberately at iteration 9; read "Why the loop stopped" before
you consider running another pass — the reasoning matters more than the result.

A follow-on feature (**remote probe endpoints**, commit `94b569f`) has since landed on
top of it and is also finished and green — see that section below.

Read this, then `TASK_STATUS.md`, then `.codex/design_charter.md`.

## Verified state

| | |
| --- | --- |
| Repo | `BYO-Firmware-MCP` — **its own git repo**, nested inside `FirmCLI-WIP`, not a submodule |
| Remote | `github.com/JasonPeng2019/BYO-Firmware-MCP` |
| Branch | **`Proto-1-WIP-working`** — not `Proto-1-WIP`, which is stranded at `8a51138` and is far behind. Everything from iteration 6 onward is on `-working`. Check you are on it before anything else. |
| Base | `6f3da0a` (feature diff is `git diff 6f3da0a..HEAD`) |
| Suite | **730 ran / 723 passed / 7 skipped** |
| Lint | `ruff check src/ tests/` clean |
| Types | `pyright src/` clean (19 pre-existing errors in `tests/`, deliberately untouched) |

Measured at HEAD by running them, not taken from an agent report. That
distinction is load-bearing — see "Operating lessons."

Run the suite with `python -m unittest discover -s tests`. **stdlib `unittest`
only — never pytest.** ruff line-length 100, target py310.

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

## Close-out — done

All four steps are complete, so do not redo them:

1. **D30 is recorded in `reviews/ledger.md`** as raised, verified correct, and ruled
   out of scope — *not* INVALID, since a true finding that is not worth fixing is a
   different verdict. Site counts were re-verified by hand before being written down.
2. **The iteration-9 close-out note** is in the ledger, with the rounds-1–5 vs 6–9
   contrast as its justification.
3. **`TASK_STATUS.md` is rewritten** against re-measured numbers. Phase 1's row reads
   "stopped by decision, not cleared," with reasoning.
4. **The two carried triage items are fixed** — M9 and the `continue_setup` budget
   gap — and the hook contract now documents provider-qualified selectors. See
   "What was done at close-out" below.

Ledger housekeeping if you ever add a row: rows are 5 columns / 6 pipes (line 34 is
a pre-existing 7-pipe row — leave it). Stage explicitly by path; never `git add -A`.

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

## What was done at close-out

Three items, chosen because each is a real product gap rather than a message
upgrade. Each new test was proven by breaking the behavior it guards.

### Provider-qualified selectors are now in the hook contract

This was the outstanding gap in the second pipe. `get_all_connected_probes(unique_id=X)`
tries `get_probe_with_id(X, is_explicit)` **first** and returns immediately on a hit,
skipping enumeration; a `provider:uid` prefix makes `_get_probe_classes` set
`is_explicit=True` and restrict the search to one class. For the hard case — pyOCD
genuinely blind to the local device — **`remote:host:port` is the only route that
survives**, and it requires the prefix, because `TCPClientProbe.get_probe_with_id`
returns `None` unless `is_explicit`. The contract never said any of this existed.

Added as `unique_id_guidance` on the probe contract (`tools/discovery.py`):
pass-through semantics, the `provider:` prefix, the `remote:` route, and the caveat
that pyOCD splits at the *first* colon and rejects the whole selector when the text
before it is not a registered provider.

At close-out this was **documentation, not machinery** — no provider-prefix composer,
no `remote:` validator, no URL parser. The agent writes the selector; the server
passes it through. **That part still holds and must stay that way.** Composing
`provider:uid` server-side is tempting since the server holds both fields, but it
would change what every existing hook does: a row whose `provider` is a good guess
rather than a fact currently still resolves by falling through to another class, and
composing would turn that into a hard failure.

> **Superseded in part — see "Remote probe endpoints" below.** A later commit
> (`94b569f`) added a *registration* path so an agent no longer has to author a
> discovery hook just to hand the server a `remote:` endpoint. The pass-through rule
> above is unchanged: the new tools still never compose or rewrite a selector.

Two things stop the text going stale. `UniqueIdGuidanceDriftTests` re-derives every
claim from the installed pyOCD rather than restating it, so a pyOCD that changes its
selector parsing fails the suite instead of silently leaving agents with instructions
that no longer work — nothing in production consumes this text, and D21 already showed
what unverified documentation does in this repo. And
`test_a_provider_qualified_remote_selector_survives_the_whole_pipe` drives a real
`remote:bench.local:5555` row through hook → snapshot → token → `resolve()`, proving
the two-colon form is not mangled (C12 was a colon misparse) and clears D25's check.

### M9 — fixed

`except OperationCancelledError: raise` now precedes the generic wrap at all three
`services/uart_capture.py` sites, so a cancelled UART operation reaches
[`kernel/operations.py:835`](src/pyocd_debug_mcp/kernel/operations.py#L835) with its
type intact and records CANCELLED rather than FAILED.

One trap worth knowing if you touch these tests: `capture_uart_output`'s **first**
`cancellation_checkpoint()` is *outside* its try block, so a stub that raises
immediately propagates cleanly even with the bug present. The test arms its stub from
`on_port_open`, which fires inside the try, to land on the checkpoint the wrap
actually covers. `tools/serial.py`'s `_is_cancellation` `__cause__` branch was left in
place — belt-and-braces for this path now, still live for any other layer that wraps.

### The `continue_setup` budget gap — fixed

`continue_setup` joins `_PROBE_INVENTORY_TOOLS`. The categorization question the old
note left open — "should it always pay the larger budget, even on branches that never
snapshot?" — mis-frames the cost. That block resolves with `max(...)`, not `+=`, so
membership raises a *ceiling*, not a duration: a branch that never snapshots finishes
exactly as fast, it just stops being cancelled mid-discovery on the branch that does.
Measured 0.0s reserved against 123.0s required with one hook of each kind.

### Deliberately not done

**19 pre-existing `pyright` errors in `tests/`** (trust-model rounds 1/3/4,
change-loop). They predate this task and touching them is churn against the charter's
*"You should not edit parts that are not broken."* Verified still exactly 19 at
close-out — the new tests added none.

## Remote probe endpoints — follow-on feature (commit `94b569f`)

Built after the close-out above. Spec: `REMOTE_PROBE_PLAN.md`. Review:
`reviews/remote-probe-review.md`.

**What it is.** `register_remote_probe(host, port, description)` and
`unregister_remote_probe(host, port)`, backed by a JSON registry at
`.firm/remote_probes.json`, surfacing as ordinary probe rows in every snapshot.

**Why.** The `remote:` selector already worked and the contract already documented it,
but *using* it meant authoring a discovery hook — a Python file plus a manifest, run as
a subprocess — to print a constant the agent already knew. Nothing to discover. This is
the charter's *"expose a tool for the agent to supply the missing piece — and persist
it."*

**Three decisions that are load-bearing. Do not "improve" them** (each is commented at
its site and was break-tested):

1. Remote rows are **not** gated behind "native discovery came back empty," unlike hook
   rows. This is an explicit registration costing one file read, and must stay visible
   alongside a working local probe.
2. `unique_id` carries the **full `remote:` prefix**. `TCPClientProbe.get_probe_with_id`
   returns `None` unless `is_explicit`, which pyOCD only sets when the prefix is
   present. Strip it and the feature silently stops working.
3. `uart_snapshot()` is untouched and still returns `probes=()`.

**The invariant that makes it safe:** with nothing registered, behavior is
byte-identical to before. That is a test, and it is the most important one here.

**Verification policy is deliberate.** The tool probes the endpoint, reports
`reachable` honestly, and **registers either way**. Refusing an endpoint that is
unreachable *right now* would be paternalism (the server may not be started yet);
registering silently while implying success would be fabrication.

**`_registry_lock` is load-bearing — do not remove it.** Neither tool takes a
`board_id`, and `kernel/operations.py`'s `worker_lock(None)` is a plain
`nullcontext()`, so nothing upstream serializes them. Measured: 24 concurrent
registrations, 24 survive with the lock, **1 without**. It is deliberately **not** held
across `check_endpoint`, or unrelated registrations would serialize behind each other's
multi-second TCP timeout.

**One trap, recorded because it nearly shipped.** The first regression test for that
race was **vacuous**. Moving `check_endpoint` outside the critical section — itself
correct — relocated the blocking point outside the window the test forced, so it passed
with the lock removed entirely. Caught by neutering the lock and re-running, not by the
green suite. An interleave test *cannot* be rebuilt against correct locking (the second
thread never reaches the hook; it deadlocks), so it is now a barrier-synchronised
invariant test. **General lesson: a fix that changes control flow can silently
invalidate the test written against the old flow.**

**Hardware-verified**, not only unit-tested.
`tests/manual/manual_remote_probe_hardware_check.py` runs `pyocd server` against a real
ST-LINK, registers, snapshots, opens a session through the production connect path,
reads the core PC, unregisters. Deliberately outside the automated suite (needs a probe
and a spare port). On Windows that server subprocess **needs `PYTHONIOENCODING=utf-8`**
or it dies printing its own probe table.

**Known gap, not a defect:** there is no UART equivalent.
`adapters/uart_pyserial.py` opens with `serial.Serial()`, which rejects URL forms;
`serial.serial_for_url()` would accept `socket://` and `rfc2217://`. Swapping it looks
like one line but sits on the path that runs before *every* serial action, so it needs
its own verification that plain `COM7` / `/dev/tty*` names behave identically. Not
attempted.

**Two review findings ruled EXTRANEOUS** and recorded rather than fixed: the registry
file being an un-mocked dependency for `server`-singleton tests (test hygiene; identical
exposure to `_run_cmd` / `list_serial_ports`; clean checkout is green), and
re-registration without a `description` blanking a previous one (a human label that
never reaches selection or `open_session`).

## Key documents

| Path | What it is |
| --- | --- |
| `.codex/design_charter.md` | **The standard every change is measured against.** Read it before deciding anything is worth fixing. |
| `reviews/REVIEW_POLICY.md` | Materiality rubric (MUST-FIX vs EXTRANEOUS) and the targeted-test-rerun rule |
| `reviews/ledger.md` | **Every finding ever raised** — C1–C20, D1–D30, M1–M9 — verdict, one-line reason, resolution, plus the iteration-9 close-out note |
| `TASK_STATUS.md` | Current. Rewritten at close-out against re-measured numbers |
| `REMOTE_PROBE_PLAN.md` | Spec for the remote-probe follow-on, incl. its non-goals list |
| `reviews/remote-probe-review.md` | That feature's review: R1 (fixed), R2/R3 (extraneous), and the round-2 addendum on the vacuous test |
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
| `src/pyocd_debug_mcp/remote_probes.py` | Registry, `RemoteProbeEntry.selector`, and `_registry_lock` — **the lock is load-bearing** |
| `src/pyocd_debug_mcp/tools/remote_probes.py` | `register_remote_probe` / `unregister_remote_probe`; `check_endpoint` runs deliberately *outside* the lock |
| `src/pyocd_debug_mcp/adapters/uart_pyserial.py` | `serial.Serial()` — why there is no UART equivalent of the `remote:` route |

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
