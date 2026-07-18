# Server_A_Codex_Prompts

Ordered implementation prompts for building Server A (Turnkey Brain). Feed them to
the implementation agent **one at a time, in order**. Each prompt is self-contained
and ready to paste with no editing.

Ground-truth documents, in order of authority:

1. `Server_A_functionality.md` â€” the original design document. **On any conflict,
   this document wins.**
2. `Server_A_Turnkey_Design_Spec.md` (Revision 3) â€” the acceptance criteria (AC numbers).
3. `server_A_implementation_plan.md` (Revision 3) â€” scope, file layout, sequencing.

Four pillars every prompt serves: **Generalizability** (works with any MCU on any
toolchain â€” no test-specific hardcoding), **Simplicity** (Occam's razor â€” the most
efficient form that is still highly effective), **Correctness** (behavior matches
`Server_A_functionality.md` exactly), **Cleanliness** (neat, organized, easy for an
agent and a human to use â€” never a tangled web).

Universal rules (also embedded in each prompt):

- Do all coding, repo reading, and result evaluation **yourself**. Never delegate
  those to a subagent.
- Delegate **all test execution** to subagents. Each test run is performed twice:
  once by a Codex subagent (model `gpt-5.6-luna`, "5.6 Luna", medium effort) and
  once by a Claude subagent (model `claude-sonnet-5`, Sonnet 5, medium effort).
  Both must pass. Subagents testing MCP behavior connect to the server(s) as
  real MCP clients; software suites run `uv run --locked pytest` (plus `ruff` /
  `pyright` where the prompt says so).
- Adversarial subagents are used only where a prompt says so, and their Codex
  side always uses model `gpt-5.6-sol` ("5.6 Sol", medium effort) â€” never
  5.6 Luna â€” since adversarial review is a distinct role from test execution.
  When an adversarial subagent reports findings, you must independently
  evaluate each finding for validity against `Server_A_functionality.md` before
  acting; fix only what you judge valid, and record your disposition of every
  finding.
- Never convert unavailable hardware, credentials, or authorization into a pass:
  record such steps as blocked, with the reason.

---

## Prompt 1 â€” Orientation (read everything, build nothing)

PILLARS: correctness, cleanliness.

Read, in full: `Server_A_functionality.md`, `Server_A_Turnkey_Design_Spec.md`,
`server_A_implementation_plan.md`, and `docs/architecture.md`. Skim
`src/pyocd_debug_mcp/` (especially `kernel/registry.py`, `kernel/processes.py`,
`tools/setup.py`, `agent_command_adapter.py`) and `tests/` to absorb the repo's
conventions: composition-root wiring, owning modules, `ToolRegistry` locking,
strict `extra="forbid"` schemas, flat `tests/test_*.py`, contract JSON files,
`uv`-locked gates.

Do not create or modify any file. Output only: (a) a one-page restatement of the
build order (the plan's milestones M1â€“M7 and which prompts below map to them),
(b) any conflict you find between the three documents, resolved in favor of
`Server_A_functionality.md`, and (c) the exact list of files you will create,
taken from the plan.

DONE WHEN: the restatement covers all seven milestones and all 68 acceptance
criteria are accounted for by some later prompt, with zero files changed.

TESTING: none for this prompt.

---

## Prompt 2 â€” Brain skeleton: package, pair registration, locks, guides (plan M1)

PILLARS: correctness (lock/guide behavior is straight from the design doc),
cleanliness (wiring-only composition root), simplicity.

RULES: code, read, and evaluate yourself; all test execution by subagents, once in
Codex `gpt-5.6-luna` (medium) and once in Claude `claude-sonnet-5` (medium); both must
pass. `Server_A_functionality.md` overrides all other documents.

Create the new subpackage `src/pyocd_debug_mcp/brain/` exactly as the plan's M1
describes:

- `brain/__init__.py` and `brain/server.py` â€” a wiring-only composition root: a
  stdio FastMCP server built on the existing `kernel/registry.RegistryFastMCP` /
  `ToolRegistry`. Register three Layer 2 agentic tools â€” `bug_fix`,
  `complex_implementation`, `complex_task` â€” locked by default, each with its own
  Layer 1 load tool (`load_bug_fix`, `load_complex_implementation`,
  `load_complex_task`) as its named prerequisite. A locked call must fail with an
  error naming its exact load tool and cause no side effects. Behind the lock,
  agentic tools return an honest "not implemented yet" refusal until later
  prompts replace it.
- `brain/session_state.py` â€” `BrainRun`: in-memory unlock set, a retained-context
  slot (filled in a later prompt), and an active-call latch field (armed in a
  later prompt). Nothing is ever persisted to disk.
- `brain/guides.py` â€” one guide per agentic tool containing: purpose, every
  parameter with what it must contain, context-quality expectations, **the three
  memory-construction prompts copied verbatim from `Server_A_functionality.md`**
  (single shared constants â€” identical across all three guides), and one complete
  example call. The memory prompts must appear nowhere except these guides â€” not
  in tool descriptions or listings.
- `scripts/register_pair.py` behind a new `byo-pair-register` console script, and
  a `turnkey-brain-mcp` console script for `brain/server.py`, both added to
  `pyproject.toml`. `byo-pair-register` registers BOTH servers into the client in
  one command; it chooses a localhost port once and writes it into both launch
  commands (Server B `--share <port>` â€” implemented in Prompt 5; Server A
  `--server-b-url ...`). No runtime discovery files.
- `tests/contracts/turnkey-brain-tools.json` â€” the new Server A contract file.
  Never edit Server B's contract file.
- Tests: `tests/test_brain_server_contract.py` (tool list; locked refusal names
  the load tool; unlock idempotence; guides contain the three prompts verbatim,
  identical across guides, plus a complete example call; prompts absent from tool
  descriptions) and `tests/test_brain_stdio_smoke.py` (real stdio
  initialize/list-tools; a restarted server has fresh locks and no retained
  state).

Satisfies AC-1.1, 1.2, 1.4, 2.1â€“2.6, 5.1, 5.2.

DONE WHEN: both new test files pass, the whole existing suite still passes, and
Ruff + Pyright are clean on changed files.

TESTING: subagents run the two new test files and the full
`uv run --locked pytest` suite, in both Codex 5.6 Luna medium and Sonnet 5 medium.
You evaluate their reports yourself.

---

## Prompt 3 â€” Parameter contract and validation (plan M2, part 1)

PILLARS: generalizability (all board/toolchain content is opaque), correctness
(the exact parameter set from the design doc), simplicity (one validator, every
violation named).

RULES: code, read, and evaluate yourself; all test execution by subagents, once in
Codex `gpt-5.6-luna` (medium) and once in Claude `claude-sonnet-5` (medium); both must
pass. `Server_A_functionality.md` overrides all other documents.

Create `src/pyocd_debug_mcp/brain/params.py`:

- Typed models for every common parameter in the design doc's "Agentic tool input
  parameters" section: `tool_summary`, `task`, `memory_tier1_turn1`â€“`turn4` (each
  with exactly the four fields `action`, `reasoning`, `codebase_changes`,
  `result`), `memory_tier2`, `memory_tier3`, `relevant_files` (path + hint
  entries), `board_facts` (all listed facts present), `reference_artifacts`
  (explicitly-empty list allowed), `build_context` (workspace root, build
  command, artifact paths), `iteration_max` (integer â‰¥ 1), `green_check_guide`,
  `green_check_script` (must state the exact command that runs it),
  `green_check_expected_outputs` (â‰¥ 1 entry). Tool-specific: `bug`, `feature`,
  and `steps` (ordered list of non-empty strings â€” this is the design doc's
  `step_1 â€¦ step_n` carried as one list).
- **Opaqueness rule:** validate presence and shape only. Never parse, interpret,
  or branch on the content of board facts, build commands, paths, or artifacts â€”
  that is what makes Server A work with any MCU and any toolchain. Token-count
  goals for memory tiers are guidance, never validation.
- One validator that returns EVERY violation with the parameter (and field) name;
  unknown/extra parameters rejected by name; wrong-tool parameters rejected.
  Validation must complete before any middleman, workspace, or hardware activity
  can exist.
- Wire strict `extra="forbid"` argument models onto the three agentic tools using
  the same technique Server B's plan-tool registration uses in `server.py`.
- Tests: `tests/test_brain_params.py` â€” every missing common parameter named; a
  tier-1 parameter missing one of its four fields named field-precisely;
  `iteration_max` bounds; unknown/extra/wrong-tool parameters; empty `steps` and
  empty step strings rejected; and a sentinel middleman factory that fails the
  test if validation ever invokes it (proving rejection is side-effect-free).

Satisfies AC-3.5, 4.1, 4.2, 4.3.

DONE WHEN: the new tests pass, full suite green, Ruff + Pyright clean.

TESTING: subagents run `test_brain_params.py` plus the full suite, in both Codex
5.6 Luna medium and Sonnet 5 medium.

---

## Prompt 4 â€” Prompt renderers, decision parser, workflows (plan M2, part 2)

PILLARS: correctness (the init/delta prompt structures and the decision schema are
copied from the design doc, not paraphrased), simplicity (pure functions, no I/O).

RULES: code, read, and evaluate yourself; all test execution by subagents, once in
Codex `gpt-5.6-luna` (medium) and once in Claude `claude-sonnet-5` (medium); both must
pass. `Server_A_functionality.md` overrides all other documents.

Create three pure-logic modules:

- `brain/workflows.py` â€” the fixed step tables verbatim from the design doc:
  `bug_fix` = diagnose â†’ locate root cause â†’ patch â†’ rebuild â†’ flash â†’ green
  check (6 steps); `complex_implementation` = understand requirement â†’ implement
  â†’ rebuild â†’ flash â†’ green check (5 steps); plus the `steps`-list source for
  `complex_task`.
- `brain/prompts.py` â€” `render_init_prompt(...)` producing the design doc's
  8-block init prompt in its exact order (pre-prompt rules; tool summary + task;
  current step with position; all context parameters rendered legibly; green
  check summary + expected outputs; full action index; full return schema with
  the design doc's field descriptions; footer with iterations remaining, the
  auto-reject rule, the finish_task gate, and the leave-hardware-safe line) and
  `render_delta_prompt(...)` producing the 6-block delta prompt (re-anchor;
  tool summary + task one line each; last action result; next step or "please
  continue" injection; compact action index + compact schema; footer). Model
  both on the design doc's example prompts.
- `brain/decisions.py` â€” the action index: exactly `next_step`, `continue_step`,
  `return_text_to_user` (`text`), `request_green_check`, `validate_green_check`
  (`script_args`, `preparation_summary`), `finish_task` (`task_result`),
  `fail_task` (`failure_reason`), `finalize_needs_user_permission`
  (`permission_request`); required decision fields `observation_summary`,
  `current_strategy`, `failed_strategies`, `carry_forward_warnings`, optional
  `problem_hypotheses`; and `parse_decision(text) -> Decision | Rejection`.
  Reject: non-JSON, text outside the single JSON object, missing or extra
  fields, unknown actions, and missing/extra action parameters â€” each
  `Rejection` carries a reason plus the compact schema for the re-prompt.
- Tests: `tests/test_brain_prompts.py` (block order and presence for both prompt
  shapes; every accepted parameter appears in the init render) and
  `tests/test_brain_decisions.py` (a valid decision for all 8 actions; each
  rejection class).

Satisfies AC-4.4 (render half), 7.1 (logic), 7.8, and the render halves of
12.1, 12.2, 12.5.

DONE WHEN: new tests pass, full suite green, Ruff + Pyright clean.

TESTING: subagents run the two new test files plus the full suite, in both Codex
5.6 Luna medium and Sonnet 5 medium.

---

## Prompt 5 â€” Server B shared endpoint: `--share <port>` (plan M3)

PILLARS: correctness (the design doc's "Server B Sharing" section: one shared
endpoint, one owner/queue of the board), simplicity (port fixed at registration â€”
no discovery files, no races).

RULES: code, read, and evaluate yourself; all test execution by subagents, once in
Codex `gpt-5.6-luna` (medium) and once in Claude `claude-sonnet-5` (medium); both must
pass. `Server_A_functionality.md` overrides all other documents.

Modify Server B minimally:

- Add an opt-in `--share <port>` launch flag. When set, the same Server B process
  that serves stdio also serves a **localhost-only** streamable-HTTP MCP endpoint
  on that port. HTTP callers traverse the identical managed dispatch, handler
  locks, plans, permissions, gates, and per-board serialization â€” zero behavior
  differences by transport. When the flag is absent, there is no listener of any
  kind (today's posture is the default). An occupied port is a clear launch
  failure, not a silent fallback.
- Do not change any tool, plan, or safety behavior. Keep the change confined to
  the kernel/registry serving layer.
- Record the deviation in `decisions/ADR-serverb-shared-endpoint.md` (what
  changed, why the design doc requires it, localhost-only bound, default-off) and
  amend `docs/architecture.md`'s transport paragraph accordingly.
- Confirm `byo-pair-register` (from Prompt 2) injects the same port into Server
  B's `--share <port>` and Server A's `--server-b-url`.
- Tests: `tests/test_brain_serverb_share.py` â€” one process serves stdio and HTTP
  clients with identical tool lists; session/board state established through one
  transport is visible through the other; two concurrent calls to a slow fake
  tool on one board serialize; `--share` absent â‡’ no listener; occupied port â‡’
  clear failure.

Satisfies the core of AC-6.5.

DONE WHEN: the new share tests pass AND the entire existing Server B suite passes
unchanged, Ruff + Pyright clean.

TESTING: subagents run the share tests plus the FULL existing suite, in both
Codex 5.6 Luna medium and Sonnet 5 medium. Evaluate both reports yourself before
proceeding.

---

## Prompt 6 â€” Middleman session runtime (plan M4)

PILLARS: generalizability (any provider CLI via operator config â€” no vendor flags
in product code), correctness (fresh middleman per call, dies with the call),
cleanliness (one small session protocol).

RULES: code, read, and evaluate yourself; all test execution by subagents, once in
Codex `gpt-5.6-luna` (medium) and once in Claude `claude-sonnet-5` (medium); both must
pass. `Server_A_functionality.md` overrides all other documents.

Create:

- `brain/middleman_config.py` â€” an operator-owned JSON config loaded only from an
  explicit path given at Server A launch (never discovered from the workspace),
  reusing `agent_command_adapter.py`'s validation conventions (argv arrays,
  placeholder substitution, secret-name rejection, no shell strings). Fields:
  `name`, `provider` label, `init_command` and `resume_command` argv templates,
  `turn_timeout_seconds` (default 600), optional env passthrough. The Server B
  URL comes from Server A's `--server-b-url` launch argument, not from this
  config.
- `brain/middleman.py` â€” `MiddlemanSession` with exactly `start(init_prompt)`,
  `send(delta_prompt)`, `terminate()`. Every agentic tool call creates a fresh
  session (never reused); each turn is a bounded run via
  `kernel/processes.run_owned`; `terminate()` kills the whole process group; a
  Server A stdio EOF terminates any live session before exit. The middleman's
  MCP registration points at the shared Server B URL.
- A fake middleman provider executable under `tests/fixtures/` that replays a
  scripted list of decision replies (including malformed ones) â€” the workhorse
  for all later loop tests.
- Tests: `tests/test_brain_middleman.py` â€” fresh session per call with no state
  bleed between two calls; termination after return and after simulated stdio
  EOF with an explicit no-orphan-process assertion; per-turn timeout ends the
  session; the fake middleman successfully initializes and lists tools against a
  real `--share` Server B endpoint, board-free.

Satisfies AC-1.3, 6.1â€“6.4, X.7.

DONE WHEN: new tests pass, full suite green, Ruff + Pyright clean.

TESTING: subagents run `test_brain_middleman.py` plus the full suite, in both
Codex 5.6 Luna medium and Sonnet 5 medium.

---

## Prompt 7 â€” Turnkey loop engine (plan M5, part 1)

PILLARS: correctness (the loop, iteration accounting, and auto-reject behavior are
exactly the design doc's), simplicity (one dispatch table, one exit path).

RULES: code, read, and evaluate yourself; all test execution by subagents, once in
Codex `gpt-5.6-luna` (medium) and once in Claude `claude-sonnet-5` (medium); both must
pass. `Server_A_functionality.md` overrides all other documents.

Create `brain/loop.py` and wire the three agentic tools in `brain/server.py`
(validate â†’ spawn middleman â†’ run loop â†’ return; returns are interim plain text
until Prompt 10):

- Per-call `CallState`: step cursor, iteration ledger, queued user text,
  `check_validated` flag (used in Prompt 8), per-call scratch manifest.
- Step cursor is strictly monotonic â€” never revisits, never skips; `next_step`
  advances exactly one; `continue_step` re-issues the current step with a
  "please continue" injection.
- Iteration ledger: EVERY middleman decision â€” accepted, refused, or
  schema-rejected â€” consumes exactly one iteration (the design doc's own example
  shows 20 at init and 16 at turn 5). The true remaining count appears in every
  prompt footer. Exhaustion ends the call that turn; no further prompt is sent.
- Rejections (from Prompt 4's parser): discard the reply, re-prompt with the
  rejection reason plus the compact schema; nothing else changes.
- `return_text_to_user`: queue the text (delivered with the final return); the
  loop continues on the current step. `fail_task` and
  `finalize_needs_user_permission` end the loop immediately. Free text outside an
  accepted decision changes nothing.
- One exit finalizer hook that every termination path funnels through (used by
  Prompts 8 and 10).
- Tests: `tests/test_brain_loop.py` using the fake middleman: fixed workflows
  advance in order; `steps` presented verbatim in order; rejection costs one
  iteration and the next delta's "last action result" states the reason;
  terminal actions end immediately; exhaustion; init sent exactly once then
  deltas only.

Satisfies AC-3.1â€“3.4, 7.1, 7.2, 7.4â€“7.7, 12.1â€“12.4 (sent halves), X.3, X.6.

DONE WHEN: new tests pass, full suite green, Ruff + Pyright clean.

TESTING: subagents run `test_brain_loop.py` plus the full suite, in both Codex
5.6 Luna medium and Sonnet 5 medium.

---

## Prompt 8 â€” Green check (plan M5, part 2)

PILLARS: correctness (deterministic proof, never self-declared), generalizability
(Server A executes exactly the command the check declares â€” it never guesses an
interpreter or toolchain), simplicity.

RULES: code, read, and evaluate yourself; all test execution by subagents, once in
Codex `gpt-5.6-luna` (medium) and once in Claude `claude-sonnet-5` (medium); both must
pass. `Server_A_functionality.md` overrides all other documents.

Create `brain/green_check.py` and register its handlers in the loop's dispatch
table:

- Materialize `green_check_guide` and `green_check_script` into the per-call
  OS-temp scratch directory, recorded in the scratch manifest (cleanup deletes
  exactly the manifest's entries â€” Prompt 10).
- `request_green_check`: return the guide's instructions and the expected
  outputs in the next prompt's "last action result". Execute nothing; touch no
  hardware.
- `validate_green_check`: run the check by executing EXACTLY the command it
  declares, via `kernel/processes.run_owned`, workspace cwd, finite timeout, no
  shell string. Pass if and only if every entry of
  `green_check_expected_outputs` is satisfied by the actual output â€” a literal,
  deterministic comparison with no heuristics. Failure reports the actual output
  and the unmet expectations to the middleman; a script that cannot run at all
  is a failed validation carrying the observed error; in both cases the loop
  continues.
- A pass sets `check_validated` for THIS call only. `finish_task` before a
  validated check is refused with an explicit reason and costs an iteration;
  after a validated check it ends the call successfully. The middleman's own
  claims about the result are ignored entirely.
- Tests: `tests/test_brain_green_check.py` with small local scripts covering:
  all-expected-outputs-present passes; missing output fails with the comparison
  reported; nonzero-exit/missing-file errors are failed validations; a hanging
  script hits the timeout; the finish_task gate blocks then unblocks; no
  carryover between calls.

Satisfies AC-7.3, 8.1â€“8.5.

DONE WHEN: new tests pass, full suite green, Ruff + Pyright clean.

TESTING: subagents run `test_brain_green_check.py` plus the full suite, in both
Codex 5.6 Luna medium and Sonnet 5 medium.

---

## Prompt 9 â€” Adversarial checkpoint on the core (used sparingly)

PILLARS: all four.

RULES: you evaluate and fix yourself; the adversarial subagent only criticizes.
Its Codex side uses model `gpt-5.6-sol` ("5.6 Sol", medium effort) â€” the
adversarial-review model, distinct from the 5.6 Luna testing model.
`Server_A_functionality.md` overrides all other documents.

The core (parameters â†’ prompts â†’ decisions â†’ loop â†’ green check) is now the
riskiest completed surface. Launch ONE adversarial subagent (Codex `gpt-5.6-sol`,
medium effort, read-only) with this brief: "Attack `src/pyocd_debug_mcp/brain/`
against `Server_A_functionality.md` and the four pillars â€” find behavior that
deviates from the design doc, overcomplicated mechanisms, board/toolchain/provider
assumptions, and tangled or confusing interfaces. Report concrete findings with
file/line and the ideal behavior each violates."

Then, for EVERY finding, independently verify it yourself against
`Server_A_functionality.md` and the spec before acting. Fix only the findings you
judge valid. Record a written disposition for each finding: valid â†’ what you
changed; invalid â†’ why. Do not launch a second adversarial run in this prompt.

DONE WHEN: every finding has a recorded disposition, all fixes are implemented,
and the affected tests plus the full suite pass.

TESTING: subagents re-run every test file touched by fixes plus the full suite,
in both Codex 5.6 Luna medium and Sonnet 5 medium.

---

## Prompt 10 â€” Returns and wrap-up cleanup (plan M6, part 1)

PILLARS: correctness (the two literal messages come from the design doc
word-for-word), cleanliness (manifest-only deletion â€” cleanup can never touch user
work).

RULES: code, read, and evaluate yourself; all test execution by subagents, once in
Codex `gpt-5.6-luna` (medium) and once in Claude `claude-sonnet-5` (medium); both must
pass. `Server_A_functionality.md` overrides all other documents.

Create `brain/returns.py` and `brain/cleanup.py`, replacing Prompt 7's interim
returns:

- Success return: the finished `task_result` plus the green-check evidence (the
  validated comparison), plus any queued `return_text_to_user` text.
- Permission return: the literal sentence `agentic tool did not finish: user
  permission required; get user permission and try again.` plus the middleman's
  `permission_request`.
- Every other non-success (fail_task, iteration exhaustion, middleman death or
  timeout, internal error): `agentic tool did not finish: <final response from
  the middleman>; diagnose the issue and try again.` â€” substituting the
  middleman's final response, or a description of what terminated the call if
  none exists. One uniform shape; no special cases.
- No return may expose internal identifiers, raw prompts, or middleman decision
  payloads beyond these defined fields.
- `brain/cleanup.py`: from the loop's single exit finalizer, delete exactly the
  per-call scratch-manifest entries (green-check guide/script materializations,
  middleman plan/strategy artifacts, exchange files) â€” on success, fail,
  permission, exhaustion, death, and disconnect alike. Never delete anything not
  in the manifest. A failed deletion does not block the return; report the
  leftover path in the return.
- Add the return shapes to `tests/contracts/turnkey-brain-tools.json`.
- Tests: `tests/test_brain_returns_cleanup.py` â€” all four exit paths Ã— cleanup;
  workspace files and `build_context` artifacts untouched; the two literal
  message texts exact; green-check evidence present on success; no leakage.

Satisfies AC-9.1â€“9.4, 10.1â€“10.3, X.4.

DONE WHEN: new tests pass, full suite green, Ruff + Pyright clean.

TESTING: subagents run the new test file plus the full suite, in both Codex 5.6
Luna medium and Sonnet 5 medium.

---

## Prompt 11 â€” Delta (follow-up) calls (plan M6, part 2)

PILLARS: correctness (the design doc's delta form: `tool_summary`, `task`, and
"the next turnkey step or 'please continue'"), simplicity (one `continuation`
parameter).

RULES: code, read, and evaluate yourself; all test execution by subagents, once in
Codex `gpt-5.6-luna` (medium) and once in Claude `claude-sonnet-5` (medium); both must
pass. `Server_A_functionality.md` overrides all other documents.

Implement delta calls in `brain/session_state.py`, `brain/params.py`, and
`brain/loop.py`:

- After a completed full-form call, `BrainRun` retains the full context (memory
  tiers, files, board facts, references, build context, green-check materials,
  iteration policy). In memory only â€” never on disk.
- Delta form: exactly `tool_summary`, `task`, and `continuation` (non-empty
  text). `continuation` = "please continue" resumes at the step the previous
  call last issued; any other text is issued verbatim as the current step, after
  which the retained sequence resumes (or, if finished, the green-check/finish
  gate remains). Each delta call still spawns a FRESH middleman whose init
  prompt is built from the retained context plus the delta inputs.
- A delta call with no retained context is rejected with an error naming the
  full-form requirement. A follow-up call supplying the full parameter set
  replaces the retained context.
- This completes the permission round-trip: permission return â†’ Client A obtains
  permission â†’ delta "please continue" resumes without resupplying context.
- Tests: `tests/test_brain_delta.py` â€” retained context visible in the fake
  middleman's init prompt; resume at last-issued step; a new-step continuation
  issued verbatim; refusal without context; full-form replacement.

Satisfies AC-11.1â€“11.4.

DONE WHEN: new tests pass, full suite green, Ruff + Pyright clean.

TESTING: subagents run `test_brain_delta.py` plus the full suite, in both Codex
5.6 Luna medium and Sonnet 5 medium.

---

## Prompt 12 â€” Cross-cutting hardening, docs, consolidated software gate (plan M7)

PILLARS: all four â€” this prompt closes the generalizability and cleanliness
criteria and freezes the surface.

RULES: code, read, and evaluate yourself; all test execution by subagents, once in
Codex `gpt-5.6-luna` (medium) and once in Claude `claude-sonnet-5` (medium); both must
pass. `Server_A_functionality.md` overrides all other documents.

Implement and verify:

- Active-call latch in `brain/server.py`: at most one agentic call per session;
  an overlapping call gets an immediate "busy" refusal with no side effects;
  load tools stay available throughout.
- `tests/test_brain_import_closure.py`: `brain/` must import no `pyocd`, no
  board adapters, no Server B services, and no firmstore writers â€” making
  board/toolchain knowledge structurally impossible in Server A.
- `tests/test_brain_hardening.py`: restart shows nothing persisted; a
  generalizability swap test â€” two entirely different fake board/toolchain
  parameter sets (different MCU names, build commands, artifact layouts) produce
  identical Server A behavior; returns never include middleman stdout/stderr
  beyond the defined fields; load-tool and validation-refusal paths perform no
  agent or hardware work; a snapshot review of every refusal/return text for
  plain, self-describing language.
- Docs: `docs/turnkey-brain.md` (tool surface, middleman adapter config, pair
  registration, permission round-trip, the D-25/D-26 defaults), a README
  section, and a short Server A boundary note in `docs/architecture.md`.
  Finalize `tests/contracts/turnkey-brain-tools.json`.
- Consolidated gate: full `uv run --locked pytest`, `uv run --locked ruff check
  .`, `uv run --locked pyright`, `uv build`, and a real-stdio pair smoke
  (register pair, list tools on both servers).

Satisfies AC-X.1, X.2, X.5, X.8â€“X.13; re-verifies AC-1.1â€“1.4 end-to-end.

DONE WHEN: every gate above passes in both providers' subagent runs and the docs
accurately describe the shipped surface.

TESTING: subagents execute the entire consolidated gate, in both Codex 5.6
Luna medium and Sonnet 5 medium. You compare and evaluate the two reports yourself.

---

## Prompt 13 â€” Bounded real-hardware smoke (both providers, real board)

PILLARS: correctness (proof on real hardware), generalizability (resolve the
board from live inventory â€” hardcode nothing).

RULES: code, read, and evaluate yourself; all test execution by subagents, once in
Codex `gpt-5.6-luna` (medium) and once in Claude `claude-sonnet-5` (medium); both must
pass or be honestly recorded as blocked. `Server_A_functionality.md` overrides
all other documents.

With the operator's explicit go-ahead and at least one board physically
connected (identify it through Server B's inventory â€” do not assume which board
it is):

1. Prepare a minimal firmware workspace for the connected board with a trivial
   seeded bug whose fix is observable over UART or a memory/symbol read.
2. Register the pair with `byo-pair-register`. Configure the middleman adapter
   for each provider.
3. For each provider (Codex 5.6 Luna medium, then Sonnet 5 medium), a subagent acting
   as Client A must: call `load_bug_fix`, build the full parameter set from the
   guide (including a real green check whose declared command exercises the
   board through Server B), then call `bug_fix` and let the turnkey loop run to
   a green-check-validated finish on real hardware.
4. Record evidence bundles (commands, MCP timelines, green-check output) under
   `docs/evidence/`, following the repo's existing evidence conventions. If a
   second board is connected, repeat step 3 on it; if hardware or credentials
   are unavailable, record blocked â€” never a pass.

DONE WHEN: both providers' runs end with `finish_task` accepted after a real
on-board green-check validation (or a recorded, reasoned blocked entry), and no
orphan processes or scratch files remain afterward.

TESTING: this entire prompt IS testing â€” performed only by the two provider
subagents connected to the servers. You orchestrate and evaluate; you do not
drive the boards yourself.

---

## Prompt 14 â€” FINAL TEST 1: adversarial refinement loop

PILLARS: all four.

RULES: the adversarial subagent only criticizes; you independently evaluate and
implement. All re-test execution by subagents in both Codex 5.6 Luna medium and
Sonnet 5 medium. The adversarial subagent itself always runs on Codex
`gpt-5.6-sol` ("5.6 Sol", medium effort) â€” the adversarial-review model, never
5.6 Luna. `Server_A_functionality.md` overrides all other documents.

Run this loop:

1. Launch an adversarial subagent (Codex `gpt-5.6-sol`, medium effort,
   read-only) to evaluate the ENTIRE product â€”
   Server A, the `--share` change, configs, docs, and tests â€” against the four
   pillars: simplicity (anything more complex than its job needs),
   generalizability (any MCU/toolchain/provider assumption), correctness (any
   behavior deviating from `Server_A_functionality.md`), and
   cleanliness/organization (anything tangled, confusing, or hard for an agent
   or human to use). Findings must be concrete: file/line, the ideal behavior
   violated, and why it matters.
2. For every finding, independently assess validity against
   `Server_A_functionality.md` and the spec. Implement fixes ONLY for findings
   you judge valid. Record a disposition for every finding (valid â†’ fix made;
   invalid â†’ reason).
3. Have subagents re-run all affected tests plus the full suite in both
   providers; all must pass.
4. Launch the adversarial subagent (Codex `gpt-5.6-sol`) again on the updated
   product and repeat from step 2.

Terminate the loop only when a full adversarial pass produces no findings at
all, or none that you judge valid (with every rejection reasoned in writing).

DONE WHEN: the final adversarial pass yields zero valid criticisms, the complete
disposition log exists, and the full suite is green in both providers' runs.

TESTING: as embedded above â€” every re-verification is subagent-executed in both
Codex 5.6 Luna medium and Sonnet 5 medium.

---

## Prompt 15 â€” FINAL TEST 2: end-to-end product test from a blank repo

PILLARS: all four â€” this is the product's definition of done.

RULES: subagents do all testing and all board work; you orchestrate, read
results, and evaluate. Both providers are exercised: Codex `gpt-5.6-luna`
(5.6 Luna) medium and Claude `claude-sonnet-5` (Sonnet 5) medium.
`Server_A_functionality.md` defines
correct behavior throughout. Record any unavailable hardware or credential as
blocked â€” never as a pass.

1. Create a fresh, blank repository in a new directory (git init, empty
   workspace). Do not copy any code from the home checkout.
2. Copy ONLY the correct authoritative datasheets for the boards that are
   physically connected from the home repo into the fresh repo (identify the
   connected boards through live inventory first; take whichever datasheet PDFs
   the home checkout holds for exactly those boards).
3. Install/register the product for this fresh workspace with the single
   `byo-pair-register` command and the operator middleman config.
4. **Test Server A first.** For each provider, a subagent acting as Client A
   exercises the full Server A surface board-free: list tools; verify every
   agentic tool is locked and its refusal names its load tool; call all three
   load tools and check each guide (verbatim memory prompts, parameters,
   example call); verify parameter-validation refusals name every violation;
   verify a delta call without retained context is refused; verify the busy
   refusal during a running call.
5. **Then test Server B talking to Server A.** For each provider, run the full
   pair path: board setup/validation through Server B for each connected board,
   then an agentic call whose middleman works through the shared Server B
   endpoint, including a real green check, a permission escalation round-trip
   (permission return â†’ grant â†’ delta "please continue" â†’ validated finish),
   and wrap-up cleanup (no scratch files, no orphan processes).
6. **Full project on real hardware.** Have each provider's subagent use the
   product to write a complete small firmware project from scratch in the fresh
   repo â€” one per connected board, using each board's own toolchain â€” via
   `complex_implementation` (and `complex_task` for at least one multi-step
   plan). Every project must end with a green-check-validated `finish_task`
   proving the implemented behavior on the physical board (UART output,
   symbol/memory value, or breakpoint state as the check demands).
7. Verify on BOTH connected boards. For every step above, capture evidence
   (commands, MCP timelines, prompts/results, green-check outputs) into an
   evidence bundle in the fresh repo, and copy a summary into the home repo's
   `docs/evidence/` per its conventions.

DONE WHEN: both providers complete steps 4â€“7 with all green checks validated on
both physical boards, evidence bundles exist for every step, honest blocked
records exist for anything genuinely unavailable, and the fresh repo afterward
contains only the user's project work plus evidence â€” no leftover scratch, no
orphan processes, nothing persisted by Server A.

TESTING: this entire prompt IS the test â€” executed exclusively by the two
provider subagents connected to the MCP servers, with your own role limited to
orchestration and evaluation.
