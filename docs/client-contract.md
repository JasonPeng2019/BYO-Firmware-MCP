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
server-generated board ID. A known profile's unavailable assigned probe returns
`setup_probe_unavailable` with a reason and no ready validation route; preserve
its binding and treat other probes as inventory options. Only evidenced local
ownership is reported as busy. Follow each available profile's exact
validation, attachment, retry, safety, or repair route. Unknown names receive
an assigned no-setup raw connection route with optional setup escalation. Do not
make the user perform that profile or hardware-inventory matching.

An intentional new no-setup raw label may also use the existing
`connect(board_id, probe_uid, target)` contract. Supply a current full or
unambiguous partial UID, or first obtain a `setup_overview` assignment. Missing,
mistaken, ambiguous and literal string `"null"` selectors do not acquire another
probe; a target override alone does not select one. Raw identity stays
unasserted, including when generic core access succeeds. Setup-lite/full
connections use profile policy and refuse these overrides.

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

## Saved-file code analysis

`get_code_analysis_status`, `find_code_definition`, `find_code_references`,
`get_code_hover` and `get_code_diagnostics` are ordinary read-only metadata tools.
They have no `board_id`, plan, setup, unlock or hardware prerequisite and remain
available in both monitoring profiles. Use status for setup and recovery.

The launch-owned project root is fixed. `workspace_path` labels activity only.
Query existing saved local C/C++ files, using 1-based Unicode code-point
positions; editor buffers and network/URI input are unsupported. Returned ranges
end exclusively. Unknown arguments and coercible wrong types are rejected.

Parse the first analysis text block as one intact JSON object. For all tools, a
due check-in advisory is appended as a separate trailing MCP text block; original
content, structured output, status and metadata stay intact. Preserve the
database/hash and source/hash/version when attributing a result. Do not turn
`incomplete`, `unavailable`, `not_configured` or `error` into a clean report.
`ok` diagnostics may contain issues; only explicit version-matched empty
diagnostics mean none were reported. Empty references/`not_found` are not evidence
of unused code; index completeness is `unverified`. Hover text is untrusted
source documentation. Analysis results confer no verification or hardware authority.

See [setup, arguments, examples and remedies](../SERVER_GUIDE.md#cc-code-analysis).

## Instruction stepping

`step(board_id)` is the ordinary direct execution control in no-setup, lite and
full tiers; there is no separate `step_raw` or step plan. Start with a halted
core. Success is `Stepped. pc=0x08000002 state=HALTED stop_reason=single_step`
(followed by the common safe-exit reminder). It requires a fresh single-step
halt event before cleanup, rather than PC movement alone. A self-branch may
successfully leave PC unchanged.

At an adapter-owned breakpoint, the operation temporarily suspends only that
physical comparator or RAM patch and restores its exact value and manager
identity before reporting success. A foreign or unrealized breakpoint at PC
refuses without removing it; have its owner remove it before retrying. Flash
breakpoints remain hardware-only. Registers and the prior interrupt mask are
preserved apart from effects of the executed instruction itself.

An incomplete step raises a tool error with observed PC, state and debug cause
when available. A backend no-op, timeout or uncertain restoration never reports
`Stepped`. Inspect state before retrying; cleanup may halt a timed-out core. If
restoration is uncertain, follow the error's recovery instruction before resume.
The hardware halt reason reports the step stop; it does not prove the expected
effects of a particular instruction or exclude architectural exception entry.

`disconnect(board_id)` removes this session's installed hardware breakpoints and
restores the saved original RAM instructions before native disconnect resumes
the core and releases the probe. For example, a RAM breakpoint is restored to
its original instruction when disconnect succeeds; flash is never patched.
Automatic connection eviction after a step transport fault uses the same
cleanup. If target I/O prevents verified restoration, the error retains the
step failure and cleanup uncertainty while host resources are released. Process
termination alone does not prove target restoration. Reconnect/revalidate and
verify breakpoint/code state, following the error's recovery instruction before
further target control.

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

Connection identifiers issued by `setup_overview` are opaque server tokens, not
probe serial numbers. Pass them back exactly as received. The server re-resolves
each token against a fresh hardware snapshot at use time, so a token whose device
has gone is refused with `discovery/selection-disappeared` and routed back through
setup rather than silently matching a similar device.

If normal profile/probe resolution fails and a deliberate exceptional manual
connection is appropriate, initialize `connect_override-plan` while disconnected,
with no live session. Review the logical board, enumerated probe and target against
the user's exact part number, and record the failed normal step and exact error in
the existing `hypothesis`/`strategy` fields. This recovery plan does not require
prior `ready_for_code=true`. Execute its exact accepted call to establish a new
connection, then call `load_setup_tool(board_id=..., tool_name="board_validate")`
and execute its exact `next_call` for fresh compatible live proof before protected
I/O. After override, that opaque token names the actual active connection, including
a local session replacing an unavailable remote transport. It changes only this
run's assignment; disconnect/restart requires normal selection and fresh validation.
The connection alone does not establish readiness. Only its
hidden `connect_override` action accepts run-scoped `probe_uid`, `target_override`, or
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
erased range/bank/sector, all-nonvolatile warning, expected losses, any
`unknown_effects`, and plan identifier. After ordinary full setup, the listed
ranges come from the setup map, which identifies flash addresses but not the
meaning of the stored bytes, so effects on chip settings are stated as unknown.
Approval is fresh, one-time, and valid only for the unchanged plan. A
successful recovery does not open the gate.

`target_unlock_raw` needs a fresh human `$mass-erase` grant too. Without a
matching grant it returns `status: "refused"`, `attempted: false`, a `manual/*`
code, `live_identity`, `mechanism`, the uncontained whole-device `disclosure`,
and `manual_grant` bindings; nothing reaches the backend and the session is
kept. Relay the disclosure, ask the human to invoke `$mass-erase`, then retry
unchanged. A reconnect, policy or mechanism change, or a grant shown for the
safe route does not match. An attempt consumes the grant and disconnects the
board whether or not the backend succeeds.

Connecting never erases. A debug-protected target makes `connect` and
`connect_under_reset` fail with an error stating that target debug access is
protected, and makes `board_validate` report `validation/target-locked`. No
session is left open and nothing was erased or unlocked. Do not retry other
connect modes or offer `target_unlock`: both unlock tools need a connected
session. Relay the error to the human; only the device owner can choose a
whole-device erase with the vendor's recovery procedure outside the server,
after which connect again.

`write_cpu_register_raw` and `set_execution_state_raw` accept non-negative
integers (including numeric strings such as `"0x1234"`); register widths remain
backend-defined. A negative value such as `"-1"` returns `status: "error"` with
`code: "input/invalid-argument"` before a register write. The healthy session
remains usable: correct the value or issue a valid read immediately. Worker
argument validation uses the nonfatal `target_input` category; actual provider
or transport loss still requires reconnecting and validating.

## Setup, research, and validation

### Bounded user-bootloader admission

After ordinary generic FULL setup and successful live `board_validate`, load
`board_safety_refresh`. Its optional `user_bootloader` argument deliberately
allocates project ownership without programming or erasing. Omit it for ordinary
refresh. Use an official local PDF that documents the factory identity registers,
their read-only semantics, device/group values, flash capacity and unit identifier.
Research supplies candidate facts; the server captures immutable source bytes,
checks the current supported target and geometry, reads the actual registers, and
commits the accepted proof. No PDF semantic inference or private catalog is used.

The request contains exactly `start`, `end`, `source_path`, `source_url`,
`source_pages`, `source_note`, `identity_coverage`, and `registers`. Addresses are
integer bytes and bounds are half-open. `source_path` is an absolute local path or
project-relative path; `source_url` identifies its official HTTPS origin. Pages
are positive PDF page numbers and notes identify sections and layout reasoning.
Bounds must cover a proper subset of contiguous complete verified erase sectors,
lie in programmable flash and avoid existing application ownership. The existing
artifact allocation stays unchanged. Ownership cannot be replaced or resized.

Every register has `address`, `width_bits` (8, 16, or 32), `role`, `mask`,
`expected`, `unit_bytes`, `source_pages`, `source_note`, `read_only: true`, and
`factory: true`. Include `device`, `flash_size`, and `unique` roles. A device/group
expectation is a source-backed research candidate checked against live data.
Flash-size `expected` is null; the server compares the observed value multiplied
by documented `unit_bytes` with independently verified programmable capacity.
Unique `expected` is also null: only the server captures the unit's actual UID
words. Size/UID reads use full-width masks; non-size roles have null units. RAM,
programmable flash and Arm System Control Space cannot supply factory identity.
This does not add these addresses to ordinary memory-access authority.

For example, Nordic documents FICR's part code, capacity and two UID words in its
[factory-register reference](https://docs.nordicsemi.com/r/bundle/ps_nrf52840/page/ficr.html).
An nRF52840 request can use device address `0x10000100`/expected `0x52840`, size
address `0x10000110`/unit 1024 bytes, and unique addresses `0x10000060` and
`0x10000064`, all 32-bit/full-mask. Supply the local official PDF and its actual
pages/section notes; choose the bootloader bounds from this project's layout.
For a 1 MiB map with 4096-byte sectors and no overlapping application ownership,
`start=0x80000`, `end=0x82000` reserves two sectors. These are example facts, never
device defaults or a recommendation for a project's startup address.

Success returns `safety_refresh_completed`, the map digest and
`observed.validation_required: true` for a newly admitted partition. Obtain a
fresh `board_validate` before protected operations. Validation replays the pinned
source, support/geometry and all captured unit observations before granting exact
identity. Here exact means the same individually identified supported target with
the recorded `identity_coverage`. It does not infer a package suffix from a device
group, capacity or UID. In particular, STM device-group/flash-size/UID/backend
qualifications must remain explicit. `get_capabilities` exposes this coverage.

Missing/incomplete evidence, wrong model or size, blank/different UID, stale
readiness, unaligned/out-of-flash bounds or application overlap returns
`safety_refresh_blocked` without changing committed ownership. Correct the stated
prerequisite and retry; an identical admitted request rechecks the existing proof.
Keep the captured source bytes available: loss or mutation blocks exact readiness.
Refresh, later application allocation, restart and eligible exact-backup policy
recovery preserve this authority; restart/recovery always require fresh validation.
Mutable profile/map edits cannot admit a partition. `flash_bootloader` remains a
separate permission-guarded image workflow with vector, stack, reset, content and
erase-sector checks. Factory ROM bootloaders are not programming targets.

### Guarded user-bootloader deployment

After admission, complete the advertised `board_validate` route for the same
connected unit. Inspect `get_capabilities`: FULL bootloader programming requires
fresh `identity.capability: exact` with the disclosed identity coverage, a
bootloader partition, and verified bounded erase/program support. A capability
row alone grants no permission. Ordinary processor-compatible application
validation does not authorize this operation.

Use a suitable restorable user-bootloader ELF/AXF or addressed HEX. Its load
bytes, entry point, vector/reset handler and all affected erase sectors must
stay inside the admitted bootloader partition; its initial stack must point
into reviewed writable RAM. The guarded route takes addresses from the image;
it accepts no address override or unaddressed BIN. Admission reserves ownership
but does not relocate an image or establish its startup behavior.

1. Initialize `flash_bootloader-plan` with the universal all-null envelope and
   follow its returned fields. Explain the intended partition, effect of a
   failed bootloader, recovery artifact and separate startup check to the human.
2. Obtain explicit human permission and submit the complete plan with
   `action_parameters: {"artifact": "/project/build/bootloader.elf"}` and
   `user_permission: "one-time"` (or an explicitly authorized `"full-session"`
   grant). The fixed budget is `max_calls: 1`, `max_calls_buffer: 0`.
3. Execute the returned `preferred_call` exactly. For a client with a static
   tool list, use its unchanged single-child `stable_client_fallback` instead.
   These are alternatives for the same guarded action. Execution checks the
   bound artifact bytes, board and connection against current validated policy;
   a rebuild or reconnect requires a replacement plan, with fresh validation
   after reconnect. Plan acceptance alone does not prove image containment or
   authorize programming; execution enforces those checks before any target effect.
4. Check the actual result. Success requires independent readback of every
   artifact-addressed byte before the final reset attempt. The separately
   reported target state may be `running`, `halted`, or
   `reset_state_unconfirmed`. Even `running` proves no bootloader banner,
   application handoff or other intended startup behavior; observe that behavior
   separately. If reset state is unconfirmed, reconnect and check state before
   use; reconnect requires fresh validation. Use the public `reset_and_run`
   route deliberately when startup needs to be retried from a halted state.

Programming erases only affected sectors contained by this partition. Bytes in
those sectors that are absent from the image need not survive; preserve them in
the restorable artifact if needed. Existing application ownership and bytes
outside the affected sectors remain separate. Restore a suitable saved
user-bootloader artifact through the same permission-carrying plan and verified
flash route, then check startup separately. A failed or no-op attempt reports
failure and spends that plan's call; it grants no broader flash or erase scope.
Correct the cause and obtain a replacement plan for a retry. Public refresh
replays the committed partition; restart, reconnect or eligible policy recovery
requires fresh validation before further protected work. Policy recovery alone
does not restore firmware bytes.

If the server can see no debug probe, `setup_overview` returns a missing-probe
status (`discovery/no-native-probe`), not a board-naming ambiguity. Its remedy names
the locked-environment check to run first; only if that also reports nothing, and the
user can still see the device in a vendor tool, does it offer
`get_discovery_hook_contract`. That tool executes nothing and returns the server's own
hook directory plus the manifest and output schemas it will validate against. The agent
writes the hook and manifest; the server never authors them. `refresh_discovery_hooks`
then reloads the server-designated manifest and runs each eligible hook once, taking no
path, no argv, and no code.

Both discovery tools are always visible, need no prior unlock, and grant nothing. Hook
output is configuration only: it cannot select a target, set a connection policy,
restore a gate, plan, or permission, or authorize any hardware action. Hooks execute
only for a kind whose native discovery came back empty, so a machine where pyOCD and
pyserial work never runs one. A backend-open failure (`probe/open-failed`,
`uart/open-failed`) is an action failure, never a discovery failure, and its response
deliberately carries no hook contract call.

Call the all-NULL `board_setup-plan` first whenever hardware access is desired,
before loading it and before any other `*-plan` tool. Ask for the familiar
board name and exact board/MCU identity. Every matching YAML routes first to
`board_validate`; an absent profile or a specific validation remedy routes
through `load_setup_tool` and setup/repair. Fresh setup also requires a local
official PDF datasheet and exact MCU ordering code. Supply its digest only as an
optional cross-check; the server computes the authoritative SHA-256 and captures
the exact bytes. Do not ask the user for a CMSIS-Pack filename or pyOCD target.
If no exact local support exists, inspect the returned continuation form. When
`accepted_response.alternatives` includes `installed_target`, research official
evidence that one of its dynamically listed `eligible_targets` supports the
unchanged exact part. These names have matching installed device metadata and
usable geometry. The server accepts a literal installed device identifier or a
complete explicit device pattern from a built-in module providing one target
class, allowing its declared lowercase `x` placeholders and cosmetic separators.
It does not use prefix matches, guessed package suffixes, or evidence prose to
establish compatibility. Shared variant modules require an unambiguous literal
device identifier or the pack route. The same checks run before live attachment
and during persisted support replay. A core name or compatible CPUID does not
identify an exact package. If compatibility cannot be established or support is
insufficient, use the primary official-pack response; the server derives and
verifies its leaf and target.
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
target, address, mask, region, or partition to a pack response. After an
accepted continuation, call `board_fix_setup` under the still-active paired
allowance. Never retry `board_setup` or bypass the continuation by editing
`.firm`.

Research and friendly-choice pauses retain this same authorization, including a
second research prompt returned by a repair. Each accepted answer permits one
`board_fix_setup` resumption; a repeated answer or a fix without a fresh answer
refuses. Correct missing or invalid answer fields through the same continuation.
There are at most ten answered resumptions within thirty minutes of plan
acceptance. Primary `max_calls=1` and `max_calls_buffer=0` remain fixed; replies
never replenish those calls or issue a new permission. Completion, terminal
failure, cancellation, revocation, replacement, expiry, or changed session closes
the allowance. A replacement requires fresh plan authorization.

A populated setup plan records the actual attached session, or the disconnected
state. Setup may reuse only that unchanged session with the same assigned probe
and backend target. If installed or verified support cannot qualify the live
target for the supplied part, the populated plan refuses before acceptance:
disconnect, repeat `setup_overview`, and prepare a fresh plan. An attached
research reply must omit optional attachment settings; changing them requires a
new disconnected plan. FULL validation may establish a connection from the
plan's disconnected state; only that setup-owned transition updates its binding.
An unrelated reconnect always requires a replacement plan.

An advertised installed-target response contains exactly these fields (replace
the example target only with an eligible returned entry supported by evidence
for the supplied part):

```json
{
  "pyocd_target": "nrf52840",
  "evidence": [{"source": "official pyOCD target documentation and vendor datasheet", "claim": "The documented target supports the supplied nRF52840-QIAA part."}],
  "reasoning_summary": "The official target mapping supports the unchanged ordering code; live identity validation is still required."
}
```

For the pack alternative, send exactly `pack_id`, `version`, `filename`, `url`,
`source_path`, `official_sha256` (text or null), `evidence`, and
`reasoning_summary`, using the acquired official bytes and provenance. Both
routes require a nonempty evidence list of objects containing exactly nonblank
`source` and `claim`, plus nonblank `reasoning_summary`. If documented attachment
settings are needed, add all three optional fields together:
`debug_protocol` (`default`, `swd`, `jtag`), `debug_connect_mode` (`attach`, `halt`,
`pre-reset`, `under-reset`), and positive integer `debug_clock_hz` in Hz.
An installed core-only target with no physical flash/RAM geometry is ineligible;
use the pack route. A rejected installed target may return `setup_research_required`
with exact pack fields or required attachment fields. Follow that returned form;
an accepted response stages facts and does not itself establish readiness.

For LITE geometry confirmation, `accepted_response.response_schema` publishes
all nested fields and `response_example` is accepted by the confirmation parser.
This RAM-only example illustrates the shape; replace the range, permissions,
page, and note with confirmed facts from the supplied official PDF:

```json
{
  "decision": "confirm-or-correct",
  "regions": [{
    "name": "SRAM", "kind": "ram",
    "start": "0x20000000", "end": "0x20001000",
    "readable": true, "writable": true, "executable": false,
    "source_pages": [1], "source_note": "Confirmed RAM table in the supplied datasheet."
  }],
  "flash": {"backend_target": null, "erase_sectors": []},
  "recovery": null
}
```

Every region requires exactly those nine fields. Use only the returned `kind`
enum, unsigned integer or hexadecimal byte addresses, `start < end` (exclusive
end), literal boolean permissions, positive integer PDF page numbers, and
nonblank names/notes. Unknown regions stay omitted; at least one is required.
Flash requires exactly `backend_target` (matching target text or null) and
`erase_sectors` (list; `[]` when unknown). Confirmed sectors use integer `start`
and exclusive `end`, with optional `bank`. Recovery is null unless documented;
otherwise it requires exactly `mechanism: "backend_mass_erase"`, `source_pages`,
and `source_note`, plus confirmed `physical_flash` regions and complete covering
erase sectors. Null target/recovery makes no claim of flash/recovery support.
Evidence belongs in each region/recovery `source_pages` and `source_note`, with
no top-level `evidence` key. For example, omitting `source_note` and adding `note`
returns `setup/lite-confirmation-invalid` naming missing
`regions[0].source_note` and extra `regions[0].note`; correct those fields and
resubmit while the continuation is active. Continuation lifetime/session binding
is a separate prerequisite; these schemas alone do not prove setup completion.

The exact current plan fields, budgets, and permission modes are generated from
the runtime source of truth in [`plan-tool-contract.md`](plan-tool-contract.md).
Artifact load addresses for `flash_application` and `flash_bootloader` come
only from the ELF/HEX; neither plan accepts a caller-provided target address.
A flash succeeds only after readback matches every artifact-addressed byte. Its
result reports `programming` (verified byte count and ranges) separately from
`target_state`; `reset_state_unconfirmed` leaves only execution state unknown.
A live target lacking a flash algorithm for any addressed byte is refused with
`flash/unsupported-target` before reset or programming, and a no-op, partial,
or unreadable write fails with `flash/verification-failed`.

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

Never supply allowed ranges. Stable authority comes from the active immutable
committed generation; `memory_map.yaml` is its compatibility projection.
Selected ELF/HEX bytes are checked again at execution time.
Guarded reads require current board validation. Writes additionally require a
live identity proof associated with the current canonical map digest. UNKNOWN
or unmapped spans and write-only peripheral reads are denied before backend
access. An authoritatively mapped PROHIBITED security/provisioning span remains
readable, but every mutation of it is refused. A refresh never turns a
deliberately prohibited security/provisioning range into ordinary memory.

Follow the exact remedy named in a refusal:

- For a missing or damaged **committed generation**, `get_capabilities` and
  `get_setup_status` return a `recovery.load_call`. Follow it to load
  `board_policy_restore`, then supply `generation_backup_path`, an absolute local
  pathname or one relative to the firmware project. For example,
  `board_policy_restore(board_id="controller", generation_backup_path="backups/selected-generation.json")`.
  The source must be the exact saved file selected by the surviving original
  `current.json`: its board, schema, generation ID, full-file SHA-256 and existing
  semantic policy/profile/map checks must agree. The tool atomically restores
  those bytes, preserving the pointer and all identity/ownership/evidence. It
  performs no hardware I/O, requires no live probe assignment, and leaves the
  backup unchanged. It replays compatibility profile/map projections from the
  verified immutable snapshot so overview can issue fresh validation, using the
  existing sole profile pathname (or the canonical path when absent). Duplicate
  profile files require operator resolution; the tool never deletes or chooses
  between them. Old setup/refresh/validation
  loaders redirect corrupt policy to this eligible route.
  Omitting the path (default `None`), an unreadable source or mismatched bytes
  returns `policy_restore_blocked` with the exact prerequisite. There is no
  automatic backup; obtain the selected generation from a trusted project backup.
  An invalid/missing pointer additionally requires operator restoration of the
  original pointer from that backup. If the evidence is unavailable, hardware
  stays blocked; compatibility files, retained generations and interrupted
  pre-pointer replacements cannot reconstruct authority. Success is
  `policy_restore_completed` with `validation_required=true`, `ready_for_code=false`:
  follow `setup_overview` and fresh `board_validate` before protected planning.
  Repeating restoration also clears live gates/plans and requires validation.
  Interruption leaves either the prior damaged state or the complete original
  generation; a restarted server never inherits a live stamp. If compatibility
  replay is interrupted or fails, repeat the same exact restoration to finish
  it. Projection failure reports blocked, with hardware readiness still closed.
- `board_safety_refresh` deterministically rebuilds the complete map from the
    healthy committed profile plus replayed server-owned evidence. A present unreadable generic map
    refuses replacement because possible one-way deployment ownership cannot be recovered; and
- For FULL, `board_validate` establishes live identity proof and map association
  when it is absent or an identity anchor changed. For LITE, follow the same
  public overview/load/validation route to check the current assigned probe,
  saved target, committed partial policy and live target state. It connects if
  needed, retains `trusted-not-proven`, and creates no FULL identity gate.
  A LITE `validation_passed` result authorizes no extra ranges or operations;
  existing containment, plans, permissions and raw warnings still apply.

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
flash never leaves modified bytes without durable ownership. It records
ownership only, never a deployment: a target refused for missing flash support
commits none, and only a successful, readback-verified flash binds the artifact.
HEX requires a matching ELF
companion. Do not rebuild the selected output concurrently after execution
starts.

`set_breakpoint` likewise requires the current selected ELF. Executability is
proved from that ELF's executable segments; the whole stable application
partition is never treated as executable. The location must also fall inside a
reviewed application or bootloader deployment partition, so an existing reviewed
bootloader partition can qualify a location on its own. A generic board whose
safety map still has neither application allocation (`deployment_policy` kind `none`)
nor an admitted bootloader partition
cannot set a breakpoint anywhere and refuses with `safety/no-deployment-allocation`:
deploy the current build once through `flash_application-plan` and
`flash_application` to establish the artifact-derived allocation. A safety refresh
cannot create an application allocation. Ordinary refresh only re-derives existing
authority; intentional user-bootloader admission follows the separate documented
evidence contract above.

Both `set_breakpoint` and `set_breakpoint_raw` request hardware explicitly for
flash/code outside positively identified writable RAM, including unknown memory
geometry. The selected core's FPB must support the address and have capacity;
otherwise insertion refuses before requesting a code-memory write. Writable flash
does not permit software fallback. For example, after a flash breakpoint exhausts
capacity, remove an owned hardware breakpoint and retry the desired location.
Confirmed writable RAM retains pyOCD's supported AUTO/software behavior. Existing
allocation, ELF, plan and tier checks still apply to the guarded route.

Failed insertions report verified rollback only after manager/target flush,
manager absence and physical readback of the saved original RAM instruction or
cleared hardware comparator. Verified rollback does not require reflashing.
Missing provider objects, failed flushes or unreadable/mismatched physical state
remain uncertain even when the manager has no entry. Follow the returned recovery
instructions: unknown software writes require code restoration before resuming;
hardware-only uncertainty requires comparator cleanup/power-cycle and revalidation.
An existing enabled breakpoint remains idempotent. A foreign queued request that
could patch non-RAM code is left untouched for its owner to cancel.

Use `serial_exchange` when a console command's immediate acknowledgement or a
later command depends on volatile application state. UART readiness remains a
separate `get_setup_status` barrier and never establishes live silicon identity.
Both `read_serial` and `serial_exchange` return the complete time-bounded UART
capture as JSON-escaped `captured_text`; exchanges also return complete
`step_captured_texts` in step order. These fields are not excerpts. Capture is
bounded by the planned read windows and the server's operation deadline, not by
an undocumented byte or presentation limit.

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

`action_batch` contains one board and a nonempty list of ordinary child calls.
Do not nest it. Each child is authorized only when it reaches normal dispatch;
the batch stops after the first failure. A server-generated static-client plan
fallback always contains exactly one child and must be submitted unchanged.

The JSON response retains `board_id`, `completed`, and `failure`. `completed`
contains only the successful prefix, with each child's zero-based `index`,
`tool_name`, and unchanged `result`. All-success batches return
`status=batch_completed` and `failure=null`. Errors, refusals, exceptions, and
malformed results return `status=batch_failed`; `failure` identifies the stopped
child by `index` and `tool_name`, retains its `result`, and explains the stop in
`error_type` and `message`. No later child runs. Earlier effects, including any
effects of the stopped child, remain; the batch performs no rollback.

Setup or validation that needs user input or research returns
`status=batch_pending`, with the pending child in `failure` and the successful
prefix in `completed`. `setup_continuation_accepted` also suspends execution:
`continue_setup` stages evidence and still requires `board_fix_setup`. Follow
the child's returned continuation/remedy, finish setup and any required live
validation, then submit remaining work. Do not replay the successful prefix
unless you intend to repeat its effects.

For example, two successful `read_memory_raw` children return (result text
abbreviated here):

```json
{"status":"batch_completed","board_id":"board","completed":[{"index":0,"tool_name":"read_memory_raw","result":"..."},{"index":1,"tool_name":"read_memory_raw","result":"..."}],"failure":null}
```

If child 1 instead returns `{"status":"refused","message":"outside map"}`,
child 2 is never called:

```json
{"status":"batch_failed","board_id":"board","completed":[{"index":0,"tool_name":"read_memory_raw","result":"..."}],"failure":{"index":1,"tool_name":"read_memory_raw","result":"{\"status\":\"refused\",\"message\":\"outside map\"}","error_type":"ChildResultError","message":"Child returned refused."}}
```

For a first child returning `setup_research_required`, the response instead has
`status=batch_pending`, `completed=[]`, and `failure.index=0`; its unchanged
`failure.result` contains the continuation ID and research instructions.

Cancellation is best-effort for interruptible work. A flash transaction that
has started completes within its finite backend bound before resources are
released. Do not interrupt `target_unlock` or bootloader flash as an operating
practice.

Observe every safe-exit reminder. Structured `on_exit` is available only on
eligible serial tools and accepts only `uart_write` or `reset_and_run`; never
send shell strings or arbitrary commands. Disconnect explicitly when work is
complete.
