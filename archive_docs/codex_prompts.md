# Modular implementation tasks and Codex prompt sequences

This document converts `Implementation_Plan.md` into component-sized implementation tasks. Run
the tasks in order. Within a task, send the prompts consecutively to the same agent/worktree so
the later prompt can inspect and validate the earlier work. Do not skip dependency tasks merely
because a later prompt mentions the same feature.

The validation cadence is intentional:

- A **smoke test** is a fast import, focused test, or in-process MCP exercise run while a
  component is being built.
- A **checkpoint** runs the component's focused tests plus ruff and pyright on the affected
  code. It should diagnose and fix failures, including regressions caused by the component.
- A **milestone validation** runs the complete software suite only after the last component in
  that milestone. This avoids repeatedly running downstream-equivalent tests.
- A **hardware validation** is included only where a fake backend cannot establish the required
  behavior. Never report hardware acceptance as passed when the named hardware is unavailable;
  record the exact unrun command, prerequisites, and remaining acceptance criteria instead.

Use the repository's actual command conventions after inspecting `pyproject.toml`; expected
defaults are `uv run pytest`, `uv run ruff check .`, and `uv run pyright`.

## Dependency and validation map

| Task | Component | Depends on | Validation boundary |
| ---: | --- | --- | --- |
| 1 | Kernel registry, run state, dispatch, handshake | — | M1 full software |
| 2 | Per-board connections and routing | 1 | M2 full software |
| 3 | FirmStore and profile schema v2 | 2 | Focused checkpoint |
| 4 | Cache, reports, migration | 3 | M3 full software |
| 5 | Plan engine and plan definitions | 1, 2 | Focused checkpoint |
| 6 | Permission store, enforcement, pilot actions | 5 | M4 full software |
| 7 | Session, execution, and register surface | 6 | Focused checkpoint |
| 8 | Memory, flash, serial, utility surface | 7 | M5 full software + hardware smoke |
| 9 | Setup preflight and resumable workflow | 3, 4, 6 | Focused checkpoint |
| 10 | Research, target, and pack resolution | 9 | Focused checkpoint |
| 11 | Validation, setup tools, and Stage 0 reuse | 10 | M6 full software + hardware |
| 12 | Region model, linker extraction, verification | 11 | Focused checkpoint |
| 13 | Fingerprints, map build, and refresh | 12 | Focused checkpoint |
| 14 | Gate lifecycle and action containment | 13, 8 | M7 full software + hardware |
| 15 | Destructive target unlock | 14, 6 | M8 full software + hardware |
| 16 | Batch execution | 14, 6 | Focused checkpoint |
| 17 | Managed operations and cancellation | 16 | Focused checkpoint |
| 18 | Finalizers, cleanup, and startup hygiene | 17 | M9 full software + hardware |
| 19 | Security, performance, contracts, and docs | all prior | M10 software checkpoint |
| 20 | Full-system and hardware acceptance | 19 | Final release validation |

## Task 1 — Kernel registry, run state, dispatch, and handshake

Coverage: M1; AC-1.1–1.4, AC-3.3, AC-3.5, and the M1 portions of AC-3.1/3.6 and CC-2.

### Prompt 1.1 — SDK spike and implementation

```text
read implementation_plan.md, then implement this:

Implement Task 1 / M1: the server kernel and initialization handshake. Read Design_Proto_Spec.md sections 3.1, 3.3, and the relevant assumptions before changing code. First inspect the pinned MCP SDK and prove how dynamic tool listing and tools/list_changed work; use the plan's low-level-handler or dependency-update fallback only if the current API cannot satisfy the contract. Add kernel/registry.py, kernel/run_state.py, kernel/operations.py v1, tools/handshake.py, package initializers, and turn server.py into the composition root without changing the legacy 20 tools' behavior. Visibility must never substitute for a handler lock. Dispatch blocking backend work off the event loop with finite timeouts and existing event-log behavior. Add focused unit and in-process MCP tests while implementing. Preserve unrelated worktree changes and document any SDK decision in architecture notes.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 1.2 — M1 checkpoint and full validation

```text
read implementation_plan.md, then implement this:

Validate and finish Task 1 as the M1 milestone checkpoint. Inspect the implementation rather than assuming it is complete. Exercise a locked hidden handler both through advertised discovery and by direct invocation, verify restart-default ServerRun state, and use an in-process MCP client to list tools and validate every initialization_handshake guidance requirement. Verify tools/list_changed behavior supported by the chosen SDK path. Update the versioned contract snapshot to add the handshake intentionally. Run the focused kernel/MCP tests, then the complete pytest suite, ruff, pyright, package/import checks, and a bounded stdio server boot smoke. Fix task-caused failures. Summarize AC coverage, commands/results, and any deferred risk; do not add later-milestone behavior.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 2 — Per-board connection management and board routing

Coverage: M2; AC-2.5, AC-2.7, AC-14.1, routing half of AC-19.1, serialization half of
AC-17.5, and CC-15 groundwork.

### Prompt 2.1 — Implement per-board mechanics

```text
read implementation_plan.md, then implement this:

Implement Task 2 / M2. Replace the global session handle and global lock with services/connections.py containing a ConnectionManager keyed by board_id, one active connection per logical board, one logical board per connection, and one lock per board. Thread required board_id parameters through every board-facing legacy tool, connect/disconnect, runtime session records, dispatch calls, and event records. Disconnect must clear only the named connection. Keep assignments strictly in memory and avoid introducing gate behavior that belongs to M7. Update existing tests and add two-fake-board routing, duplicate-assignment, restart, and same-board-versus-cross-board concurrency tests. Use stable connection identity rather than mutable display labels for one-to-one checks.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 2.2 — M2 checkpoint and full validation

```text
read implementation_plan.md, then implement this:

Finish and validate Task 2 as the M2 boundary. Trace every board-facing tool from MCP schema through dispatch to its backend handle and prove there is no remaining global-session escape path. Run focused routing tests showing board B cannot reach board A, duplicate assignment is rejected, two boards can execute concurrently, two calls on one board serialize, disconnect isolation holds, and restart clears assignments. Rebaseline the contract snapshot for required board_id parameters. Then run the full pytest suite, ruff, pyright, import/package checks, and stdio boot smoke; fix regressions attributable to this task. Hardware is not required here because the later two-board acceptance run validates the same routing on real probes.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 3 — FirmStore and board-profile schema v2

Coverage: M3 store/profile half; AC-6.1, AC-6.2, AC-6.7, and groundwork for AC-6.5,
CC-16, CC-18, and CC-19.

### Prompt 3.1 — Implement store and profiles

```text
read implementation_plan.md, then implement this:

Implement Task 3: FirmStore and board-profile schema v2. Add a single project-local path-layout owner and atomic write helpers for .firm/boards, packs, setup, safety, validation, and cache. Build firmstore/profiles.py over board_config.py with schema_version, required exact mcu_part_number, filename-stem equals board_id enforcement, unique display names, staged commit APIs, and read compatibility for legacy boards/. Remove pack metadata ownership from profiles: deprecate legacy pack_name with a clear warning while packs/manifest.yaml remains authoritative. Enforce A-6 identifier and display-name limits, absolute timezone-bearing timestamps where written, and ensure no gate/permission authority is persisted. Add focused schema, atomicity, duplicate, mismatch, and manifest-ownership tests. Do not implement setup workflow commits yet.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 3.2 — Component checkpoint

```text
read implementation_plan.md, then implement this:

Checkpoint Task 3. Review all persistence entry points and make FirmStore the only writer for the new artifact model. Test interrupted atomic writes, malformed and mismatched profiles, duplicate Unicode display names, exact preservation of user-supplied MCU part numbers, legacy read-only fallback, and absence of package identifiers from v2 profiles. Run only the focused store/profile/board-config tests plus ruff and pyright for affected code; fix failures. Do not run the full suite yet because Task 4 is the M3 milestone boundary and will repeat the integrated validation.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 4 — Attachment cache, reports, and profile migration

Coverage: remainder of M3; AC-18.1–18.4 module behavior and report/migration groundwork.

### Prompt 4.1 — Implement cache, reports, and migration

```text
read implementation_plan.md, then implement this:

Implement Task 4. Add firmstore/cache.py with stable USB identity records, exact-match reuse with current port-path re-resolution, all spec ignore conditions, revocation, and no authority-bearing data. Add immutable setup/validation report writer skeletons using JSON plus append-only logs. Update .gitignore for .firm/cache/ and .firm/packs/files/. Add scripts/migrate_boards_to_firm.py using the repository's checkout-command convention: migrate the three tracked profiles, inject mcu_part_number from an explicit mapping, strip pack_name, and remain safe/idempotent. Add exact-match/ignore matrix, report immutability, git-check-ignore, and migration round-trip tests. Never silently guess part numbers during migration.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 4.2 — M3 checkpoint and full validation

```text
read implementation_plan.md, then implement this:

Finish and validate Tasks 3–4 as the M3 boundary. Run cache exact-match and ignore-condition tests, migration round trips for every tracked board, profile integrity tests, report writer tests, and git check-ignore assertions. Inspect generated/migrated fixtures to prove pack metadata occurs only in the manifest and host attachment data is ignored. Then run the complete pytest suite, ruff, pyright, package/import checks, and stdio boot smoke. Fix task-caused regressions and report the migration command and rollback-safe behavior. No hardware run is needed; Task 11 validates cache integration against actual inventory.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 5 — Plan engine and declarative plan definitions

Coverage: plan core of M4; AC-4.1–4.10, AC-3.2/3.4/3.6 plan mechanics, and AC-19.2.

### Prompt 5.1 — Implement plan lifecycle

```text
read implementation_plan.md, then implement this:

Implement Task 5: guardrails/plan_engine.py and guardrails/plan_defs.py. Read Design_Proto_Spec.md sections 3.3–3.5 and assumptions A-4, A-9, A-10, and A-11. Model all-NULL initialization per plan tool/run, complete populated-plan validation, fixed and capped flexible budgets, immutable exact canonical parameter binding, atomic replacement, board/run/session scoping, and thread-safe decrement exactly once at execution start. Pre-execution rejections must not consume budget; failures, timeouts, and cancellation after start must. Exhaustion and invalidation must relock through the registry. Make plan definitions the single declarative source for purpose, fields, action schema, budget mode, permission metadata, and NULL-response text. Add AC-numbered unit tests including concurrency and restart. Keep permissions as an injectable interface until Task 6.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 5.2 — Component checkpoint

```text
read implementation_plan.md, then implement this:

Checkpoint Task 5 with adversarial plan-engine tests. Cover populated-before-NULL, partially NULL requests, placeholder reasoning, false flags, fixed-budget drift, flexible-budget ceilings, semantically exact parameter comparison, cross-board and stale-run use, replacement atomicity, simultaneous final-budget calls, pre-start refusal without burn, started failure with burn, and relock at exhaustion. Verify NULL responses are generated entirely from plan definitions and contain every required field and instruction. Run focused plan/registry tests plus ruff and pyright on affected code. Do not run the full suite; Task 6 completes M4 and runs it once.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 6 — Permission store, dispatch enforcement, and pilot actions

Coverage: remainder of M4; AC-5.1–5.6, AC-19.4, the plan enforcement checklist, and
M4 pilots for read_serial, write_serial, and write_memory.

### Prompt 6.1 — Implement permissions and pilot integration

```text
read implementation_plan.md, then implement this:

Implement Task 6. Add a run-scoped PermissionStore keyed by tool and board, supporting exactly one-time and full-session grants, structured revocation, one-time 1,0 enforcement, disclosure in NULL responses, consumption, and reset. Extend kernel dispatch with the ordered §3.4 enforcement checklist before execution/budget decrement. Generate and register plan tools for the three pilots: read_serial and write_serial as capped multi-call actions, write_memory as fixed 1,0; move their handlers into tools/ modules and make underlying actions hidden and physically locked. Ensure permission behavior is generic even though these pilots are not all permission-locked. Add in-process MCP visibility transitions and AC-numbered permission/board-scope tests. Preserve existing serial validation and event behavior.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 6.2 — M4 checkpoint and full validation

```text
read implementation_plan.md, then implement this:

Finish and validate Tasks 5–6 as M4. Exercise each pilot from all-NULL call through populated plan, visibility unlock, exact execution, budget exhaustion, and relock using an in-process MCP client. Test one-time consumption, full-session reuse with NULL permission, revocation, cross-tool/cross-board isolation, restart reset, failed-after-start burn, and pre-start no-burn. Rebaseline the contract snapshot deliberately. Run all AC-4.x/5.x focused tests, then the complete pytest suite, ruff, pyright, package/import checks, and bounded stdio boot. Fix regressions. Do not perform hardware testing; Task 8 tests the completed revised surface on hardware.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 7 — Session, execution-control, and register action surface

Coverage: first half of M5; final visibility lists for these groups and AC-14.7/14.11.

### Prompt 7.1 — Implement the action groups

```text
read implementation_plan.md, then implement this:

Implement Task 7, the session, execution, CPU-register, and peripheral-register portions of the revised Layer 2 surface. Build tools/session.py, tools/execution.py, and tools/registers.py with the exact names, visibility, plan budgets, permissions, parameters, and behavior in Design_Proto_Spec.md §§3.3 and 3.14. Add connect_override, reset_and_run, reset_and_halt, connect_under_reset, read_cpu_register, read_execution_state, write_cpu_register, set_execution_state, and fixed-budget register_write; remove or supersede legacy unified reset and core-register actions as specified. Extend SWD protocols for reset-line attach and runtime register enumeration. Enforce core-supported register classes and reject security/provisioning names. Keep register_write under the explicit M5 interim plan-guarded policy until Task 14 adds safety-map peripheral/prohibited containment. Manual connection overrides must never rewrite profiles. Route every action through standard dispatch and response wrapping. Add focused fake-backend matrices and reset-never-unlocks regression tests.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 7.2 — Component checkpoint

```text
read implementation_plan.md, then implement this:

Checkpoint Task 7. Compare implemented schemas and visibility against the spec table; test decimal/hex values, unsupported and security register names, control-flow versus ordinary register partitioning, register_write plan locking and interim-policy labeling, permission requirements, reset-line unsupported failures, manual override non-persistence, state transitions, safe-exit reminders, and board routing. Run the focused session/execution/register tests and contract subset plus ruff and pyright. Fix issues but defer the complete suite and hardware run to Task 8, which completes the M5 surface.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 8 — Memory, flash, serial, breakpoint, and utility action surface

Coverage: remainder of M5; AC-14.2, symbol-first half of AC-14.3, plan/permission half of
AC-14.5, AC-14.9, final AC-3.1 lists, A-12, A-13, and legacy-surface retirement.

### Prompt 8.1 — Implement the remaining revised surface

```text
read implementation_plan.md, then implement this:

Implement Task 8 and complete M5. Finalize tools/memory.py, flash.py, serial.py, breakpoint support, and misc.py. Provide find_symbol, read_memory_symbol, guarded read_memory_address with a 64 KiB cap, symbol-first write_memory with explicit raw-address fallback and nonempty reason, distinct flash_application and permission-locked flash_bootloader, plan-guarded serial actions with preserved bounds, set/remove breakpoint, and always-available wait bounded to 1–60000 ms. Use the explicit interim policy from Implementation_Plan.md for region checks that cannot land until M7; mark that deviation in the contract rather than weakening or pretending to implement safety. Add safe-exit reminders to every Layer 2 success and failure. Retire the superseded legacy action schemas while retaining unlock_recover only until Task 15. Preserve convergence-watcher behavior on renamed mutations and add focused tests.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 8.2 — M5 software validation

```text
read implementation_plan.md, then implement this:

Validate Tasks 7–8 as M5. Assert the exact always-visible, plan-visible, hidden, and physically locked tool sets using an in-process MCP client. Test every revised schema, response reminder, plan budget, permission redirect, symbol-first refusal, read cap, serial/wait bound, register class, reset behavior, and interim flash artifact check. Prove superseded legacy tools are absent except the explicitly temporary unlock_recover. Rebaseline the versioned contract and extraction-manifest note intentionally. Run the complete pytest suite, ruff, pyright, package/import checks, and stdio boot smoke; fix all task-caused failures.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 8.3 — M5 hardware smoke

```text
read implementation_plan.md, then implement this:

Perform the M5 hardware smoke using the updated Stage 1 harness on the official nucleo_l476rg and nrf52833dk bench targets. First enumerate and unambiguously record probe/serial identities so no command can target the wrong device. Exercise connect/info/state, halt/resume/step, reset_and_run, supported register reads, bounded memory/symbol reads, plan-guarded UART read/write where safe, and reset-line behavior where wired. Do not perform bootloader flash, recovery, raw writes, or any destructive action for this smoke. Save machine-readable results and exact commands. If either board or required wiring is unavailable, do not simulate a pass: record the blocked hardware cases and leave software validation clearly separate.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 9 — Setup preflight and resumable workflow

Coverage: setup mechanics in M6; AC-2.1–2.4/2.6, AC-7.1–7.7, and report/assignment
integration groundwork.

### Prompt 9.1 — Implement deterministic preflight and state machine

```text
read implementation_plan.md, then implement this:

Implement Task 9. Build setup_flow/preflight.py and setup_flow/setup.py around the exact §3.7 routing table and terminal statuses. Deterministically inventory user input, probes, serial ports, cache matches, targets, and build artifacts before requesting research. Produce conversational choices and agent_prompt fields that explicitly prohibit exposing structured internals. Implement resumable phase records, fresh preflight on repair, first-unverified-phase resume, staged reports, retry limits, disconnect/revocation closure, and the special one-setup-plus-one-fix allowance under one setup plan. Integrate naming and assignment routing for known, unknown, incomplete, and mismatched profiles without silently rewriting profiles. Leave target/pack research and safety as explicit phase interfaces for Tasks 10 and 14. Add table-driven fake-inventory tests for every preflight row and workflow transition.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 9.2 — Component checkpoint

```text
read implementation_plan.md, then implement this:

Checkpoint Task 9. Test no-probe/no-UART blocked paths, ambiguous probe/port/build choices, exact cache match, external-adapter confirmation, known-name validation routing, unknown-name setup routing, incomplete-profile repair routing, mismatch correction without mutation, paired setup/fix budgets, third-attempt replacement, cancellation/disconnect closure, and immutable per-attempt reports. Verify all user-facing prompts are plain prose and contain the no-internals relay instruction. Run focused setup/preflight/plan/report tests plus ruff and pyright. Do not run full M6 validation yet.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 10 — Research handoff, target resolution, and pack staging

Coverage: AC-8.1–8.6, AC-9.1–9.6, pack and target portions of M6.

### Prompt 10.1 — Implement research and resolution

```text
read implementation_plan.md, then implement this:

Implement Task 10. Add setup_flow/research.py with self-contained strict-schema prompts, requested-fields-only reply validation, immutable exact MCU part number, candidate hashing/deduplication, rejected-candidate replay, three-candidate budgets, and blocked-versus-research classification. Add setup_flow/targets.py for exact detection, deterministic candidate validation, and live-connect-before-core-commit. Add setup_flow/packs.py with project-local staging, pinned and official checksum verification, target enumeration from the staged pack, materially-different retry logic, and promote-only-after-validation; extend existing pack_provision code rather than duplicating it. Research must never grant permission, open a gate, or directly persist candidates. Record evidence, observations, and rejection reasons in reports. Add local fixture-pack and fake-backend tests without network dependence.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 10.2 — Component checkpoint

```text
read implementation_plan.md, then implement this:

Checkpoint Task 10 with round-trip and failure-matrix tests. Cover exact auto-detection, unknown target research, strict requested-field rejection, attempted part-number change, duplicate and merely-renamed package candidates, checksum mismatch, target absent from staged pack, live-connect failure before commit, successful core commit ordering, optional enrichment validation, silicon-ID optionality, retry exhaustion, and blocked locked-target/missing-probe classification. Inspect artifacts to prove failed candidates live only in reports and pack metadata only in its manifest. Run focused research/target/pack/setup tests plus ruff and pyright; defer full suite and hardware to Task 11.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 11 — Board validation, setup MCP tools, and Stage 0 reuse

Coverage: remainder of M6; AC-6.3–6.6, AC-12.2–12.5, AC-18.1–18.3 integration, A-20,
and setup/validation reporting.

### Prompt 11.1 — Implement validation and tool integration

```text
read implementation_plan.md, then implement this:

Implement Task 11 and complete M6. Add setup_flow/validate.py with validation steps 1–9 and the exact seven-result vocabulary, leaving map consistency/gate stamp as explicit closed placeholders until Task 14. Validation must be bounded and non-destructive and must not mutate a profile on silicon mismatch. Add tools/setup.py for load_setup_tool, board_setup-plan, board_setup, board_fix_setup, and board_validate with A-20 per-board/run redirects and structured continuation payloads. Complete staged core/optional commit ordering, attachment-cache integration, immutable setup/validation reports, and docs for the status schema. Refactor stage0_check.py to call shared setup/validation internals while preserving its CLI. Use the conservative Q-2 and Q-8 decisions recorded in the plan. Add fake-backend call-recording and status-matrix tests.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 11.2 — M6 software validation

```text
read implementation_plan.md, then implement this:

Validate Tasks 9–11 as M6. Run all state-machine rows, research round trips, pack staging, staged-commit ordering, cache integration, A-20 gating, status payload schema, report-presence, validation status matrix, silicon-mismatch non-mutation, and backend-call non-destructiveness tests. Confirm stage0_check and MCP validation share implementation rather than merely matching behavior. Update contracts and architecture documentation. Then run the complete pytest suite, ruff, pyright, package/import checks, stdio boot, and a CLI Stage 0 smoke with a fake or no-hardware inventory. Fix regressions.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 11.3 — M6 hardware setup and validation

```text
read implementation_plan.md, then implement this:

Run the M6 first-time setup plus board_validate hardware acceptance on nucleo_l476rg and nrf52833dk. Use clean, isolated test artifact roots; record exact probe/UART selections, target/pack resolution, live connection, silicon/test-read/UART observations, cache behavior on the second resolution, profile commit order, terminal statuses, and immutable reports. Validation must remain non-destructive. Confirm a deliberately mismatched silicon expectation fails without rewriting the profile, then restore only the test fixture. Do not claim gate opening yet because M7 owns the real safety map and stamp. Preserve result artifacts and exact commands. If hardware is missing, produce a precise blocked checklist rather than a pass.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 12 — Safety regions, linker extraction, and double verification

Coverage: foundational half of M7; AC-10.2–10.5 and containment primitives for AC-14.x.

### Prompt 12.1 — Implement deterministic safety sources

```text
read implementation_plan.md, then implement this:

Implement Task 12. Add safety/regions.py with typed half-open ranges, provenance, action categories, UNKNOWN-by-default classification, full-containment checks, and prohibited-overrides-all semantics. Add safety/linker.py to extract application/bootloader/RAM partitions, loadable segments, entry point, vector table, and build configuration from tracked ELF/linker-map fixtures using existing pyelftools capability. Add safety/verify2.py with a strict evidence schema and deterministic Pack/CMSIS/SVD/target-versus-datasheet comparison, alias handling, conflicts, and fail-closed reconciliation. Do not accept caller-supplied allowed ranges. Add boundary/property tests, overlapping/prohibited matrices, malformed artifact tests, and agreement/conflict matrices. Treat absent build artifacts per the conservative plan decision: non-flash safety may proceed, flash remains unavailable.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 12.2 — Component checkpoint

```text
read implementation_plan.md, then implement this:

Checkpoint Task 12. Stress zero-length, overflow, adjacent, overlapping, multi-region, bank-boundary, prohibited-subrange, alias, and unknown-range cases. Validate linker extraction against every tracked reference ELF/HEX/map fixture and prove conflicts or incomplete evidence cannot become allowed space. Confirm output is deterministic across input ordering and includes usable provenance. Run focused safety region/linker/verify2 tests plus ruff and pyright. Do not run the full suite; Tasks 13–14 complete M7 and repeat integration coverage once.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 13 — Fingerprints, safety-map construction, and refresh

Coverage: AC-10.1/10.6 and AC-11.1–11.6 mechanics.

### Prompt 13.1 — Implement map build and freshness routing

```text
read implementation_plan.md, then implement this:

Implement Task 13. Add safety/fingerprints.py with canonical per-source SHA-256 sub-fingerprints and aggregate fingerprints for profile, part/target, pack, evidence, app/boot artifacts, geometry, and schema. Add safety/map_build.py for board_safety_setup statuses and FirmStore-owned memory_map.yaml, source_manifest.json, and safety_report.json writes. Add safety/refresh.py to diff sub-fingerprints, choose the exact scoped rebuild/remedy from §3.11, recheck overlaps, atomically commit a new aggregate, and emit refresh_scope_unclear when safe routing is impossible. Anchor changes must route to full setup plus validation, not refresh. Integrate continuation/report schemas but do not open the gate yet. Add canonicalization, drift-matrix, interrupted-write, conflict, and stale-source tests.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 13.2 — Component checkpoint

```text
read implementation_plan.md, then implement this:

Checkpoint Task 13. Test every fingerprint source changing alone and in meaningful combinations, unchanged rebuild stability, file-order/path normalization, anchor versus refreshable drift, unclear scope, failed scoped rebuild preservation of the old map, and successful overlap re-evaluation before promotion. Verify the persisted map schema includes all provenance/fingerprints but no gate or authority state. Run focused fingerprint/map/refresh/store tests plus ruff and pyright. Defer full suite and hardware to Task 14.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 14 — Gate lifecycle, validation stamp, and action containment

Coverage: remainder of M7; AC-12.1, AC-13.1–13.5, AC-14.3/14.4/14.6/14.8/14.10,
AC-19.1/19.3, CC-3, CC-14, and write freshness enforcement.

### Prompt 14.1 — Implement gate and enforce safety

```text
read implementation_plan.md, then implement this:

Implement Task 14 and complete M7. Add guardrails/gate.py with a default-closed per-board/per-connection gate stamped only by successful board_validate using board identity, live hardware result, probe identity, and current aggregate fingerprint. Clear assignment, stamp, and only that board's gate on disconnect; never persist gate state. Complete validation steps 8–9, setup safety phase, and safety-reference commit. In kernel dispatch require validated session for guarded reads and gate plus per-call fingerprint freshness for writes. Replace every M5 interim policy with actual checks: RAM-only write_memory, peripheral register_write minus prohibited ranges, executable set_breakpoint, and application/bootloader flash segment, entry/vector, target identity, and erase-sector containment before backend erase/write. Closed/stale refusals must name the exact remedy. Add backend-call-recording tests proving rejection happens pre-mutation.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 14.2 — M7 software validation

```text
read implementation_plan.md, then implement this:

Validate Tasks 12–14 as M7. Run AC-numbered region, verification, fingerprint drift, map build/refresh, gate lifecycle/restart, per-board isolation, and every action-containment test. Include crafted artifacts crossing partitions and erase-sector boundaries; prove zero erase/write backend calls on rejection. Prove there is no open-gate tool, disk artifacts never restore a gate, reads follow A-4, writes check freshness every call, disconnect isolation holds, and each refusal names the correct remedy. Rebaseline contracts and remove all temporary M5 safety deviations. Run the complete pytest suite, ruff, pyright, package/import checks, and stdio boot; fix regressions.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 14.3 — M7 hardware safety validation

```text
read implementation_plan.md, then implement this:

Run M7 hardware validation on nucleo_l476rg and nrf52833dk using disposable reference firmware and backed-up recoverable state. Build safety maps from real pack/device and linker artifacts, validate to open each gate, flash only the application partition, relink to cause application fingerprint drift, run board_safety_refresh, and flash again without revalidation as allowed. Then disconnect and prove refresh alone cannot reopen the gate; board_validate must. Use boundary images to verify computed erase sectors stay inside the partition without attempting an unsafe out-of-range erase. Record maps, fingerprints, backend/pyOCD evidence, reports, and exact commands. Stop and report a blocker if pyOCD cannot force/prove sector containment; never weaken AC-14.10.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```
## Task 15 — Destructive recovery with target_unlock

Coverage: M8; AC-15.1–15.8 and AC-5.7.

### Prompt 15.1 — Implement recovery plan and action

```text
read implementation_plan.md, then implement this:

Implement Task 15 / M8. Replace unlock_recover with target_unlock-plan and target_unlock. Extend plan definitions and tools/unlock.py for fixed 1,0, mechanism research when unknown, and an unchanged-plan approval handshake. Before authorization, render exact live identity, vendor mechanism, mass-erase flag, every map-derived erased range/bank/sector, all-nonvolatile disclosure, expected losses, and plan_id in ordinary user-relayable prose plus structured data. Bind fresh one-time approval to run, board, target, probe, safety-map fingerprint, erase ranges, and complete plan; invalidate on any change. Full-session permission must never cover mass erase. Restrict execution to typed documented vendor recovery operations, preserve manual_only refusal, write immutable attempt reports, and leave the gate closed until validation. Remove legacy tool/schema and add AC-numbered tests.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 15.2 — M8 software validation

```text
read implementation_plan.md, then implement this:

Validate Task 15 as M8. Test wrong budgets, incomplete disclosures, full-chip language, any changed field after permission, changed target/probe/map/plan, second mass erase under prior/full-session grants, cross-board approval, expired/restarted runs, unsupported/manual recovery, single consumption, typed-backend-only execution, and gate-closed-until-board_validate. Inspect reports for exact ranges and absence of authority persistence. Rebaseline the contract with legacy unlock_recover absent. Run focused recovery/permission/gate tests, then the complete pytest suite, ruff, pyright, package/import checks, and stdio boot; fix regressions.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 15.3 — M8 hardware recovery validation

```text
read implementation_plan.md, then implement this:

Perform the destructive M8 hardware test only on the designated recoverable nrf52833dk bench board after positively matching its probe identity and preserving any needed firmware. Exercise the supported vendor recovery path with explicit fresh one-time permission captured through target_unlock-plan, compare the permission disclosure to the real safety map, execute once, prove the gate remains closed, then run board_validate and confirm normal guarded operation returns. Verify a second requested recovery requires fresh permission, but do not execute a needless second erase. Save logs/reports and exact versions. If the designated board or authorization is unavailable, do not run recovery and report the hardware criterion as blocked.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 16 — Batch execution

Coverage: first half of M9; AC-16.1–16.5 and A-14.

### Prompt 16.1 — Implement action_batch

```text
read implementation_plan.md, then implement this:

Implement Task 16: tools/batch.py and action_batch. Validate the entire child list before starting for one shared board identity and reject nested batches, but do not pre-consume or bypass child authorization. Execute children sequentially through the identical standard dispatch path used by direct calls so each child checks its active plan, exact parameters, permission, validation, gate, freshness, lock, timeout, and budget at its own execution time. Stop at the first failure and return ordered completed results plus that failure and the standard safe-exit reminder. Avoid holding locks in a way that deadlocks child dispatch. Add fake-backend tests for order, same-board precheck before any child, nested rejection, per-child budget, mid-batch drift/gate closure, stop-on-failure, and direct-versus-batch parity.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 16.2 — Component checkpoint

```text
read implementation_plan.md, then implement this:

Checkpoint Task 16 with adversarial batches: empty/oversized or malformed input according to the spec's safest bounded interpretation, mixed boards, disguised recursion, first-child refusal, later-child failure, plan exhaustion, parameter drift, permission consumption, freshness change between children, and simultaneous same-board calls. Prove no child runs after failure and no precheck burns a budget. Run focused batch/dispatch/plan/gate tests plus ruff and pyright. Do not run the full suite or hardware; Task 18 completes M9 and will validate lifecycle integration once.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 17 — Managed operations, cancellation, timeout, and busy semantics

Coverage: AC-17.1–17.5 and the runtime core of M9.

### Prompt 17.1 — Implement ManagedOperation

```text
read implementation_plan.md, then implement this:

Implement Task 17. Upgrade kernel/operations.py to ManagedOperation with request-to-operation-to-resource tracking, one board worker/lock boundary, finite A-11 timeouts, and MCP cancellation wiring verified against the installed SDK. Use cooperative cancellation for interruptible work; mark flash non-interruptible mid-transaction so cancellation means finish safely then release. Own one idempotent finally cleanup path for success, failure, timeout, cancellation, and client EOF: stop I/O, close UART, close pyOCD, terminate owned subprocess groups, release reset, release lock, and restore the A-15 final board state unless intentional halt applies. Define observable per-board busy behavior while retaining cross-board concurrency. Add subprocess fake-backend integration tests, not only unit mocks.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 17.2 — Component checkpoint

```text
read implementation_plan.md, then implement this:

Checkpoint Task 17. Spawn the stdio server with a fake backend and test client death mid-operation, MCP cancellation of a slow read, cancellation during fake flash, timeout parity, repeated cleanup calls, reset-line release, port/session/lock reuse by the next call, same-board busy response, cross-board independence, and intended-halt versus reset-and-run final state. Verify budget consumption at execution start remains exactly once through every exit path. Run focused lifecycle integration tests plus ruff and pyright. Record whether the test client actually sends notifications/cancelled. Defer full M9 suite and real-client bench work to Task 18.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 18 — Structured finalizers, process cleanup, and startup hygiene

Coverage: remainder of M9; AC-17.6–17.8, CC-5, CC-13, and CC-15.

### Prompt 18.1 — Implement finalizers and hygiene

```text
read implementation_plan.md, then implement this:

Implement Task 18 and complete M9. Add kernel/finalizers.py with a structured allowlist containing only uart_write and reset_and_run, accepted only by eligible long-running/stateful tools; reject shell strings and arbitrary commands at schema validation. Finalizers are best-effort and always precede mandatory cleanup without blocking it. Add kernel/hygiene.py and owned-process marker records for bounded startup cleanup of helpers/locks left by a previous run. Migrate every subprocess call site to an owned process-group abstraction with Windows CREATE_NEW_PROCESS_GROUP and POSIX process-group handling, explicit validated argv, finite timeouts, and safe identity checks before termination. Integrate stdio EOF shutdown and A-15 board final-state behavior. Add seeded-marker, hostile-input, failing-finalizer, and platform-abstraction tests.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 18.2 — M9 software validation

```text
read implementation_plan.md, then implement this:

Validate Tasks 16–18 as M9. Run all AC-16.x/17.x tests, including real subprocess client kill/cancel/timeout, non-interruptible fake flash, same-board busy and cross-board concurrency, cleanup order, failing finalizer, non-whitelisted input, seeded stale-helper cleanup, process identity mismatch refusal, and restart recovery. Audit every operation and subprocess call for a finite timeout and identical mandatory cleanup. Rebaseline contracts for action_batch/finalizer schemas. Then run the complete pytest suite, ruff, pyright, package/import checks, and stdio boot/shutdown smoke; fix regressions.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 18.3 — M9 real-client/hardware lifecycle smoke

```text
read implementation_plan.md, then implement this:

Run the M9 lifecycle bench against a safely identified board and at least one real MCP client known to send cancellation plus one available client that does not, if present. Interrupt bounded serial/read work and a disposable application flash at safe test points, verifying cancellation behavior, flash completion-before-release, reset-line/probe/UART release, final board state, and immediate reconnection without host intervention. Do not interrupt target_unlock or bootloader flash. Capture protocol traces sufficient to determine whether notifications/cancelled was sent, without secrets. If a client or board is unavailable, document the exact unverified Q-1 matrix entries and rely only on the already-passing timeout cleanup for that case.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 19 — Security audit, performance tests, contracts, and documentation

Coverage: M10 software work; CC-1, CC-4, CC-9–13, CC-20–22, contract retirement, and docs.

### Prompt 19.1 — Implement hardening and documentation

```text
read implementation_plan.md, then implement this:

Implement Task 19 / M10 software hardening. Add measured non-CI-gating performance tests for gate/freshness overhead <=250 ms, enumeration <=10 s at eight devices, and NULL-plan/handshake <=2 s, with recorded host context. Add security assertions for stdio-only/no socket exposure, no arbitrary shell strings, no agent-writable persistence route, no caller-supplied allowed ranges, no part-number mutation, no persisted authority, and finite timeout at every dispatch site. Audit user-relayable text for plain English/no internal payload leakage and add Unicode display-name round-trip coverage. Rewrite docs/architecture.md for layers/gate/plans/.firm, update README tool surface, add docs/agent-contract.md, and formally supersede extraction-era snapshots without deleting historical evidence. Do not change product behavior merely to game a benchmark.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 19.2 — M10 software checkpoint

```text
read implementation_plan.md, then implement this:

Validate Task 19's software boundary. Run the focused security/audit, performance, text, Unicode, MCP contract, packaging, and documentation consistency tests; inspect any performance outlier and record measured values rather than hiding slower-host results. Grep the implementation for sockets, subprocess shell use, unbounded waits, direct .firm writes outside FirmStore, persisted gate/permission/plan state, legacy tool names, and stale interim-policy markers. Run ruff and pyright for affected code plus package-build and repeated stdio startup/shutdown smokes. Fix defects and produce a machine-readable AC/CC coverage report identifying only genuinely hardware/manual items left for Task 20. Defer the complete pytest and whole-repository quality run to Task 20 so final validation performs it once after the acceptance matrix is frozen.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

## Task 20 — Full-system and hardware acceptance

Coverage: final M10 sweep of all 122 ACs, all cross-cutting requirements, official board pair,
and simultaneous two-board isolation.

### Prompt 20.1 — Prepare a traceable acceptance run

```text
read implementation_plan.md, then implement this:

Prepare Task 20 without changing semantics. Build or update a traceability matrix mapping every AC-1.x through AC-19.x and every CC to an automated test, hardware procedure, or explicitly manual/procedural proof. Verify the official nucleo_l476rg and nrf52833dk fixtures, reference firmware, probe/UART identifiers, safe disposable artifacts, tool/client versions, and recovery prerequisites. Create a bounded run order that avoids repeating already-proven destructive operations: software suite once, per-board setup/safety/validation/actions, one approved recovery validation, lifecycle/cancellation, then simultaneous-board isolation. Ensure every hardware command requires explicit board/probe identity and stores machine-readable results. Do not mark an item covered merely because a similarly named test exists; inspect assertions.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 20.2 — Execute final software and hardware matrix

```text
read implementation_plan.md, then implement this:

Execute the final acceptance matrix from Task 20.1. Run the complete software suite, ruff, pyright, package/import and stdio checks once. On nucleo_l476rg and nrf52833dk, run setup/repair routing as needed, safety map build/refresh, validation, representative always-available and guarded actions, application partition flash containment, UART, cleanup/reconnect, and the designated single recovery proof. With both boards and probes attached simultaneously, prove one-to-one assignment, board-specific plans and permissions, board A validated while board B is denied, cross-board concurrent operations, and board A disconnect leaving board B's gate/plan/permission intact. Record exact commands, versions, identities, timings, artifacts, and results. Never convert missing hardware or user authorization into a pass.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```

### Prompt 20.3 — Close out validation and report remaining blockers

```text
read implementation_plan.md, then implement this:

Close Task 20. Reconcile actual software and hardware results against every row of the traceability matrix, investigate and fix implementation defects, and rerun only affected focused tests plus any invalidated downstream acceptance steps. Do not rerun unrelated destructive hardware tests. Produce a final validation report with pass/fail/blocked status, evidence links, performance measurements, client cancellation observations, hardware identities, and explicit treatment of open questions Q-1 through Q-10 and risks R-1 through R-11. Distinguish repo-complete work from external blockers such as missing two-board bench access or the licensing decision. The repository is complete only if all in-scope criteria pass, full software quality checks are green, the server boots over stdio, and every hardware-required criterion has real evidence.

for design decisions that need clarification or are ambiguous, pick the best choice for the specified task at hand and the product spec in design_proto_spec.
```
