# MCP client contract

This document defines how an MCP client must interact with BYO Server. The live
tool schema and server enforcement remain authoritative.

## Start and discovery

Call `initialization_handshake` after every new server connection. Use the
returned `tools/list` as the current advertised surface and respond to
`notifications/tools/list_changed`. Never guess or directly call an unlisted
action. A client whose callable bindings remain static may use only the exact
single-child `action_batch` fallback returned by an accepted plan, unchanged.
That fallback is transport compatibility, not authority. Visibility is
advisory; every action still has a physical handler lock.

Ask the user in ordinary language for one familiar name per board they want to
use in the current project, or “no board.” Other visible debug probes may remain
unassigned. Do not ask for board IDs, connection IDs, continuation tokens,
permission enum values, or structured payloads. Never silently select, rename,
reassign, or rewrite a profile.

`no board` is a normalized literal sentinel, not a candidate board name. Pass
it to `setup_overview` by itself. If it is mixed with names, ask the user to
clarify in ordinary language and do not route or access hardware.

Pass those familiar names to `setup_overview`. Use its per-name route and
server-generated board ID. Every matching profile, including an incomplete or
previously failed profile, goes to `board_validate` first. Follow its exact
attachment, retry, safety, or repair remedy. Unknown names go to setup. Do not
make the user perform that profile or hardware-inventory matching.

Copy each route's `load_call`, `next_call`, `plan_initialization_call`, and
server-known `plan_action_parameters_template` values directly. Never ask the
user for a board ID, connection ID, stable UART identity, current port path,
datasheet hash, or validation retry field. Ask the user only for the listed
ordinary-language facts and friendly ambiguous hardware choice. The diagnostic
port path may change; the stable identity remains the plan input. After
`load_setup_tool`, follow its bounded guidance for that requested tool only.
For `validation_needs_user_input`, copy the returned `accepted_response` as the
exact retry; it preserves any probe or UART selector already resolved. Terminal
validation statuses deliberately have `accepted_response: null`.

## Plans

For a `*-plan` tool, first call it with every field set to JSON `null`. Relay
its guidance, then submit only the exact plan JSON envelope. Put the exact
underlying action arguments inside the nested `action_parameters` object; do
not flatten them and do not add prose, Markdown, a wrapper key, or extra
fields. Omit `user_permission` from populated non-permission plans. Do not use
placeholders or partial NULL requests. An accepted plan is bound to one run,
board, session, tool, and canonical parameter set.

An accepted plan returns machine-readable `preferred_call` and
`stable_client_fallback` objects plus only concise unlock guidance and reminders. It does not
repeat the all-NULL planning tutorial or require the client to construct the plan again. Prefer the direct action when the client
exposes it. If it does not, submit the fallback's exact `action_batch`
arguments. Never edit its board, child name, or arguments, and never combine a
primary setup call with its separately conditioned paired-repair fallback.
The child traverses the identical plan, permission, validation, gate,
freshness, lock, timeout, budget, event, and cleanup path as a direct call.

Plan replacement is atomic. A pre-execution refusal does not spend a call.
Once execution starts, success, backend failure, timeout, and cancellation all
spend exactly one call. When a budget is exhausted, initialize and submit a new
plan instead of retrying the hidden action.

## Connection routing

Use visible `connect` only with the server-generated `board_id`. It is a
profile-only action: do not supply or infer a probe UID, pyOCD target, external
board-config path, or launch-environment override. Unknown fields are rejected
through direct MCP dispatch and through `action_batch`.

If normal profile/probe resolution fails and a deliberate exceptional manual
connection is appropriate, initialize `connect_override-plan`. Only its hidden
`connect_override` action accepts run-scoped `probe_uid`, `target_override`, or
`external_board_config`; those values never rewrite a profile. Do not use the
override path to conceal a profile/hardware mismatch that setup or validation
should correct.

## Permission

Conversation is not permission. When a plan requests approval, relay its
ordinary-language disclosure and pass only the exact structured permission
value the tool requests.

`one-time` permission is consumed at execution start. `full-session`
permission applies only where the plan definition allows it and never covers
mass erase. Revocation, disconnect, replacement, target/probe/artifact-digest
change, or restart invalidates the applicable authority.

For `target_unlock`, relay the complete live destructive disclosure: board and
target identity, probe identity, vendor mechanism, mass-erase status, every
erased range/bank/sector, all-nonvolatile warning, expected losses, and plan
identifier. Approval is fresh, one-time, and valid only for the unchanged
plan. A successful recovery does not open the gate.

## Setup, research, and validation

Call the all-NULL `board_setup-plan` first whenever hardware access is desired,
before loading it and before any other `*-plan` tool. Ask for the familiar
board name and exact board/MCU identity. Every matching YAML routes first to
`board_validate`; an absent profile or a specific validation remedy routes
through `load_setup_tool` and setup/repair. Fresh setup also requires a local
official PDF datasheet and exact MCU ordering code. Supply its digest only as an
optional cross-check; the server computes the authoritative SHA-256 and captures
the exact bytes. Do not ask the user for a CMSIS-Pack filename or pyOCD target.
If no exact local support exists, research one official pack and return only the
exact response fields; the server derives and verifies the leaf and target.
Select UART by the
stable identity returned by inventory, not by a volatile COM/device path. The server
inventories probes, serial ports, cache matches, targets, and builds before it
requests research. Relay only the supplied `agent_prompt` and friendly
`choices`; never expose the rest of the control payload.

Research responses must include exactly `exact_response_fields`. Do not add
sources, explanations, authority, or profile changes unless requested. Never
change `fields_that_must_not_change`, especially `mcu_part_number`. Research
does not grant permission, open a gate, or persist a candidate.

When setup returns `setup_needs_user_input` or `setup_research_required`, call
`continue_setup` with the same board and continuation plus exactly the returned
response object. For a friendly choice this is only one returned `choice_id`.
For pack research it is the exact official-source schema; never add a proposed
target, address, mask, region, or partition. After an
accepted continuation, call `board_fix_setup` under the still-active paired
allowance. Never retry `board_setup` or bypass the continuation by editing
`.firm`.

The exact current plan fields, budgets, and permission modes are generated from
the runtime source of truth in [`plan-tool-contract.md`](plan-tool-contract.md).
Artifact load addresses for `flash_application` and `flash_bootloader` come
only from the ELF/HEX; neither plan accepts a caller-provided target address.

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

Validation has exactly six results:

- `validation_passed`
- `validation_needs_user_input`
- `validation_research_required`
- `validation_blocked`
- `validation_failed`
- `validation_incomplete`

Only `validation_passed` can stamp the current in-memory gate. Validation proves
the selected probe connection, replayed exact or explicitly compatible live
identity evidence, and association with the current map. Support without a safe
identity proof remains connected-diagnostics-only. It never captures UART or asserts firmware behavior; use
`get_setup_status.ready_for_uart_work` for current attachment readiness. A
compatible core proof permits bounded read/debug and artifact-contained application programming,
but not bootloader or recovery authority; inspect `identity_capability` and
`ready_for_flash_planning` before deployment. A
silicon mismatch must not rewrite the profile. Setup, refresh, reports, cache
hits, or a prior validation result do not open a gate.

## Safety and remedies

Never supply allowed ranges. Stable authority comes from the one strict
`memory_map.yaml`; selected ELF/HEX bytes are checked again at execution time.
Guarded reads require current board validation. Writes additionally require a
live identity proof associated with the current canonical map digest. UNKNOWN
and PROHIBITED spans are denied before backend access. A refresh never turns a
deliberately prohibited security/provisioning range into ordinary memory.

Follow the exact remedy named in a refusal:

- `board_safety_refresh` deterministically rebuilds the complete map from the
    profile plus replayed server-owned evidence. A present unreadable generic map
    refuses replacement because possible one-way deployment ownership cannot be recovered; and
- `board_validate` establishes live identity proof and map association when it
  is absent or an identity anchor changed.

Refresh accepts only `board_id`; it never accepts artifacts or caller ranges.
It may update the map association of existing live identity proof, but cannot
create identity authority. A missing application or bootloader partition stays
fail-closed and cannot be inferred from the full-flash ceiling.

The three validation trigger categories are: first live connection after setup
or server restart; reconnect, disconnect, or connection/probe change; and live
identity mismatch, repair, or recovery. Ordinary build/relink, flash, reset,
UART work, safety refresh, artifact collection, and report/bookkeeping changes
do not themselves trigger validation.

For firmware, inspect the project and use the general native-build helper template returned by
`get_setup_status.build_guidance`: supply the exact argv after `--`, plus cwd/environment/output
parameters as needed. Prefer compatible local tools, but normal acquisition is allowed when none
exist; network is inherited unless `--offline` is explicitly chosen. That option is a best-effort
common-client environment guard, not a network sandbox. The server never detects or selects a
provider, SDK, compiler, target, or installation layout. Optionally call
`collect_build_artifacts`, submit the matching flash plan, then call the flash
action. Do not refresh merely because build bytes changed. Plan acceptance
binds the selected artifact digest. Before backend mutation, execution verifies
that digest and checks ELF/HEX target, load segments, entry point, vector table,
deployment allocation/partition, and erase sectors. A new generic board begins
with no deployment authority. An approved plan-bound application flash creates a minimal
artifact-derived allocation, or monotonically expands it for a larger artifact, after the server
proves the pack driver is sector-bounded. Existing bytes in those sectors may be replaced; the whole
device need not be blank. The allocation is persisted before programming so a failed or partial
flash never leaves modified bytes without durable ownership.
HEX requires a matching ELF
companion. Do not rebuild the selected output concurrently after execution
starts.

`set_breakpoint` likewise requires the current selected ELF. Executability is
proved from that ELF's executable segments; the whole stable application
partition is never treated as executable.

Use `serial_exchange` when a console command's immediate acknowledgement or a
later command depends on volatile application state. UART readiness remains a
separate `get_setup_status` barrier and never establishes live silicon identity.

Recovery plans use the target-neutral `backend_mass_erase` mechanism. The
server checks live backend support, renders the complete map-derived erase
disclosure, and requires fresh one-time permission. Recovery clears live proof
and requires validation afterward.

All heavy dependencies remain local-first. Inspect bounded standard locations,
validate any reused SDK/pack/toolchain, and explain what is absent before a
large network fallback. Never infer authority from a discovered filename.

Refresh cannot reopen a gate after disconnect or restart. Never interpret a
disk artifact, plan, permission, successful refresh, report, or cache entry as
live identity proof.

## Batch, cancellation, and exit

`action_batch` contains one board and a bounded list of ordinary child calls.
Do not nest it. Each child is authorized only when it reaches normal dispatch;
the batch stops after the first failure. A server-generated static-client plan
fallback always contains exactly one child and must be submitted unchanged.

Cancellation is best-effort for interruptible work. A flash transaction that
has started completes within its finite backend bound before resources are
released. Do not interrupt `target_unlock` or bootloader flash as an operating
practice.

Observe every safe-exit reminder. Structured `on_exit` is available only on
eligible serial tools and accepts only `uart_write` or `reset_and_run`; never
send shell strings or arbitrary commands. Disconnect explicitly when work is
complete.
