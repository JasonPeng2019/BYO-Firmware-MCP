# Design Specification — Guarded Hardware Server ("Server B")

> **Scope note.** This document specifies observable behavior and product requirements only.
> It defines WHAT the finished product must do — never how it is built. Tool names, parameters,
> statuses, and persisted-artifact contents are specified because they are observable by the
> client agent, the user, or external systems. No implementation technology is mandated anywhere
> in this document.
>
> **Terminology.**
> - **Server** — the product specified here: a locally run, tool-exposing service that lets an
>   AI client agent operate embedded development boards on behalf of a human.
> - **Agent** — the AI client agent that calls the Server's tools and converses with the User.
> - **User** — the human firmware developer who owns the workspace, the boards, and all authority.
> - **Server Run** — one continuous execution of the Server, from process start to process end.
>   (The broad design calls this an "MCP Server Live Run.")
> - **Connection** — one live physical attachment (debug probe and/or serial adapter) to one board.
> - **Board profile** — the persisted, portable description of one logical board role.
> - **Gate** — the per-board, in-memory authorization state that must be open before guarded
>   write-capable hardware actions may execute.
>
> **Numbering.** Features are sections 3.1–3.19. Acceptance criterion `AC-n.m` is the m-th
> criterion of feature 3.n. All acceptance criteria are independently testable.

---

## 1. Overview & Goals

### 1.1 What the product is

The Server gives an AI client agent controlled access to embedded development boards: flashing
firmware, reading and writing memory and registers, controlling execution, using serial (UART)
I/O, and recovering locked devices. It is designed so that an agent — even a confused or
adversarially prompted one — cannot render a board permanently unrecoverable, and cannot take
high-risk actions without explicit, specific, current user permission.

### 1.2 Goals

1. **No permanent lockout.** No action available to an ordinary agent workflow can permanently
   brick the attached microcontroller. Actions that could (security/provisioning writes, mass
   erase, destructive recovery) are either unavailable or gated behind explicit per-instance
   user permission.
2. **Reasoned action.** Before any guarded hardware action is available, the agent must submit a
   plan containing a reason, hypothesis, strategy, expected outcomes, and a bounded call budget.
3. **Verified ground truth.** All memory-safety boundaries are established from at least one
   deterministic machine source, and hardware-critical boundaries from two independent sources
   that must agree, before write-capable actions open.
4. **Natural human interaction.** The user only ever converses in ordinary language with the
   agent. The user never sees or supplies structured payloads, internal identifiers,
   continuation tokens, or permission enumerations.
5. **Recoverability and cleanliness.** Every operation is bounded in time, every operation's
   resources are released deterministically on success, failure, cancellation, or shutdown, and
   the board is always left in a state from which the next operation can proceed.
6. **Multi-board correctness.** Multiple boards can be worked on in one Server Run with strict
   per-board isolation: no board ever inherits another board's validation, permission, plan, or
   result.

### 1.3 Non-goals

- The Server is not a network service. It serves exactly one local client with no
  authentication (see §4.3).
- The Server is not a research tool. When outside documentation research is needed, it
  delegates the research to the agent and deterministically validates the returned evidence.
- The Server does not execute arbitrary user- or agent-supplied shell commands, including as
  "cleanup" steps.

---

## 2. Users / Roles / Permissions

### 2.1 Roles

| Role | Description | May do | May never do |
| :--- | :--- | :--- | :--- |
| **User** | Human owner of the workspace and hardware. | Supply board facts (name, exact MCU part number, UART baud rate); resolve physical ambiguity (which probe/port/build); grant and revoke permissions; physically attach/detach hardware. | Is never required to read, write, or approve structured payloads, internal IDs, or field names. |
| **Agent** | AI client operating the Server's tools. | Converse with the user; perform requested documentation research; call tools with structured arguments; relay server-originated questions conversationally. | Write persisted artifacts directly; change the user's stated MCU part number; invent memory partitions; relax prohibited regions; mark unknown memory writable; authorize guarded actions by itself; open any gate. |
| **Server** | The product. | Inventory hardware and workspace; execute hardware actions; validate all agent-supplied facts deterministically; persist artifacts; enforce every gate, plan, budget, and boundary. | Treat tool visibility as authorization; silently rewrite user-authoritative facts; accept arbitrary allowed ranges from the agent. |

### 2.2 Permission model (summary — details in §3.4, §3.5, §3.13, §3.15)

1. **Visibility is not authorization.** A tool that is hidden from the advertised tool list is
   also functionally locked: calling it by name fails until it is legitimately unlocked.
2. **Layered gating.** Every direct hardware action ("Layer 2") is guarded by a corresponding
   plan tool ("Layer 1"). Write-capable Layer 2 actions additionally require an established
   safety map and a validated, fresh session ("Layer 0" / the gate).
3. **User permission scopes.** Where user permission is required, it is one of:
   - `one-time` — covers exactly one accepted underlying call;
   - `full-session` — covers that specific tool for that specific board for the remainder of
     the current Server Run.
4. **Permission is specific and non-transferable.** Approval is bound to one tool, one board,
   and (for destructive recovery) one plan and one set of erase ranges. Prior approvals,
   approvals for other boards, general conversational assent, and agent recommendations never
   substitute.
5. **Everything resets per Server Run.** All plans, unlocks, permissions, gates, and session
   assignments are in-memory only and are lost when the Server Run ends.
6. **Soft-gate honesty.** The Server cannot cryptographically verify that the human really
   approved; it therefore (a) instructs the agent explicitly how and when to ask, (b) requires
   the approval to be passed only through the designated plan-tool parameter, and (c) states in
   its instructions that ordinary conversation is never authorization.

---

## 3. Features

### 3.1 Initialization Handshake & Operating Guidance

**Description.** The Server exposes an initialization handshake action whose published
description tells the agent to call it upon first connecting. Its response is operating guidance
(a "prompt injection" to the agent) that teaches the agent how to drive the Server safely for
the rest of the Server Run.

**States.**
- *Not yet called* — Server behaves identically whether or not the handshake was called; all
  enforcement is independent of it (guidance is advisory, gates are not).
- *Called* — no server-side state change is required; the response is informational.

**Inputs & validation.** No parameters. Any supplied parameters are ignored or rejected with a
clear message.

**Outputs.** A textual guidance document that must include, at minimum:
1. A statement that the Server intentionally hides some hardware-control tools at startup, and
   that the currently visible tool list is authoritative — the agent must not guess, request, or
   call unlisted tools.
2. An index of currently visible tools and an explanation of the plan-tool (`*-plan`) pattern,
   including the all-`NULL` first call rule (§3.4).
3. Startup instructions: ask the user conversationally for a unique familiar name for each
   connected board, or "no board"; never ask the user for structured data, board IDs,
   connection IDs, or permission values.
4. Routing rules: existing profile name → validate; unknown name → set up; incomplete/failed
   profile → repair; ambiguous physical match → present the server-provided friendly choices
   and never silently choose, rename, or rewrite a profile.
5. The rule that normal conversation is never permission, and that approval must be gathered
   clearly and passed only as instructed by the requesting tool.
6. Per-board isolation rules: never reuse another board's validation, approval, plan, or
   result; on disconnect or Server Run end, repeat validation before guarded actions.
7. If no board is connected: do not begin setup, validation, or hardware actions.

**Edge & error cases.**
- Agent skips the handshake: all gates and locks still enforce identically (see AC-3.3).
- Agent calls the handshake repeatedly: same guidance is returned; no side effects.

**Acceptance criteria.**
- **AC-1.1** Calling the handshake returns guidance containing every element listed under
  Outputs (verifiable by inspection of the returned text).
- **AC-1.2** The handshake action is visible and callable at the start of every Server Run
  before any other action has been called.
- **AC-1.3** Calling or not calling the handshake produces no difference in any subsequent
  authorization decision (locked tools remain locked either way).
- **AC-1.4** The guidance explicitly instructs the agent never to expose structured payloads,
  continuation tokens, or internal field names to the user.

---

### 3.2 Server Run Startup: Board Naming & Assignment

**Description.** At the start of every Server Run, before any board-specific work, the user
names the connected boards through ordinary conversation. The Server enumerates each physical
connection separately with friendly descriptions; the agent maps user names to profiles and
connections one-to-one.

**States.**
- *Unassigned* — a connection with no bound board identity. No board-specific action may target it.
- *Provisionally assigned* — a name matched a profile; validation has not yet succeeded.
- *Bound* — validation succeeded; the connection carries the profile's identity for the rest of
  the session (until disconnect or Server Run end).

**Inputs & validation.**
- User supplies (via agent): one unique familiar name per connected board, or "no board."
- The Server rejects: two active connections mapped to one board identity; one connection mapped
  to two identities; proceeding to setup/validation while names and active connections are not
  one-to-one.
- Exact attachment-cache matches (§3.18) may resolve an assignment silently; anything ambiguous
  must be presented to the user as friendly choices.

**Outputs.**
- Per-connection friendly descriptions (probe description, port description, read-only board
  details) suitable for verbatim conversational relay.
- Assignment confirmations and, on mismatch, an instruction to correct the assignment.

**Edge & error cases.**
- *"No board"*: no setup, validation, or hardware action begins.
- *Name matches a profile but hardware does not validate against it*: the Server reports a
  hardware/profile mismatch and asks (via the agent) for a corrected assignment. It never
  rewrites or silently reassigns the profile.
- *Name matches no profile*: the setup flow (§3.7) is offered.
- *Profile exists but recorded setup state is incomplete or failed*: the repair flow (§3.7) is
  offered.
- *Duplicate display names on disk*: the Server refuses to resolve the name and reports the
  conflict.

**Acceptance criteria.**
- **AC-2.1** At every Server Run start, board-specific actions fail until the naming/assignment
  flow has produced a one-to-one mapping for the targeted board.
- **AC-2.2** The Server enumerates every physical connection separately and provides friendly,
  human-relayable descriptions for each.
- **AC-2.3** A user-supplied name matching exactly one existing profile routes to validation
  only — never to first-time setup.
- **AC-2.4** A name matching no profile routes to the setup-plan flow; a profile with recorded
  incomplete/failed setup routes to the repair flow.
- **AC-2.5** Assigning one board identity to two active connections, or two identities to one
  connection, is rejected.
- **AC-2.6** On a reported hardware/profile mismatch, no profile content changes; the only
  path forward offered is correcting the assignment.
- **AC-2.7** All assignments are in-memory: after Server restart, no assignment survives.

---

### 3.3 Layered Action Gating & Tool Visibility

**Description.** Every direct hardware action is guarded. The observable layering is:
- **Layer 0 — Safety establishment:** a safety map of memory regions must exist and be current,
  and the board must be validated, before write-capable actions can run.
- **Layer 1 — Plan tools:** visible `*-plan` tools that collect reasoning and (where required)
  user permission, then unlock their hidden underlying action.
- **Layer 2 — Hardware actions:** the direct actions, each guarded by its Layer 1 tool and
  checked against Layer 0 boundaries.

**States.** For each guarded action, per board: *hidden+locked* (default) → *visible+unlocked*
(active plan with remaining calls) → back to *hidden+locked* (plan exhausted, replaced,
invalidated, or Server Run ended). Hidden is the start state of every Server Run.

**Inputs & validation.** Not applicable directly; this feature constrains all others.

**Outputs.**
- The advertised tool list always reflects current unlock state: always-available tools plus
  any currently unlocked underlying actions.
- Calls to locked tools return a failure that names the corresponding `*-plan` tool (or other
  prerequisite) to use instead.

**Edge & error cases.**
- A client that caches an old tool list and calls a now-relocked tool receives the same failure
  as if the tool were never visible.
- A tool unlocked for board A is still locked for board B: a call carrying board B's identity
  fails.

**Always-available tools** (visible in every Server Run, subject to their own preconditions):
session tools (`connect`, `disconnect`, `get_board_info`, `get_state`), execution tools
(`halt`, `resume`, `step`, `reset_and_run`), read-only register/memory tools
(`read_cpu_register`, `read_execution_state`, `find_symbol`, `read_memory_symbol`),
`remove_breakpoint`, `action_batch`, `wait`, `load_setup_tool`, the setup/validation tools once
loaded (`board_safety_setup`, `board_safety_refresh`, `board_validate`), and every `*-plan`
tool (`board_setup-plan`, `connect_override-plan`, `write_cpu_register-plan`,
`set_execution_state-plan`, `read_memory_address-plan`, `write_memory-plan`,
`set_breakpoint-plan`, `flash_application-plan`, `flash_bootloader-plan`,
`register_write-plan`, `reset_and_halt-plan`, `connect_under_reset-plan`, `target_unlock-plan`,
`read_serial-plan`, `write_serial-plan`).

**Always-guarded actions** (initially hidden and locked; only their `*-plan` is visible):
`board_setup`, `board_fix_setup`, `connect_override`, `write_cpu_register`,
`set_execution_state`, `read_memory_address`, `write_memory`, `set_breakpoint`,
`flash_application`, `flash_bootloader`, `register_write`, `reset_and_halt`,
`connect_under_reset`, `target_unlock`, `read_serial`, `write_serial`.

**Acceptance criteria.**
- **AC-3.1** At Server Run start, every always-guarded action is absent from the tool list AND
  returns a failure if called by name.
- **AC-3.2** After a valid plan is accepted, the underlying action appears in the tool list and
  is callable for exactly that board with exactly the planned parameters.
- **AC-3.3** Unlocking is never achieved by tool-list manipulation alone: a hidden tool called
  directly (bypassing the list) still fails until its plan/permission conditions are met.
- **AC-3.4** When a plan is exhausted, replaced, or invalidated, its underlying action is
  removed from the tool list (unless another active plan exposes it) and calls to it fail.
- **AC-3.5** All unlocks reset when the Server Run ends: a new Server Run starts with the
  default visibility and lock state.
- **AC-3.6** A failure returned for a locked action names the prerequisite (`*-plan` tool,
  setup, validation, or permission) required to proceed.

---

### 3.4 Plan Tools (Layer 1 Guardrails)

**Description.** Each guarded action has a `*-plan` tool that collects structured reasoning
before the action is exposed. Plans are immutable, bound to exact parameters, budgeted, and
per-board.

**States.** Per plan tool, per board, per Server Run:
- *Uninitialized* — the all-`NULL` call has not yet occurred; populated plans are rejected.
- *Initialized* — all-`NULL` call made; a populated plan may be submitted.
- *Active plan* — a valid plan exists with remaining calls; underlying tool unlocked.
- *Closed* — plan exhausted, replaced, invalidated (board/session change, revocation), or
  Server Run ended.

**Inputs & validation.**
1. **All-`NULL` first call:** the first call in a Server Run to any `*-plan` tool must have
   every parameter `NULL`. The Server rejects a populated plan until this call has occurred for
   that plan tool in the current Server Run. (This is the only call exempt from the
   board-identity requirement.)
2. **Populated plan call** must include, correctly formatted:
   - board identity;
   - `hypothesis` (text) with `hypothesis_made = true`;
   - `strategy` (text) with `strategy_evaluated = true`;
   - `expected_fail_return` and `expected_success_return`;
   - `max_calls` and `max_calls_buffer`;
   - all parameters of the underlying action, fixed exactly;
   - `user_permission`, where the tool is permission-locked (§3.5).
3. The flag fields must be `true` and the corresponding text fields non-empty; the Server
   rejects plans with flags set but empty/placeholder reasoning fields.
4. For **fixed-budget actions**, the Server rejects any plan whose budget differs from
   `max_calls = 1, max_calls_buffer = 0`. Fixed-budget actions: `board_setup`,
   `board_fix_setup`, `write_cpu_register`, `set_execution_state`, `write_memory`,
   `set_breakpoint`, `flash_application`, `flash_bootloader`, `register_write`,
   `target_unlock`.
5. **Multiple-call actions** may request larger budgets: `connect_override`,
   `read_memory_address`, `reset_and_halt`, `connect_under_reset`, `read_serial`,
   `write_serial`. (Budget ceilings: see Assumption A-9.)

**Outputs.**
- All-`NULL` response: the underlying tool's purpose; every required plan field and what it must
  contain; the underlying action's own parameters; whether the budget is fixed `1,0` or
  flexible; any user-permission requirement (and whether full-session permission is already
  active, §3.5); any extra instructions.
- Valid-plan response: a plan identifier, the underlying tool's name, the total permitted calls
  (`max_calls + max_calls_buffer`), and an instruction redirecting the agent to call the
  underlying tool.
- Invalid-plan response: which fields are missing/invalid and what a corrected call requires.

**Edge & error cases.**
- Submitting a second valid plan for the same underlying tool and board atomically closes and
  replaces the first; there is never a moment with two active plans for one tool+board.
- Plans are never edited: any change to any plan field or underlying parameter requires a
  complete new plan call.
- Every accepted underlying call consumes one call from the budget — including calls that
  fail, time out, are cancelled after starting, or return nothing useful. Calls rejected
  before execution begins consume nothing.
- The remaining-call count decrements exactly once per accepted call, at the moment execution
  begins, with no double-spend under concurrency.
- Before executing any guarded action, the Server verifies all of: an active plan exists; the
  plan names that exact tool; the supplied board identity matches the plan; the call's
  parameters exactly match the plan's declared parameters; the plan belongs to the current
  Server Run; remaining calls > 0; the board assignment and session are still valid; any
  required user permission is active; and all Layer 0/2 safety checks pass. Any failure blocks
  execution with a reason.

**Acceptance criteria.**
- **AC-4.1** A populated plan submitted before the all-`NULL` call for that plan tool in the
  current Server Run is rejected with instructions to make the all-`NULL` call.
- **AC-4.2** The all-`NULL` response contains every element listed under Outputs.
- **AC-4.3** A plan missing any required field, or with `hypothesis_made`/`strategy_evaluated`
  not `true`, or with empty reasoning text, is rejected.
- **AC-4.4** A plan for a fixed-budget action with any budget other than `1,0` is rejected.
- **AC-4.5** A valid plan unlocks exactly one underlying tool for exactly one board, and the
  response includes plan identifier, underlying tool name, total calls, and a redirect
  instruction.
- **AC-4.6** Calling the underlying tool with any parameter differing from the plan is rejected
  and consumes no budget.
- **AC-4.7** An underlying call that starts and then fails or is cancelled still consumes one
  budgeted call.
- **AC-4.8** When the budget reaches zero, the underlying tool relocks and further calls fail;
  a replacement plan is required.
- **AC-4.9** Submitting a new valid plan for the same tool+board closes the prior plan; calls
  under the prior plan's parameters fail thereafter.
- **AC-4.10** No plan survives a Server restart.

---

### 3.5 User Permission (Permission-Locked Plan Tools)

**Description.** Some plan tools additionally require user permission before they unlock their
underlying action: `board_setup-plan`, `set_execution_state-plan`, `flash_bootloader-plan`, and
`target_unlock-plan` when the recovery is destructive.

**States.** Per permission-locked tool, per board, per Server Run:
- *No permission* — populated plans without valid permission return a request for permission.
- *One-time granted* — exactly one accepted underlying call is covered, then permission is
  consumed.
- *Full-session granted* — permission persists for that tool+board until the Server Run ends or
  the user revokes it.

**Inputs & validation.**
- `user_permission` accepts exactly `one-time` or `full-session` (or `NULL` when full-session
  permission is already active for that tool+board).
- `one-time` permission requires the plan budget `max_calls = 1, max_calls_buffer = 0`.
- A missing, `NULL`, or invalid permission value without active full-session permission causes
  the plan call to fail with instructions to obtain valid permission and correct the fields.

**Outputs.**
- When full-session permission is already active, subsequent all-`NULL` responses for that tool
  state this and state that `user_permission` may be left `NULL`.
- Permission-request responses instruct the agent to ask the user clearly, in ordinary
  language, and to pass the resulting approval only via the plan tool.

**Edge & error cases.**
- Full-session permission removes only the repeated permission prompt; every underlying use
  still requires an active plan with all other checks.
- Permission granted for tool X on board A never applies to tool X on board B, nor to tool Y
  anywhere.
- User revocation (communicated by the agent) immediately closes the permission and relocks
  affected tools.
- The Server's instructions must state that general conversational assent is not authorization;
  only the structured permission value passed through the plan tool counts.
- Mass erase is excluded from full-session coverage: it requires fresh permission every time
  (§3.15), regardless of any prior grant.

**Acceptance criteria.**
- **AC-5.1** A permission-locked plan with no valid permission and no active full-session grant
  is rejected with a permission request.
- **AC-5.2** `one-time` permission with a plan budget other than `1,0` is rejected.
- **AC-5.3** After one accepted underlying call under `one-time` permission, the next attempt
  requires a new plan with new permission.
- **AC-5.4** After `full-session` is granted for tool+board, a later valid plan with
  `user_permission = NULL` succeeds for that tool+board, and the all-`NULL` response discloses
  the active grant.
- **AC-5.5** Full-session permission for one tool+board has no effect on any other tool or
  board.
- **AC-5.6** All permissions are void after the Server Run ends.
- **AC-5.7** Mass-erase operations request fresh user permission on every occurrence, even
  under an existing full-session grant.

---

### 3.6 Board Profiles & Persisted Artifacts

**Description.** Each logical board role is persisted as a portable board profile in the
project's artifact store. Profiles represent roles/configurations, not immutable physical
devices: a compatible replacement board may validate into an existing profile.

**States.** Per profile: *absent* → *core committed* (identity + connectivity facts verified) →
*safety-complete* (safety map committed and referenced) → any of these plus *stale* (recorded
setup incomplete/failed, or fingerprints drifted).

**Inputs & validation.**
- A profile stores: a stable machine-facing board identifier; a unique user-facing display
  name; the exact user-supplied MCU part number; the derived MCU family; probe family and
  probe type; the resolved debug-target identifier; the UART baud rate; probe/serial matching
  hints; and optional validated fields (safe test-read address, silicon-identity definition,
  expected UART output substring).
- The profile's storage name must correspond to its internal board identifier; the Server
  rejects a profile whose stored name and internal identifier disagree.
- Display names must be unique across profiles; board identifiers are stable once created even
  if the display name later changes.
- The user's exact MCU part number is authoritative and is never silently replaced — including
  never being changed to match an unexpectedly detected device.
- Device-support package metadata (source location, file name, version, integrity checksum,
  provided targets) lives in exactly one separate manifest artifact; profiles contain no
  package identifiers.

**Outputs.**
- Persisted artifacts, all project-local: board profiles; the device-support manifest and its
  staged files; per-attempt setup reports and logs; per-board safety maps, source manifests,
  and safety reports; per-attempt validation reports and logs; and a host-local attachment
  cache (§3.18) that is excluded from version control.
- Commit ordering is observable: core profile fields are committed only after target support
  and a live connection succeed; optional enrichment fields only after live validation; the
  safety-map reference only after Safety Setup completes.

**Edge & error cases.**
- The agent can never create, modify, or delete persisted artifacts directly; only Server
  actions do, after deterministic validation.
- No gate state is ever persisted: artifacts on disk never restore an open gate (§3.13).
- A profile whose display name is changed keeps its board identifier and all committed facts.
- Failed candidate values (targets, packs, enrichment) are never committed to profiles; they
  are recorded in reports with the evidence and observed output.

**Acceptance criteria.**
- **AC-6.1** A profile artifact whose storage name does not match its internal board identifier
  is rejected wherever it is read, with a clear error.
- **AC-6.2** Creating a second profile with an existing display name is rejected.
- **AC-6.3** After a successful live connection, the core profile fields listed above are
  persisted; before it, none are.
- **AC-6.4** Optional enrichment fields appear in a profile only after their live validation
  passed; failed candidates appear only in reports.
- **AC-6.5** No sequence of agent tool calls can result in a persisted artifact containing an
  agent-supplied value that the Server did not deterministically validate.
- **AC-6.6** The MCU part number in a committed profile always equals the value the user
  supplied (or the known-board definition they selected), regardless of any research or
  detection outcome.
- **AC-6.7** Device-support package metadata appears in exactly one manifest artifact and in no
  profile.

---

### 3.7 Board Setup & Repair

**Description.** First-time setup creates a profile and complete safety map for one intended
connection. Repair resumes an incomplete or failed setup at its first unverified phase. Both are
guarded by `board_setup-plan` (permission-locked). Setup collects only facts a firmware
developer plausibly knows; everything else is resolved from local hardware/workspace inventory
or delegated to agent research (§3.8).

**States.** Per setup workflow: *planned* → *in progress (phase N)* → one of the terminal
statuses: `setup_completed`, `setup_needs_user_input`, `setup_research_required`,
`setup_blocked`, `setup_unresolved`, `setup_connection_failed`, `setup_validation_failed`,
`setup_safety_incomplete`.

**Inputs & validation.**
- From the user (conversationally): board name; exact MCU part number; UART baud rate;
  selections when probes/ports/build configurations are ambiguous; confirmation when an
  external serial adapter cannot be provably mapped.
- The user is never asked for debug-target identifiers, register ranges, flash geometry, or
  protected addresses.
- A setup plan is scoped to one intended connection and one logical profile (new name +
  generated identifier, or existing identifier in repair mode) and identifies setup vs. repair
  mode.
- One valid setup plan permits exactly one `board_setup` call and one `board_fix_setup` call.
- **Deterministic preflight**, before any research: inventory user input, connected probes and
  serial ports, attachment-cache matches, available built-in and manifest-pinned targets,
  auto-detected target, and discovered build/link artifacts. Deterministic outcomes:

| Condition | Result |
| :--- | :--- |
| No probe present | `setup/no-probe`; no research |
| Multiple probes | Conversational user selection |
| Required serial port absent | `setup/no-uart`; no research |
| Multiple serial ports | Exact cache match, else conversational selection |
| External adapter unmappable | Conversational confirmation, then cached |
| Multiple build configurations | Conversational selection of the intended one |
| Exactly one exact target detected | Use it (research optional, enrichment only) |
| No exact target detected | Research prompt (§3.8, §3.9) |

**Outputs.**
- Structured statuses (above) with an agent-facing prompt explaining how to proceed or what to
  say to the user — never exposing internal payloads to the user.
- A structured report persisted for every attempt containing: inventories, selected hardware,
  cache outcome, target/package resolution, research exchanges, candidate validation results,
  connection results, safety sources, fingerprints, and terminal status.

**Edge & error cases.**
- If `board_setup` fails or is incomplete, the same plan's single `board_fix_setup` call
  remains available for the first repair attempt without re-asking the user — even under
  one-time permission.
- Any further setup or repair attempt requires a replacement setup plan: under one-time
  permission the user must be asked again first; under full-session permission it proceeds
  without re-prompting, within the deterministic retry limits (Assumption A-10).
- Setup/repair authorization also closes on: disconnect of the scoped connection, user
  revocation, or completion/cancellation of the workflow.
- Repair resumes at the first unverified phase and re-runs current hardware preflight before
  trusting any recorded state; it never trusts old inventory as current fact and never blindly
  resumes a previous hardware operation. Per-phase repair behavior:

| Failed phase | First step | Agent may provide |
| :--- | :--- | :--- |
| Input | Ask user conversationally | The missing user fact |
| Preflight | Re-enumerate hardware | Nothing |
| Probe/serial selection | Present friendly choices | The user's choice |
| Build-configuration selection | Rediscover artifacts | The intended configuration |
| Target resolution | Retry exact detection | One target candidate |
| Target support/package staging | Recheck support | One materially different package candidate |
| Connection | Reconnect | A revised part-consistent target only |
| Validation | Rerun failed optional checks | The requested optional fields |
| Safety research | Reload local support data and evidence | Missing official-document facts |
| Safety map | Rebuild and compare | No arbitrary ranges |
| Commit | Retry the atomic write | Nothing |

- When setup completes: validation follows automatically in the flow, and both `board_setup`
  and `board_fix_setup` relock behind a new plan.
- A deterministic blocked/unresolved result or retry-budget exhaustion stops the workflow with
  that status; the agent is instructed to stop rather than loop.

**Acceptance criteria.**
- **AC-7.1** `board_setup` and `board_fix_setup` are callable only after a valid, permitted
  setup plan, and at most once each per plan.
- **AC-7.2** With no probe attached, setup terminates deterministically with the no-probe
  status and no research prompt is issued.
- **AC-7.3** Every ambiguity in the preflight table resolves through the stated deterministic
  or conversational path; the Server never guesses among multiple probes, ports, or build
  configurations.
- **AC-7.4** After a failed `board_setup`, one `board_fix_setup` succeeds or fails under the
  same plan without any new user prompt; the second repair attempt is impossible without a
  replacement plan.
- **AC-7.5** Every setup or repair attempt — successful or not — produces a persisted
  structured report with all listed content.
- **AC-7.6** Setup completion relocks both setup actions and is followed by validation before
  any gate opens.
- **AC-7.7** Repair never skips re-verification: recorded phase results are re-checked against
  live preflight before being reused.

---

### 3.8 Agent Research Handoff

**Description.** When a fact requires outside documentation research, the Server does not
perform the research. It returns a self-contained research prompt; the agent researches and
retries the tool with the requested fields. The Server deterministically validates every
returned candidate.

**States.** Per unresolved fact: *research requested* → *candidate staged* → *validated and
accepted* or *rejected and recorded* → (on repeat) *research requested with prior failures
included* → possibly *budget exhausted → blocked/unresolved*.

**Inputs & validation.**
- The agent may return only the fields the prompt requested:
  - target research → the target identifier only;
  - package research → one complete manifest candidate;
  - validation research → the requested test-read/silicon-identity fields only;
  - safety research → official-document facts needed for comparison with locally loaded
    device-support data.
- Extra, changed, or out-of-scope fields cause rejection.
- The agent may never: change the exact MCU part; invent memory partitions; relax prohibited
  regions; mark unknown memory writable; authorize hardware actions; persist state; or open a
  gate — regardless of research content.
- Each candidate is fingerprinted; a candidate identical to a previously failed one is rejected
  without re-validation, and the next prompt demands a materially different candidate.

**Outputs.** A research response contains everything the agent needs without hidden server
state: a status, a continuation token, an agent-facing prompt stating the unresolved fact and
the authoritative board/MCU facts, relevant observed output, previously rejected candidates and
their failures, acceptable sources, the exact response fields expected, the fields the agent
must not change, the Server's validation plan, and whether the user must be asked anything
(default: no).

**Edge & error cases.**
- Research never authorizes any write, erase, unlock, security-state change, or MCU identity
  change; execution authority flows only through plans and permissions.
- Conditions research cannot solve — locked targets, missing host drivers, vanished probes —
  never produce research prompts; they produce their own blocked statuses.
- Retry budgets are tracked per fact; exhaustion yields a deterministic
  blocked/unresolved status (Assumption A-10).

**Acceptance criteria.**
- **AC-8.1** Every research-required response contains all elements listed under Outputs.
- **AC-8.2** A research reply containing fields not requested by the prompt is rejected.
- **AC-8.3** A research reply attempting to alter the MCU part number is rejected and the part
  number remains unchanged everywhere.
- **AC-8.4** A byte-identical resubmission of a previously failed candidate is rejected without
  a fresh validation attempt, and the subsequent prompt lists the prior failure.
- **AC-8.5** No research acceptance, by itself, changes any gate, unlock, or permission state.
- **AC-8.6** A locked target or missing probe produces a blocked status, not a research prompt.

---

### 3.9 Target & Device-Support Resolution

**Description.** The Server resolves the debug-target identifier deterministically when
possible; otherwise the agent supplies one evidence-backed candidate. If the target is not
supported by built-in support or the pinned manifest, the agent may supply one official
device-support package candidate, which the Server stages and verifies before adoption.

**States.** *Auto-detected exact* → committed after live connection. *Candidate proposed* →
*staged* → *live-verified* → committed; or *rejected* → recorded → new materially different
candidate required.

**Inputs & validation.**
- Target candidate: exactly one identifier plus evidence entries (source + claim) and a
  reasoning summary. Must be syntactically valid and consistent with the user's part number.
- The Server verifies the target is exposed by built-in support or by a staged/pinned package,
  and requires a successful live connection before committing it.
- Package candidate: one complete official candidate. The Server stages it, computes its
  integrity checksum, compares against an officially published checksum when one is available,
  enumerates the targets it provides, requires the requested target to be present, connects
  using the staged package, and promotes it into the manifest only after validation.

**Outputs.** Continue/blocked/research statuses per the support state:

| Support state | Behavior |
| :--- | :--- |
| Target built into the Server's device support | Continue |
| Target supplied by the currently pinned manifest | Continue |
| Target unavailable in both | Package-research prompt |

**Edge & error cases.**
- A failed package candidate is recorded together with the observed target-listing output; the
  next prompt requires a materially different candidate.
- A candidate target inconsistent with the user's part number is rejected without any hardware
  attempt.
- Optional enrichment (test-read address, silicon-identity definition) may be researched only
  after the target exists. A test-read candidate must classify as safely readable in the safety
  map and succeed against live hardware. A silicon-identity candidate must satisfy
  `(actual_value & mask) == (expected_value & mask)` at the requested width. Absence of
  enrichment never blocks core setup when required identity checks can otherwise complete.

**Acceptance criteria.**
- **AC-9.1** When exactly one exact target is auto-detected, no target research prompt is
  issued.
- **AC-9.2** A proposed target is committed to the profile only after a successful live
  connection using it.
- **AC-9.3** A staged package failing checksum comparison (when an official checksum exists) is
  rejected and not promoted.
- **AC-9.4** A staged package that does not expose the requested target is rejected with the
  observed target listing recorded.
- **AC-9.5** A failed enrichment candidate is absent from the profile and present in the report
  with evidence, observed output, and candidate fingerprint.
- **AC-9.6** Core setup can complete without optional enrichment when required identity checks
  pass.

---

### 3.10 Safety Map Construction (Safety Setup)

**Description.** `board_safety_setup` builds the per-board safety map — the region/type
reference used to classify every hardware request. It is required during first-time setup and
after anchor changes. Until it completes, write-capable actions remain unavailable or locked
behind their plan tools.

**States.** Per board: *no map* → *building* → one of `safety_setup_completed`,
`safety_setup_needs_user_input`, `safety_setup_research_required`, `safety_setup_incomplete`,
`safety_setup_conflict`, `safety_setup_blocked`. Any non-complete status keeps affected actions
closed.

**Inputs & validation — source authority.** Facts retain explicit source ownership; sources are
never interchangeable:
- **User-owned:** board selection/name, exact MCU part number, UART baud rate, physical
  selections, intended build configuration. Nothing else.
- **Build-configuration-owned:** application flash partition; user-bootloader flash partition;
  application and bootloader RAM allocations; entry point, vector table, and loadable firmware
  segments. These come from the project's authoritative build/link artifacts; the agent may
  help the user choose a configuration but must not invent partition addresses. Physical device
  data never substitutes for the project's partitioning.
- **Doubly verified hardware facts** — each requires two independently obtained sources that
  must agree (deterministically loaded device-support data + agent-supplied official
  datasheet/reference-manual facts): prohibited persistent configuration/security regions
  (option bytes, provisioning, one-time-programmable, lifecycle, protection,
  debug-authentication and equivalents); CPU and system-control register ranges; peripheral
  register windows; physical flash and RAM geometry; ROM/system bootloader ranges; erase
  page/sector geometry.
- The comparison checks: exact device variant, addresses, aliases, region type, bank
  boundaries, register-block identity, support-data version, and document revision. A
  representation difference may be accepted only when it can be deterministically reconciled.
- Conflicts are recorded and the affected actions remain closed.

**Outputs.**
- A persisted safety map recording, per region: name, kind (prohibited registers, ROM
  bootloader, bootloader flash, application flash, RAM, peripheral window, etc.), start/end,
  and how it was verified or derived.
- Per-source fingerprints and an aggregate fingerprint covering: board profile, part/target,
  device-support data, datasheet evidence, application build artifacts, bootloader build
  artifacts, flash geometry, and the map schema.
- The listed statuses, each with an agent-facing prompt (ask-the-user guidance for ambiguous
  builds; a research prompt for missing datasheet facts).

**Edge & error cases.**
- Prohibited classifications override any broader peripheral/flash/writable classification of
  the same addresses.
- Unknown (unmapped) memory is denied by default for every request type.
- The Server never accepts agent-supplied "allowed ranges" for a request; the map supplies the
  ranges and the agent selects a named operation.
- Partition-versus-prohibited overlap checks run whenever the map is built or rebuilt; overlap
  is a conflict.

**Acceptance criteria.**
- **AC-10.1** Before `safety_setup_completed`, every write-capable hardware action for that
  board fails closed.
- **AC-10.2** A doubly verified region is accepted only when both sources agree (or reconcile
  deterministically); a disagreement yields `safety_setup_conflict` and the affected actions
  stay closed.
- **AC-10.3** Application and bootloader partitions in the map always come from the selected
  build/link artifacts, never from device documentation or agent assertion.
- **AC-10.4** An address inside a prohibited region is denied for every request type even if it
  also falls inside a peripheral, flash, or RAM classification.
- **AC-10.5** A request touching any address absent from the map is denied.
- **AC-10.6** The persisted map contains per-source fingerprints and an aggregate fingerprint,
  and records each region's verification/derivation provenance.

---

### 3.11 Configuration Freshness & Safety Refresh

**Description.** The Server separates rarely changing hardware identity from frequently
changing configuration inputs. Every write-capable guarded action cheaply checks both: (1) the
board was hardware-validated in the current active session, and (2) current inputs still hash
to the stamped aggregate fingerprint. `board_safety_refresh` handles routine configuration
drift without a full rebuild.

**States.** Per board: *fresh* (both checks pass) → *config-stale* (fingerprint mismatch; gate
closed) → refreshed and re-stamped, or escalated to full safety setup; *hardware-stale*
(disconnect/restart/target change) → requires re-validation (§3.12).

**Inputs & validation.** Refresh takes the board identity; it compares per-source
fingerprints, identifies the changed source groups, rebuilds only affected map regions, re-runs
partition-versus-prohibited overlap checks, produces a new aggregate fingerprint, and re-stamps
the active session when hardware validation is still intact.

**Outputs.** `safety_refresh_completed`, `refresh_scope_unclear` (directs the agent to run full
`board_safety_setup`), `safety_conflict`, `safety_refresh_blocked`.

**Edge & error cases / routing.**

| Change | Required action |
| :--- | :--- |
| Firmware rebuilt; build/link artifacts changed | `board_safety_refresh` |
| Device-support package re-pinned / checksum changed | `board_safety_refresh` |
| Datasheet evidence changed | `board_safety_refresh` (revalidates affected regions) |
| Part number or target changed | `board_safety_setup`, then `board_validate` |
| Flash geometry, major partition model, or map schema changed | `board_safety_setup` (validate when the hardware anchor is invalidated) |
| Fingerprint mismatch of unknown scope | `board_safety_refresh`; on `refresh_scope_unclear`, `board_safety_setup` |

- A configuration-only change never requires physically reconnecting while hardware validation
  remains intact; refresh alone can re-stamp and reopen the session gate.
- Refresh can never reopen a gate lost to disconnect, Server restart, target change, or any
  loss of hardware validation — those require `board_validate`.

**Acceptance criteria.**
- **AC-11.1** A change to any fingerprinted source closes the affected board's gate before the
  next write-capable action executes.
- **AC-11.2** After a build-artifact-only change, a successful refresh reopens the gate without
  any reconnection or re-validation, and only build-derived regions are rebuilt.
- **AC-11.3** A part-number or target change makes refresh insufficient: the gate reopens only
  after full safety setup and validation.
- **AC-11.4** `refresh_scope_unclear` never reopens the gate; it directs to full safety setup.
- **AC-11.5** After a disconnect, no sequence of refresh calls reopens the gate without a
  successful `board_validate` on the reconnected assignment.
- **AC-11.6** Freshness checks run on every write-capable guarded call, and a stale result
  blocks the call with the applicable required action named.

---

### 3.12 Board Validation & Session Gate

**Description.** `board_validate` is the repeatable per-connection board-health gate. It
verifies that one assigned connection is compatible with its profile — profile compatibility,
not immutable device identity — and on success binds the connection to the profile's name and
identifier for the session and opens only that board's gate. It never recreates profiles or
rebuilds maps.

**States.** Per connection: *unvalidated* → *validating* → *validated & gate open* (stamped with
board identity, live hardware result, probe identity, current aggregate fingerprint) → cleared
on disconnect/Server Run end/anchor change.

**Inputs & validation.** Board identity (with its provisional assignment). Default validation
is safe and non-destructive, and observably performs:
1. Load the profile and its matching safety map/fingerprints.
2. Re-enumerate probes and serial ports.
3. Resolve hardware through current inventory and the attachment cache.
4. Confirm target support through built-in support or the pinned manifest.
5. Connect and verify live MCU identity.
6. Perform configured safe test-memory and silicon-identity checks.
7. Open the serial port and, when configured, require the expected output substring within a
   bounded capture.
8. Confirm the safety map is complete and internally consistent.
9. Stamp the connection/session and open only that board's gate.

**Outputs.**

| Result | Meaning |
| :--- | :--- |
| `validation_passed` | All checks passed; session gate opened |
| `validation_passed_uart_not_configured` | Hardware passed; no expected serial behavior configured |
| `validation_needs_user_input` | Physical selection ambiguous; agent asks conversationally |
| `validation_research_required` | Requested validation metadata needs research |
| `validation_blocked` | Locked target, missing backend/driver, unavailable package, or invalid safety state |
| `validation_failed` | A configured check ran and failed |
| `validation_incomplete` | Required profile/safety data absent |

Every validation attempt persists a structured report and log.

**Edge & error cases.**
- Ordinary validation never installs packages, flashes firmware, or performs recovery; those
  are separate actions with their own plan rules.
- A compatible replacement board validating into an existing profile assumes that profile's
  name and identifier for the current session only.
- A locked target is reported as blocked; unlock is a separate plan-guarded action (§3.15),
  after which validation must run again.
- Validation of one board never opens, stamps, or affects any other board's gate.

**Acceptance criteria.**
- **AC-12.1** A successful validation opens exactly one gate — the validated board's — and
  stamps it with that connection's identity and the current aggregate fingerprint.
- **AC-12.2** Each of the seven result statuses is produced under its stated condition and no
  other.
- **AC-12.3** Validation makes no persistent changes to profiles or maps and performs no write,
  flash, or recovery operation on the board.
- **AC-12.4** Every validation attempt, pass or fail, persists a report and log.
- **AC-12.5** With a live MCU identity mismatch, validation fails and the profile remains
  unmodified; the only offered remedies are correcting the physical assignment or attaching the
  correct board.

---

### 3.13 Write-Gate Lifecycle

**Description.** Each board's gate is session- and connection-bound, starts closed, closes
automatically on defined events, and has no standalone "open" command. Only the responsible
successful validation, safety refresh, or safety setup + validation sequence reopens the
affected board's gate.

**States.** Per board+connection: *closed* (default at every Server Run start and connection
start) ↔ *open* (post-validation, fresh).

**Inputs & validation.** None directly; gate transitions are side effects of other features.

**Outputs.** Guarded calls blocked by a closed gate return the specific required action
(validate, refresh, safety setup, setup/repair), per the routing below.

**Edge & error cases — closure and reopening matrix.**
- *Never opened (setup-side):* no profile match → full setup-plan flow, setup, paired fix if
  needed, then validate. Recorded failed/incomplete core setup → repair flow. Core committed
  but safety incomplete → safety setup, then validate. Safety
  research/incomplete/conflict/blocked → resolve the reported cause, continue safety setup.
- *New session:* new Server Run → re-name, re-assign, validate each board. Disconnect or
  connection termination → immediately clear that connection's assignment, validation stamp,
  and gate; after reconnect + reassignment, validate before reopening. Same board replugged or
  compatible replacement → assign intended profile and validate (cache may resolve the port
  silently).
- *Configuration drift:* per the routing table in §3.11.
- *Hardware/identity:* live MCU mismatch → attach the correct board and validate. Locked
  target → unlock (§3.15), then validate. Backend/probe unavailable → restore hardware/host,
  then validate.
- Persisted artifacts never restore an open gate. The gate is never persisted as open.

**Acceptance criteria.**
- **AC-13.1** At every Server Run start and for every new connection, all gates are closed.
- **AC-13.2** There exists no tool call whose sole function is "open gate."
- **AC-13.3** Disconnecting a board immediately closes its gate and clears its assignment and
  validation stamp, even mid-workflow.
- **AC-13.4** Restarting the Server with all artifacts intact still requires naming, assignment,
  and validation before any guarded write on any board.
- **AC-13.5** Each closed-gate rejection names the correct reopening path per the matrix above.

---

### 3.14 Hardware Actions (Layer 2)

**Description.** The direct board actions. Every Layer 2 action: requires the target board's
identity as a parameter; acts only on that board's assigned connection; validates only what its
own operation requires against the safety map; and returns its result together with a reminder
to exit the hardware layer safely. Write-capable actions additionally require the open, fresh
gate (§3.11, §3.13). The general rule: **the actual target of a request must match the category
claimed by the action** — a caller cannot make an address into a register, or application space
into bootloader space, by labeling it so.

**States.** Each action is *locked* or *unlocked* per §3.3/§3.4. The board's core is observably
`RUNNING`, `HALTED`, or `RESET`-related as reported by `get_state`.

**Inputs & validation, outputs, and edge cases — by group:**

#### 3.14.1 Session & information (always available)
- `connect(...)` — opens a persistent debug session. Optional manual identifiers (probe unique
  ID, target override, board identity, external board configuration) follow documented
  precedence with environment fallbacks.
- `disconnect()` — closes the active session and releases the probe.
- `get_board_info()` — returns the active board's target, MCU/probe family, recovery policy,
  silicon-identity expectation, UART baud rate, and configured safe test address.
- `get_state()` — returns the core state.
- `connect_override` (guarded, multi-call) — accepts manual values (probe unique ID, target,
  board identity, external board configuration) for exceptional connections; these values never
  silently rewrite persistent profile state.

#### 3.14.2 Execution control
- Always available: `halt()`, `resume()`, `step()` (returns the new program counter),
  `reset_and_run` (reset; execute from the reset vector; session stays active).
- Guarded (multi-call plans): `reset_and_halt` (reset and halt immediately at startup),
  `connect_under_reset` (assert physical reset, attach while reset is active, halt, release —
  requires probe reset-line support; fails with a clear message when unsupported).
- None of the reset actions unlocks a locked target; they only reset (§3.15 handles unlock).

#### 3.14.3 CPU registers
- `read_cpu_register(name)` / `read_execution_state(name)` — always available reads.
- `write_cpu_register(name, value)` — guarded, fixed `1,0`. Allowed registers: the ordinary
  general-purpose and floating-point registers (R0–R12 class) only.
- `set_execution_state(name, value)` — guarded, fixed `1,0`, **user permission required**.
  Allowed registers: those that change control flow or CPU mode (program counter, stack
  pointers, link register, status/control/interrupt-mask registers and equivalents).
- The supported register set is determined from the connected core at runtime; unknown or
  unsupported register names are rejected. Values accepted in hexadecimal (`0x...`) or decimal.
- Registers relating to security, provisioning, or nonvolatile configuration are prohibited in
  both tools.

#### 3.14.4 Memory access
- `find_symbol(query)` — always available; searches the firmware's symbol/debug metadata.
- `read_memory_symbol(symbol, ...)` — always available; resolves the symbol from the firmware
  metadata and reads that variable.
- `read_memory_address(address, width/length, ...)` — guarded, multi-call. Reads a value or a
  bounded block; block length must be positive and within the documented cap (Assumption A-12).
- `write_memory(symbol-or-address, value, width, allow_address_fallback?, reason?)` — guarded,
  fixed `1,0`. Behavior:
  1. Given a symbol: resolve it from the firmware metadata and write that variable; the
     resolved region must match the requested write type.
  2. Given a raw address without the explicit fallback flag: reject with guidance — "Try a
     symbol first. Provide a symbol name or explicitly request address fallback."
  3. Raw-address writes require the fallback flag, a brief reason symbols are unsuitable, and
     are limited to mapped RAM with full containment.
  - The tool's published description instructs: prefer symbol access whenever source or debug
    metadata identifies the variable; use raw addresses only for dynamically allocated,
    pointer-derived, stack, optimized-out, or otherwise unsymbolized memory.
- Transfer widths accepted: 8, 16, or 32 bits. Prohibited and unknown regions are rejected for
  all memory operations.

#### 3.14.5 Peripheral register writes
- `register_write(address, mask, value)` — guarded, fixed `1,0`. The agent is expected to have
  confirmed the register's name, address, and field against official documentation and submit
  the exact address, mask, and value. The complete affected range must lie inside a mapped
  peripheral-register window and outside every prohibited security/provisioning subrange.
  Writes touching flash security, option bytes, one-time-programmable, debug protection, or
  lifecycle configuration are unavailable in ordinary workflows.

#### 3.14.6 Flashing
- `flash_application(artifact, ...)` — guarded, fixed `1,0`. The Server parses the firmware
  artifact and requires: all loadable flash segments and any explicit target address fit
  entirely inside the build-derived application partition; required erase sectors fit entirely
  inside that partition; the entry point and vector table lie in the expected application
  range; the live MCU identity matches the profile. Bootloader, prohibited, ROM-bootloader, and
  unknown regions are rejected. Requires current session validation and fingerprint freshness.
- `flash_bootloader(artifact, ...)` — guarded, fixed `1,0`, **user permission required** via
  `flash_bootloader-plan`. Same checks against the build-derived bootloader partition;
  application, prohibited, ROM-bootloader, and unknown regions rejected. Default behavior
  before an approved plan: return failure instructing the agent to use the plan tool with user
  permission (one-time or full-session).
- Application and bootloader flashing are permanently separate commands; neither accepts
  agent-supplied allowed ranges.

#### 3.14.7 Breakpoints
- `set_breakpoint(symbol-or-address)` — guarded, fixed `1,0`. Resolves the location and
  requires a mapped executable region supported by the target's breakpoint mechanism.
- `remove_breakpoint(address)` — always available.

#### 3.14.8 Serial (UART)
- `read_serial(expected_text?, read_seconds, baudrate?, port?, reset_on_open?)` — guarded,
  multi-call. Captures bounded output; `read_seconds` must be > 0; `baudrate` defaults to the
  profile and must be positive; optional expected text (any output matches when omitted);
  optional reset-after-open to capture early boot output.
- `write_serial(text, baudrate?, port?, append_newline?, timeout_seconds)` — guarded,
  multi-call. Sends bounded UTF-8 text; `timeout_seconds` must be > 0.

#### 3.14.9 Utility
- `wait(ms)` — always available; pauses between actions (bounds: Assumption A-13).

**Outputs (all Layer 2).** The action's result plus a safe-exit reminder. Failures name the
violated rule (region, freshness, plan, permission, or parameter) without exposing internals
the user should never see.

**Edge & error cases (all Layer 2).**
- A call naming a board other than the one its plan covers is rejected.
- A call against a closed gate (write-capable actions) is rejected with the reopening path.
- A call whose parameters differ from its plan is rejected without consuming budget.
- Requests spanning multiple regions are denied unless every touched address satisfies the
  action's rule (full containment).

**Acceptance criteria.**
- **AC-14.1** Every Layer 2 action requires and honors a board identity parameter; a request
  reaches only that board's assigned connection (observable with two boards attached).
- **AC-14.2** Every Layer 2 response includes both the operation result and a safe-exit
  reminder.
- **AC-14.3** `write_memory` with a raw address and no fallback flag is rejected with the
  symbol-first guidance; with the flag but no reason it is rejected; with flag + reason it
  succeeds only inside mapped RAM.
- **AC-14.4** A `flash_application` artifact with any loadable segment outside the application
  partition is rejected before any erase or write occurs.
- **AC-14.5** `flash_bootloader` without an approved permission-carrying plan returns a failure
  directing to `flash_bootloader-plan`.
- **AC-14.6** A `register_write` whose range crosses into a prohibited subrange is rejected even
  if it begins inside a valid peripheral window.
- **AC-14.7** `write_cpu_register` rejects control-flow/mode registers; `set_execution_state`
  accepts them only with permission; both reject names unsupported by the connected core.
- **AC-14.8** `set_breakpoint` rejects addresses outside mapped executable regions.
- **AC-14.9** `read_serial` and `write_serial` reject non-positive durations/timeouts and
  non-positive baud rates.
- **AC-14.10** An erase implied by flashing never touches sectors outside the named partition.
- **AC-14.11** Reset actions never change a locked target's security state.

---

### 3.15 Destructive Recovery (Target Unlock)

**Description.** Unlock/recovery of a locked target is a separate, plan-guarded, destructive
action: `target_unlock`, planned through `target_unlock-plan`. It is limited to documented
vendor recovery operations and never permits arbitrary writes to security or provisioning
registers.

**States.** *Uninitialized* (all-`NULL` call pending) → *initialized* → possibly *research
requested* (unknown mechanism) → *permission requested* → *plan approved & active* →
*executed / consumed* → *revalidation required*.

**Inputs & validation.**
- The all-`NULL`-first rule applies (§3.4). Budgets are fixed: any plan with values other than
  `max_calls = 1, max_calls_buffer = 0` is rejected.
- A populated plan includes: board identity, hypothesis, strategy, the two reasoning flags,
  expected fail/success returns, the fixed budget, the recovery-specific parameters, and user
  permission.
- When the recovery mechanism is unknown, the plan tool may return a research prompt; the
  returned candidate must describe a mechanism supported by the Server's attached probe/target
  capability. Research never authorizes execution.
- After permission is granted, the agent resubmits the complete **unchanged** plan with
  one-time user permission; any change voids the approval.

**Outputs.**
- Before any destructive action, a permission request containing: exact live MCU and board
  identity; the proposed recovery mechanism; whether it performs a mass erase; every known
  memory range/partition that will be erased; applicable erase banks/sectors; whether all
  nonvolatile memory will be erased; the expected loss (application, bootloader, configuration,
  user data); and the plan identifier.
- If the device exposes only a full-chip erase primitive, the request explicitly states that
  the entire addressable nonvolatile memory will be erased and lists every known affected
  region.
- On approval: the previous plan closes, the approved plan activates, `target_unlock` becomes
  visible and unlocked, and the response redirects the agent to call it.

**Edge & error cases.**
- Approval is specific to one plan, single-use, short-lived, bound to the target identity and
  erase ranges, and invalidated if the target, probe, safety map, or plan changes.
- Prior approval, general workspace permission, conversational assent, agent recommendation, or
  approval for another board is never reused.
- Mass erase requires fresh user permission every single time — full-session permission never
  covers it.
- After a successful unlock, the gate remains closed until `board_validate` passes again.
- Setup and validation only report locked state; they never perform recovery.

**Acceptance criteria.**
- **AC-15.1** A `target_unlock-plan` with any budget other than `1,0` is rejected.
- **AC-15.2** The permission request contains every element in the Outputs list, including
  concrete erase ranges.
- **AC-15.3** A full-chip-erase-only device produces a permission request explicitly stating
  total nonvolatile erasure.
- **AC-15.4** Resubmitting the plan with any field changed after approval is rejected; the
  approval does not transfer.
- **AC-15.5** A second mass erase in the same Server Run under any prior grant still triggers a
  fresh permission request.
- **AC-15.6** Changing the target, probe, safety map, or plan between approval and execution
  invalidates the approval.
- **AC-15.7** After `target_unlock` executes, no guarded write is possible until validation
  passes again.
- **AC-15.8** No parameterization of `target_unlock` performs a write to security/provisioning
  registers outside the documented vendor recovery operation.

---

### 3.16 Batch Execution (`action_batch`)

**Description.** Executes a list of other tool calls back to back in the listed order, for one
board.

**States.** *Running child N* → *completed* / *stopped on failure* (see edge cases).

**Inputs & validation.**
- All children must carry the same board identity; multi-board batches are rejected.
- Nested batches are rejected.
- Every guarded child requires its own active plan and consumes one call from that plan; the
  batch performs the same plan, permission, parameter, freshness, and safety checks per child
  as a direct call would.

**Outputs.** Per-child results in order, plus the standard safe-exit reminder.

**Edge & error cases.**
- A batch can never bypass freshness checks or region classification: each child is checked at
  its own execution time.
- If a child is rejected before execution, it consumes no budget; children that began execution
  consume budget regardless of outcome.
- On a child failure the batch stops at that child and reports results so far (Decision A-14).

**Acceptance criteria.**
- **AC-16.1** A batch containing children with two different board identities is rejected
  before any child runs.
- **AC-16.2** A batch containing a batch is rejected.
- **AC-16.3** A guarded child without an active covering plan causes that child to fail exactly
  as a direct call would.
- **AC-16.4** Gate closure between children (e.g., disconnect mid-batch) blocks subsequent
  write-capable children.
- **AC-16.5** Children execute strictly in listed order.

---### 3.17 Operation Lifecycle: Timeouts, Cancellation, Cleanup & Finalizers

**Description.** Every operation is bounded, isolated, and deterministically cleaned up. The
Server always releases its own resources without depending on the agent, and without needing
device-specific knowledge.

**States.** Per operation: *queued/blocked on board lock* → *running* → *completed / failed /
cancelled / timed out* → *finalizer (optional)* → *cleaned up*.

**Inputs & validation.**
- Every operation has a timeout (defaults: Assumption A-11).
- Only one active operation per physical probe/board at a time; a second request waits or fails
  with a busy indication.
- Long-running or stateful tools (serial sessions, custom bootloader sessions, manufacturing
  tests, interactive debug) may accept an optional structured finalizer in the **original**
  request — e.g., "on exit: write this serial text with this bounded timeout" or "on exit:
  reset and run." Finalizers are structured actions from a documented whitelist, never
  arbitrary shell commands, and are best-effort and short. Ordinary single-shot tools carry no
  finalizer parameter.

**Outputs.** On completion by any path, the board, probe, and serial port are observably free
for the next operation.

**Edge & error cases.**
- **Mandatory deterministic cleanup** runs on success, error, cancellation, timeout, and Server
  shutdown: stop active reads/writes; close the serial connection; close the debug session;
  terminate any helper work the operation started (forcibly if it does not exit); release
  reset/control lines; release the board lock. This never depends on anything the agent
  provides.
- **Client cancellation:** when the client signals cancellation of an in-flight request, the
  Server stops the corresponding operation and frees its resources (marking it cancelled,
  stopping its work, closing debug and serial connections, releasing the lock). Clients are
  also expected to cancel on their own request timeouts.
- **Client disconnect** terminates the Server Run; all cleanup still occurs.
- **Flashing caveat:** cancelling an active flash lets the flash finish before releasing the
  probe, to avoid partially written firmware; only then are resources released.
- **Finalizer ordering:** the optional finalizer runs first when safe; deterministic cleanup
  always follows, regardless of finalizer success. Finalizer failure never prevents resource
  cleanup.
- **Startup hygiene:** at Server Run start, leftover helper state from a previous run is
  detected and cleaned.
- **Defined final board state:** cleanup leaves the board in a defined state rather than
  accidentally halted (default: running from reset when the operation did not intentionally
  leave it halted — Decision A-15). The board may legitimately remain halted, running old
  firmware, inside a custom bootloader, awaiting serial input, or in reset — none of these may
  prevent the next operation from reconnecting.

**Acceptance criteria.**
- **AC-17.1** Killing the client mid-operation results, within a bounded time, in: serial port
  closed, debug session closed, helper work terminated, board lock released.
- **AC-17.2** A cancelled non-flash operation stops promptly and its resources are freed; a
  subsequent operation on the same board succeeds without manual intervention.
- **AC-17.3** A cancelled in-progress flash completes the flash before releasing resources; the
  written firmware is complete and bootable.
- **AC-17.4** An operation exceeding its timeout is terminated and cleaned up identically to a
  cancellation.
- **AC-17.5** Two concurrent operations on one probe/board never interleave; the second waits
  or fails busy.
- **AC-17.6** A failing finalizer does not prevent any mandatory cleanup step.
- **AC-17.7** A finalizer request specifying anything outside the documented structured
  whitelist is rejected at call time.
- **AC-17.8** After an unclean previous run, a new Server Run starts successfully and cleans up
  the leftovers.

---

### 3.18 Host-Local Attachment Cache

**Description.** Portable board identity and physical bench attachment are different facts. The
profile can say which MCU and probe family a board uses; it cannot prove which physical adapter
is wired to it. When the user resolves such ambiguity, the Server records stable hardware
identities (not volatile port paths) in a host-local cache so the user is not re-asked.

**States.** Per (board, probe identity, serial-adapter identity) record: *absent* → *confirmed
(with timestamp)* → *reused silently* / *ignored* / *revoked*.

**Inputs & validation.**
- Records store: board identifier, probe family and stable probe serial identity, serial
  adapter's stable identity (serial/vendor/product identifiers), a confirmation flag, and the
  confirmation time.
- Reuse requires an exact match of board identifier, probe identity, and serial-adapter
  identity; the current port path is then resolved fresh.

**Outputs.** Silent resolution of an otherwise ambiguous assignment; otherwise a conversational
choice as in §3.2.

**Edge & error cases.** The cache is a hint, never ownership. It is ignored when: a stable
identity is missing; hardware changed; multiple records match; the probe differs; or the user
revokes it. It never permanently binds a profile to physical hardware — the user may assign a
compatible replacement later. It is excluded from version control.

**Acceptance criteria.**
- **AC-18.1** After the user confirms an ambiguous adapter once, an identical hardware
  arrangement in a later Server Run resolves without re-asking.
- **AC-18.2** A port-path change alone (same stable identities) does not re-prompt.
- **AC-18.3** Any of the ignore conditions (missing identity, changed hardware, multiple
  matches, different probe, revocation) causes a conversational re-confirmation instead of
  silent reuse.
- **AC-18.4** The cache file is absent from version-control tracking in a freshly initialized
  project.

---

### 3.19 Per-Board Isolation (Multi-Board Operation)

**Description.** Multiple boards may be attached and worked on within one Server Run. Every
authorization construct is scoped to exactly one board.

**States.** Independent per board: assignment, validation stamp, fingerprint stamp, gate,
plans, permissions.

**Inputs & validation.** Every plan and hardware action requires the board identity (sole
exception: the all-`NULL` plan-details call). The identity must name the intended board's
profile.

**Outputs.** Actions and results are attributable to exactly one board.

**Edge & error cases.**
- Reading from board 1 can never accidentally read board 2: the identity routes to the single
  assigned connection.
- One board's disconnect, gate closure, or permission revocation leaves the other boards'
  states untouched.

**Acceptance criteria.**
- **AC-19.1** With two boards attached and only board A validated, every guarded action naming
  board B fails while the same action naming board A succeeds.
- **AC-19.2** A plan created for board A does not unlock the underlying action for board B.
- **AC-19.3** Disconnecting board A closes only board A's gate; board B's open gate and active
  plans are unaffected.
- **AC-19.4** Full-session permission granted for a tool on board A does not satisfy the
  permission requirement for the same tool on board B.

---

## 4. Cross-Cutting Requirements

### 4.1 Security posture (observable)

- **CC-1** The Server serves exactly one local client on one computer, with no authentication
  and no network exposure. It must be unreachable from other machines.
- **CC-2** Tool visibility is never authorization; every hidden tool is also functionally
  locked (AC-3.3).
- **CC-3** Unknown memory is denied by default for every operation; prohibited classifications
  override all broader classifications.
- **CC-4** The agent has no path to: persist state directly, supply allowed ranges, alter the
  user's part number, or open any gate (AC-6.5, AC-8.3, AC-8.5).
- **CC-5** No tool accepts arbitrary shell commands, including cleanup/finalizer parameters
  (AC-17.7).
- **CC-6** Security/provisioning regions and mass erase are unavailable to ordinary workflows;
  mass erase requires fresh, range-disclosing user permission every time (AC-5.7, AC-15.2).

### 4.2 User-interaction boundary

- **CC-7** All server-originated content intended for the user is plain prose suitable for
  verbatim conversational relay; every response that requires user input or research includes
  an explicit instruction to the agent not to expose structured payloads, continuation tokens,
  or internal field names.
- **CC-8** There is no user-facing terminal-command layer and no separate research provider.

### 4.3 Interoperability

- **CC-9** The Server interoperates with standard tool-calling agent clients (see Assumption
  A-1), including honoring client-issued request cancellation where the client sends it, and
  behaving safely (via timeouts and deterministic cleanup) where the client does not (see
  Open Question Q-1).

### 4.4 Performance targets (observable; numbers are Decisions A-16)

- **CC-10** Gate/freshness checks add no more than 250 ms to a guarded call on a typical
  developer workstation.
- **CC-11** Hardware enumeration at startup completes within 10 seconds for up to 8 attached
  probes/ports.
- **CC-12** The all-`NULL` plan response and initialization handshake return within 2 seconds.
- **CC-13** Every operation has a finite timeout; no tool call can hang a Server Run
  indefinitely (AC-17.4).

### 4.5 Reliability & recoverability

- **CC-14** No single action available without destructive-recovery permission can leave the
  MCU permanently unrecoverable; application/bootloader crashes must remain recoverable through
  the Server's own reset/flash actions.
- **CC-15** After any operation ends by any path, the next operation can reconnect without
  manual host intervention (AC-17.1–AC-17.5).

### 4.6 Data lifecycle

- **CC-16** Profiles: created by setup, updated only by Server actions after validation,
  user-deletable as files; deleting a profile makes its name unknown at next resolution (routes
  to setup).
- **CC-17** Reports/logs: one immutable structured record per setup/validation attempt;
  failures, research exchanges, and fingerprint changes are always recorded.
- **CC-18** Cache: revocable and regenerable at any time with no effect on profiles.
- **CC-19** No secret material is persisted; nothing in the artifact store grants authority
  (artifacts never restore an open gate — AC-13.4).

### 4.7 Accessibility & internationalization

- **CC-20** The product's entire human interface is conversational text relayed by the agent;
  no requirement in this specification depends on a graphical interface, color, sound, or
  pointing device.
- **CC-21** All user-relayable text is in clear, jargon-minimal language; hardware choices are
  described by human-friendly distinguishing features (e.g., probe identifier endings), never
  raw internal identifiers alone.
- **CC-22** Initial release is English-only (Decision A-17; see Q-6). Display names supplied by
  the user may contain non-ASCII characters and must round-trip losslessly.

---

## 5. Assumptions & Decisions

- **A-1** The client protocol is the Model Context Protocol (MCP) over local standard I/O, per
  the broad design's structure statement. This is treated as an interoperability constraint
  (which clients must work), not an implementation mandate.
- **A-2** The "revised actions" list in the broad design supersedes the "current action list."
  This specification specifies the revised surface; legacy tools (`flash_firmware`,
  `unlock_recover`, unified `reset`, `read/write_core_register`, raw `read_memory`/
  `write_memory` without symbol preference) are replaced and must not appear.
- **A-3** The broad design shows memory access both as one multi-action tool and as separate
  tools in the visibility lists. **Decision:** separate tools as listed in §3.3/§3.14
  (`find_symbol`, `read_memory_symbol` always available; `read_memory_address`, `write_memory`
  guarded), with the symbol-first/fallback behavior applied to the address-based paths.
- **A-4** Guarded **read** actions (`read_memory_address`, `read_serial`) require an active
  plan and a validated session for the named board but not configuration-fingerprint freshness;
  **write-capable** actions require plan + validation + freshness. (The broad design defines
  freshness checks for "write-capable guarded actions" only.)
- **A-5** "Short-lived" destructive-recovery approval is defined as: valid only within the
  current Server Run and consumed by the single permitted call; it also expires if any bound
  fact changes (§3.15).
- **A-6** Board identifiers are machine-facing slugs: lowercase letters, digits, and
  underscores, 1–64 characters, unique per project. Display names are free-form unique strings
  ≤ 100 characters.
- **A-7** Where full-session permission exists for `board_setup-plan`, replacement setup plans
  proceed without re-prompting but remain subject to the retry budget (A-10).
- **A-8** `wait` is always available (the broad design lists it among revised actions without
  guarding).
- **A-9** Multiple-call plan budgets are capped at `max_calls ≤ 20` and
  `max_calls_buffer ≤ 10` per plan; larger requests are rejected with guidance to re-plan.
- **A-10** Deterministic retry budgets: at most 3 candidates per researched fact (target,
  package, enrichment field, safety fact) per Server Run, and at most 3 setup/repair plan
  cycles per board per Server Run; exhaustion yields the applicable
  blocked/unresolved status.
- **A-11** Default operation timeouts: 30 s for session/register/memory/breakpoint actions,
  120 s for flash and validation, 300 s for setup phases; per-call serial parameters keep their
  documented explicit values (e.g., `read_seconds` default 3.0 s, `write_serial`
  `timeout_seconds` default 1.0 s).
- **A-12** `read_memory_address` block reads are capped at 64 KiB per call; larger reads
  require multiple calls (and budget accordingly).
- **A-13** `wait` accepts 1–60,000 ms; values outside are rejected.
- **A-14** `action_batch` stops at the first failed child and reports the results of all
  children so far plus the failure; it never continues past a failure.
- **A-15** Default defined final board state after cleanup is "running from reset," except when
  the operation's own purpose was to leave the core halted (e.g., `reset_and_halt`,
  `halt`, a breakpoint stop), in which case the halted state is preserved.
- **A-16** The concrete performance numbers in §4.4 (250 ms, 10 s, 2 s, 8 devices) are proposed
  defaults chosen for testability; the broad design gave none.
- **A-17** Initial release language is English; all statuses and prompts are English text.
- **A-18** Timestamps in persisted artifacts use an unambiguous absolute format with timezone
  (the broad design's examples use UTC ISO-8601-style values).
- **A-19** "Materially different" for a retried package candidate means differing in at least
  source location, version, or content checksum — not merely a renamed file.
- **A-20** The setup tools gated behind `load_setup_tool` (`board_setup-plan`,
  `board_safety_setup`, `board_safety_refresh`, `board_validate`) are visible in the tool list
  but return a redirect to `load_setup_tool` until it has been called for that board and tool
  in the current Server Run.
- **A-21** User revocation of a full-session permission is delivered by the agent as a
  structured revocation through the corresponding plan tool; the Server honors it immediately
  (see Q-5 for the enforcement caveat).

## 6. Open Questions

- **Q-1** Which supported clients actually send cancellation on user interrupt? The broad
  design notes at least one major client does not. Until verified per client, the timeout path
  is the guaranteed cleanup trigger — should default timeouts be tightened for clients known
  not to cancel?
- **Q-2** What is the intended behavior when the workspace contains **no** build/link artifacts
  at all (e.g., brand-new project)? Can setup complete with a safety map lacking
  application/bootloader partitions, leaving only RAM/register operations available, or must
  setup block until a build exists?
- **Q-3** Should a user-initiated "forget this board" flow exist (deleting profile + safety map
  + cache records atomically), or is manual file deletion the supported path?
- **Q-4** Is there a maximum number of simultaneously attached boards to support and test
  (two are implied by examples; CC-11 assumes up to 8 enumerable devices)?
- **Q-5** The user-permission gate is acknowledged as soft (the Server cannot verify a human
  approved). Is a stronger, client-side elicitation mechanism required for destructive
  recovery on clients that support it, and what is the required behavior on clients that do
  not?
- **Q-6** Are non-English user-facing prompt translations required for the initial release
  (CC-22 assumes no)?
- **Q-7** For `connect_under_reset` on probes without a wired reset line, is a degraded
  fallback (e.g., halt-after-connect) desired, or is a hard failure (current specification)
  correct?
- **Q-8** Should validation's bounded serial capture parameters (duration, size) be
  profile-configurable per board, or fixed product-wide?
- **Q-9** When a display name is renamed while its board is actively assigned in a running
  session, does the session continue under the old name until re-validation, or is the rename
  deferred? (This specification implies session names bind at validation; confirm.)
- **Q-10** Is there a required audit trail for Layer 2 actions beyond action-specific responses
  and setup/validation reports? The broad design says per-action responses are sufficient
  "unless that action's own specification requires more auditing" — which actions, if any,
  require more?
