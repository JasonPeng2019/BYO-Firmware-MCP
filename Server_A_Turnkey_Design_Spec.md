# Design Specification — Server A (Turnkey Brain)

> Source: `Server A functionality.md` (broad design document). Revision 3.
> Scope: the externally observable behavior of Server A only. Server B (the guarded
> hardware server) and Client A (the user-facing CLI agent) are described only where
> their behavior is part of Server A's observable contract. This document specifies
> WHAT the product does, never HOW it is built.
>
> Revisions 2–3 apply four review lenses: **simplicity** (no requirement more
> elaborate than its job needs), **generalizability** (identical behavior for any
> MCU, board, toolchain, or agent provider), **correctness** (fidelity to the broad
> design document wherever earlier drafts drifted), and **neatness** (an interface an
> agent or user can follow without untangling anything). Revision 3 redesigns the
> delta-call continuation to the broad doc's exact meaning (D-22), makes the green
> check self-describing so any toolchain works (D-24), adds a complete example call
> to every guide, and closes the last open questions as decisions (D-25, D-26). AC
> numbering is preserved; changed criteria are changed in place, new ones appended.

---

## 1. Overview & goals

Server A ("Turnkey Brain") is a locally running tool server that a user-facing AI
agent (Client A — a Codex or Claude CLI session) connects to. Its purpose is to
execute complex firmware tasks end-to-end on the user's behalf: for each "agentic"
tool call, Server A creates a fresh subordinate AI agent (the "middleman agent"),
gives it access to the workspace repository and to the guarded hardware server
(Server B), and drives it through an automated step-by-step loop until the task is
proven complete on real hardware, then returns the outcome to Client A.

Goals:

- **G-1** Let Client A delegate an entire multi-step firmware task (bug fix, feature
  implementation, or a custom plan) with a single tool call.
- **G-2** Guarantee that "done" is proven deterministically on hardware (the "green
  check"), never self-declared by an AI agent.
- **G-3** Guard the powerful agentic tools behind guide tools so that Client A
  always receives usage guidance (including memory-construction instructions)
  before it can invoke them.
- **G-4** Constrain the middleman agent to a fixed decision vocabulary so that every
  turn of the automated loop is machine-checkable, and escalate anything requiring
  user permission back to the user instead of acting unilaterally.
- **G-5** Work identically for any target hardware and any build system: everything
  board-, toolchain-, or task-specific enters through call parameters, never through
  behavior built into Server A.

Out of scope for this document: Server B's internal guardrail layers and Client A's
own capabilities. One Server B behavior is inside this contract because Server A
depends on it: the broad design document defines Server B as a single shared
endpoint that serializes board-affecting work for all connected clients, and
Server A's middleman connects to that same shared endpoint (see 3.6 and D-17).

---

## 2. Users / roles / permissions

| Role | Description | Permissions |
| :--- | :--- | :--- |
| **User (human)** | The person running the CLI session. | Sole authority for granting permission for permission-gated actions. Never interacts with Server A directly; all interaction is mediated by Client A. |
| **Client A (client agent)** | The CLI agent facing the user (any provider). | May call Server A's Layer 1 load tools at any time. May call a Layer 2 agentic tool only after that tool's load tool has been called in the current session. Supplies all task context and parameters. Relays user-permission requests to the user. |
| **Middleman agent** | The per-call subordinate AI agent created by Server A. | May read and modify the workspace repository, may use Server B, and may respond to Server A only with a single structured decision per turn drawn from the fixed action index. Cannot end a task successfully without a validated green check. Cannot obtain user permission itself; must escalate. |
| **Server B (external system)** | The guarded hardware server — one shared endpoint per session, the single owner/queue of the physical board. | Accessed by both Client A and the middleman agent; board-affecting calls from all clients serialize inside Server B. Server A itself performs no hardware action except running the green-check script. |

Permission rules:

- **P-1** Any action the middleman deems to require user permission must terminate
  the current agentic tool call with a permission request; the middleman must never
  perform the action first.
- **P-2** Server A never initiates side effects (builds, flashes, board actions)
  except within the lifetime of an agentic tool call that Client A initiated, or
  when running the green-check script as part of a validation the middleman
  requested.

---

## 3. Features

### 3.1 Server pairing, session lifetime

**Description.** Server A launches together with Server B under a single command.
One opening of the pair persists for the entire Client A CLI session and terminates
when Client A disconnects.

**States.**

- *Not running* — no session exists.
- *Running / idle* — session open, no agentic tool call in progress.
- *Running / busy* — an agentic tool call (and therefore a middleman agent) is
  active.
- *Shutting down* — Client A has disconnected; all resources are being released.

**Inputs & validation.** Launch is triggered by the single launch command; no
tool-level inputs.

**Outputs.** After launch, Client A can list and see Server A's tools (all Layer 1
load tools and all Layer 2 agentic tools).

**Edge & error cases.**

- Client A disconnects while an agentic tool call is in flight: the call is
  abandoned, the middleman agent is terminated, per-call cleanup (see 3.10) runs,
  and the session ends.
- Server B fails to start: the pair reports a launch failure to the operator and
  neither server is left running.

**Acceptance criteria.**

- **AC-1.1** One command starts both Server A and Server B; no second command is
  needed before Client A can use Server A's tools.
- **AC-1.2** Session state accumulated in Server A (tool unlocks, retained task
  context for delta calls) persists across multiple tool calls within one Client A
  CLI session.
- **AC-1.3** When Client A disconnects, Server A and Server B both terminate, any
  live middleman agent is terminated, and no middleman agent or board-holding
  process outlives the session.
- **AC-1.4** After a session ends, a new session starts with no unlocked tools and
  no retained task context from any prior session.

---

### 3.2 Layer 1 — load (guide) tools

**Description.** Every Layer 2 agentic tool is locked at session start and has a
corresponding Layer 1 load tool. Calling a load tool (a) returns a guide covering
the agentic tool's purpose, its parameters, its context expectations, and the
tier 1–3 memory-construction prompts, and (b) unlocks that agentic tool for the
remainder of the current session.

**States (per agentic tool, per session).**

- *Locked* (initial) — the agentic tool rejects calls.
- *Unlocked* — the agentic tool accepts calls.

**Inputs & validation.** Load tools take no required inputs.

**Outputs.** A human/agent-readable guide containing, at minimum: the agentic
tool's purpose; every parameter with what it must contain; the expectations for
context quality; the verbatim tier 1, tier 2, and tier 3 memory-construction
prompts (see 3.5); and one complete example call for the agentic tool.

**Edge & error cases.**

- Calling a load tool for an already-unlocked agentic tool returns the same guide
  again and leaves the tool unlocked (idempotent).
- Calling a locked agentic tool fails with an error that names the specific load
  tool that must be called first; the failed call causes no side effects (no
  middleman is created, no hardware or workspace activity occurs).

**Acceptance criteria.**

- **AC-2.1** At session start, every agentic tool call fails until its load tool
  has been called in that session.
- **AC-2.2** The lock error message names the exact load tool required.
- **AC-2.3** After a load tool is called once, its agentic tool succeeds (given
  valid parameters) for the rest of the session with no further load calls.
- **AC-2.4** The load tool's returned guide includes the tier 1, tier 2, and
  tier 3 memory-construction prompt texts and one complete example call for its
  agentic tool.
- **AC-2.5** The memory-construction prompts are not delivered to Client A through
  any channel before the corresponding load tool is called.
- **AC-2.6** A locked-tool failure produces no observable side effects.

---

### 3.3 Layer 2 — agentic tools (the three task types)

**Description.** Server A exposes exactly three agentic tools. Each runs the
turnkey loop (3.7) against a fresh middleman agent and differs only in where the
step sequence comes from and one tool-specific parameter:

1. **`bug_fix`** — fixed workflow owned by Server A:
   diagnose → locate root cause → patch → rebuild → flash → green check.
   Tool-specific parameter: `bug`.
2. **`complex_implementation`** — fixed workflow owned by Server A:
   understand requirement → implement → rebuild → flash → green check.
   Tool-specific parameter: `feature`.
3. **`complex_task`** — the step sequence is supplied by Client A as `steps`, an
   ordered list of plain-text steps (the broad design document's
   `step_1 … step_n`, carried as one ordered list — see D-16); Server A feeds them
   to the middleman one at a time in order, advancing exactly when the middleman
   reports the current step done.

**States (per agentic tool call).**

- *Validating* — inputs are being checked; no middleman exists yet.
- *Running step k of N* — the middleman is working the current step.
- *Awaiting green check* — a green-check validation is executing.
- *Finalizing* — the loop has ended; cleanup and return construction are underway.
- *Returned* — the tool call has returned to Client A.

**Inputs & validation.** See 3.4 (common parameters) plus:

- `bug` (`bug_fix` only, required): observed behavior, expected behavior, and
  reproduction steps.
- `feature` (`complex_implementation` only, required): required behavior and its
  acceptance conditions.
- `steps` (`complex_task` only, required): at least one step; every step is
  non-empty text. An empty list or an empty step is rejected before any middleman
  is created.

**Outputs.** One of the three return shapes defined in 3.9.

**Edge & error cases.**

- A tool-specific parameter supplied to the wrong tool (e.g. `feature` passed to
  `bug_fix`) is rejected as an unknown parameter.
- A second agentic tool call arriving while one is already running in the same
  session is rejected immediately with a "busy" error and causes no side effects
  (see cross-cutting concurrency, section 4).

**Acceptance criteria.**

- **AC-3.1** `bug_fix` drives the middleman through its six fixed steps in order;
  the middleman is shown exactly one current step at a time and cannot obtain a
  later step before reporting the current one done.
- **AC-3.2** `complex_implementation` behaves identically with its five fixed
  steps.
- **AC-3.3** `complex_task` presents Client A's steps verbatim, in list order, one
  at a time, advancing only on the middleman's `next_step` decision.
- **AC-3.4** For all three tools, step advancement is monotonic: the loop never
  revisits a completed step and never skips a step.
- **AC-3.5** Parameter validation failures are reported to Client A with the name
  of every invalid or missing parameter and occur before any middleman agent,
  workspace access, or hardware access exists.

---

### 3.4 Common agentic-tool input parameters

**Description.** All three agentic tools require the same common parameter set,
supplied by Client A. These are the middleman's entire inherited context. Server A
validates each parameter's presence and shape, then treats its content as opaque:
board facts, build commands, file paths, and artifacts are rendered to the
middleman exactly as supplied and never interpreted, so any MCU, board, or build
system is supported equally (D-18).

**Inputs & validation.** All of the following are required on a full-form call
(for the delta form, see 3.11):

| Parameter | Content | Validation |
| :--- | :--- | :--- |
| `tool_summary` | One-paragraph summary of what this call implements and what done looks like. | Non-empty text. |
| `task` | The concrete goal of this call in the user's terms. | Non-empty text. |
| `memory_tier1_turn1` … `turn4` | One per turn for Client A's last four turns (turn1 = most recent), each with exactly four fields: `action`, `reasoning`, `codebase_changes`, `result`, each written in explicit detail (goal 100–500 tokens per field). | All four fields present per turn; each field non-empty (`codebase_changes` may be the literal "none"). |
| `memory_tier2` | The 12 turns before tier 1 compacted to a 250–1000-token total, same per-turn story in denser form. | Non-empty text. |
| `memory_tier3` | The session compacted from its beginning (goal 250–1000 tokens): all codebase changes, all work done, how the work got here, with wrong directions/errors/failed attempts filtered out. | Non-empty text. |
| `relevant_files` | Workspace file paths, each with a one-line hint of why it matters. | Each entry has a path and a hint. |
| `board_facts` | Board id, display name, MCU part number, debug target, probe family/UID, serial port, baud rate, recover policy. | All listed facts present; values are opaque text to Server A. |
| `reference_artifacts` | Paths to known-good reference firmware images, in whatever format the task uses, for symbols and recovery. | Present; may be an explicitly empty list when no reference exists (D-20), in which case the middleman is told none exists. |
| `build_context` | Workspace root, build command, and build/flash artifact output paths — whatever toolchain the project uses. | All three present; values are opaque text to Server A. |
| `iteration_max` | Hard cap on loop iterations. | Integer ≥ 1. |
| `green_check_guide` | The guide text: when the middleman should run the check, required preparation, and evidence to gather. | Non-empty text. |
| `green_check_script` | The runnable check that proves the task on the board, stating the exact command that runs it (interpreter explicit — D-24). | Present, including its run command. |
| `green_check_expected_outputs` | The literal strings/values the script must produce on success. | At least one expected output. |

**Outputs.** None directly; the parameters are rendered into the middleman's init
prompt (3.12).

**Edge & error cases.**

- Any missing or invalid required parameter → the call is rejected per AC-3.5.
- Token-count "goals" for memory tiers are guidance, not hard validation: a call
  is not rejected for being outside the 100–500 / 250–1000 token goals (D-4).
- Unknown/extra parameters are rejected by name.

**Acceptance criteria.**

- **AC-4.1** A full-form call missing any required common parameter is rejected
  with every missing parameter named.
- **AC-4.2** A tier-1 memory parameter missing any of its four fields is rejected
  with the parameter and missing field named.
- **AC-4.3** `iteration_max` values below 1, non-integers, or non-numbers are
  rejected.
- **AC-4.4** Every accepted parameter's content is visible, legibly rendered, in
  the middleman's init prompt (verifiable via AC-12.1).

---

### 3.5 Memory-construction prompts

**Description.** Server A owns three fixed prompt texts that teach Client A how to
produce the memory parameters. They are delivered exclusively as part of a load
tool's guide (3.2). Their required content:

- **Tier 1 prompt** — instructs: fill one parameter per turn for the last four
  turns, most recent first; exactly the four fields `action`, `reasoning`,
  `codebase_changes`, `result`; explicit detail with a 100–500-token goal per
  field; report facts as they happened; no compression, editorializing, or merging
  of turns.
- **Tier 2 prompt** — instructs: compact the 12 turns before tier 1 into one
  parameter with a 250–1000-token total goal; same four-part story per turn in one
  to three sentences; keep every turn recognizable; keep concrete identifiers
  (files, symbols, commands, boards, outputs); drop narration.
- **Tier 3 prompt** — instructs: compact the whole session into one parameter with
  a 250–1000-token goal; cover every codebase change, everything done, the logic
  and decisions, and the current state, written as how-we-got-here for a fresh
  agent; selectively filter out wrong directions, errors, and failed
  implementations.

**Acceptance criteria.**

- **AC-5.1** Each load tool's guide contains all three prompts, each conveying
  every instruction listed above.
- **AC-5.2** The prompts are identical across the three load tools' guides within
  a session.

---

### 3.6 Middleman agent lifecycle

**Description.** Every agentic tool call creates one brand-new middleman agent of
the same AI provider as Client A (D-15). The middleman lives exactly as long as
the tool call and is never reused (D-19). Its capabilities are the workspace code
repository and Server B — the **same shared Server B endpoint** the session uses,
so there is exactly one owner of the physical board and board-affecting work from
the middleman and Client A serializes inside Server B (D-17).

**States.** *Nonexistent* → *Active (working the loop)* → *Terminated*.

**Inputs.** The middleman receives only what Server A sends it: the init prompt
and subsequent delta prompts (3.12).

**Outputs.** One structured decision per turn (3.7).

**Edge & error cases.**

- The middleman dies or exceeds the per-turn wait bound (D-21) mid-call: the call
  ends as a non-success and returns the failure return shape (3.9) with the
  observed failure as the final response.
- The tool call ends by any path: the middleman is terminated before the tool
  call returns; it must not survive the return.

**Acceptance criteria.**

- **AC-6.1** Two consecutive agentic tool calls observably use different middleman
  agents: no memory, state, or artifacts carry over from one call's middleman to
  the next except what Server A itself retains for delta calls (3.11).
- **AC-6.2** During a call, the middleman can successfully perform workspace
  repository operations and Server B operations through the session's shared
  Server B endpoint.
- **AC-6.3** After the tool call returns, no middleman agent process remains
  running.
- **AC-6.4** The middleman's provider matches Client A's provider.
- **AC-6.5** The middleman and Client A observe one and the same Server B:
  board state, sessions, and validation established through one are visible to
  the other, and their board-affecting operations never run concurrently on the
  same board.

---

### 3.7 Turnkey loop & decision protocol

**Description.** Server A drives the middleman in turns. Each turn, Server A sends
a prompt and the middleman must reply with exactly one decision object — nothing
outside it. The decision vocabulary (`action`) is exactly:

| Action | Meaning | Required action parameters |
| :--- | :--- | :--- |
| `next_step` | Current step done; advance. | none (evidence goes in `observation_summary`) |
| `continue_step` | Still working the current step. | none |
| `return_text_to_user` | Surface text to the user through Client A. | `text` — exact user-facing message, plain language, no internal identifiers or payloads |
| `request_green_check` | Get the green-check instructions. | none |
| `validate_green_check` | Ask Server A to run the green-check script and judge it. | `script_args`, `preparation_summary` |
| `finish_task` | End successfully. | `task_result` |
| `fail_task` | End unsuccessfully. | `failure_reason` |
| `finalize_needs_user_permission` | End the turn: an action needs user permission. | `permission_request` |

Every decision must also carry: `observation_summary` (only what was actually done
and observed this turn, quoted precisely; no intentions or predictions);
`current_strategy` (the single approach being executed next); `failed_strategies`
(the complete, carried-forward list of abandoned approaches with the evidence that
ruled each out); `carry_forward_warnings` (the complete, carried-forward list of
hard constraints and traps); and optionally `problem_hypotheses` (specific,
testable root-cause explanations — omitted when nothing is uncertain).

**States (per turn).** *Prompt sent* → *Decision received* → either *Accepted &
handled* or *Rejected & re-prompted*.

**Inputs & validation (of the middleman's reply).** Server A auto-rejects any
reply that is not exactly one well-formed decision object matching the schema:
malformed structure, missing required fields, extra fields, an `action` outside
the index, or missing/extra action parameters for the chosen action. A rejected
reply is discarded; the middleman is re-prompted with the rejection reason and the
compact schema. Each rejection consumes one iteration.

**Outputs (per decision, Server A's handling).**

- `next_step` → the next step is issued; after the final step, the green-check /
  finish path is the remaining gate.
- `continue_step` → a "please continue" prompt re-issues the current step.
- `return_text_to_user` → the `text` is conveyed to the user via Client A (D-7);
  the loop then continues with the current step.
- `request_green_check` → the green-check instructions are returned in the next
  prompt's "last action result" (see 3.8).
- `validate_green_check` → Server A runs the script and reports pass or fail
  deterministically (see 3.8).
- `finish_task` → allowed only if a green check has validated within this tool
  call; otherwise the decision is refused, the refusal reason is reported as the
  last action result, and the refusal consumes an iteration.
- `fail_task` / `finalize_needs_user_permission` → the loop ends; the
  corresponding return shape (3.9) is produced.

**Loop termination.** The loop ends on exactly: `finish_task` (accepted),
`fail_task`, `finalize_needs_user_permission`, or the iteration budget reaching
zero. Reaching `iteration_max` ends the call as a non-success (3.9).

**Edge & error cases.**

- Every middleman decision — accepted or rejected — consumes one iteration (D-8).
- A `finish_task` attempted before any validated green check does not end the
  loop; it is refused with an explicit reason.
- If the final iteration is consumed without a terminal action, the call returns
  the non-success shape citing iteration exhaustion and the middleman's last
  response.

**Acceptance criteria.**

- **AC-7.1** A syntactically malformed reply, a reply with a missing or extra
  top-level field, or an unknown `action` is never acted upon: it produces no step
  advancement, no green-check run, and no return to Client A; the middleman is
  re-prompted with the rejection reason and the compact schema.
- **AC-7.2** Each rejection reduces the remaining-iteration count by exactly one,
  and the updated count is visible in the next prompt's footer.
- **AC-7.3** `finish_task` before a validated green check is refused and the loop
  continues; `finish_task` after a validated green check ends the call
  successfully.
- **AC-7.4** `fail_task` ends the call immediately with the non-success return
  shape carrying `failure_reason`.
- **AC-7.5** `finalize_needs_user_permission` ends the call immediately with the
  permission return shape carrying `permission_request`.
- **AC-7.6** When the iteration budget is exhausted, the call ends within that
  turn and returns the non-success shape; no further prompts are sent to the
  middleman.
- **AC-7.7** `next_step` advances exactly one step; `continue_step` leaves the
  current step unchanged.
- **AC-7.8** A decision using an action parameter not defined for its action (or
  omitting a required one) is rejected under AC-7.1's rules.

---

### 3.8 Green check

**Description.** The green check is the deterministic proof that the task is done:
a Client-A-authored script that exercises the real board (rebuild/flash if needed,
reset, observe real behavior such as UART output, symbol/memory values, or
execution state) plus the literal expected outputs it must produce. The script is
the task's property, so any toolchain and any board are supported without Server A
knowing either: the check states the exact command that runs it, and Server A
executes exactly that command, never guessing an interpreter or toolchain (D-24).
Server A — never the middleman — runs the script and judges the result by
comparing actual output against `green_check_expected_outputs`.

**States (per tool call).** *No check validated* (initial) → *Check validated*
(after the first passing validation; persists for the rest of the call).

**Inputs & validation.**

- `request_green_check`: no parameters; returns the guide instructions (when to
  run, required preparation, evidence to gather) and the expected outputs.
- `validate_green_check`: `script_args` (the inputs the guide says the script
  needs) and `preparation_summary` (evidence the required preparation is
  complete). Both fields must be present (an empty `script_args` is valid when
  the guide requires none).

**Outputs.**

- Pass: the middleman's next prompt reports the validation passed, with the
  matched evidence; the call's state becomes *Check validated* and `finish_task`
  is unblocked.
- Fail: the middleman's next prompt reports the validation failed, including the
  actual output and the expected outputs it did not satisfy; the loop continues.

**Edge & error cases.**

- The script cannot run at all (missing artifact, board unreachable, script
  error): reported to the middleman as a failed validation with the observed
  error; the loop continues (D-9). It does not count as a pass and does not end
  the call by itself.
- Multiple validations are allowed within one call, each consuming an iteration;
  only a pass changes state.
- Judgment is strictly deterministic: the check passes if and only if every entry
  in `green_check_expected_outputs` is satisfied by the actual output. No
  AI/heuristic judgment is involved, and the middleman's own claims about the
  result are ignored.

**Acceptance criteria.**

- **AC-8.1** The middleman's assertion of success has no effect on green-check
  state; only Server A's own script run and literal comparison can set *Check
  validated*.
- **AC-8.2** Given a script whose output contains every expected output, the
  validation passes; given output missing any expected output, it fails and the
  failing comparison is reported to the middleman.
- **AC-8.3** `request_green_check` returns the guide's preparation requirements
  and the expected outputs without running the script or touching the board.
- **AC-8.4** A validation pass in one tool call does not carry into any other
  tool call: each new call starts at *No check validated*.
- **AC-8.5** A script execution error is reported as a failed validation with the
  error visible to the middleman, and the loop continues.

---

### 3.9 Returns to Client A

**Description.** Every agentic tool call returns exactly one of three shapes:

1. **Success** — the finished `task_result` plus the green-check evidence (the
   validated comparison).
2. **User permission needed** — the literal message: `agentic tool did not
   finish: user permission required; get user permission and try again.` plus the
   middleman's `permission_request` content so Client A can ask the user
   precisely.
3. **Any other non-success** (fail_task, iteration exhaustion, middleman death,
   internal error) — the message: `agentic tool did not finish: <final response
   from the middleman>; diagnose the issue and try again.` where the placeholder
   is replaced by the middleman's final response (or, when none exists, a
   description of what terminated the call). This shape is deliberately uniform
   across all non-permission failures (D-23).

**Edge & error cases.**

- If the middleman never produced any response (e.g. it died before its first
  decision), the non-success shape carries the observed termination cause in
  place of the final response.
- Text sent earlier via `return_text_to_user` is delivered to the user regardless
  of which return shape ends the call.

**Acceptance criteria.**

- **AC-9.1** Success returns include both the task result and the green-check
  evidence.
- **AC-9.2** The permission return contains the exact sentence above and the
  middleman's permission request.
- **AC-9.3** Every non-success, non-permission ending — including iteration
  exhaustion and internal errors — uses the "diagnose the issue and try again"
  shape.
- **AC-9.4** No return path exposes internal identifiers, raw prompts, or
  middleman decision payloads beyond the fields defined above.

---

### 3.10 Wrap-up cleanup

**Description.** When the loop ends by any path, Server A deletes every
markdown/doc file created or referenced specifically for this call: documents made
for the agentic tool's input parameters (e.g. the green-check guide file and the
green-check script) and documents made for the middleman's decision-return
parameters (e.g. plan and strategy artifacts). None of these survive the tool
call. Server A also owns any artifacts the middleman produced during the call
(such as its plan) for the duration of the call.

**Edge & error cases.**

- Cleanup runs on success, failure, permission finalization, iteration
  exhaustion, middleman death, and Client A disconnect alike.
- Files that are part of the user's workspace product (source changes, build
  outputs) are never deleted by cleanup — only per-call instruction/artifact
  documents are.
- If a per-call file cannot be deleted, the return to Client A still completes
  and the leftover is reported (D-10).

**Acceptance criteria.**

- **AC-10.1** After any tool-call return, none of the per-call documents (green
  check guide, green-check script, middleman plan/strategy docs) exist on disk.
- **AC-10.2** Cleanup never removes workspace source files, the user's own
  documents, or build artifacts referenced by `build_context`.
- **AC-10.3** Cleanup occurs on every termination path, verified at minimum for:
  success, `fail_task`, `finalize_needs_user_permission`, and iteration
  exhaustion.

---

### 3.11 Delta (follow-up) calls

**Description.** Within one pair-of-servers session, follow-up agentic tool calls
after a completed first call may use the delta form: Client A supplies only
`tool_summary`, `task`, and `continuation` — either the literal "please continue"
or the text of the next turnkey step (D-22). Server A retains and reuses the full
context from the first full-form call (memory tiers, files, board facts,
references, build context, green-check materials, iteration policy). "please
continue" resumes at the step the previous call last issued; any other
`continuation` text is issued verbatim as the current step, after which the
retained sequence resumes (or, if it is finished, the green-check/finish gate
remains).

**States.** *No retained context* (before any successful full-form call in the
session) → *Context retained* (after one).

**Inputs & validation.**

- Delta form requires `tool_summary`, `task`, and `continuation` (non-empty
  text); all other common parameters are optional.
- A delta-form call arriving when no context is retained is rejected with an
  error stating that a full-form call is required first.
- A follow-up call that supplies the full parameter set replaces the retained
  context (D-11).

**Outputs.** Identical return shapes to any agentic call (3.9). Each delta call
still creates a fresh middleman (per 3.6) whose init prompt is built from the
retained context plus the new delta inputs.

**Edge & error cases.**

- The canonical permission round-trip works through this feature: a call ends
  with the permission shape, Client A obtains user permission, and a delta call
  ("please continue") resumes the task without resupplying full context.
- Retained context is discarded when the session ends (AC-1.4).

**Acceptance criteria.**

- **AC-11.1** After a full-form call, a delta call with only `tool_summary`,
  `task`, and `continuation` = "please continue" runs successfully, its
  middleman's init prompt contains the retained context, and it resumes at the
  step the previous call last issued.
- **AC-11.2** A delta call in a fresh session (no prior full-form call) is
  rejected with an error naming the requirement.
- **AC-11.3** Supplying full parameters on a follow-up call replaces the retained
  context for subsequent deltas.
- **AC-11.4** A delta call whose `continuation` is any other non-empty text
  issues that text verbatim as the current step.

---

### 3.12 Middleman prompting contract

**Description.** The prompts Server A sends the middleman are part of its
observable behavior (the middleman is an external AI system driven entirely by
them).

**Init prompt** — sent exactly once per agentic tool call, containing, in order:

1. Pre-prompt role and rules: middleman firmware agent for one task; driven by an
   automated brain, not a human; tools are the workspace repository and Server B;
   work only the current step; reply each turn with exactly one decision object
   and nothing else.
2. `tool_summary` and `task`.
3. The current step with its position (e.g. "1 of 6").
4. All context parameters rendered legibly: tier-1 turns, tiers 2–3, relevant
   files, board facts, reference artifacts, build context.
5. Green check: the guide summary and the expected outputs.
6. The full action index: every action, its description, its parameters, and what
   each parameter should contain.
7. The full return schema with complete field descriptions.
8. Footer: iterations remaining; schema mismatches are auto-rejected and cost an
   iteration; `finish_task` is blocked until a green check validates; leave the
   hardware layer safe after every board action.

**Delta prompt** — sent every later turn, containing, in order:

1. One-line re-anchor: automated brain turn; reply with exactly one decision.
2. Tool summary and task, one line each.
3. Last action result: Server A's response to the previous decision (step
   accepted, green-check result, correction/refusal, or schema-rejection reason).
4. The next step, or a "please continue" injection when the current step is
   unfinished.
5. Compact action index and compact schema (names only).
6. Footer: iterations remaining, the auto-reject rule, and the green-check
   reminder.

**Acceptance criteria.**

- **AC-12.1** Exactly one init prompt is sent per tool call, and it contains all
  eight elements above in the stated order.
- **AC-12.2** Every subsequent prompt in the call is a delta prompt containing
  all six elements above in the stated order.
- **AC-12.3** Every prompt's footer shows the true remaining-iteration count.
- **AC-12.4** After a schema rejection, the very next delta prompt's "last action
  result" states the rejection reason.
- **AC-12.5** Every prompt instructs the middleman to leave the hardware layer
  safe (init prompt footer; delta prompts via the green-check/safety reminder).

---

## 4. Cross-cutting requirements

Stated as observable behavior only.

**Concurrency & exclusivity.**

- **AC-X.1** At most one agentic tool call runs at a time per session; an
  overlapping call is rejected immediately with a "busy" error and no side
  effects. Load-tool calls and other read-only calls remain available while an
  agentic call runs.

**Safety & security posture.**

- **AC-X.2** Server A performs no board-affecting action outside a live agentic
  tool call, and within one only via the middleman's Server B usage or the
  green-check script run.
- **AC-X.3** The middleman can influence Server A only through the fixed decision
  vocabulary; no middleman output outside an accepted decision changes Server A's
  state, steps, or returns.
- **AC-X.4** Actions requiring user permission are never performed inside a tool
  call; they always surface via the permission return shape, and the work resumes
  only when Client A calls again.
- **AC-X.5** Server A's returns to Client A never include credentials, raw
  transport traffic, or content of files outside the workspace.

**Reliability & resource lifecycle.**

- **AC-X.6** Every tool call terminates: given any middleman behavior (including
  silence), the call returns to Client A after at most `iteration_max` middleman
  decisions, each bounded by the per-turn wait bound (D-21).
- **AC-X.7** After any call ends and after session end, no orphaned middleman
  processes or per-call files remain (per AC-1.3, AC-6.3, AC-10.1).

**Observable performance.**

- **AC-X.8** Load-tool calls and parameter-validation rejections involve no
  AI-agent or hardware work and return promptly (within 2 seconds).
- **AC-X.9** Server A adds no waiting of its own between a middleman decision and
  the next prompt or return; end-to-end latency is dominated by middleman and
  hardware work.

**Internationalization & formats.**

- **AC-X.10** All guides, prompts, and return messages are in English (D-12).
  Text passed via `return_text_to_user` is relayed verbatim in whatever language
  the middleman produced.

**Data lifecycle.**

- **AC-X.11** Server A durably stores nothing across sessions: no memory-tier
  content, board facts, or task history from a previous session is observable in
  a new one.

**Accessibility.** Server A has no direct human interface; all human-facing text
flows through Client A. Its only accessibility obligation:

- **AC-X.12** All error and return messages are plain text, self-describing, and
  actionable without reference to internal state the reader cannot see.

**Generalizability.**

- **AC-X.13** Server A's behavior is identical for any MCU, board, toolchain, and
  agent provider: substituting different `board_facts`, `build_context`,
  `green_check_script`, or provider configuration changes only the content
  rendered and executed, never which behaviors occur. Server A contains no logic
  conditioned on a specific vendor, board family, or build system.

---

## 5. Assumptions & Decisions

- **D-1 (Scope)** This spec covers Server A. Server B's guardrail internals are
  specified separately; the one Server B behavior inside this contract is the
  shared endpoint of D-17, which the broad design document itself defines.
  Client A's behavior is specified only as obligations on what it supplies.
- **D-2 (Exactly three agentic tools)** The tool set is closed: `bug_fix`,
  `complex_implementation`, `complex_task`, each with one Layer 1 load tool. The
  broad doc's "some tools may be" phrasing is resolved to exactly these three.
- **D-3 (Unlock granularity)** Each load tool unlocks exactly its own agentic
  tool, for the remainder of the session only.
- **D-4 (Token goals are guidance)** The 100–500 and 250–1000 token figures for
  memory tiers are quality targets communicated in the guides, not validation
  limits; calls are not rejected on token counts.
- **D-5 (Validation before side effects)** All parameter validation completes
  before any middleman, workspace, or hardware activity begins, so a rejected
  call is always side-effect-free.
- **D-6 (Single active call)** One agentic call at a time per session; overlap is
  rejected rather than queued.
- **D-7 (`return_text_to_user` is non-terminal)** Surfacing text does not end the
  loop. The text reaches the user no later than the tool call's return; earlier
  delivery is permitted but not required.
- **D-8 (Iteration accounting)** Every middleman decision — accepted, refused
  (e.g. premature `finish_task`), or schema-rejected — consumes exactly one
  iteration. The broad doc's example confirms this (20 iterations at init, 16
  remaining at turn 5), so `iteration_max` is a true hard cap on middleman turns.
- **D-9 (Green-check execution errors fail closed)** A script that cannot run is
  a failed validation (reported with the error), never a pass and never an
  automatic task failure.
- **D-10 (Cleanup best-effort, reported)** A cleanup deletion failure does not
  block the return; the undeleted path is reported so the operator can remove it.
- **D-11 (Context replacement)** A follow-up call carrying the full parameter set
  overwrites the retained context; deltas after it use the new context.
- **D-12 (English)** All Server-A-authored text is English-only in this version.
- **D-13 (Fixed step counts)** `bug_fix` has exactly six steps and
  `complex_implementation` exactly five, as enumerated in 3.3; the green check is
  the final gate in both.
- **D-14 (Permission resume)** After a permission return, resumption is an
  ordinary follow-up call (typically delta form, "please continue"); Server A
  does not itself verify that the user granted permission — Client A is trusted
  for that, and Server B's own gates still apply.
- **D-15 (Provider matching)** The middleman uses the same AI provider as Client
  A's session (from "spins up a thread of the same provider").
- **D-16 (Steps as one ordered list)** The broad doc's `step_1 … step_n`
  parameters for `complex_task` are carried as a single ordered list `steps`, one
  text entry per step. This preserves the doc's intent — Client A authors the
  plan, one step per entry, executed in order — with one simple, strictly
  validatable parameter instead of an unbounded family of numbered parameters.
- **D-17 (One shared Server B)** Per the broad doc's "Server B Sharing" section,
  Server B is a single shared local endpoint that serializes board-affecting work
  for all its clients. The middleman connects to that same endpoint used by the
  session — never to a private second Server B — so exactly one process owns the
  physical board.
- **D-18 (Opaque context / generalizability)** Server A validates the presence
  and shape of `board_facts`, `build_context`, `relevant_files`, and
  `reference_artifacts` but never interprets their content. All board-, MCU-, and
  toolchain-specific knowledge lives in the parameter values and the green-check
  script that Client A supplies.
- **D-19 (Fresh middleman per call)** The broad doc's overview says a middleman
  "persists until the pair of servers close," while its detailed "Server A
  functionality" section says every agentic tool call opens a new middleman that
  dies when the tool returns. The detailed section is authoritative: fresh per
  call, never reused. Continuity across calls is provided by Server A's retained
  context (3.11), not by a long-lived middleman.
- **D-20 (Reference artifacts may be empty)** `reference_artifacts` is required
  but may be an explicitly empty list when no known-good firmware exists (e.g.
  first bring-up); the middleman is told none exists. (Resolves Revision 1
  OQ-2.)
- **D-21 (Per-turn wait bound)** Server A waits a bounded, operator-configurable
  time for each middleman decision (a sensible default is set per deployment). A
  turn exceeding the bound ends the call via the non-success shape. (Resolves
  Revision 1 OQ-1.)
- **D-22 (Delta continuation)** The delta form's `continuation` input carries the
  broad doc's "the next turnkey step or 'please continue'" literally: "please
  continue" resumes at the step the previous call last issued; any other text is
  the next turnkey step and is issued verbatim as the current step. (Resolves
  Revision 1 OQ-6.)
- **D-23 (Uniform non-success message)** The broad doc gives one literal
  non-success message; iteration exhaustion is not specially distinguished. The
  middleman's final response embedded in the message tells Client A what
  happened. (Resolves Revision 1 OQ-8.)
- **D-24 (Self-describing green check)** The green check states the exact command
  that runs it; Server A executes exactly that command and never guesses an
  interpreter, shell, or toolchain. This keeps the check portable to any build
  system and host.
- **D-25 (Cancellation)** In this version, the only cancellation of an in-flight
  agentic call is ending the session (3.1): disconnect terminates the middleman
  and runs cleanup. No separate cancel tool exists. (Resolves Revision 1 OQ-3.)
- **D-26 (Green-check script trust)** The caller-authored check runs with the
  same trust as Client A itself: inside the workspace, under a finite
  operator-configured timeout, with its output captured for the deterministic
  comparison. No sandbox is implied. (Resolves Revision 1 OQ-4.)

---

## 6. Open Questions

None. Every question raised while drafting this specification has been resolved
into a recorded decision: OQ-1 → D-21, OQ-2 → D-20, OQ-3 → D-25, OQ-4 → D-26,
OQ-5 → D-7, OQ-6 → D-22, OQ-7 → D-16, OQ-8 → D-23.
