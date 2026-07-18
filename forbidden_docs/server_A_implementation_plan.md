# Implementation Plan — Server A (Turnkey Brain)

> Design authority: `Design_A_Turnkey_Spec.md` **Revision 3** (all AC/D numbers below
> refer to it). Broad-design source: `Server A functionality.md`.
> Baseline inspected: branch `Jason-v3-BYO`, commit `5a98858` plus working tree.
> This document is about HOW. It does not restate or amend product behavior.
>
> **Plan Revision 3.** Revision 2 aligned the plan with the spec's four lenses:
> *correctness* — the middleman uses the session's one shared Server B endpoint
> (spec D-17) instead of a per-call private Server B and its probe-contention
> mess; *simplicity* — `complex_task` steps are one validated list (spec D-16),
> the green-check milestone is folded into the loop milestone, and low-value test
> theater is dropped; *generalizability* — the brain package is structurally
> board-, toolchain-, and provider-neutral, enforced by an import-closure test
> (AC-X.13); *neatness* — per-call scratch lives in an OS-temp directory, not in
> Server B's `.firm/` store. Revision 3 continues at the feature level: the
> Server B endpoint is now fixed once at registration time — no runtime discovery
> file, no race, nothing to clean up; delta calls implement the spec's exact
> continuation semantics (spec D-22, new AC-11.4); the green check executes
> exactly the command the check declares (spec D-24); every guide ships one
> complete example call (AC-2.4); and the two previously "confirm with owner"
> flags are settled decisions recorded here and in the ADR.

---

## 1. Current-state summary

### What this repository is today

The repo implements **Server B only** — the guarded hardware MCP server
(`pyocd-debug-mcp`), a mature product with a 949-test green baseline
(`v2_Brain_Spec_2_Gap_Sheet.md`). **No Server A / Turnkey Brain code exists.**
Whole-source searches for `turnkey`, `middleman`, `bug_fix`, `complex_task`,
`complex_implementation`, `green_check`, and `agentic` find nothing but the
benchmark's `green_check_ok` result field in `benchmark_support.py`. The "Brain
Spec 2 gap" documents track Server B hardening (P4-01…P4-09, all complete), not
Server A.

### Architecture and conventions Server A must follow

- Single package `src/pyocd_debug_mcp/`, Python ≥3.10, `uv`-locked, MCP SDK
  (`mcp>=1.2.0`, FastMCP), pytest (`asyncio_mode=auto`), Ruff (line 100), Pyright.
  Console scripts declared in `pyproject.toml`.
- **Composition-root pattern**: business rules live in owning modules; `server.py`
  only wires them (`docs/architecture.md`).
- **Kernel** (`kernel/`): `registry.py` (`ToolRegistry`/`RegistryFastMCP`) — dynamic
  discovery, hidden-but-registered tools, locked-tool refusals naming the
  prerequisite; `operations.py` — managed dispatch, finite timeouts, per-board
  serialization, cooperative cancellation; `processes.py` — `run_owned`
  validated-argv subprocesses with process groups; `hygiene.py` — startup cleanup
  of stale owned processes.
- **Load-tool precedent**: `tools/setup.py` `load_setup_tool` returns one bounded
  per-tool guide and unlocks the tool — exactly the spec §3.2 interaction shape.
- **Agent-launching precedent**: `agent_command_adapter.py` launches an
  operator-configured agent CLI from a validated argv template with placeholders,
  secret-name rejection, finite timeouts, structured evidence; fake-provider
  executables give CI coverage without vendor credentials
  (`test_agent_command_adapter.py`, `test_r11_benchmark.py`).
- **State policy**: live authority is in-memory (`ServerRun`) and never persisted —
  matching spec AC-1.4/X.11 exactly.
- **Transport**: Server B is stdio-only today; the broad design document's
  "Server B Sharing" section defines Server B as one shared endpoint (HTTP or
  stdio) that serializes board work for multiple clients. Spec D-17 adopts that.
  Adding the shared listener is a deliberate, ADR-recorded change (M3, risk R-2).
- **Contract tests**: `tests/contracts/product-server-tools.json` +
  `test_product_server_contract.py` for Server B; Server A gets its own contract
  file, and Server B's is untouched.

### What is directly reusable

| Need (spec) | Existing foundation |
| :--- | :--- |
| Locked tools whose refusal names the unlock (AC-2.1/2.2) | `ToolRegistry` prerequisite mechanism; `load_setup_tool` guide pattern |
| Launching a provider CLI as the middleman (AC-6.x) | `agent_command_adapter.py` argv-template adapter |
| Terminating middleman processes, no orphans (AC-1.3, 6.3, X.7) | `kernel/processes.py` process groups, `kernel/hygiene.py` |
| Bounded green-check script execution (AC-8.5, X.6) | `kernel/processes.run_owned` |
| Deterministic expected-output comparison precedent (AC-8.2) | `benchmark_support.py` strict result verification |
| Session-scoped in-memory state, empty after restart (AC-1.4, X.11) | `kernel/run_state.ServerRun` pattern |
| Strict `extra="forbid"` tool schemas (AC-3.5, 4.x) | Plan-tool registration technique in `server.py` |
| Multi-client board serialization (AC-6.5) | `kernel/operations.py` per-board locks — already client-count-agnostic |

Everything else — the brain server, tools, parameter model, prompts, decision
protocol, loop, green check, returns, cleanup, delta retention, and Server B's
shared listener — is net-new.

---

## 2. Gap analysis

Legend: **NS** = not started, **PM** = partially met (reusable machinery exists,
not wired to Server A). Server A does not exist, so nothing is already met.
"Will live" paths are new unless marked *(existing)*.

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-1.1 | NS | `brain/server.py`; `byo-pair-register` console script | One registration command wires both servers (M1); interpretation flagged in R-3. |
| AC-1.2 | NS | `brain/session_state.py` (`BrainRun`) | In-memory per-process session state. M1/M6. |
| AC-1.3 | PM | `brain/server.py` shutdown + `kernel/processes.py`, `kernel/hygiene.py` *(existing)* | stdio-EOF → terminate middleman → exit. M4. |
| AC-1.4 | NS | `brain/session_state.py` | Nothing persisted; restart test. M1, re-verified M7. |
| AC-2.1 | PM | `brain/server.py` via `kernel/registry.ToolRegistry` *(existing)* | Agentic tools registered locked with load-tool prerequisite. M1. |
| AC-2.2 | PM | Same | Prerequisite naming is the existing refusal shape. M1. |
| AC-2.3 | NS | `brain/session_state.py` unlock set | Idempotent load, session-scoped. M1. |
| AC-2.4 | NS | `brain/guides.py` | Guides embed tier 1–3 prompts. M1. |
| AC-2.5 | NS | `brain/guides.py` + tool descriptions | Contract test: prompts absent from tool list. M1. |
| AC-2.6 | NS | `brain/server.py` dispatch guard | Lock refusal precedes all validation/side effects. M1. |
| AC-3.1 | NS | `brain/workflows.py` + `brain/loop.py` | Fixed 6-step `bug_fix`. M5. |
| AC-3.2 | NS | `brain/workflows.py` | Fixed 5-step `complex_implementation`. M5. |
| AC-3.3 | NS | `brain/workflows.py` | `steps` list verbatim, in order (spec D-16). M5. |
| AC-3.4 | NS | `brain/loop.py` step cursor | Monotonic advance only. M5. |
| AC-3.5 | NS | `brain/params.py` | All validation before middleman creation; violations named. M2. |
| AC-4.1 | NS | `brain/params.py` | Full-form required-set check. M2. |
| AC-4.2 | NS | `brain/params.py` | Tier-1 four-field model. M2. |
| AC-4.3 | NS | `brain/params.py` | `iteration_max` integer ≥ 1. M2. |
| AC-4.4 | NS | `brain/prompts.py` | Every accepted param rendered in init prompt; content opaque (spec D-18). M2, proven end-to-end M5. |
| AC-5.1 | NS | `brain/guides.py` | Verbatim tier prompts (spec §3.5). M1. |
| AC-5.2 | NS | `brain/guides.py` | Single prompt constants shared by all three guides. M1. |
| AC-6.1 | NS | `brain/middleman.py` | Fresh session per call; only `BrainRun` retained context carries over. M4. |
| AC-6.2 | PM | `brain/middleman_config.py` + `agent_command_adapter.py` conventions *(existing)*; Server B endpoint discovery | Middleman registered against the shared endpoint from M3. M4. |
| AC-6.3 | PM | `brain/middleman.py` + `kernel/processes.py` *(existing)* | Terminate-on-return via owned process groups. M4. |
| AC-6.4 | NS | `brain/middleman_config.py` | Operator config declares provider, matching Client A's session; see R-1. M4. |
| AC-6.5 | NS | Server B share mode (`kernel/registry.py` listener, port fixed at registration); middleman registration in M4 | One Server B process owns the board; existing per-board locks serialize all clients. M3/M4. |
| AC-7.1 | NS | `brain/decisions.py` + `brain/loop.py` | Strict schema; invalid reply → discard, re-prompt with reason. M2 (logic), M5 (loop). |
| AC-7.2 | NS | `brain/loop.py` ledger + `brain/prompts.py` footer | Every decision consumes one iteration (spec D-8). M5. |
| AC-7.3 | NS | `brain/loop.py` + `CallState.check_validated` | `finish_task` gated on validated check. M5. |
| AC-7.4 | NS | `brain/loop.py` → `brain/returns.py` | M5. |
| AC-7.5 | NS | `brain/loop.py` → `brain/returns.py` | M5. |
| AC-7.6 | NS | `brain/loop.py` | Exhaustion ends the call; no further prompts. M5. |
| AC-7.7 | NS | `brain/loop.py` step cursor | M5. |
| AC-7.8 | NS | `brain/decisions.py` per-action param schemas | M2. |
| AC-8.1 | NS | `brain/green_check.py` | Only Server A's own run + literal compare sets state. M5. |
| AC-8.2 | PM | `brain/green_check.py`; comparison precedent *(existing)* | Pass iff every expected output present. M5. |
| AC-8.3 | NS | `brain/loop.py` `request_green_check` handler | Instructions returned; nothing executed. M5. |
| AC-8.4 | NS | `CallState` | Check state is call-scoped. M5. |
| AC-8.5 | PM | `brain/green_check.py` via `run_owned` *(existing)* | Script error ⇒ failed validation, loop continues. M5. |
| AC-9.1 | NS | `brain/returns.py` | Success: task result + check evidence. M6. |
| AC-9.2 | NS | `brain/returns.py` | Literal permission sentence, contract-locked. M6. |
| AC-9.3 | NS | `brain/returns.py` | Uniform non-success shape (spec D-23). M6. |
| AC-9.4 | NS | `brain/returns.py` | Defined fields only; leakage asserted absent. M6. |
| AC-10.1 | NS | `brain/cleanup.py` + per-call scratch manifest | Tracked per-call docs deleted on every exit path. M6. |
| AC-10.2 | NS | `brain/cleanup.py` | Deletes only the tracked manifest — never workspace globs. M6. |
| AC-10.3 | NS | `brain/cleanup.py` from the loop's single exit finalizer | All four exit paths tested. M6. |
| AC-11.1 | NS | `brain/session_state.py` + `brain/params.py` delta mode | "please continue" resumes at last-issued step (spec D-22). M6. |
| AC-11.2 | NS | `brain/params.py` | Delta without retained context ⇒ named refusal. M6. |
| AC-11.3 | NS | `brain/session_state.py` | Full-form follow-up replaces context. M6. |
| AC-11.4 | NS | `brain/loop.py` + delta mode | Other `continuation` text issued verbatim as the current step (spec D-22). M6. |
| AC-12.1 | NS | `brain/prompts.py` init renderer (8 ordered blocks) | M2 (render), M5 (sent once). |
| AC-12.2 | NS | `brain/prompts.py` delta renderer (6 ordered blocks) | M2/M5. |
| AC-12.3 | NS | `brain/loop.py` ledger → footer | True remaining count. M5. |
| AC-12.4 | NS | `brain/loop.py` | Rejection reason in next delta's "last action result". M5. |
| AC-12.5 | NS | `brain/prompts.py` footers | Hardware-safety line every prompt. M2/M5. |
| AC-X.1 | NS | `brain/session_state.py` active-call latch | Busy refusal; load tools unaffected. M7. |
| AC-X.2 | NS | Structural: `brain/` has no board adapters | Import-closure test. M7. |
| AC-X.3 | NS | `brain/decisions.py` + `brain/loop.py` | Only accepted decisions mutate state. M5. |
| AC-X.4 | NS | `brain/loop.py` permission path + `brain/returns.py` | M6. |
| AC-X.5 | NS | `brain/returns.py` | Middleman stdout/stderr never copied raw beyond defined fields. M7. |
| AC-X.6 | NS | `brain/loop.py` cap + `brain/middleman.py` per-turn bound (spec D-21) | M5. |
| AC-X.7 | PM | `brain/middleman.py`, `brain/cleanup.py` + `kernel/hygiene.py` *(existing)* | Orphan-check assertions in middleman tests. M4/M6. |
| AC-X.8 | NS | `brain/server.py` | Load/validation paths do no agent or hardware work; trivial to satisfy, asserted once. M7. |
| AC-X.9 | NS | `brain/loop.py` | No self-imposed waits; review item, not a dedicated test. M7. |
| AC-X.10 | NS | All `brain/` authored text | English source; review item. M7. |
| AC-X.11 | NS | Structural: no durable writes; scratch is OS-temp per call | Restart + import-closure tests. M7. |
| AC-X.12 | NS | Refusal/return text conventions | Contract-test snapshot of refusal texts. M7. |
| AC-X.13 | NS | Structural: `brain/` imports no `pyocd`, board adapters, or build helpers; params opaque (spec D-18) | Import-closure test + a swap test (two different fake board/toolchain param sets, identical behavior). M7. |

**Unmappable criteria:** none. The AC-1.1 "one command" interpretation and the
shared-listener posture are settled decisions (R-3, R-2/ADR); nothing awaits an
external call.

---

## 3. Milestones

New code lives in `src/pyocd_debug_mcp/brain/` with its own small composition root
(`brain/server.py` is wiring-only; every rule lives in an owning module). Server B
changes are confined to M3. All milestones end with the whole repo green
(`uv run --locked pytest`, Ruff, Pyright); Server B's contract file is never
edited.

Plan-level decisions (HOW, consistent with the spec):

- **D-P1** Server A is a second stdio FastMCP server, console script
  `turnkey-brain-mcp`; `byo-pair-register` registers both servers into the client
  in one command (AC-1.1; R-3).
- **D-P2** Middleman = provider CLI in non-interactive mode driven per turn via an
  operator-owned adapter config (extends the `agent_command_adapter.py` schema
  with `init_command`/`resume_command` templates, `provider` label, and
  `turn_timeout_seconds` implementing spec D-21). No vendor flags are hard-coded;
  CI uses fake providers (R-1).
- **D-P3** Shared Server B (spec D-17): Server B gains an opt-in `--share <port>`
  mode — the same process that serves Client A over stdio also listens on a
  localhost-only streamable-HTTP endpoint at that port. `byo-pair-register`
  chooses the port once at registration time and writes it into both launch
  commands (Server B gets `--share <port>`, Server A gets `--server-b-url`), so
  there is no runtime discovery, no endpoint file, no race, and nothing extra to
  clean up. One process owns the board; the existing per-board serialization in
  `kernel/operations.py` already covers all clients. Recorded as an ADR (the
  stdio-only posture in `docs/architecture.md` changes deliberately; localhost
  only; occupied port ⇒ clear launch failure per AC-1.1's edge case).
- **D-P4** Per-call scratch (green-check guide/script materialization, middleman
  exchange files, plan artifacts) lives in one OS-temp directory per call,
  tracked in a per-call manifest; cleanup deletes exactly the manifest entries.
  Nothing brain-related is written under `.firm/` at all.
- **D-P5** Layer 1 is three named load tools (`load_bug_fix`,
  `load_complex_implementation`, `load_complex_task`) — 1:1 per spec D-3.
- **D-P6** `complex_task` uses the `steps` array exactly as spec D-16 defines;
  strict `extra="forbid"` schemas throughout.

### M1 — Brain skeleton: pair registration, locks, load tools, guides

- **Goal / ACs:** Server A exists, both servers register with one command, three
  locked agentic tools + three load tools return full guides with the memory
  prompts. AC-1.1, 1.2, 1.4, 2.1–2.6, 5.1, 5.2.
- **Files:** `brain/__init__.py`; `brain/server.py` (composition root:
  `RegistryFastMCP` stdio server, registration, dispatch guard);
  `brain/session_state.py` (`BrainRun`: unlock set, retained-context slot,
  active-call latch — armed in M7); `brain/guides.py` (guides + verbatim tier
  prompt constants + one complete example call per agentic tool — AC-2.4); `pyproject.toml` (add `turnkey-brain-mcp`,
  `byo-pair-register`); `scripts/register_pair.py`.
- **Interfaces:** tool surface v0 (agentic tools refuse: locked, then
  "not implemented" behind the lock); new contract file
  `tests/contracts/turnkey-brain-tools.json`.
- **Verification:** `test_brain_server_contract.py` (tool list; lock refusal
  names load tool; unlock idempotence; guides contain all three prompts verbatim
  and identical across guides, plus one complete example call; prompts absent
  from descriptions);
  `test_brain_stdio_smoke.py` (real stdio initialize/list-tools; restart shows
  fresh locks). Covers AC-1.1/1.2/1.4/2.1–2.6/5.1/5.2.
- **Dependencies:** none.

### M2 — Pure logic: parameters, prompts, decisions, workflows

- **Goal / ACs:** the full parameter contract and the deterministic
  render/parse core, all unit-testable without subprocesses. AC-3.5, 4.1–4.4
  (render half), 7.1 (logic), 7.8; render halves of 12.1, 12.2, 12.5.
- **Files:** `brain/params.py` (typed common + tool-specific params, tier-1
  four-field records, `steps` list, opaque-content rule per spec D-18, validator
  naming every violation, strict `extra="forbid"` argument models);
  `brain/prompts.py` (`render_init_prompt` — 8 ordered blocks;
  `render_delta_prompt` — 6 ordered blocks; footers); `brain/decisions.py`
  (action index, per-action param schemas,
  `parse_decision(text) -> Decision | Rejection`); `brain/workflows.py` (fixed
  step tables; `steps` source for `complex_task`).
- **Interfaces:** `Decision`/`Rejection` are the loop engine's sole input types;
  agentic tools' real MCP schemas replace M1 placeholders and enter the contract
  file.
- **Verification:** `test_brain_params.py` (each missing param/field named —
  AC-4.1/4.2/4.3; unknown/extra params rejected; empty `steps` rejected;
  validator provably runs before any middleman factory — AC-3.5);
  `test_brain_prompts.py` (block order/presence; every accepted param rendered —
  AC-4.4, 12.1/12.2/12.5 render halves); `test_brain_decisions.py` (all 8
  actions valid; malformed/extra-text/missing-field/unknown-action/wrong-params
  rejected — AC-7.1 logic, 7.8).
- **Dependencies:** M1.

### M3 — Shared Server B endpoint (`--share` mode)

- **Goal / ACs:** one Server B process serves Client A (stdio) and the middleman
  (localhost streamable HTTP) simultaneously, per spec D-17. AC-6.5 (with M4);
  enables AC-6.2.
- **Files:** `kernel/registry.py` (optional concurrent localhost HTTP listener
  alongside stdio, behind an explicit `--share <port>` launch flag);
  `decisions/ADR-serverb-shared-endpoint.md`; a short `docs/architecture.md`
  amendment.
- **Interfaces:** the `--share <port>` flag and URL convention, fixed once at
  registration by `byo-pair-register` and consumed by M4. No tool, plan, or
  safety behavior changes — HTTP callers traverse the identical managed
  dispatch, locks, and gates.
- **Verification:** `test_brain_serverb_share.py`: stdio + HTTP clients connect
  to one process and list identical tools; session/board state established via
  one transport is visible via the other; two concurrent calls to a slow fake
  tool on one board serialize (AC-6.5 core); `--share` off ⇒ no listener
  (posture preserved by default); occupied port ⇒ clear launch failure. Existing
  full Server B suite stays green.
- **Dependencies:** none (parallel to M1/M2).

### M4 — Middleman session runtime

- **Goal / ACs:** spawn, drive per-turn, and terminate a real middleman process
  registered against the shared Server B. AC-1.3, 6.1–6.4, X.7; completes
  AC-6.5.
- **Files:** `brain/middleman_config.py` (operator config per D-P2, loaded only
  from an explicit path at Server A launch — same trust stance as the existing
  adapter; reuses its name/argv/env/secret validation); `brain/middleman.py`
  (`MiddlemanSession`: `start(init_prompt)`, `send(delta_prompt)`,
  `terminate()`; per-turn bounded runs via `run_owned`; process-group
  termination on exit and on Server A stdio EOF); fake middleman provider
  executable under `tests/fixtures/` (replays scripted decision sequences).
- **Interfaces:** `MiddlemanSession` protocol consumed by M5; adapter config
  documented in `docs/turnkey-brain.md` (started here).
- **Verification:** `test_brain_middleman.py`: fresh session per call, no state
  bleed (AC-6.1); termination after return and after simulated stdio EOF with an
  explicit no-orphan assertion (AC-1.3, 6.3, X.7); per-turn timeout ends the
  call (spec D-21); provider label surfaced (AC-6.4, operator-declared per repo
  trust model); fake middleman initializes and lists tools against the real
  shared Server B endpoint, board-free (AC-6.2, 6.5).
- **Dependencies:** M1 (shell), M2 (types), M3 (endpoint).

### M5 — Turnkey loop and green check: agentic tools end-to-end

- **Goal / ACs:** the three agentic tools run the complete loop, including the
  green-check gate, against the fake middleman. AC-3.1–3.4, 7.1–7.8, 8.1–8.5,
  12.1–12.5 (sent halves), X.3, X.6.
- **Files:** `brain/loop.py` (step cursor — monotonic; iteration ledger — every
  decision consumes one, spec D-8; decision dispatch; rejection → re-prompt with
  reason; exhaustion exit; queued `return_text_to_user`; one exit-finalizer hook
  used by M6); `brain/green_check.py` (materialize guide/script into the per-call
  scratch dir; `request_green_check` returns instructions without executing;
  `validate_green_check` executes exactly the command the check declares
  (spec D-24) via `run_owned`, passes iff every expected output is present,
  execution error ⇒ failed validation;
  `check_validated` flag gates `finish_task`); `brain/server.py` wiring
  (validate → spawn middleman → run loop → interim return).
- **Data model:** per-call `CallState` (step cursor, ledger, queued text,
  `check_validated`, scratch manifest).
- **Verification:** `test_brain_loop.py` (fixed workflows in order, never
  skipping — AC-3.1/3.2/3.4/7.7; `steps` verbatim in order — AC-3.3; rejection
  discarded/re-prompted/costs one/true footer — AC-7.1/7.2/12.3/12.4; terminal
  actions end immediately — AC-7.4/7.5; exhaustion — AC-7.6/X.6; init once then
  deltas — AC-12.1/12.2; free text outside an accepted decision changes nothing —
  AC-X.3); `test_brain_green_check.py` with local scripts (pass/fail/error/hang
  cases — AC-8.1–8.5; gate blocks then unblocks `finish_task` — AC-7.3; no
  cross-call carryover — AC-8.4).
- **Dependencies:** M2, M4.

### M6 — Returns, cleanup, delta calls

- **Goal / ACs:** the three return shapes, cleanup on every exit path, delta-form
  retention. AC-9.1–9.4, 10.1–10.3, 11.1–11.3, X.4.
- **Files:** `brain/returns.py` (success with check evidence; the two literal
  message templates; queued user text included; defined fields only);
  `brain/cleanup.py` (delete exactly the scratch-manifest entries from the
  loop's single exit finalizer; deletion failure reported per spec D-10);
  `brain/session_state.py` + `brain/params.py` (retained context after a
  completed full-form call; delta `continuation` per spec D-22 — "please
  continue" resumes at the last-issued step, any other text is issued verbatim
  as the current step; refusal without context; full-form replacement per D-11).
- **Interfaces:** final return payloads enter the contract file; permission
  round-trip documented in `docs/turnkey-brain.md`.
- **Verification:** `test_brain_returns_cleanup.py` (all four exit paths ×
  cleanup — AC-10.1/10.3; workspace files untouched — AC-10.2; literal texts —
  AC-9.2/9.3; evidence in success — AC-9.1; no leakage — AC-9.4; permission
  path never executes the gated action — AC-X.4); `test_brain_delta.py`
  (AC-11.1–11.4: retained context visible in the fake middleman's init prompt,
  resume at last-issued step, and a new-step continuation issued verbatim).
- **Dependencies:** M5.

### M7 — Cross-cutting hardening, contract, docs

- **Goal / ACs:** exclusivity, structural neutrality, persistence, message
  quality; consolidated gate. AC-X.1, X.2, X.5, X.8–X.13; AC-1.1–1.4 re-verified
  end-to-end.
- **Files:** `brain/server.py` active-call latch (AC-X.1);
  `test_brain_import_closure.py` (`brain/` imports no `pyocd`, board adapters,
  services, or firmstore writers — AC-X.2, X.11, X.13 structural halves);
  `test_brain_hardening.py` (restart persistence — AC-X.11; refusal-text
  snapshot — AC-X.12; redaction — AC-X.5; load/validation paths do no agent or
  hardware work — AC-X.8; generalizability swap test: two different fake
  board/toolchain parameter sets produce identical behavior — AC-X.13).
  AC-X.9/X.10 are review-checklist items, not dedicated tests. Docs:
  `docs/turnkey-brain.md` (surface, adapter config, pair registration,
  permission round-trip, the spec D-25/D-26 defaults); README section;
  `docs/architecture.md` Server A boundary note.
- **Optional, operator-triggered (never CI):** bounded real-provider middleman
  smoke and an on-hardware green check via Server B, recorded under
  `docs/evidence/` per the P4-08/P4-09 convention.
- **Verification:** full-repo `uv run --locked pytest`, Ruff, Pyright,
  `uv build`, real-stdio pair smoke — the P4-07 consolidated-gate style.
- **Dependencies:** M1–M6.

---

## 4. Testing strategy

Every AC is owned by at least one deterministic automated test; vendor- or
hardware-dependent evidence is optional and never converts to a pass (existing
repo policy). The linchpin is the **fake middleman provider** — a small script
replaying per-case decision sequences (including malformed ones), giving
deterministic coverage of every loop branch without credentials, models, or
hardware. Generalizability is tested the same way: fake parameter sets for two
entirely different pretend boards/toolchains must produce identical Server A
behavior (AC-X.13), and the import-closure test makes board/toolchain knowledge
structurally impossible inside `brain/`.

| Layer | Vehicle | ACs covered |
| :--- | :--- | :--- |
| Contract | `turnkey-brain-tools.json` + `test_brain_server_contract.py` | 2.1–2.6, 5.1, 5.2, 9.2, 9.3 (literal texts), X.12 |
| Pure unit | `test_brain_params.py`, `test_brain_prompts.py`, `test_brain_decisions.py` | 3.5, 4.1–4.4, 7.1 (logic), 7.8, 12.1/12.2/12.5 (render) |
| Server B share | `test_brain_serverb_share.py` + existing Server B suite | 6.5 (core), share-off posture |
| Loop integration (fake middleman) | `test_brain_middleman.py`, `test_brain_loop.py`, `test_brain_green_check.py`, `test_brain_returns_cleanup.py`, `test_brain_delta.py` | 1.3, 3.1–3.4, 6.1–6.5, 7.1–7.8, 8.1–8.5, 9.1–9.4, 10.1–10.3, 11.1–11.4, 12.1–12.5, X.3, X.4, X.6, X.7 |
| Real stdio smoke | `test_brain_stdio_smoke.py` | 1.1, 1.2, 1.4, X.1 |
| Structural / hardening | `test_brain_import_closure.py`, `test_brain_hardening.py` | X.2, X.5, X.8, X.11, X.13 |
| Review checklist (no test) | PR review against `docs/turnkey-brain.md` conventions | X.9, X.10 |
| Optional operator evidence (non-CI) | real-provider middleman smoke; on-hardware green check | bench halves of 6.2, 8.2 |

## 5. Risks, unknowns, and mitigations

- **R-1 — Multi-turn provider CLI sessions are not standardized** (dominant
  risk). Vendor resume flags differ and change; the repo's settled GAP-19 stance
  forbids hard-coding them. *Mitigation:* operator-owned
  `init_command`/`resume_command` templates; CI on fake providers;
  real-provider runs as optional evidence. *Residual:* a CLI that cannot resume
  needs a thin wrapper — the same posture as the existing agent adapter.
- **R-2 — Shared listener changes Server B's deliberate stdio-only posture.**
  Settled as a deliberate, ADR-recorded deviation (spec D-17 requires it).
  *Mitigation:* opt-in `--share <port>` flag, localhost-only bind, identical
  dispatch/gate path for HTTP callers, and a test that share-off leaves no
  listener. Concurrency risk (blocking pyOCD calls under two transports) is
  contained by the existing per-board serialization the share test exercises
  directly.
- **R-3 — "One command starts both servers" (AC-1.1)** is settled: one
  registration command wires both stdio servers (an MCP client opens one stdio
  channel per server), meeting the observable criterion; no supervising wrapper
  process is added.
- **R-4 — Green-check script trust (spec D-26).** Server A executes
  caller-authored code with Client A's trust. *Mitigation:* `run_owned` bounded
  execution, workspace-scoped cwd, finite timeout, exactly the declared command
  (spec D-24), no shell string; documented as trusted input. A sandbox would be
  its own ADR.
- **R-5 — Windows process-tree cleanup for provider CLIs** is bench-unverified
  even for Server B. *Mitigation:* reuse `kernel/processes` groups + `hygiene`
  sweep; explicit no-orphan assertions in the middleman tests; bench evidence
  recorded, not assumed.

## 6. Sequencing rationale

1. **M1 first**: every later milestone needs the server shell, and the lock/guide
   surface is pure, low-risk, and immediately demonstrable.
2. **M2 second**: the parameter model, renderers, and decision parser are the
   spec's largest surface and are verifiable as pure logic — cheap iteration that
   freezes the interfaces everything downstream consumes.
3. **M3 in parallel**: the shared Server B endpoint is a small, self-contained
   Server B change with its own tests, and M4 needs its endpoint. Doing it
   early surfaces the one cross-product risk (R-2) while the brain work is still
   flexible.
4. **M4 next** because the middleman runtime carries the dominant risk (R-1,
   R-5): attack it with stable types in hand, before the loop engine ossifies
   around wrong assumptions.
5. **M5 composes** M2+M4 into the product's core — loop plus green check land
   together because the gate is a loop-dispatch feature and splitting them added
   a milestone boundary with no verification value.
6. **M6** needs every exit path to exist before the three return shapes and the
   single-exit cleanup finalizer can be honest.
7. **M7 last**: cross-cutting invariants are asserted against the finished
   surface; docs and contract freeze; optional real-provider/hardware evidence is
   gathered without ever gating CI on it.

Each milestone ends with the whole repo green, so work can stop at any boundary
with Server B unaffected and the partial Server A surface honestly refusing what
is not yet implemented.
