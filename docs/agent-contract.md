# Agent contract

This document defines how an MCP agent must interact with BYO Server. The live
tool schema and server enforcement remain authoritative.

## Start and discovery

Call `initialization_handshake` after every new server connection. Use the
returned `tools/list` as the current advertised surface and respond to
`notifications/tools/list_changed`. Never guess or directly call an unlisted
action. A client whose callable bindings remain static may use only the exact
single-child `action_batch` fallback returned by an accepted plan, unchanged.
That fallback is transport compatibility, not authority. Visibility is
advisory; every action still has a physical handler lock.

Ask the user in ordinary language for one familiar name per connected board,
or “no board.” Do not ask for board IDs, connection IDs, continuation tokens,
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
repeat the all-NULL planning tutorial or ask the agent to construct the plan again. Prefer the direct action when the client
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
mass erase. Revocation, disconnect, replacement, target/probe/fingerprint
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
authoritative PDF datasheet. Supply its digest only as an optional cross-check;
the server computes and records the authoritative SHA-256. Select UART by the
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
For target/pack research it is the exact official-source schema. After an
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

Validation has exactly seven results:

- `validation_passed`
- `validation_passed_uart_not_configured` means the profile has no expected UART
  content assertion, not that the UART hardware is unavailable. Use
  `get_setup_status.ready_for_uart_work` for current attachment readiness.
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

Every raw or symbol-resolved memory read checks the exact scalar width or block
length before backend access. Reads may use mapped RAM, flash, ROM, CPU-system,
or peripheral regions, but UNKNOWN and PROHIBITED spans are denied. For
`safety/unknown`, complete `board_safety_setup`; for `safety/prohibited`, choose
a mapped, non-prohibited address. A safety rebuild never turns a deliberately
prohibited security/provisioning range into readable memory.

Follow the exact remedy named in a refusal:

- `board_validate` establishes a live validation stamp;
- `board_safety_refresh` handles scoped application, already-authorized
  bootloader, pack, or official-evidence drift while a live stamp still exists;
- `board_safety_setup` rebuilds structural safety evidence; and
- full safety setup plus validation is required for board/target, geometry, or
  schema anchor changes. Unclear scope routes to full safety setup.

Refresh accepts symmetric `application_elf`/`application_hex`/`application_map`
and `bootloader_elf`/`bootloader_hex`/`bootloader_map` fields. An explicit
bootloader refresh can replace only build-derived regions already inside a
server-owned reviewed bootloader partition; it cannot create or widen that
partition. A board without complete pinned catalog evidence returns terminal
`safety_setup_unsupported_board`, lists the reviewed automatic board types,
and exposes no unusable continuation. Adding support is a maintainer task that
requires pinned device-support and official-document evidence, runtime
identity, deterministic reconciliation, and reviewed geometry—not caller
ranges.

For pack or official-evidence drift, refresh reloads the current repository-
pinned assets and installed reviewed runtime, reruns deterministic two-source
reconciliation, and promotes the coupled replacement only if retained build
regions can be reproduced from their content-addressed artifacts. A failed
reconciliation returns an explicit blocked refresh with a report and leaves the
old map closed.

Before a coding workflow begins, require `get_setup_status` to report both
`configuration_ready` and `live_session_ready`. After relinking, pass the
selected application ELF to `board_safety_refresh`; the build may narrow but
cannot widen the catalog deployment envelope. Pass bootloader artifacts only
when the existing map already owns a reviewed bootloader envelope. Use
`serial_exchange` when a
console command's immediate acknowledgement or later command depends on
volatile application state. It validates the complete bounded step list before
opening one UART handle. Separate serial calls may reopen a board UART and some
adapters reset on open; `clear_input` and capture reopen are explicit opt-ins.
After flash/reset, `ready_probe_delay_seconds` provides a bounded same-open
observation window in which an unsolicited boot/prompt marker can satisfy
readiness before the one planned readiness probe is sent.

For a checkout-local clean-root acceptance, prefer the bounded
`scripts/run_fresh_workspace_e2e.py` setup-only runner. Supply every required
identity explicitly and pass `--authorize-setup` only after the user has
approved that non-destructive attempt. The runner cannot launch code, a build,
flash, UART writes, or arbitrary commands. Treat a nonzero exit or any evidence
status other than `pass` as a hard stop; only a separate orchestrator may begin
coding after verifying the exact readiness payload.

When `get_setup_status` includes `build_guidance`, follow its provider-neutral
`native_project_build` workflow first: reuse the project's validated local IDE
or CLI, then call the returned `collect_build_artifacts` template with the
outputs that actually exist. A reviewed profile may also return a labeled
`toolchain_fallback`; use the Zephyr fallback only for a Zephyr project without
an already-valid local build command. Treat every build route as advisory only:
do not infer memory permission from it, and always refresh safety from the final
ELF/map.

For any other native build system, keep using its normal validated CLI or IDE
build and optionally normalize explicit outputs with the always-visible
`collect_build_artifacts` MCP tool (or
`python -m pyocd_debug_mcp.artifact_collector` outside MCP). Supply typed `--elf`, `--hex`,
`--bin`, and `--map` paths rather than asking the collector to search or guess.
The result uses canonical `firmware.*` names and a deterministic provenance
manifest, but it grants no memory authority. Pass the canonical ELF/HEX/MAP
paths explicitly to `board_safety_refresh`; the safety flow does not currently
ingest the collector manifest automatically. HEX-only and BIN-only bundles may
be valid provenance outputs, but a raw BIN has no trusted address and collection
alone never makes either safe to flash.

For Zephyr sysbuild, the helper selects the generated `domains.yaml` default
domain and takes ELF, HEX, BIN, and linker map only from that coherent image. It
does not choose an aggregate or bootloader image by basename or directory depth,
and a missing application linker map is a clear build-output failure rather than
a silently incomplete safety handoff.

Recovery plans use the target-neutral `backend_mass_erase` mechanism. The
server checks that the live typed backend reports that capability before it
renders a disclosure or asks for fresh one-time permission. Never substitute a
vendor command, and never treat a legacy profile label as authorization.

Apply the same local-first rule to all heavy dependencies. Before downloading
an SDK, RTOS, toolchain, device pack, or large library, inspect only bounded
standard locations: explicit/environment paths, the project and its parents,
and normal vendor directories under the user's home/application data. Reuse a
compatible installed NCS/Zephyr tree, STM32CubeIDE-provided STM32Cube/ThreadX
tree, or equivalent vendor package after validating its version, target
support, and executable tools. Do not trust names alone or recursively scan an
entire drive. Explain what compatible component is absent before a large
network fallback, and never copy unrelated discovered files into the project.

For a console-dependent workflow, additionally require
`uart_attachment_ready` and `ready_for_uart_work`. A missing console does not
block a project that does not use UART, but it is an explicit readiness failure
for tests whose acceptance evidence depends on terminal output.

Refresh cannot reopen a gate after disconnect or restart. Never interpret a
disk artifact, plan, permission, or successful refresh as an open gate.

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
