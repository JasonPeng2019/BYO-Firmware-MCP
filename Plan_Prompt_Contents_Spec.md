# Plan-Tool Prompt Contents Specification

Companion to [Design_Proto_Spec.md](Design_Proto_Spec.md) (implements AC-4.1–AC-4.5, AC-5.4,
AC-3.6) and [Implementation_Plan.md](Implementation_Plan.md) (this document is the content
source for `guardrails/plan_defs.py`, milestones M4–M5).

This document specifies the **exact contents** of every `*-plan` tool's responses:

1. the **initial all-`NULL` response** (the "teaching" prompt returned the first time a plan
   tool is called with every parameter `NULL`), and
2. the **populated plan call** — the exact JSON the agent must submit to create the plan and
   unlock the underlying tool, per budget class.

Wording may be edited at implementation time; **topic coverage is mandatory**. Every element
marked ▸ must appear in the rendered response.

**Self-containment rule.** Section 1 below is the reference schema, but no rendered NULL
response may rely on the agent having seen it. Every per-tool layout in Sections 3–8 carries
its own complete **VALIDATION**, **BUDGET**, and **PERMISSION** blocks, and the rendered
response must include that tool-specific text in full — the agent must be able to build a
correct plan call from a single tool's NULL response alone.

---

## 1. The Plan Call Contract (reference schema)

### 1.1 Initial all-`NULL` call

The first call to any `*-plan` tool in a Server Run must have every parameter `NULL`:

```json
{
  "board_id": null,
  "hypothesis": null,
  "strategy": null,
  "hypothesis_made": null,
  "strategy_evaluated": null,
  "expected_fail_return": null,
  "expected_success_return": null,
  "max_calls": null,
  "max_calls_buffer": null,
  "action_parameters": null,
  "user_permission": null
}
```

A populated plan submitted before this call is rejected with instructions to make the
all-`NULL` call first. The all-`NULL` response is the prompt specified in Sections 2–8.

### 1.2 Populated plan call — common envelope

`action_parameters` holds the underlying tool's own parameters, **frozen verbatim into the
plan** — every later call to the underlying tool must match them exactly, or it is rejected
without consuming budget.

```json
{
  "board_id": "<assigned board id>",
  "hypothesis": "<what you believe is true about the board/bug and why this action tests it>",
  "strategy": "<how you will use the result; what you do on success and on failure>",
  "hypothesis_made": true,
  "strategy_evaluated": true,
  "expected_fail_return": "<the concrete output/state you expect if the hypothesis is wrong>",
  "expected_success_return": "<the concrete output/state you expect if it is right>",
  "max_calls": 1,
  "max_calls_buffer": 0,
  "action_parameters": { "...underlying tool parameters, exact values..." },
  "user_permission": "one-time"
}
```

Validation rules — server-enforced, and **restated in full inside every tool's rendered
VALIDATION block** (§2, §§3–8):

- `hypothesis_made` and `strategy_evaluated` must be `true`, and the paired text fields must
  contain real reasoning — empty or boilerplate text is rejected.
- `user_permission` appears **only** on permission-locked plan tools. Omit it elsewhere;
  including it on a non-permission tool is a malformed plan.
- Plans are immutable. To change any field or any value in `action_parameters`, submit a
  complete new plan call; it atomically replaces the old plan.
- **Malformed-plan rejection (every plan tool):** a populated plan call whose JSON does not
  match the required format for that tool — missing fields, wrong types, unknown or extra
  fields, budget values violating the tool's class, `action_parameters` not matching the
  underlying tool's parameter schema, or a missing/invalid `user_permission` where one is
  required — is **rejected without creating or replacing any plan**. The rejection response
  lists exactly which fields are missing or invalid and instructs the agent to submit a
  complete new corrected plan call. Rejected submissions never consume budget, never count as
  the all-`NULL` call, and never disturb an existing active plan.

### 1.3 Fixed `1,0` budget class

Tools: `board_setup` (+ paired `board_fix_setup`), `write_cpu_register`,
`set_execution_state`, `write_memory`, `set_breakpoint`, `flash_application`,
`flash_bootloader`, `register_write`, `target_unlock`.

Always submit exactly `"max_calls": 1, "max_calls_buffer": 0`. Any other values are rejected.
The one accepted underlying call — even if it fails, times out, or is cancelled after
starting — consumes the plan. A second attempt requires a full replacement plan (and fresh
permission where the tool is permission-locked with `one-time`).

### 1.4 Multi-call budget class

Tools: `connect_override`, `read_memory_address`, `reset_and_halt`, `connect_under_reset`,
`read_serial`, `write_serial`.

`max_calls` = calls you *expect* to need for the stated strategy. `max_calls_buffer` = leeway
for calls that execute but return empty, mistimed, inconclusive, or timed-out results — these
still consume budget. Total available = `max_calls + max_calls_buffer`; ceilings
`max_calls ≤ 20`, `max_calls_buffer ≤ 10` (Design_Proto_Spec A-9). Every call in the plan uses
the exact `action_parameters` declared in the plan; different parameters mid-task means a
replacement plan — choose parameters that legitimately cover the whole strategy.

### 1.5 Permission-locked plan tools

Tools: `board_setup-plan`, `set_execution_state-plan`, `flash_bootloader-plan`,
`target_unlock-plan` (destructive recovery).

Ask the user in plain conversation first; **conversation itself is never permission** — the
grant only counts when passed as `user_permission`. `"one-time"` covers exactly one accepted
underlying call and requires budget `1,0`. `"full-session"` covers that tool + that board for
the rest of the Server Run; later NULL responses must state the grant is active and that
`user_permission` may then be `null`. Mass erase is excluded from full-session coverage:
`target_unlock` mass-erase paths demand fresh permission every time.

### 1.6 What a valid plan returns

```json
{
  "plan_id": "plan_...",
  "underlying_tool": "<real tool name>",
  "total_calls": 1,
  "instructions": "Call <real tool> now with exactly the planned parameters. It is unlocked for board <board_id> only."
}
```

The underlying tool simultaneously appears in the advertised tool list, unlocked for that
board only, until the budget is exhausted, the plan is replaced/invalidated, or the Server Run
ends.

---

## 2. Universal NULL-Response Skeleton

Every all-`NULL` response renders these sections in order. Sections 3–8 fill the per-tool
slots; the fixed boilerplate below is shared. The **VALIDATION**, **BUDGET**, and
**PERMISSION** slots must be rendered with the tool-specific full text given in Sections 3–8 —
never as a cross-reference.

▸ **[MECHANISM]** — fixed text: "The real action `<tool>` is hidden and locked. This response
is step 1. To unlock it: call `<tool>-plan` again with every field populated as described
below. A valid plan unlocks `<tool>` for the named board with exactly the planned parameters.
Plans are immutable — any change requires a complete new plan call, which replaces this one."

▸ **[PURPOSE]** — what the underlying action does to the board. *(per-tool)*

▸ **[USE-WHEN / NOT-WHEN]** — scenarios it is for; the cheaper always-available alternative to
try first. *(per-tool)*

▸ **[PLAN-FIELDS]** — the §1.2 envelope, with one sentence per reasoning field on what it must
contain for *this* tool.

▸ **[ACTION-PARAMETERS]** — the underlying tool's own parameters: name, type, meaning,
defaults, validation rules; note they are frozen verbatim. *(per-tool)*

▸ **[VALIDATION]** — the tool's complete plan-validation rules: reasoning-field requirements,
whether `user_permission` belongs in the JSON, immutability/replacement, and the
malformed-plan rejection contract. *(per-tool, self-contained)*

▸ **[BUDGET]** — the tool's complete budget rules: the exact values to submit (fixed class) or
how to size them (multi-call class), consumption semantics, and what exhaustion requires.
*(per-tool, self-contained)*

▸ **[PERMISSION]** — the tool's complete permission rules, or an explicit "none — omit the
field" statement. Stateful: when full-session is already active for this tool + board, this
section instead states that and that `user_permission` may be `null`. *(per-tool,
self-contained)*

▸ **[PRECONDITIONS]** — which of these the server will still verify at execution time: active
plan, exact board, exact parameters, remaining calls, validated session, open gate, fingerprint
freshness — and that a refusal names the remedy. *(per-tool subset)*

▸ **[WARNINGS]** — worst realistic outcome; whether and how it is recoverable. *(per-tool)*

▸ **[SOFT-GUARDRAILS]** — 3–5 concrete "confirm before submitting the plan" checks.
*(per-tool)*

▸ **[EXIT]** — the state the board is left in; required cleanup/follow-up. *(per-tool)*

▸ **[EXAMPLE-PLAN]** — a filled JSON example (§§3–8 below). Values are illustrative; the agent
substitutes its own reasoning and parameters.

---

## 3. Setup and Connection

### 3.1 `board_setup-plan` (guards `board_setup` **and** `board_fix_setup`)

- **PURPOSE**: first-time creation of a board profile + complete safety map for one intended
  connection, or (repair mode) resumption of a recorded incomplete/failed setup at its first
  unverified phase.
- **USE-WHEN / NOT-WHEN**: only when the user-named board matches **no** existing profile
  (new board), or the matching profile records incomplete/failed setup (repair). If the name
  matches a healthy profile, the correct call is `board_validate` — never re-setup an existing
  board just because this is a new Server Run. Requires `load_setup_tool` first.
- **PAIRED ALLOWANCE** (unique to this tool; must be stated): one valid plan permits **one**
  `board_setup` call **and one** `board_fix_setup` call. If setup fails, the paired fix call is
  already authorized — use it without re-asking the user, even under one-time permission. Any
  further attempt needs a replacement plan (new user prompt under one-time; none under
  full-session, within retry limits). `board_fix_setup` has no NULL response of its own — this
  response is its contract too.
- **ACTION-PARAMETERS**:

| Field | Meaning |
| :--- | :--- |
| `mode` | `"setup"` (new profile) or `"repair"` (existing incomplete profile) |
| `connection_choice` | the server-provided choice id for the physical connection, from enumeration |
| `display_name` | the user's unique familiar name for the board |
| `proposed_board_id` | slug for the new profile (setup) or the existing id (repair) |

- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, and
  `hypothesis`/`strategy` must contain real reasoning about this board and this setup/repair —
  empty or boilerplate text is rejected. `user_permission` is **required** on this tool (see
  PERMISSION). A plan JSON that does not match this tool's required format — missing envelope
  fields, wrong types, unknown or extra fields, a budget other than `1,0`,
  `action_parameters` not matching exactly {`mode`, `connection_choice`, `display_name`,
  `proposed_board_id`}, or missing/invalid `user_permission` without an active full-session
  grant — is rejected **without creating or replacing any plan**; the rejection lists the
  invalid fields and asks for a complete new corrected plan call. Rejected submissions consume
  no budget, do not count as the all-`NULL` call, and leave any active plan untouched. Plans
  are immutable — changing anything means a new plan call that atomically replaces this one.
- **BUDGET — fixed**: submit exactly `"max_calls": 1, "max_calls_buffer": 0`; any other values
  are rejected. The budget applies per underlying tool: the plan's one `board_setup` call and
  one paired `board_fix_setup` call are each consumed even if the call fails, times out, or is
  cancelled after starting. When both are spent (or the workflow completes), further attempts
  require a full replacement plan — and a fresh user prompt if the grant was one-time.
- **PERMISSION — required**: ask the user in plain conversation before submitting;
  conversation itself is never permission — the grant counts only when passed as
  `user_permission`. `"one-time"` covers this plan's paired setup+fix allowance and requires
  the `1,0` budget. `"full-session"` covers `board_setup-plan` + this board for the rest of
  the Server Run, so replacement setup plans then proceed without re-prompting (within retry
  limits). When full-session is already active for this board, this NULL response instead
  states that and `user_permission` may be `null`. A missing or invalid value with no active
  grant makes the plan call fail with a permission request.
- **PRECONDITIONS**: one-to-one name↔connection assignment; `load_setup_tool` called.
- **WARNINGS**: setup connects to live hardware and performs safe reads only; it never
  flashes or erases. Expect `setup_needs_user_input` / `setup_research_required` statuses —
  follow their `agent_prompt`, ask the user conversationally, never expose JSON, continuation
  ids, or internal field names.
- **SOFT-GUARDRAILS**:
  1. Confirm the user explicitly named this board this session.
  2. Confirm no existing profile's `display_name` matches (else validate instead).
  3. Confirm you have the user's **exact** MCU part number — never guess or "correct" it.
  4. Be ready to resolve probe/port/build ambiguity by relaying the server's friendly
     choices, not by choosing silently.
- **EXIT**: on completion both setup actions relock; proceed to `board_validate`.
- **EXAMPLE-PLAN**:

```json
{
  "board_id": "left_controller",
  "hypothesis": "The board the user calls 'left controller' is a new STM32L476RGT6 build with no existing profile; setup should resolve target and safety map from the attached ST-Link and this workspace's linker artifacts.",
  "strategy": "Run board_setup once; if it reports a failed phase, use the paired board_fix_setup once with whatever fact the status requests; then run board_validate.",
  "hypothesis_made": true,
  "strategy_evaluated": true,
  "expected_fail_return": "setup_needs_user_input, setup_research_required, or a phase failure status naming the failed phase",
  "expected_success_return": "setup_completed, followed by a passing board_validate",
  "max_calls": 1,
  "max_calls_buffer": 0,
  "action_parameters": {
    "mode": "setup",
    "connection_choice": "probe_1",
    "display_name": "left controller",
    "proposed_board_id": "left_controller"
  },
  "user_permission": "one-time"
}
```

### 3.2 `connect_override-plan` (guards `connect_override`)

- **PURPOSE**: exceptional manual connection using explicit probe `unique_id`, debug target,
  board definition, and/or external board-config path, bypassing normal resolution.
- **USE-WHEN / NOT-WHEN**: only after normal `connect`/validation resolution has failed and
  you can say why. Never to work around a hardware/profile mismatch the user should correct.
- **ACTION-PARAMETERS**: `unique_id` (string|null), `target` (string|null),
  `board_id` (string), `board_config` (path|null). Frozen verbatim.
- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, with real
  reasoning about why the normal connection path failed — empty or boilerplate text is
  rejected. This tool is **not** permission-locked: omit `user_permission` entirely;
  including it makes the plan malformed. A plan JSON not matching this tool's format —
  missing envelope fields, wrong types, unknown/extra fields, budget over the multi-call
  ceilings, or `action_parameters` not matching exactly {`unique_id`, `target`, `board_id`,
  `board_config`} — is rejected without creating or replacing any plan; the rejection lists
  the invalid fields and asks for a corrected plan call, consuming no budget and leaving any
  active plan untouched. Plans are immutable — any change is a new, replacing plan call.
- **BUDGET — multi-call**: set `max_calls` to the connection attempts your strategy expects
  and `max_calls_buffer` as leeway for attempts that run but fail inconclusively — every
  attempt with these exact frozen values consumes one call, successful or not. Total =
  `max_calls + max_calls_buffer`; ceilings `max_calls ≤ 20`, `max_calls_buffer ≤ 10`.
  Different override values mid-task = replacement plan. Recommended for connection
  troubleshooting: `"max_calls": 3, "max_calls_buffer": 2`.
- **PERMISSION — none**: omit `user_permission` from the plan JSON.
- **PRECONDITIONS**: plan + board match. Manual values **never rewrite persistent profile
  state** — must be stated verbatim.
- **WARNINGS**: overriding the target for a mismatched chip yields undefined debug behavior.
- **SOFT-GUARDRAILS**:
  1. State which normal resolution step failed and how.
  2. Confirm the manual target is consistent with the user's part number.
  3. Confirm the probe uid was taken from the server's own enumeration, not guessed.
- **EXIT**: session behaves like a normal connection; disconnect cleanly when done.
- **EXAMPLE-PLAN**: budget `3,2`; `action_parameters` as above with concrete values; no
  `user_permission` field.

---

## 4. CPU and Execution

### 4.1 `write_cpu_register-plan` (guards `write_cpu_register`)

- **PURPOSE**: write one ordinary core register: **R0–R12 and floating-point registers only**.
- **USE-WHEN / NOT-WHEN**: patching a computation mid-debug (halted core). PC, SP/MSP/PSP,
  LR, xPSR, CONTROL, PRIMASK, BASEPRI, FAULTMASK belong to `set_execution_state` — this tool
  rejects them. Unknown/unsupported names for the connected core are rejected. Reading is
  always available via `read_cpu_register` — read first.
- **ACTION-PARAMETERS**: `name` (register, allowed class), `value` (hex `0x...` or decimal).
- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, with real
  reasoning about the register write and its expected effect — boilerplate is rejected. This
  tool is **not** permission-locked: omit `user_permission`; including it makes the plan
  malformed. A plan JSON not matching this tool's format — missing fields, wrong types,
  unknown/extra fields, any budget other than `1,0`, or `action_parameters` not matching
  exactly {`name`, `value`} — is rejected without creating or replacing any plan; the
  rejection lists the invalid fields and asks for a corrected plan call, consuming no budget.
  Plans are immutable — a different register or value is a new, replacing plan call.
- **BUDGET — fixed**: submit exactly `"max_calls": 1, "max_calls_buffer": 0`; any other values
  are rejected. The one accepted write consumes the plan even if it fails, times out, or is
  cancelled after starting; another write requires a complete replacement plan.
- **PERMISSION — none**: omit `user_permission` from the plan JSON.
- **PRECONDITIONS**: validated session; open gate; plan match.
- **WARNINGS**: wrong values corrupt in-flight computation; recoverable by reset.
- **SOFT-GUARDRAILS**:
  1. Core halted first.
  2. Register is in the allowed class.
  3. Value width fits the register.
  4. State the readback you expect afterward.
- **EXIT**: read the register back to confirm; resume or reset deliberately.
- **EXAMPLE-PLAN**: budget `1,0`; `action_parameters: {"name": "r0", "value": "0x00000001"}`;
  no `user_permission` field.

### 4.2 `set_execution_state-plan` (guards `set_execution_state`) — permission-locked

- **PURPOSE**: write control-flow / CPU-mode registers: PC, SP, MSP, PSP, LR, xPSR, CONTROL,
  PRIMASK, BASEPRI, FAULTMASK and related.
- **USE-WHEN / NOT-WHEN**: redirecting execution or unmasking/masking interrupts during deep
  debugging. **Not** for restarting the program — `reset_and_run` is always available and
  safer. Not for ordinary registers (use `write_cpu_register`).
- **ACTION-PARAMETERS**: `name`, `value` (as 4.1, execution-state class).
- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, with real
  reasoning about why execution state must change and what happens next — boilerplate is
  rejected. `user_permission` is **required** (see PERMISSION). A plan JSON not matching this
  tool's format — missing fields, wrong types, unknown/extra fields, any budget other than
  `1,0`, `action_parameters` not matching exactly {`name`, `value`}, or missing/invalid
  `user_permission` without an active full-session grant — is rejected without creating or
  replacing any plan; the rejection lists the invalid fields and asks for a corrected plan
  call, consuming no budget. Plans are immutable — any change is a new, replacing plan call.
- **BUDGET — fixed**: submit exactly `"max_calls": 1, "max_calls_buffer": 0`; any other values
  are rejected. The one accepted write consumes the plan even on failure, timeout, or
  cancellation after start; another write requires a replacement plan — and a fresh user
  prompt if the grant was one-time.
- **PERMISSION — required**: ask the user plainly first, naming the register, the value, and
  the risk; conversation itself is never permission — the grant counts only when passed as
  `user_permission`. `"one-time"` covers exactly this one write and requires the `1,0`
  budget. `"full-session"` covers `set_execution_state` + this board for the rest of the
  Server Run; later plans may then pass `user_permission: null`, and this NULL response will
  say so when the grant is active. Missing/invalid permission with no active grant fails the
  plan call with a permission request.
- **PRECONDITIONS**: validated session; open gate; permission active; plan match.
- **WARNINGS**: can jump execution, corrupt the stack, mask interrupts, or fault the CPU.
  Usually recoverable with a reset — say so, and say how.
- **SOFT-GUARDRAILS**:
  1. Explain why a reset or breakpoint cannot achieve the goal.
  2. State the recovery step if the core faults (`reset_and_halt` / `reset_and_run`).
  3. Confirm the target address/stack value comes from symbols or a verified read, not
     arithmetic guesswork.
  4. User asked in plain language; grant passed only via `user_permission`.
- **EXIT**: verify state via `get_state`/`read_execution_state`; leave the board in a
  deliberate run/halt state.
- **EXAMPLE-PLAN**: budget `1,0`; `"user_permission": "one-time"`;
  `action_parameters: {"name": "pc", "value": "0x08008231"}`.

---

## 5. Memory and Breakpoints

### 5.1 `read_memory_address-plan` (guards `read_memory_address`)

- **PURPOSE**: read a value or bounded block from a raw address.
- **USE-WHEN / NOT-WHEN**: **symbol-first doctrine, stated verbatim**: "Prefer symbol access
  whenever source code or debug symbols identify the intended variable
  (`find_symbol` and `read_memory_symbol` are always available and need no plan). Use raw
  addresses only for dynamically allocated, pointer-derived, stack, optimized-out, or
  otherwise unsymbolized memory."
- **ACTION-PARAMETERS**: `address` (hex/decimal), and either `word_size` (8|16|32) for a
  single value or `length` (bytes, > 0, ≤ 64 KiB) for a block. Frozen verbatim — plan the
  exact address/size you will actually poll.
- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, with real
  reasoning including why symbols are insufficient for this read — boilerplate is rejected.
  This tool is **not** permission-locked: omit `user_permission`; including it makes the plan
  malformed. A plan JSON not matching this tool's format — missing fields, wrong types,
  unknown/extra fields, budget over the multi-call ceilings, or `action_parameters` not
  matching exactly {`address`, `word_size` | `length`} — is rejected without creating or
  replacing any plan; the rejection lists the invalid fields and asks for a corrected plan
  call, consuming no budget. Plans are immutable — a different address or size is a new,
  replacing plan call.
- **BUDGET — multi-call**: set `max_calls` to the number of polls of this exact location your
  strategy needs and `max_calls_buffer` as leeway for reads that return before the value
  settles — every executed read consumes one call regardless of usefulness. Total =
  `max_calls + max_calls_buffer`; ceilings `max_calls ≤ 20`, `max_calls_buffer ≤ 10`. Every
  call uses the exact frozen `address`/size. Recommended when watching a status word settle:
  `"max_calls": 4, "max_calls_buffer": 2`.
- **PERMISSION — none**: omit `user_permission` from the plan JSON.
- **PRECONDITIONS**: validated session; address must classify into a mapped region; unknown
  memory is denied.
- **WARNINGS**: reads of some peripheral registers have side effects (clear-on-read).
- **SOFT-GUARDRAILS**:
  1. Confirm you tried `find_symbol` first and why it was insufficient.
  2. Confirm the address lies in a mapped region and note which.
  3. For peripherals, check the reference manual for read side effects.
- **EXIT**: none required.
- **EXAMPLE-PLAN**: budget `4,2`;
  `action_parameters: {"address": "0x20000400", "length": 64}`; no `user_permission` field.

### 5.2 `write_memory-plan` (guards `write_memory`)

- **PURPOSE**: write one value to target memory, by symbol (preferred) or raw address
  (explicit fallback, RAM only).
- **USE-WHEN / NOT-WHEN**: flipping a variable/flag to test a hypothesis. Not for peripheral
  registers (`register_write`), not for flash (flash tools).
- **ACTION-PARAMETERS**: `symbol` (string|null), `address` (hex/decimal|null), `value`,
  `word_size` (8|16|32), `allow_address_fallback` (bool), `reason` (string, required when
  fallback true). Rules, stated verbatim: a raw address without `allow_address_fallback: true`
  is rejected with "Try a symbol first."; the fallback path also requires a brief reason
  symbols are unsuitable and is limited to mapped RAM with full containment; a symbol write's
  resolved region must match the write type; prohibited and unknown regions are rejected.
- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, with real
  reasoning about the variable, the value, and the expected behavior change — boilerplate is
  rejected. This tool is **not** permission-locked: omit `user_permission`; including it
  makes the plan malformed. A plan JSON not matching this tool's format — missing fields,
  wrong types, unknown/extra fields, any budget other than `1,0`, `action_parameters` not
  matching exactly {`symbol`, `address`, `value`, `word_size`, `allow_address_fallback`,
  `reason`}, or a fallback write missing its `reason` — is rejected without creating or
  replacing any plan; the rejection lists the invalid fields and asks for a corrected plan
  call, consuming no budget. Plans are immutable — a different variable or value is a new,
  replacing plan call.
- **BUDGET — fixed**: submit exactly `"max_calls": 1, "max_calls_buffer": 0`; any other values
  are rejected. The one accepted write consumes the plan even on failure, timeout, or
  cancellation after start; another write requires a complete replacement plan.
- **PERMISSION — none**: omit `user_permission` from the plan JSON.
- **PRECONDITIONS**: validated session; open gate; fingerprint freshness; region rules above.
- **WARNINGS**: wrong writes crash the application; recoverable by reset/reflash.
- **SOFT-GUARDRAILS**:
  1. Symbol resolution attempted and shown.
  2. Value and width match the variable's type/size.
  3. Effect is reversible or a recovery step is stated.
  4. State the readback you will perform to confirm.
- **EXIT**: read the location back; note the board may now behave differently by design.
- **EXAMPLE-PLAN (symbol path)**: budget `1,0`;
  `action_parameters: {"symbol": "motor_speed_target", "address": null, "value": "0x0000012C", "word_size": 32, "allow_address_fallback": false, "reason": null}`.
- **EXAMPLE-PLAN (fallback path)**: same envelope;
  `action_parameters: {"symbol": null, "address": "0x2000FF80", "value": "0x01", "word_size": 8, "allow_address_fallback": true, "reason": "heap-allocated node; no symbol exists for this element"}`.

### 5.3 `set_breakpoint-plan` (guards `set_breakpoint`)

- **PURPOSE**: set one hardware/software breakpoint at a symbol-resolved or explicit address.
- **USE-WHEN / NOT-WHEN — prints first, step-through second (must be stated verbatim-ish)**:
  breakpoint-and-step-through debugging is the **escalation path, not the first move**.
  Debug first with print-statement logging via the §8.3 instrumentation protocol: inject
  tagged prints, capture them with `read_serial`, and reconstruct the execution flow from the
  log. Reach for `set_breakpoint` (and the always-available `step`) only when print-based
  diagnosis has **failed or cannot work** — e.g., the fault fires before UART is initialized,
  prints perturb the timing enough to hide the bug, the code path has no UART access, or the
  logs localized the failure to a few instructions and you now need register/memory state at
  an exact halt point. The location must resolve to a mapped **executable** region supported
  by the target's breakpoint mechanism. `remove_breakpoint` is always available — no plan
  needed for cleanup.
- **ACTION-PARAMETERS**: `location` (symbol name or hex address).
- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, with real
  reasoning that records the print-based attempt and why escalation is needed — boilerplate
  is rejected. This tool is **not** permission-locked: omit `user_permission`; including it
  makes the plan malformed. A plan JSON not matching this tool's format — missing fields,
  wrong types, unknown/extra fields, any budget other than `1,0`, or `action_parameters` not
  matching exactly {`location`} — is rejected without creating or replacing any plan; the
  rejection lists the invalid fields and asks for a complete new corrected plan call,
  consuming no budget, not counting as the all-`NULL` call, and leaving any active plan
  untouched. Plans are immutable — a different location is a new, replacing plan call.
- **BUDGET — fixed**: submit exactly `"max_calls": 1, "max_calls_buffer": 0`; any other values
  are rejected. The one accepted `set_breakpoint` call consumes the plan even on failure,
  timeout, or cancellation after start; another breakpoint requires a complete replacement
  plan (one breakpoint per plan).
- **PERMISSION — none**: omit `user_permission` from the plan JSON.
- **PRECONDITIONS**: validated session; executable-region classification.
- **WARNINGS**: hardware breakpoints are finite; a breakpoint left behind halts the board
  unexpectedly later.
- **SOFT-GUARDRAILS**:
  1. State what print-statement diagnosis (§8.3) you already ran and why it failed or is
     unsuitable here — a plan whose `hypothesis`/`strategy` shows no prior or considered
     print-based attempt should be reconsidered before submission.
  2. Address is symbol-derived (state the symbol) or justified.
  3. State what you expect when it hits (core HALTED, pc = location) and what state you will
     inspect there.
  4. Commit to removing it — name the `remove_breakpoint` follow-up in `strategy`.
- **EXIT**: remove the breakpoint when the task step completes; leave the core in a
  deliberate state.
- **EXAMPLE-PLAN**: budget `1,0`; `action_parameters: {"location": "uart_rx_handler"}` — with
  a `hypothesis` that records the failed print-based attempt, e.g., "Tagged prints ([TRC-01..04],
  tracked in uart_debug_prints.md) narrowed the hard fault to uart_rx_handler, but the fault
  fires before the next print flushes; a halt at entry is needed to read the registers."

**`remove_breakpoint` (always available — no plan tool).** Its tool description must carry
the matching doctrine in short form: breakpoints are the escalation path after print-statement
logging (§8.3); every `set_breakpoint` should be paired with a `remove_breakpoint` once the
inspection is done, and no breakpoint may be left behind at task completion.

---

## 6. Firmware and Registers

### 6.1 `flash_application-plan` (guards `flash_application`)

- **PURPOSE**: flash a firmware artifact into the **application** partition only.
- **USE-WHEN / NOT-WHEN**: deploying a rebuilt application. Never for bootloader images
  (`flash_bootloader`) and never a way to write arbitrary flash — the server verifies every
  loadable segment, required erase sector, entry point, and vector table fit the
  linker-derived application partition, and that live MCU identity matches the profile.
- **ACTION-PARAMETERS**: `artifact_path` (ELF/HEX from the selected build configuration),
  `halt_after` (bool, default false).
- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, with real
  reasoning about what changed in this build and how you will verify it ran — boilerplate is
  rejected. This tool is **not** permission-locked: omit `user_permission`; including it
  makes the plan malformed. A plan JSON not matching this tool's format — missing fields,
  wrong types, unknown/extra fields, any budget other than `1,0`, or `action_parameters` not
  matching exactly {`artifact_path`, `halt_after`} — is rejected without creating or
  replacing any plan; the rejection lists the invalid fields and asks for a corrected plan
  call, consuming no budget. Plans are immutable — a rebuilt artifact (new path or content)
  is a new, replacing plan call.
- **BUDGET — fixed**: submit exactly `"max_calls": 1, "max_calls_buffer": 0`; any other values
  are rejected. The one accepted flash consumes the plan even if it fails, times out, or is
  cancelled after starting (an in-progress flash is allowed to finish); flashing again — same
  or new artifact — requires a complete replacement plan.
- **PERMISSION — none**: omit `user_permission` from the plan JSON.
- **PRECONDITIONS**: validated session; open gate; **fingerprint freshness** — after a
  rebuild, run `board_safety_refresh` first or the gate closes on you; stated verbatim.
- **WARNINGS**: interrupting flash risks incomplete firmware (the server lets an in-progress
  flash finish on cancellation); a wrong-but-valid image simply runs wrong — recover by
  flashing a correct build.
- **SOFT-GUARDRAILS**:
  1. Artifact freshly built from current source (state build config).
  2. Board named matches the artifact's intended board.
  3. State the observable post-flash behavior you expect (e.g., UART boot text) and how you
     will check it.
  4. If you edited the linker, stop — linker changes are exceptional and need safety-map
     rebuild, not a flash.
- **EXIT**: board resets and runs (or halts if requested); verify behavior via the planned
  observation.
- **EXAMPLE-PLAN**: budget `1,0`;
  `action_parameters: {"artifact_path": "firmware/nucleo_l476rg/reference/build/firmware.elf", "halt_after": false}`;
  no `user_permission` field.

### 6.2 `flash_bootloader-plan` (guards `flash_bootloader`) — permission-locked

- All of 6.1, with these substitutions:
- **PURPOSE**: bootloader partition only; application, prohibited, ROM-bootloader, and
  unknown regions rejected.
- **VALIDATION**: as 6.1, except `user_permission` is **required**: a plan missing or
  carrying an invalid `user_permission` without an active full-session grant is rejected as
  malformed — no plan created or replaced, invalid fields listed, corrected call requested,
  no budget consumed. `action_parameters` must match exactly {`artifact_path`, `halt_after`}.
- **BUDGET — fixed**: submit exactly `"max_calls": 1, "max_calls_buffer": 0`; any other values
  are rejected. The one accepted bootloader flash consumes the plan even on failure, timeout,
  or cancellation after start; another attempt requires a complete replacement plan — and a
  fresh user prompt if the grant was one-time.
- **PERMISSION — required**: ask the user plainly first, explaining what a failed bootloader
  means; conversation itself is never permission — the grant counts only when passed as
  `user_permission`. `"one-time"` covers exactly this one flash and requires the `1,0`
  budget. `"full-session"` covers `flash_bootloader` + this board for the rest of the Server
  Run — appropriate for repeated bootloader iterations this run; later plans may then pass
  `user_permission: null`, and this NULL response will say so when the grant is active.
  Missing/invalid permission with no active grant fails the plan call with a permission
  request.
- **WARNINGS**: a bad bootloader can leave the application unbootable — still recoverable
  over SWD, but the user must be told before granting.
- **SOFT-GUARDRAILS**:
  1. Is a bootloader flash truly required, or is this an application change?
  2. Artifact from the **bootloader** build configuration (not the app build).
  3. User explicitly informed what a failed bootloader means.
  4. Post-flash verification plan stated (e.g., bootloader banner on UART).
- **EXAMPLE-PLAN**: as 6.1 plus `"user_permission": "one-time"` and the bootloader artifact
  path.

### 6.3 `register_write-plan` (guards `register_write`)

- **PURPOSE**: one peripheral/configuration register write: exact `address`, `mask`, `value`.
- **USE-WHEN / NOT-WHEN**: poking a peripheral (GPIO, timer, UART config) during diagnosis.
  The full affected range must lie inside a mapped peripheral window and outside every
  prohibited subrange. Flash-security, option-byte, OTP, debug-protection, and lifecycle
  registers are **unavailable, period** — no plan makes them writable. Labeling an address a
  "register" does not make it one.
- **ACTION-PARAMETERS**: `address` (hex), `mask` (hex — bits to modify), `value` (hex).
- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, with real
  reasoning that shows the documentation check (see SOFT-GUARDRAILS) was done — boilerplate
  is rejected. This tool is **not** permission-locked: omit `user_permission`; including it
  makes the plan malformed. A plan JSON not matching this tool's format — missing fields,
  wrong types, unknown/extra fields, any budget other than `1,0`, or `action_parameters` not
  matching exactly {`address`, `mask`, `value`} — is rejected without creating or replacing
  any plan; the rejection lists the invalid fields and asks for a corrected plan call,
  consuming no budget. Plans are immutable — a different register, mask, or value is a new,
  replacing plan call.
- **BUDGET — fixed**: submit exactly `"max_calls": 1, "max_calls_buffer": 0`; any other values
  are rejected. The one accepted write consumes the plan even on failure, timeout, or
  cancellation after start; another write requires a complete replacement plan.
- **PERMISSION — none**: omit `user_permission` from the plan JSON.
- **PRECONDITIONS**: validated session; open gate; freshness; peripheral-window
  classification.
- **WARNINGS**: wrong peripheral writes can wedge clocks/pins; recoverable by reset.
- **SOFT-GUARDRAILS** (the doc-verification workflow, stated as steps):
  1. Read the reference manual/datasheet section for the register.
  2. Read the machine register description (SVD).
  3. Confirm name, address, and field agree between them.
  4. Compute the mask for read-modify-write — state which bits you are deliberately not
     touching.
  5. Note peripheral side effects (e.g., write-1-to-clear fields).
- **EXIT**: read the register back where readable; note expected behavior change.
- **EXAMPLE-PLAN**: budget `1,0`;
  `action_parameters: {"address": "0x48000014", "mask": "0x00000020", "value": "0x00000020"}`;
  no `user_permission` field.

---

## 7. Reset and Recovery

### 7.1 `reset_and_halt-plan` (guards `reset_and_halt`)

- **PURPOSE**: reset the MCU and halt immediately at startup; session stays active.
- **USE-WHEN / NOT-WHEN**: firmware crashes immediately and you must inspect startup. If you
  just need a restart, `reset_and_run` is **always available and needs no plan** — say so
  first. This is not an unlock: it never changes security state.
- **ACTION-PARAMETERS**: none (empty object `{}`).
- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, with real
  reasoning about what you will inspect at the halted start — boilerplate is rejected. This
  tool is **not** permission-locked: omit `user_permission`; including it makes the plan
  malformed. A plan JSON not matching this tool's format — missing envelope fields, wrong
  types, unknown/extra fields, budget over the multi-call ceilings, or a non-empty
  `action_parameters` object — is rejected without creating or replacing any plan; the
  rejection lists the invalid fields and asks for a corrected plan call, consuming no budget.
  Plans are immutable.
- **BUDGET — multi-call**: set `max_calls` to the reset-and-inspect iterations your strategy
  expects and `max_calls_buffer` as leeway for iterations that miss the state you were after —
  every executed reset consumes one call. Total = `max_calls + max_calls_buffer`; ceilings
  `max_calls ≤ 20`, `max_calls_buffer ≤ 10`. Recommended for a startup-inspection loop:
  `"max_calls": 2, "max_calls_buffer": 1`.
- **PERMISSION — none**: omit `user_permission` from the plan JSON.
- **PRECONDITIONS**: validated session; plan + board match.
- **WARNINGS**: leaves the core halted — firmware is not running until you resume or reset.
- **SOFT-GUARDRAILS**:
  1. Confirm `reset_and_run` is insufficient and why.
  2. Set needed breakpoints before resuming from the halt.
  3. State what you will inspect while halted.
- **EXIT**: resume or `reset_and_run` when done — do not leave the board silently halted.
- **EXAMPLE-PLAN**: budget `2,1`; `action_parameters: {}`; no `user_permission` field.

### 7.2 `connect_under_reset-plan` (guards `connect_under_reset`)

- **PURPOSE**: assert the physical reset line, attach over SWD while reset is active, halt
  the core, release reset.
- **USE-WHEN / NOT-WHEN**: firmware sleeps immediately, reconfigures debug pins/clocks, or
  crashes so early that normal attach fails. Requires the probe's reset line to be wired and
  supported — the tool fails cleanly otherwise. Not an unlock; a locked target still needs
  `target_unlock`.
- **ACTION-PARAMETERS**: none (empty object `{}`).
- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, with real
  reasoning recording how normal attach failed — boilerplate is rejected. This tool is
  **not** permission-locked: omit `user_permission`; including it makes the plan malformed.
  A plan JSON not matching this tool's format — missing envelope fields, wrong types,
  unknown/extra fields, budget over the multi-call ceilings, or a non-empty
  `action_parameters` object — is rejected without creating or replacing any plan; the
  rejection lists the invalid fields and asks for a corrected plan call, consuming no budget.
  Plans are immutable.
- **BUDGET — multi-call**: set `max_calls` to the attach attempts your strategy expects and
  `max_calls_buffer` as leeway for attempts that fail inconclusively — every executed attempt
  consumes one call. Total = `max_calls + max_calls_buffer`; ceilings `max_calls ≤ 20`,
  `max_calls_buffer ≤ 10`. Recommended: `"max_calls": 2, "max_calls_buffer": 1`.
- **PERMISSION — none**: omit `user_permission` from the plan JSON.
- **PRECONDITIONS**: validated session (or the documented recovery-attach path); probe
  reset-line support.
- **WARNINGS**: asserts physical reset — the running firmware is stopped without warning.
- **SOFT-GUARDRAILS**:
  1. Describe how normal attach failed (exact error).
  2. Confirm the probe/reset-line wiring supports it.
  3. Have a next step planned for the halted core.
- **EXIT**: core is halted post-attach; proceed deliberately, end with a deliberate
  run/halt state.
- **EXAMPLE-PLAN**: budget `2,1`; `action_parameters: {}`; no `user_permission` field.

### 7.3 `target_unlock-plan` (guards `target_unlock`) — permission-locked, destructive

The longest response; must additionally cover the two-phase approval flow.

- **PURPOSE**: documented vendor recovery of a locked target (typically mass erase +
  unlock). Never arbitrary writes to security/provisioning registers.
- **USE-WHEN / NOT-WHEN**: only when the target is confirmed locked (e.g.,
  `validation_blocked` with a locked-target cause) and no non-destructive path remains.
  Reset tools never unlock — they only reset.
- **ACTION-PARAMETERS**: `recovery_mechanism` (the documented vendor procedure identifier
  supported by the connected probe/target tooling) plus any mechanism-specific fields the
  NULL response enumerates for the board's family. If the mechanism is unknown, this plan
  tool returns a research prompt — research identifies the documented procedure; **research
  never authorizes execution**.
- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, with real
  reasoning recording the locked-state evidence and exhausted alternatives — boilerplate is
  rejected. `user_permission` is **required** at the approval phase (see PERMISSION). A plan
  JSON not matching this tool's format — missing envelope fields, wrong types, unknown/extra
  fields, **any budget other than `1,0`**, `action_parameters` not matching the enumerated
  recovery fields, or an approval resubmission whose fields differ in any way from the
  disclosed plan — is rejected without creating or replacing any plan; the rejection lists
  the invalid fields and asks for a complete new corrected plan call, consuming no budget and
  voiding nothing that was already approved (though a *changed* resubmission voids its own
  approval). Plans are immutable.
- **BUDGET — fixed**: submit exactly `"max_calls": 1, "max_calls_buffer": 0`; the server
  rejects anything else, with emphasis. The one accepted `target_unlock` call consumes the
  plan even on failure, timeout, or cancellation after start; any further recovery attempt
  requires a complete replacement plan **and fresh user permission** — always.
- **PERMISSION — required, two-phase (must be described step by step)**:
  1. Submit the populated plan. Instead of unlocking, the tool returns `permission_required`
     containing: exact live MCU + board identity, the mechanism, whether it mass-erases,
     **every known erase range/partition**, banks/sectors, whether all nonvolatile memory is
     erased, expected loss (application, bootloader, configuration, user data), and a
     `plan_id`.
  2. Present that disclosure to the user in plain language; conversation itself is never
     permission. Get explicit approval.
  3. Resubmit the **complete, unchanged** plan with `"user_permission": "one-time"`. Any
     changed field voids the approval. The tool then unlocks `target_unlock` and redirects.

  Approval is single-use, short-lived, bound to that plan + target identity + erase ranges,
  and invalidated if target, probe, safety map, or plan changes. **Mass erase requires fresh
  permission every time** — `full-session` never covers it, and prior approvals, other
  boards' approvals, and conversational assent never count.
- **PRECONDITIONS**: confirmed locked state; active plan; approval bound and current.
- **WARNINGS**: this erases exactly what the disclosure says — repeat it back to the user;
  if the device only has full-chip erase, all nonvolatile memory goes.
- **SOFT-GUARDRAILS**:
  1. Confirm locked state from a server status, not inference.
  2. List the non-destructive options you exhausted.
  3. Confirm the user understands what will be erased and what must be reflashed afterward.
  4. Have the post-recovery plan ready (reflash + `board_validate`).
- **EXIT**: after success the gate **stays closed** until `board_validate` passes again;
  then reflash firmware via the normal flash plans.
- **EXAMPLE-PLAN**: budget `1,0`; `"user_permission": "one-time"`;
  `action_parameters: {"recovery_mechanism": "nrf_pyocd_unlock"}` — resubmitted unchanged
  after the `permission_required` disclosure is approved.

---

## 8. UART

Both UART plan tools share the instrumentation protocol in §8.3 — it must be rendered in
**both** NULL responses.

### 8.1 `read_serial-plan` (guards `read_serial`)

- **PURPOSE**: capture bounded UART output from the board.
- **USE-WHEN / NOT-WHEN**: observing boot text, log output, or injected diagnostic prints
  (§8.3). Not a continuous monitor — every capture is bounded by `read_seconds`.
- **ACTION-PARAMETERS**: `expected_text` (string|null — null means any output matches; for
  exploratory/diagnostic capture **use null** so one plan covers many differently-worded
  reads), `read_seconds` (> 0, default 3.0), `baudrate` (null → profile default; must be
  positive), `port` (null → resolved), `reset_on_open` (bool — reset after the port opens to
  capture early boot text deterministically).
- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, with real
  reasoning about what output you expect and how each capture advances the diagnosis —
  boilerplate is rejected. This tool is **not** permission-locked: omit `user_permission`;
  including it makes the plan malformed. A plan JSON not matching this tool's format —
  missing envelope fields, wrong types, unknown/extra fields, budget over the multi-call
  ceilings, non-positive `read_seconds`/`baudrate`, or `action_parameters` not matching
  exactly {`expected_text`, `read_seconds`, `baudrate`, `port`, `reset_on_open`} — is
  rejected without creating or replacing any plan; the rejection lists the invalid fields and
  asks for a corrected plan call, consuming no budget. Plans are immutable — changing
  `read_seconds` or `expected_text` mid-task is a replacement plan (hence: plan generous,
  generic parameters).
- **BUDGET — multi-call, explained concretely**: set `max_calls` to the number of capture
  windows your strategy needs and `max_calls_buffer` for retries — captures that start late,
  catch nothing, or miss the window **still consume a call**. Total =
  `max_calls + max_calls_buffer`; ceilings `max_calls ≤ 20`, `max_calls_buffer ≤ 10`. Every
  capture uses the exact frozen parameters. Recommended for a §8.3 debugging session:
  `"max_calls": 6, "max_calls_buffer": 4`.
- **PERMISSION — none**: omit `user_permission` from the plan JSON.
- **PRECONDITIONS**: validated session; plan + board match; exact frozen parameters.
- **WARNINGS**: a halted core produces no output — check `get_state` before burning calls;
  opening the port can reset some boards' auto-reset circuits.
- **SOFT-GUARDRAILS**:
  1. Core running (not halted) if you expect output.
  2. Capture window long enough for the event you await.
  3. If diagnosing via prints, the §8.3 protocol is in place **before** planning reads.
  4. Budget honestly — count the observations your strategy needs.
- **EXIT**: no cleanup for the port (server-owned); §8.3 cleanup applies when
  instrumentation was used.
- **EXAMPLE-PLAN**:

```json
{
  "board_id": "left_controller",
  "hypothesis": "The motor task stalls after the third encoder interrupt; injected trace prints (tracked in uart_debug_prints.md) will show the last checkpoint reached.",
  "strategy": "Capture 6 windows of 5s each across reproductions; correlate [TRC-nn] tags against the tracking file to locate the stall; buffer covers mistimed captures.",
  "hypothesis_made": true,
  "strategy_evaluated": true,
  "expected_fail_return": "Captures show all checkpoints including [TRC-07] after the third interrupt (hypothesis wrong)",
  "expected_success_return": "Captures end at [TRC-05] or [TRC-06], never reaching [TRC-07]",
  "max_calls": 6,
  "max_calls_buffer": 4,
  "action_parameters": {
    "expected_text": null,
    "read_seconds": 5.0,
    "baudrate": null,
    "port": null,
    "reset_on_open": false
  }
}
```

### 8.2 `write_serial-plan` (guards `write_serial`)

- **PURPOSE**: send bounded UTF-8 text to the board's UART.
- **USE-WHEN / NOT-WHEN**: driving a firmware CLI/test menu, triggering a code path under
  test. The exact `text` is frozen into the plan — one plan per distinct command; for a
  command sequence, plan the calls you need or use separate plans per distinct command.
- **ACTION-PARAMETERS**: `text` (required), `baudrate` (null → profile default, positive),
  `port` (null → resolved), `append_newline` (bool — set true when firmware reads
  line-oriented input), `timeout_seconds` (> 0, default 1.0).
- **VALIDATION**: `hypothesis_made` and `strategy_evaluated` must be `true`, with real
  reasoning about what the firmware should do on receiving this text and how you will
  observe it — boilerplate is rejected. This tool is **not** permission-locked: omit
  `user_permission`; including it makes the plan malformed. A plan JSON not matching this
  tool's format — missing envelope fields, wrong types, unknown/extra fields, budget over
  the multi-call ceilings, empty `text`, non-positive `timeout_seconds`/`baudrate`, or
  `action_parameters` not matching exactly {`text`, `baudrate`, `port`, `append_newline`,
  `timeout_seconds`} — is rejected without creating or replacing any plan; the rejection
  lists the invalid fields and asks for a corrected plan call, consuming no budget. Plans
  are immutable — a different command string is a new, replacing plan call.
- **BUDGET — multi-call**: set `max_calls` to the number of times you will send this exact
  text and `max_calls_buffer` for sends that complete but produce no observable response —
  every executed send consumes one call. Total = `max_calls + max_calls_buffer`; ceilings
  `max_calls ≤ 20`, `max_calls_buffer ≤ 10`. Recommended for repeated triggering of one test
  command: `"max_calls": 3, "max_calls_buffer": 1`.
- **PERMISSION — none**: omit `user_permission` from the plan JSON.
- **PRECONDITIONS**: validated session; plan + board match; exact frozen parameters.
- **WARNINGS**: input reaches live firmware — a destructive firmware command (its own erase/
  reset menus) is your responsibility to recognize before sending.
- **SOFT-GUARDRAILS**:
  1. Confirm the firmware is in the state that accepts this input.
  2. Check whether a newline is required (`append_newline`).
  3. Pair with a planned `read_serial` to observe the response.
  4. If driving a diagnostic flow, the §8.3 protocol applies.
- **EXIT**: server-owned port cleanup; §8.3 cleanup applies when instrumentation was used.
- **EXAMPLE-PLAN**: budget `3,1`;
  `action_parameters: {"text": "test motor", "baudrate": null, "port": null, "append_newline": true, "timeout_seconds": 1.0}`;
  no `user_permission` field.

### 8.3 UART Diagnostic Instrumentation Protocol (required block, both UART responses)

Rendered under **SOFT-GUARDRAILS** in both `read_serial-plan` and `write_serial-plan` NULL
responses, in substantially this form:

> **You cannot see the board. Prints are your eyes.**
>
> When testing an implementation or diagnosing a bug via UART:
>
> 1. **Instrument heavily.** Inject a high volume of print statements into the firmware
>    source at every decision point, state transition, ISR entry/exit, and suspect branch on
>    the path under test. Do not be conservative — dense instrumentation is how you observe
>    execution flow without hardware visibility.
> 2. **Track every print in a markdown file.** Before flashing, create or update a tracking
>    file in the workspace (e.g., `uart_debug_prints.md`) with one entry per injected print:
>    source file, function, line, the exact print text/tag, and what observing it proves.
>    Give each print a unique greppable tag (e.g., `[TRC-01]`, `[TRC-02]`) so captured output
>    maps unambiguously back to code locations.
> 3. **Treat captures as hardware observations.** Use `read_serial` captures of these tags to
>    reconstruct the real execution flow — which paths ran, in what order, with what values —
>    exactly as if you were watching the board. Reason from what the prints show, not from
>    what the code "should" do.
> 4. **Budget for it.** Instrumented debugging takes multiple capture windows — set
>    `max_calls`/`max_calls_buffer` accordingly, and remember a rebuild + reflash
>    (with `board_safety_refresh` after relinking) is needed each time you change the
>    instrumentation.
> 5. **Clean up completely when the task is done.** Walk your tracking markdown entry by
>    entry and **delete every print statement you injected for this task** from the source.
>    Verify removal (grep for your tags — zero hits), rebuild, and reflash the clean
>    firmware. Then delete or clear the tracking file. Leaving diagnostic prints in the
>    codebase, or an untracked print you cannot find later, is a failed cleanup.

---

## 9. Coverage Matrix

| Plan tool | Budget | `user_permission` | Special blocks |
| :--- | :--- | :--- | :--- |
| `board_setup-plan` | fixed `1,0` | required | paired setup+fix allowance; `load_setup_tool` prerequisite |
| `connect_override-plan` | multi | omit | no-profile-rewrite rule |
| `write_cpu_register-plan` | fixed `1,0` | omit | allowed-register class |
| `set_execution_state-plan` | fixed `1,0` | required | execution-register class; disruption warning |
| `read_memory_address-plan` | multi | omit | symbol-first doctrine |
| `write_memory-plan` | fixed `1,0` | omit | symbol-first + fallback flag/reason; RAM-only fallback |
| `set_breakpoint-plan` | fixed `1,0` | omit | prints-first / step-through-second doctrine (§5.3, §8.3); executable region; remove_breakpoint free |
| `flash_application-plan` | fixed `1,0` | omit | partition containment; rebuild → refresh |
| `flash_bootloader-plan` | fixed `1,0` | required | bootloader risk disclosure |
| `register_write-plan` | fixed `1,0` | omit | doc-verification steps; prohibited registers |
| `reset_and_halt-plan` | multi | omit | reset_and_run-is-free notice; not-an-unlock |
| `connect_under_reset-plan` | multi | omit | reset-line requirement; not-an-unlock |
| `target_unlock-plan` | fixed `1,0` | required (fresh per mass erase) | two-phase disclosure/approval flow |
| `read_serial-plan` | multi | omit | buffer semantics; §8.3 instrumentation protocol |
| `write_serial-plan` | multi | omit | frozen-text note; §8.3 instrumentation protocol |

Notes:

- Every per-tool layout above is **self-contained**: its VALIDATION, BUDGET, and PERMISSION
  blocks restate the full rules from §1 specialized to that tool, and the rendered NULL
  response must include them in full rather than referencing §1.
- `board_fix_setup` is covered by `board_setup-plan` (paired allowance) and has no NULL
  response of its own.
- `remove_breakpoint`, `reset_and_run`, `find_symbol`, `read_memory_symbol`,
  `read_cpu_register`, `read_execution_state`, `connect`, `disconnect`, `get_board_info`,
  `get_state`, `halt`, `resume`, `step`, `action_batch`, `wait` are always available and have
  no plan tool; several are named inside other tools' USE-WHEN sections as the cheaper
  alternative.
- Per Design_Proto_Spec AC-5.4, every permission-locked response's **[PERMISSION]** section is
  stateful: when full-session is already active for that tool + board it must say so and state
  that `user_permission` may be `null`.
- The §8.3 instrumentation protocol is a product-requirement addition on top of
  Design_Proto_Spec (extends the tool-description guidance of §3.14.8); fold it into that
  spec's next revision.
