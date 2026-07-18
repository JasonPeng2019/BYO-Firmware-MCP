# **MCP Server Structure**

# **Prototype** 

Our product is a 3-layered MCP server running on local. For security, we compile and strip and perform security measures such as native-Rust compile, B2B Business Legal Contracts, etc.

**Client:** Client A (Client Agent):  
 Codex or Claude CLI that faces the user.

Connects to: **Server A(stdio), Server B (stdio)**. Servers should launch together under one command. One opening / session of both servers should persist through the entire codex or claude CLI run, and terminate when claude / codex disconnect. The opening of server A should open a middleman agent that persists until the pair of servers close.

**Server:**   
Server A (Turnkey Brain): One server acts as a server-client. We call it Server A (Turnkey Brain). Server A connects to a top level user codex/claude CLI client, but server A also spins up a thread of the same provider to connect to as a client itself. The codex agent is responsible for injecting the proper context, via input parameters, into the Turnkey Brain for the Middleman Agent to process (detail memory of the past 4 turns, compact memory of the 12 turns before that, workspace summary, relevant files hints, board refs, workspace and build context, verification contracts for the task, etc).  
	Server A Layer 1: Guide Tools: These tools guardrail the lower level Server A tools, which are used to directly contact the middleman agent. They provide guides on what to input into certain parameters, prompt guides, how to condense memory or how to keep track of the workspace, etc.  
	Server A Layer 2: Agentic Tools: These tools directly access the middleman agent. Some tools may be: “Execute complex task”: {Task}, “Diagnose and Fix Bug” : {Bug}, etc.  
	  
Turnkey Brain \-\> Connects to: \-\> **Board Manager, Middleman Agent (stdio)**

Agent B (Middleman Agent): 

* This is the thread that server A connects to. Server A will feed the middleman agent prompts in an automated turnkey fashion for complex tasks. The middleman agent has access to both the workspace’s code repository and Server B. One middleman agent will survive as long as Server A agentic tool call is alive, and dies when server A returns the tool call; it relies on the context params supplied from Client A \-\> Turnkey Brain \-\> Middleman Agent.   
* Agent B should only be allowed a certain set of JSONs to Server A. If something requires User Permission, it should return this to Server A via a “finalize\_needs\_user\_permission” action that finalizes the current turn. If Agent B specifically requests user permission, “agentic tool did not finish: user permission required; get user permission and try again.”, and agent A should get permission and try again.  
*  If for whatever other reason, other than a few specific reasons that have their own tools (like user permission needed), the tool fails (doesn’t finalize successful for whatever reason), the agentic tool instructions should instruct Agent A, “agentic tool did not finish: \<insert final response from Agent B\>; diagnose the issue and try again.”

Middleman Agent \-\> Connects to: \-\> **Server B (streamable http)**

Server B (Guarded Hardware Server): This is a guardrail’d MCP server. It exposes direct actions to the board, but each action is guarded by a set of guardrail actions that require the Middleman or Client Agent to execute the guardrail actions first, before Server B exposes the tools in Server B. Thus any agent trying to directly access the board must go through:  
	Server B Layer 0: Security Setup \- ask the agent to confirm immutable/locked bytes that should never be changed, set up hardware security,   
	Server B Layer 1: Guardrail actions \- like asking the calling agent to come up with a reason for the tool call, hypothesis, and expected result, and feed back prompt injections that specifically state to terminate board-side processes when the action is done, or specifically ask for proof that the action they are about to perform is safe (safe breakpoint setting, explicitly outputting proof that there are no unsafe option register actions), etc.  
	Server B Layer 2: Hardware Actions \- Actions that affect the board directly, then return the result to the agent \+ a reminder to safely exit the hardware layer. Every set of actions in layer 2 is guarded by an action in layer 1\.

# **Capabilities**

### **Client A:**

* Literally everything codex and claude can do  
* Handle memory compression, giving context to the server A, and inputting params  
* Unlocking security gates

### **Server A Layer 1:**

* Guards specific agentic tools  
* Returns text prompt guides to the client agent on how to properly use agentic tools before unlocking them.  
* Responsible for prompt injection to Client Agent

### **Server A Layer 2:**

* Responsible for automated board/workspace bootstrap initiation setup  
* Owns preloaded compound workflows.   
* Responsible for prompt instruction injection to Middleman Agent  
* Responsible for multi-step instructions  
* Middleman Decision Formatting  
* Responsible for Middleman Decision Handling  
* Interface with Middleman (General)  
* Holds ownership of Middleman Artifacts (such as plan made by the middleman agent)

### **Middleman Agent:**

* Everything codex and claude can do, but with prompt injections and instructions  
* Use memory compression and context to make decisions

### **Server B Layer 1:**

* Guards specific hardware-level tools  
* Returns text prompt guides to the client agent on how to properly use hardware tools before unlocking them  
* Responsible for prompt-injection to client Agent

### **Server B Layer 2:**

* Responsible for safeguards around hardware tools (safeguarded design, such as not letting agents write security bytes, and requiring user permission 

### **Server B Sharing**

A shared **Server B manager** is one local MCP server that multiple clients can connect to, while it serializes all board-affecting work.

Client A ─┐  
          ├── HTTP or stdio → Server B manager → board/probe/UART  
Client B ─┘

How it works:

1. Both clients connect to the same local Server B endpoint.  
2. Each tool call enters Server B.  
3. Before touching the board, the call waits for a single global lock.  
4. The first call acquires the lock and runs.  
5. Other calls remain open and wait in line.  
6. On success, failure, timeout, or cancellation, Server B:  
   * closes pyOCD/UART sessions;  
   * terminates owned subprocesses;  
   * releases the lock.  
7. The next waiting call acquires the lock automatically.

Use the lock only around board-affecting operations. Read-only metadata calls such as listing tools or inspecting cached target information can run concurrently.

So Server B is effectively both:

* the shared MCP endpoint for multiple clients; and  
* the single owner/queue manager for the physical board.

# **Server A functionality**

Server A is the turnkey brain: each agentic tool call spins up a fresh middleman subagent, connects it to Server B over MCP, and runs an automated loop that drives it to complete one task, then returns the outcome to Client A.

## Tools

* Layer 1 load tools lock the agentic tools. Calling a load tool returns a guide on the agentic tool's purpose, parameters, and context expectations — including the tier 1–3 memory construction prompts below — and unlocks that tool for the current session. A locked agentic tool fails and names its load tool.
* Layer 2 agentic tools:
  * `bug_fix` — fixed turnkey workflow. Server A owns the step sequence (diagnose \-\> locate root cause \-\> patch \-\> rebuild \-\> flash \-\> green check) and feeds it to the middleman one step at a time. Client A supplies `bug` plus the common parameters.
  * `complex_implementation` — fixed turnkey workflow. Server A owns the step sequence (understand requirement \-\> implement \-\> rebuild \-\> flash \-\> green check) for implementing a specified feature. Client A supplies `feature` plus the common parameters.
  * `complex_task` (self plan) — the plan itself is a tool parameter. Client A (the codex/claude model) writes the plan and inputs each step as a text parameter in the JSON (`step_1`, `step_2`, ... `step_n`). Server A implements the plan step by step: it feeds the middleman the current step, and advances to the next step each time the middleman returns `next_step`. The plan is custom-made by Client A for the middleman agent — not a fixed sequence like `bug_fix` and `complex_implementation`.

## Agentic tool input parameters (supplied by Client A)

Common parameters, required by all three tools:

* `tool_summary` — one-paragraph summary of what this tool call is implementing and what done looks like.
* `task` — the task statement: the concrete goal of this call in the user's terms.
* `memory_tier1_turn1` — explicitly detailed record of the most recent turn, with four required fields: `action` (everything done — each command, tool call, and edit), `reasoning` (why that action was chosen), `codebase_changes` (every file touched and the substance of each change, or "none"), and `result` (everything observed — outputs, values, errors). Each field is written in explicit detail, aiming for 100–500 tokens per field.
* `memory_tier1_turn2` — the same four fields, for the second-most-recent turn.
* `memory_tier1_turn3` — the same four fields, for the third-most-recent turn.
* `memory_tier1_turn4` — the same four fields, for the fourth-most-recent turn.
* `memory_tier2` — the 12 turns before tier 1 compacted into a goal of 250–1000 tokens total: the same action / reasoning / codebase-changes / result story per turn as tier 1, in denser summary form.
* `memory_tier3` — the session compacted from its beginning to now, with a goal of 250–1000 tokens total: all codebase changes made, all things done, and how the work got here from the start of the codex/claude session — tests, logic, and decisions included. It selectively filters out wrong directions, errors, and failed implementations, since those are red herrings.
* `relevant_files` — workspace file paths, each with a one-line hint of why it matters to this task.
* `board_facts` — board id, display name, MCU part number, debug target, probe family/UID, serial port, baud rate, recover policy.
* `reference_artifacts` — paths to known-good reference firmware/ELF for symbols and recovery.
* `build_context` — workspace root, build command, and build/flash artifact output paths.
* `iteration_max` — hard cap on loop iterations.
* `green_check_guide` — the guide.md text: when the middleman should run the check, the preparation required first, and the evidence to gather.
* `green_check_script` — the runnable script that proves the task on the board (see Green check).
* `green_check_expected_outputs` — the literal strings/values the script must produce on success.

Tool-specific parameters:

* `bug` (`bug_fix` only) — the bug description: observed behavior, expected behavior, and how to reproduce it.
* `feature` (`complex_implementation` only) — the feature description: the required behavior and its acceptance conditions.
* `step_1` ... `step_n` (`complex_task` only) — the plan written by Client A, one text step per parameter (see Tools).

| Parameter | `bug_fix` | `complex_implementation` | `complex_task` |
| :--- | :---: | :---: | :---: |
| All common parameters above | yes | yes | yes |
| `bug` | yes | — | — |
| `feature` | — | yes | — |
| `step_1` ... `step_n` | — | — | yes |

* Delta form — follow-up calls within the same pair-of-servers session supply only: `tool_summary`, `task`, and the next turnkey step or "please continue". Server A retains the full context from the first call.

## Memory construction prompts

These prompts produce `memory_tier1_turn1`–`turn4`, `memory_tier2`, and `memory_tier3` in the form shown in the example init prompt. They are returned to Client A as part of the load tool's guide — since every agentic tool is locked behind its Layer 1 load tool, they are not injected until the load tool for that complex tool is called.

Tier 1 prompt:

```text
[MEMORY — TIER 1]
Fill one param per turn for your last 4 turns: memory_tier1_turn1 (most
recent) through memory_tier1_turn4 (fourth-most-recent). Each param has
exactly these four fields, each written in explicit detail with a goal of
100–500 tokens per field:
action: everything you did that turn — every command run, every tool call and
  its arguments, every file opened or edited.
reasoning: why you chose that action over alternatives — the question it was
  meant to answer or the outcome it was meant to produce.
codebase_changes: every file you touched and the substance of each change —
  functions added or edited, logic changed, and why. Write "none" if the turn
  made no edits.
result: everything you observed — command output, tool results, UART text,
  register/memory values, build messages, and errors, quoted precisely.
Report the facts as they happened. Do not compress, editorialize, or merge
turns.
```

Tier 2 prompt:

```text
[MEMORY — TIER 2]
Compact the 12 turns before tier 1 into memory_tier2, with a goal of 250–1000
tokens total. Tell the same action / reasoning / codebase-changes / result
story per turn as tier 1, but denser: one to three sentences per turn, keeping
every turn recognizable. Keep concrete identifiers — file names, symbols,
commands, boards, outputs — and drop narration.
```

Tier 3 prompt:

```text
[MEMORY — TIER 3]
Compact the whole session, from your first turn to now, into memory_tier3,
with a goal of 250–1000 tokens. Cover: every codebase change made (files,
modules, behavior), everything done (builds, flashes, hardware checks, tests),
the logic and decisions that led here, and the current state — written as
how-we-got-here, so a fresh agent could pick up the work. Selectively filter
out wrong directions, errors, and failed implementations: they are red
herrings and do not belong in this summary.
```

## Green check

* The green check is the deterministic proof that the bug is fixed or the feature is fully implemented. Client A writes it per task, tailored to firmware: the script rebuilds/flashes the patched firmware if needed, resets the board, and observes real board behavior through Server B — e.g., capture UART output for expected boot/feature text, read the relevant symbols or memory values, or check execution state at a breakpoint. `green_check_expected_outputs` lists the literal strings/values the script must produce on success.
* `guide.md` tells the middleman when and how to run the check and what evidence to gather first.
* Flow: the middleman calls `request_green_check` to get the instructions, performs any needed preparation, then calls `validate_green_check`. Server A runs the script itself and deterministically compares actual output against `green_check_expected_outputs`; the middleman never self-declares success.
* `finish_task` is blocked by the brain until a green check has validated in the current tool call.

## Turnkey loop

* Every agentic tool call opens a new middleman subagent (never reused) and connects it to Server B via MCP (streamable http); it lives for the duration of the tool call and dies when the tool returns.
* The first prompt to the middleman is the init prompt; every later prompt is a delta (structures and examples under Prompts).
* The middleman must reply with exactly one JSON decision matching this schema:
  * `action` — exactly one of:
    * `next_step` — the current step is done; advance to the next. Parameters: none — completion evidence belongs in `observation_summary`.
    * `continue_step` — still working the current step; report progress and keep going. Parameters: none.
    * `return_text_to_user` — surface text through Client A. Parameters: `text` — the exact message for the user, plain language, no internal identifiers or payloads.
    * `request_green_check` — returns instructions on how to perform the green check. Parameters: none.
    * `validate_green_check` — Server A runs the green\_check script and deterministically judges success against the expected outputs. Parameters: `script_args` — the inputs guide.md says the script needs (e.g. the patched artifact path); `preparation_summary` — evidence that the preparation guide.md requires is complete.
    * `finish_task` — ends the task successfully; blocked by the brain until a green check has validated. Parameters: `task_result` — what was accomplished and how the green check proved it.
    * `fail_task` — ends the task unsuccessfully. Parameters: `failure_reason` — why the task cannot be completed, with the evidence and what was tried.
    * `finalize_needs_user_permission` — finalizes the turn when an action requires user permission. Parameters: `permission_request` — exactly what permission is needed, for which action, and why.
  * `action_params` — the chosen action's parameters, exactly as listed in the action index.
  * `observation_summary` — "State only what you actually did and observed this turn: the exact tool calls, commands, and edits you made, and their concrete results — build output, UART text, register/memory values, error messages — quoted precisely. No intentions, no predictions, no restating the plan. If you did not observe it this turn, it does not belong here."
  * `problem_hypotheses` (optional) — "Your current best explanations of the root cause, each specific and testable: name the code path, symbol, register, or mechanism you suspect, and what evidence would confirm or kill it (e.g. 'the TX helper truncates the final byte in uart_log_write — a breakpoint before the last putc will show len=6'). Omit this field when nothing is uncertain."
  * `current_strategy` — "The single approach you are executing next: the concrete actions you will take, the evidence you expect them to produce, and why that should work given your hypotheses. Commit to one strategy; do not list alternatives."
  * `failed_strategies` — "Every approach tried and abandoned in this task so far, each with the observed evidence that ruled it out (e.g. 'raised UART capture to 10 s — output still ends at \"boot o\", so timing is not the cause'). Carry the complete list forward every turn so no failed approach is ever retried."
  * `carry_forward_warnings` — "Hard constraints and traps every remaining step must respect, whether given to you or discovered (e.g. 'reset the board after flashing before any UART read', 'g_state is optimized out — read 0x20000440 directly', 'never touch option bytes'). Carry the complete list forward every turn and append new ones as you find them."
* Server A auto-rejects any response that does not fit the required return schema — malformed JSON, missing or extra fields, or an `action` outside the index. The rejected response is discarded and the middleman is re-prompted with the rejection reason and the compact schema; the rejection consumes an iteration.
* Server A handles each decision, feeds the next prompt, and owns the middleman artifacts (such as the middleman's plan).
* The loop ends on `finish_task`, `fail_task`, `finalize_needs_user_permission`, or reaching `iteration_max`.
* Wrap-up cleanup: when the middleman finalizes (loop ends by any path), Server A auto-deletes every markdown/doc created or referenced specifically for this call — docs made for the agentic tool's input params (e.g. the green\_check guide.md and script) and docs made for the middleman's decision-return params (e.g. plan and strategy artifacts). None of these survive the tool call.

## Prompts

Init prompt — sent once per agentic tool call, in this order:

1. Pre-prompt instructions — role and rules: you are the middleman firmware agent for one task, driven by an automated brain, not a human; your tools are the workspace repository and Server B; work only the current step; reply each turn with exactly one JSON decision and nothing else.
2. Tool summary and task — `tool_summary` and `task`.
3. Current step — step 1 of the fixed workflow or the Client-A plan.
4. Context — every context parameter rendered legibly: the four tier-1 turn params, tiers 2–3, relevant files, board facts, reference artifacts, build context.
5. Green check — guide.md summary and `green_check_expected_outputs`.
6. Action index — every action with its description, its parameters, and a note on what each parameter should contain.
7. Return schema — the decision JSON shape plus the full field descriptions above.
8. Footer notes — iterations remaining; schema mismatches are auto-rejected and cost an iteration; `finish_task` is blocked until a green check validates; leave the hardware layer safe after every board action.

Example init prompt:

```text
[TURNKEY BRAIN — INIT]
You are the middleman firmware agent for one task. You are driven by an automated
brain, not a human. Your tools are the workspace repository and the guarded hardware
server (Server B). Work only the current step — do not skip ahead. Your entire reply
each turn must be exactly one JSON decision object matching the schema below, with
no text outside the JSON.

TOOL: bug_fix — diagnose and repair a UART output regression, then prove the fix
on hardware.
TASK: Fix the bug where the board prints "boot o" instead of "boot ok" on startup.

CURRENT STEP (1/6): Diagnose — reproduce the failure on the live board and gather
evidence.

CONTEXT
memory_tier1 (last 4 turns, most recent first; real fields run 100–500 tokens
each, abridged here):
  turn1:
    action: Read src/uart_log.c and src/main.c and traced the boot banner path
      from main() through uart_log_write; ran find_symbol("uart_log_write") to
      confirm the symbol exists in the current ELF.
    reasoning: The capture ends exactly one byte early, which points at a length
      or off-by-one fault in the TX path rather than baud or timing; reading the
      code first narrows where the fix must go before delegating.
    codebase_changes: none.
    result: The banner string and its length argument in main.c are correct
      ("boot ok\n", len 8), so the truncation is inside uart_log.c's TX loop;
      the exact fault line is unconfirmed.
  turn2:
    action: Ran read_serial for 3.0 s at 115200 on COM9 immediately after a
      board reset to reproduce the reported symptom on live hardware.
    reasoning: Confirm the bug is real and observable on the bench before
      touching source, and capture the exact failing output for comparison.
    codebase_changes: none.
    result: Captured "boot o\n"; expected "boot ok\n". Symptom reproduced
      identically on two consecutive resets.
  turn3:
    action: Flashed build/zephyr.elf to nucleo_l476rg through the debug probe
      and reset the board.
    reasoning: The bug was reported against the current workspace tree, so the
      board must run the current build for any capture to be meaningful.
    codebase_changes: none.
    result: Flash verified; board runs from reset; no flash or connection
      errors.
  turn4:
    action: Built the workspace with "west build -b nucleo_l476rg".
    reasoning: Start from a clean, current artifact before any on-board
      testing.
    codebase_changes: none.
    result: Build passed with no warnings; artifact at build/zephyr.elf.
memory_tier2 (the 12 turns before tier 1, 250–1000 tokens, dense):
  Validated nucleo_l476rg at session start (probe ST-Link 066C, COM9 @115200)
  and confirmed the reference firmware's clean "boot ok" baseline on hardware.
  Implemented the uart_log module: added src/uart_log.c and src/uart_log.h
  (ring-buffered TX helper uart_log_write), routed the main.c boot banner and
  runtime logging through it, and added a config option for the banner text.
  Two build iterations fixed include paths; flashed and captured UART after
  each stage; module output was verified correct on hardware before the
  current banner regression appeared.
memory_tier3 (session summary from the beginning, red herrings filtered):
  This session is bringing up UART logging for the app on nucleo_l476rg. It
  started from the healthy reference firmware, with the "boot ok" baseline
  proven on hardware. The uart_log module was implemented and the boot banner
  and runtime logging were moved through it; each stage was verified by
  rebuild, flash, and UART capture, with all board access through Server B on
  a validated session. Current state: the module works except that the boot
  banner regressed to "boot o" after the uart_log refactor — this is the only
  open defect, and fixing it is the purpose of this task.
relevant_files:
  src/main.c — prints the boot banner at startup
  src/uart_log.c — UART TX helper, likely truncation site
board_facts:
  id=nucleo_l476rg  display="Nucleo L476"  mcu=STM32L476RGT6
  target=stm32l476rgtx  probe=ST-Link 066CFF3435  port=COM9  baud=115200
  recover_policy=manual_only
reference_artifacts:
  firmware/nucleo_l476rg/reference/build/firmware.elf
build_context:
  workspace=C:\work\app  build="west build -b nucleo_l476rg"
  artifact=build/zephyr.elf

GREEN CHECK
guide.md: after your patched firmware is flashed, request the green check. The
script resets the board and captures 3 s of UART at 115200. Prepare first: patched
firmware flashed, board idle, serial port free.
green_check_expected_outputs: "boot ok" appears in the capture.

ACTIONS
next_step — the current step is done; give me the next one.
  params: none (completion evidence goes in observation_summary)
continue_step — still working the current step; report progress.
  params: none
return_text_to_user — pass text to the user through the client.
  params: text — the exact message for the user, plain language, no internal
    identifiers or payloads
request_green_check — get the green-check instructions.
  params: none
validate_green_check — the brain runs the green-check script and judges success
  against expected outputs.
  params: script_args — the inputs guide.md says the script needs (here: none);
    preparation_summary — proof the required prep is done (patched firmware
    flashed, board idle, serial port free)
finish_task — end successfully (blocked until a green check has validated).
  params: task_result — what was accomplished and how the green check proved it
fail_task — end unsuccessfully.
  params: failure_reason — why the task cannot be completed, the evidence, and
    what was tried
finalize_needs_user_permission — end this turn because an action requires user
  permission.
  params: permission_request — what permission is needed, for which action,
    and why

RETURN SCHEMA — reply with exactly this JSON object:
{
  "action": "<one action name from ACTIONS>",
  "action_params": { },
  "observation_summary": "",
  "problem_hypotheses": [],
  "current_strategy": "",
  "failed_strategies": [],
  "carry_forward_warnings": []
}
observation_summary: state only what you actually did and observed this turn — the
  exact tool calls, commands, and edits you made and their concrete results (build
  output, UART text, register/memory values, error messages), quoted precisely. No
  intentions, no predictions, no restating the plan.
problem_hypotheses (optional): your current best explanations of the root cause,
  each specific and testable — name the code path, symbol, register, or mechanism
  you suspect and what evidence would confirm or kill it. Omit when nothing is
  uncertain.
current_strategy: the single approach you are executing next — the concrete actions,
  the evidence you expect, and why it should work given your hypotheses. One
  strategy; no alternatives.
failed_strategies: every approach tried and abandoned in this task, each with the
  observed evidence that ruled it out. Carry the complete list forward every turn;
  never retry anything on it.
carry_forward_warnings: hard constraints and traps every remaining step must
  respect, given or discovered. Carry the complete list forward every turn and
  append new ones as you find them.

FOOTER
Iterations remaining: 20. A reply that does not match the schema is rejected and
costs an iteration. finish_task is blocked until validate_green_check has passed.
Leave the hardware layer safe after every board action: close sessions and leave
the board running.
```

Prompt delta — sent every later turn, in this order:

1. Pre-prompt — one-line re-anchor: automated brain turn; reply with exactly one JSON decision.
2. Tool summary and high-level task — one line each.
3. Last action result — Server A's response to the previous decision (step accepted, green-check result, correction, or schema rejection reason).
4. Next turnkey step, or a "please continue" injection when the current step is unfinished.
5. Compact action index and compact schema — names only.
6. Footer — iterations remaining, auto-reject rule, green-check reminder.

Example prompt delta:

```text
[TURNKEY BRAIN — TURN 5]
Automated brain turn. Reply with exactly one JSON decision object; no text outside
the JSON.

TOOL: bug_fix — diagnose and repair a UART output regression.
TASK: Fix "boot o" -> "boot ok" on startup.

LAST ACTION RESULT: next_step accepted. Your patch to src/uart_log.c (off-by-one in
tx length) is recorded.

CURRENT STEP (4/6): Rebuild — run the build command and confirm it succeeds.

ACTIONS: next_step | continue_step | return_text_to_user | request_green_check |
validate_green_check | finish_task | fail_task | finalize_needs_user_permission
SCHEMA: { action, action_params, observation_summary, problem_hypotheses?,
current_strategy, failed_strategies, carry_forward_warnings }

FOOTER: Iterations remaining: 16. Schema mismatches are rejected and cost an
iteration. finish_task requires a validated green check.
```

## Returns to Client A

* Success: the finished task result and green-check evidence.
* User permission needed: "agentic tool did not finish: user permission required; get user permission and try again."
* Any other non-success (fail, iteration max, error): "agentic tool did not finish: \<insert final response from Agent B\>; diagnose the issue and try again."

## Access

* The middleman agent has full Server B access (streamable http) and the workspace's code repository.

