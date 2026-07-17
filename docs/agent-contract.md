# Agent contract

This document defines how an MCP agent must interact with BYO Server. The live
tool schema and server enforcement remain authoritative.

## Start and discovery

Call `initialization_handshake` after every new server connection. Use the
returned `tools/list` as the current advertised surface and respond to
`notifications/tools/list_changed`. Never guess or directly call an unlisted
action. Visibility is advisory; every action still has a physical handler lock.

Ask the user in ordinary language for one familiar name per connected board,
or “no board.” Do not ask for board IDs, connection IDs, continuation tokens,
permission enum values, or structured payloads. Never silently select, rename,
reassign, or rewrite a profile.

## Plans

For a `*-plan` tool, first call it with every field set to JSON `null`. Relay
its guidance, then submit only the exact plan JSON envelope. Put the exact
underlying action arguments inside the nested `action_parameters` object; do
not flatten them and do not add prose, Markdown, a wrapper key, or extra
fields. Omit `user_permission` from populated non-permission plans. Do not use
placeholders or partial NULL requests. An accepted plan is bound to one run,
board, session, tool, and canonical parameter set.

Plan replacement is atomic. A pre-execution refusal does not spend a call.
Once execution starts, success, backend failure, timeout, and cancellation all
spend exactly one call. When a budget is exhausted, initialize and submit a new
plan instead of retrying the hidden action.

## Permission

Conversation is not permission. When a plan requests approval, relay its
ordinary-language disclosure and pass only the exact structured permission
value the tool requests.

`one-time` permission is consumed at execution start. `full-session`
permission applies only where the plan definition allows it and never covers
mass erase. Revocation, disconnect, replacement, target/probe/fingerprint
change, or restart invalidates the applicable authority.

For `target_unlock`, relay the complete live destructive disclosure: board and
target identity, probe identity, vendor mechanism, mass-erase status, every
erased range/bank/sector, all-nonvolatile warning, expected losses, and plan
identifier. Approval is fresh, one-time, and valid only for the unchanged
plan. A successful recovery does not open the gate.

## Setup, research, and validation

Use `load_setup_tool` and `board_setup-plan` for setup or repair. The server
inventories probes, serial ports, cache matches, targets, and builds before it
requests research. Relay only the supplied `agent_prompt` and friendly
`choices`; never expose the rest of the control payload.

Research responses must include exactly `exact_response_fields`. Do not add
sources, explanations, authority, or profile changes unless requested. Never
change `fields_that_must_not_change`, especially `mcu_part_number`. Research
does not grant permission, open a gate, or persist a candidate.

Setup and validation payloads use these common fields:

- `status` and `code`: deterministic machine routing;
- `continuation_id`: opaque resume identity, never user-facing;
- `agent_prompt`: the only prose to relay;
- `choices`: friendly options to present without internal IDs;
- `observed`: server evidence, not user-facing prose;
- `constraints`: rules that remain in force;
- `rejected_candidates`: prior strict research failures;
- `accepted_response`: validated continuation content, if any; and
- `validation_plan`: server-controlled remaining checks.

Validation has exactly seven results:

- `validation_passed`
- `validation_passed_uart_not_configured`
- `validation_needs_user_input`
- `validation_research_required`
- `validation_blocked`
- `validation_failed`
- `validation_incomplete`

Only the first two can stamp the current in-memory gate. A silicon mismatch
must not rewrite the profile. Setup, safety setup, reports, cache hits, or a
prior validation result do not open a gate.

## Safety and remedies

Never supply allowed ranges. The server derives them from tracked build
artifacts and reconciled device evidence. Guarded reads require current board
validation. Writes additionally require a gate whose aggregate fingerprint is
fresh on that call.

Follow the exact remedy named in a refusal:

- `board_validate` establishes a live validation stamp;
- `board_safety_refresh` handles refreshable source drift while a live stamp
  still exists;
- `board_safety_setup` rebuilds structural safety evidence; and
- full setup plus validation is required for board/target anchor changes.

Refresh cannot reopen a gate after disconnect or restart. Never interpret a
disk artifact, plan, permission, or successful refresh as an open gate.

## Batch, cancellation, and exit

`action_batch` contains one board and a bounded list of ordinary child calls.
Do not nest it. Each child is authorized only when it reaches normal dispatch;
the batch stops after the first failure.

Cancellation is best-effort for interruptible work. A flash transaction that
has started completes within its finite backend bound before resources are
released. Do not interrupt `target_unlock` or bootloader flash as an operating
practice.

Observe every safe-exit reminder. Structured `on_exit` is available only on
eligible serial tools and accepts only `uart_write` or `reset_and_run`; never
send shell strings or arbitrary commands. Disconnect explicitly when work is
complete.
