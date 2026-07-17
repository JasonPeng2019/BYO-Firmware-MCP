# Implementation Plan — Guarded Hardware Server ("Server B")

Target: bring the `BYO-Server` repository to full compliance with
[Design_Proto_Spec.md](Design_Proto_Spec.md) (the design specification; AC-n.m numbering used
throughout). Source behavior intent: [New_Brain_Spec.md](New_Brain_Spec.md).

---

## 1. Current-State Summary

### 1.1 Architecture

The repo is a Python 3.10+ (team pin 3.12), `uv`-managed, hatchling-built package
`pyocd_debug_mcp`, exposing a **local stdio MCP server** via `FastMCP` (`mcp>=1.2.0`).
Layering is already close to the spec's mental model:

```
server.py  (tool registration, arg validation, refusal/block formatting, event logging)
  ├─ guardrails/         flash_gate.py (artifact identity/policy), recover_gate.py (confirm gate)
  ├─ services/           target_control.py (backend facade), session_runtime.py (sessions/events),
  │                      convergence_watcher.py (repeat-failure blocker), symbols.py (ELF symbol
  │                      resolve + read), uart_capture.py
  └─ adapters/           swd_interface.py (backend protocol), swd_pyocd.py (pyOCD), uart_pyserial.py
```

Supporting modules: `board_config.py` (frozen `BoardConfig` dataclass + YAML/JSON loader),
`probe_inventory.py` / `serial_resolver.py` (hardware enumeration + hint matching),
`pack_provision.py` + `packs/manifest.yaml` (pinned, sha256-verified CMSIS-Pack provisioning),
`timeouts.py` (pyOCD + external-command timeout budgets), `stage0_check.py` (a 45 KB CLI that
already implements most of the *validation* checklist: enumerate → resolve probe/serial →
target support → connect → silicon-ID/test-read → UART substring), `host_bootstrap.py`.

### 1.2 Key facts relevant to the spec

- **Tool surface**: exactly the legacy "Current Server B Layer 2 Actions" list — 20 always-visible
  tools (`connect`, `disconnect`, `get_board_info`, `get_state`, `halt`, `resume`, `step`,
  `reset`, `read/write_core_register`, `read_memory`, `read_memory_block`, `read_symbol_u32`,
  `write_memory`, `set/remove_breakpoint`, `flash_firmware`, `read_serial`, `write_serial`,
  `unlock_recover`). No hidden tools, no `*-plan` tools, no visibility dynamics.
- **Single-board, single-session**: one global `_session_handle`, one global `threading.Lock`.
  No `board_id` parameter on actions; no multi-board isolation.
- **State**: `InMemorySessionStore` + durable JSONL/JSON under `runs/`. No `.firm/` store, no
  gate, no fingerprints, no attachment cache, no setup/validation reports.
- **Guardrails today**: flash artifact identity checks (path/suffix/sha256) but **no region
  containment**; recover requires `confirm=True` + typed `recover_mode` + once-per-session; the
  convergence watcher blocks repeated identical mutation failures. Structured refusal codes
  (`PolicyRefusal`) and event logging are a solid base for the spec's failure vocabulary.
- **Board profiles**: `boards/*.yaml`, `board_id` slug regex already matches spec Assumption A-6.
  No `mcu_part_number`, no filename-stem = board_id rule, no display-name uniqueness check,
  `pack_name` still lives in board YAML (spec: manifest is sole owner).
- **Contract freeze**: `tests/test_extracted_server_contract.py` pins the 20-tool schema and the
  AST of the tool functions to `tests/contracts/source-server-tools.json`. Any surface change
  breaks it by design — it must be re-baselined, not worked around.
- **Tests/conventions**: pytest (asyncio auto mode), module-level-state monkeypatching pattern,
  ruff (line 100), pyright. Hardware-dependent proof runs via `stage0_check.py` and the Stage 1 /
  R11 harnesses against `nucleo_l476rg` + `nrf52833dk`.
- **Useful transitive capability**: pyOCD brings `pyelftools`/`ELFBinaryFile` (already used in
  `services/symbols.py`) — sufficient for ELF segment/entry-point/partition extraction in the
  safety map without new dependencies.

### 1.3 What does not exist at all

Initialization handshake; board naming/assignment; tool visibility + physical locking; plan
engine (all-NULL flow, budgets, immutable plans); user-permission store; `.firm/` artifact store;
setup/repair/research/target-pack-resolution state machines; safety map, double verification,
fingerprints, gate, refresh; `action_batch`; `wait`; per-request cancellation handling; startup
hygiene; structured finalizers; attachment cache; `target_unlock` plan/permission flow with
erase-range disclosure.

---

## 2. Gap Analysis

Status legend: **Met** (already satisfied, needs only a pinning test), **Partial** (reusable
logic exists but the observable behavior is not satisfied), **No** (not started).
"M#" = milestone (Section 3) that closes the gap.

### Feature 3.1 — Initialization Handshake

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-1.1 | No | `tools/handshake.py` (new) | Guidance text composed from live registry state. M1 |
| AC-1.2 | No | kernel registry + handshake tool | Visible at run start. M1 |
| AC-1.3 | No | kernel registry (locks independent of handshake) | Enforced by construction; test both paths. M1 |
| AC-1.4 | No | handshake text + all `agent_prompt` fields | M1 (text), re-verified M6 |

### Feature 3.2 — Startup Board Naming & Assignment

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-2.1 | No | `services/connections.py` (new) + setup tools | Board-scoped actions fail until assigned. M2 mechanics, M6 flow |
| AC-2.2 | Partial | `probe_inventory.py`, `serial_resolver.py` → `setup_flow/preflight.py` | Enumeration + friendly labels exist; per-connection descriptor payloads don't. M6 |
| AC-2.3 | No | `setup_flow/validate.py` routing | Name→validate routing. M6 |
| AC-2.4 | No | `setup_flow/setup.py` routing | Unknown→setup; incomplete→repair. M6 |
| AC-2.5 | No | `services/connections.py` | One-to-one assignment invariant. M2 |
| AC-2.6 | No | `setup_flow/validate.py` | Mismatch → correction prompt, never rewrite. M6 |
| AC-2.7 | No | `services/connections.py` | In-memory only; trivially verified after M2 |

### Feature 3.3 — Layered Gating & Tool Visibility

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-3.1 | No | `kernel/registry.py` (new) | Hidden **and** handler-locked at run start. M1 (mechanics), M5 (final lists) |
| AC-3.2 | No | registry + `guardrails/plan_engine.py` | Unlock on valid plan, exact board+params. M4 |
| AC-3.3 | No | registry lock check inside every handler | Visibility ≠ authorization. M1 |
| AC-3.4 | No | registry relock on plan close | M4 |
| AC-3.5 | No | `kernel/run_state.py` (new) | All in-memory; new process = default state. M1 |
| AC-3.6 | No | refusal messages naming prerequisite | Extends existing `PolicyRefusal` codes. M1/M4 |

### Feature 3.4 — Plan Tools

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-4.1 | No | `guardrails/plan_engine.py` (new) | All-NULL-first per plan tool per run. M4 |
| AC-4.2 | No | plan definitions (`guardrails/plan_defs.py`) | Per-tool NULL-response content. M4 |
| AC-4.3 | No | plan engine field validation | Flags true + non-empty reasoning. M4 |
| AC-4.4 | No | plan engine budget rules | Fixed `1,0` list from spec. M4 |
| AC-4.5 | No | plan engine + registry | plan_id/underlying/total_calls/redirect. M4 |
| AC-4.6 | No | enforcement checklist in dispatch wrapper | Exact-param match, no budget burn on reject. M4 |
| AC-4.7 | No | budget decrement at execution start | Includes failed/cancelled-after-start. M4 |
| AC-4.8 | No | plan engine exhaustion → relock | M4 |
| AC-4.9 | No | plan replacement (atomic close+create) | M4 |
| AC-4.10 | No | run_state reset semantics | Restart test. M4 |

### Feature 3.5 — User Permission

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-5.1 | No | `guardrails/permissions.py` (new) | M4 |
| AC-5.2 | No | permission↔budget cross-check | M4 |
| AC-5.3 | No | one-time consumption | M4 |
| AC-5.4 | No | full-session store + NULL-response disclosure | M4 |
| AC-5.5 | No | (tool, board_id) scoping | M4 |
| AC-5.6 | No | run_state reset | M4 |
| AC-5.7 | No | mass-erase fresh-permission rule | M8 (`target_unlock`) |

### Feature 3.6 — Board Profiles & Persisted Artifacts

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-6.1 | Partial | `board_config.py` → `firmstore/profiles.py` | Slug regex exists; filename-stem=board_id check missing. M3 |
| AC-6.2 | Partial | profile loader | Duplicate board_id rejected; display_name uniqueness missing. M3 |
| AC-6.3 | No | `firmstore/profiles.py` staged commit | Core-after-connect ordering. M6 |
| AC-6.4 | No | staged commit (optional fields) | M6 |
| AC-6.5 | No | server-only writers + validation pipeline | Vacuously true today (no writers); becomes real in M3/M6 |
| AC-6.6 | No | `mcu_part_number` field + immutability rule | New profile field. M3 schema, M6 enforcement |
| AC-6.7 | Partial | `packs/manifest.yaml` (sole owner) | Manifest already owns provisioning; remove `pack_name` from profiles. M3 |

### Feature 3.7 — Board Setup & Repair

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-7.1 | No | plan engine + `setup_flow/setup.py` | One setup + one fix per plan. M6 |
| AC-7.2 | Partial | `stage0_check.py` logic → `setup_flow/preflight.py` | Deterministic no-probe result exists CLI-side. M6 |
| AC-7.3 | Partial | probe/serial resolvers → preflight table | Single-fallback + ambiguity detection exist; conversational-choice payloads don't. M6 |
| AC-7.4 | No | paired fix allowance in plan engine | M6 |
| AC-7.5 | Partial | `runs/` JSONL pattern → `firmstore/reports.py` | Event-logging conventions reusable. M6 |
| AC-7.6 | No | setup completion → relock → validate chain | M6 |
| AC-7.7 | No | repair phase resume + fresh preflight | M6 |

### Feature 3.8 — Agent Research Handoff

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-8.1 | No | `setup_flow/research.py` (new) | Self-contained prompt schema. M6 |
| AC-8.2 | No | research reply validator | Requested-fields-only. M6 |
| AC-8.3 | No | part-number immutability check | M6 |
| AC-8.4 | No | candidate hash dedupe + failure replay | M6 |
| AC-8.5 | No | research/authority separation | Research never touches gate/permissions. M6 |
| AC-8.6 | No | blocked-vs-research classifier | Locked target/missing probe → blocked. M6 |

### Feature 3.9 — Target & Device-Support Resolution

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-9.1 | Partial | connect auto-detect + `stage0_check` → `setup_flow/targets.py` | Exact-detection path exists; research trigger doesn't. M6 |
| AC-9.2 | No | commit-after-live-connect ordering | M6 |
| AC-9.3 | Partial | `pack_provision.py` | sha256 verification against pin exists; staging/promotion flow missing. M6 |
| AC-9.4 | No | staged-pack target enumeration check | M6 |
| AC-9.5 | No | enrichment candidate validation + report record | M6 |
| AC-9.6 | Partial | silicon-id optional in `BoardConfig`/stage0 | Optionality exists; research/commit flow doesn't. M6 |

### Feature 3.10 — Safety Map Construction

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-10.1 | No | `guardrails/gate.py` + write-action checks | Fail closed pre-map. M7 |
| AC-10.2 | No | `safety/verify2.py` (Pack/CMSIS vs datasheet compare) | M7 |
| AC-10.3 | No | `safety/linker.py` (ELF/linker-map partition extraction) | Uses pyOCD `ELFBinaryFile`/pyelftools. M7 |
| AC-10.4 | No | `safety/regions.py` (prohibited-overrides rule) | M7 |
| AC-10.5 | No | region classifier (unknown → deny) | M7 |
| AC-10.6 | No | map schema + `safety/fingerprints.py` | M7 |

### Feature 3.11 — Freshness & Refresh

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-11.1 | No | fingerprint check in write-action dispatch | M7 |
| AC-11.2 | No | `safety/refresh.py` scoped rebuild + re-stamp | M7 |
| AC-11.3 | No | anchor-change routing (full setup + validate) | M7 |
| AC-11.4 | No | `refresh_scope_unclear` handling | M7 |
| AC-11.5 | No | gate/refresh separation after disconnect | M7 |
| AC-11.6 | No | per-call freshness check + remedy naming | M7 |

### Feature 3.12 — Board Validation & Session Gate

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-12.1 | No | `setup_flow/validate.py` + `guardrails/gate.py` | Stamp exactly one gate. M6 checks, M7 stamp |
| AC-12.2 | Partial | `stage0_check.py` checks → validate tool | Checks exist; 7-status vocabulary doesn't. M6 |
| AC-12.3 | Partial | stage0 is read-only | Keep non-destructive; add test. M6 |
| AC-12.4 | Partial | runs/ logging → `.firm/validation/` reports | M6 |
| AC-12.5 | Partial | silicon-id mismatch fails in stage0 | Profile non-mutation + remedy text. M6 |

### Feature 3.13 — Write-Gate Lifecycle

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-13.1 | No | `guardrails/gate.py` default-closed | M7 |
| AC-13.2 | No | (absence of an open-gate tool) | Contract-snapshot test. M7 |
| AC-13.3 | No | disconnect hook → clear assignment/stamp/gate | M7 |
| AC-13.4 | No | no persisted gate state | Restart test. M7 |
| AC-13.5 | No | closed-gate refusal naming remedy path | M7 |

### Feature 3.14 — Hardware Actions (L2)

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-14.1 | No | `board_id` param on all L2 tools + ConnectionManager routing | M2 |
| AC-14.2 | No | shared response wrapper (safe-exit reminder) | M5 |
| AC-14.3 | No | `tools/memory.py` symbol-first `write_memory` | Fallback flag+reason M5; RAM bounds M7 |
| AC-14.4 | No | `tools/flash.py` + `safety/regions.py` | Segment containment pre-erase. M7 |
| AC-14.5 | No | `flash_bootloader` + permission-locked plan | M4/M5 |
| AC-14.6 | No | `register_write` range check | M7 |
| AC-14.7 | Partial | `read/write_core_register` exist → class split (`write_cpu_register` vs `set_execution_state`) | Register-class lists + core-supported check. M5 |
| AC-14.8 | No | `set_breakpoint` executable-region check | M7 |
| AC-14.9 | Met | `server.py` read_serial/write_serial validations → `tools/serial.py` | Preserve refusal codes; keep tests through migration. M5 |
| AC-14.10 | No | erase-sector containment in flash path | Needs pyOCD erase-scope control; see Risk R-6. M7 |
| AC-14.11 | Met (needs test) | reset paths (`target_control.reset`) | Behavior holds today; add regression test + keep after reset split. M5 |

### Feature 3.15 — Destructive Recovery

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-15.1 | No | `target_unlock-plan` fixed `1,0` | M8 |
| AC-15.2 | No | permission-request payload w/ erase ranges | Ranges from safety map + recover mode. M8 |
| AC-15.3 | No | full-chip-erase disclosure | M8 |
| AC-15.4 | No | unchanged-plan resubmission binding | M8 |
| AC-15.5 | No | mass-erase fresh permission every time | M8 |
| AC-15.6 | No | approval invalidation on target/probe/map/plan change | M8 |
| AC-15.7 | No | post-unlock gate stays closed until validate | M8 |
| AC-15.8 | Partial | `recover_gate.py`/`target_control.recover_target` typed vendor modes | Already vendor-recovery-only; carries into new tool. M8 |

### Feature 3.16 — Batch Execution

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-16.1 | No | `tools/batch.py` (new) | Same-board precheck before any child. M9 |
| AC-16.2 | No | nested-batch rejection | M9 |
| AC-16.3 | No | per-child dispatch through the same enforcement wrapper | M9 |
| AC-16.4 | No | per-child gate/freshness at child execution time | M9 |
| AC-16.5 | No | ordered sequential execution | M9 |

### Feature 3.17 — Operation Lifecycle

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-17.1 | No | `kernel/operations.py` cleanup on client death | stdio EOF → full cleanup. M9 |
| AC-17.2 | No | MCP cancellation → operation stop + release | M9 |
| AC-17.3 | No | flash cancellation deferral (finish-then-release) | M9 |
| AC-17.4 | Partial | `timeouts.py` budgets → per-operation timeout + cleanup | Timeouts exist; cleanup-on-timeout doesn't. M9 |
| AC-17.5 | Partial | global lock → per-board lock + busy semantics | M2 (per-board), M9 (busy contract) |
| AC-17.6 | No | finalizer-then-mandatory-cleanup ordering | M9 |
| AC-17.7 | No | structured finalizer whitelist (`uart_write`, `reset_and_run`) | M9 |
| AC-17.8 | No | startup hygiene (stale helper cleanup) | M9 |

### Feature 3.18 — Attachment Cache

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-18.1 | No | `firmstore/cache.py` (new) | Stable USB identities. M3 module, M6 integration |
| AC-18.2 | No | port-path re-resolution on exact match | M3/M6 |
| AC-18.3 | No | ignore-condition list | M3/M6 |
| AC-18.4 | No | `.gitignore` entry for `.firm/cache/` | M3 |

### Feature 3.19 — Per-Board Isolation

| AC # | Status | Where it lives / will live | Notes |
| :--- | :--- | :--- | :--- |
| AC-19.1 | No | ConnectionManager + gate per board | M2 routing, M7 gate |
| AC-19.2 | No | plan (tool, board_id) scoping | M4 |
| AC-19.3 | No | disconnect isolation | M7 |
| AC-19.4 | No | permission (tool, board_id) scoping | M4 |

**Cross-cutting requirements (CC-1…CC-22)** map into milestones as noted per milestone; CC-10–13
(performance numbers) are measured in M10. **No acceptance criterion is unmappable**, but three
carry caveats that cannot be closed purely in this repo and are tracked as risks: AC-17.2 depends
on client cancellation support (spec Q-1 / Risk R-4); AC-5.x legitimacy of human approval is a
soft gate by spec §2.2.6 (Risk R-5); AC-14.10 depends on backend erase-scope control (Risk R-6).

---

## 3. Milestones

Every milestone ends with: full test suite green, ruff/pyright clean, contract snapshot
regenerated when the tool surface changed, and the server bootable over stdio. New packages
follow existing convention (`guardrails/` = policy, `services/` = mechanics, `adapters/` =
hardware). Two justified deviations: a new `kernel/` package (registry/run-state/operations —
these are neither policy nor board mechanics) and a new `tools/` package (splitting the 1,300-line
`server.py`, which becomes the composition root).

---

### M1 — Server kernel: registry, locks, run state, handshake

**Goal.** Introduce the machinery every later feature hangs off: a dynamic tool registry with
visibility + physical handler locks, a per-process `ServerRun` state object, an async
operation-dispatch wrapper (thread-offloaded, timeout-bounded — the substrate M9 finishes), and
the initialization handshake tool.
**Satisfies:** AC-1.1, AC-1.2, AC-1.3, AC-1.4; AC-3.3, AC-3.5 fully; AC-3.1, AC-3.6 mechanics
(final visibility lists land in M5). CC-2 mechanics.

**Files/modules.**
- `src/pyocd_debug_mcp/kernel/__init__.py`, `kernel/registry.py` — ToolRegistry: owns tool
  definitions with `visibility` (visible/hidden) and `lock_state`; renders the advertised list;
  every handler call passes a registry lock check first (visibility is never the enforcement).
  Emits `tools/list_changed` on change. **Spike task**: confirm the pinned `mcp` SDK version
  supports dynamic list + list_changed via FastMCP; if not, register a custom `list_tools`
  handler on the lowlevel server or upgrade the `mcp` pin (Risk R-1).
- `kernel/run_state.py` — `ServerRun`: run id, started-at, plan/permission/assignment/gate maps
  (empty containers now), reset-on-process-start semantics.
- `kernel/operations.py` (v1) — async dispatch wrapper: runs blocking backend work in a worker
  thread with a per-operation timeout, records events via the existing `_run_logged_tool`
  conventions. Cancellation/cleanup semantics completed in M9.
- `tools/__init__.py`, `tools/handshake.py` — `initialization_handshake` tool; guidance text
  assembled from the live registry (visible list) plus the static rules of spec §3.1.
- `server.py` — becomes composition root: builds registry, registers existing 20 tools as
  visible/unlocked (no behavior change), registers handshake.

**Data model.** None persisted. `ServerRun` in-memory only.

**Interfaces.** `ToolRegistry.register(defn, *, hidden=False, locked=False)`,
`.unlock(tool, board_id)`, `.relock(tool, board_id)`, `.advertised()`;
`ServerRun` accessors; `operations.dispatch(tool_name, board_id, fn, timeout)`.

**Verification.** Unit tests for registry lock/visibility invariants (locked+hidden call fails
identically whether or not listed → AC-3.3); in-process MCP client test (memory transport) that
lists tools, calls handshake, asserts required guidance elements (AC-1.1–1.4); restart test
asserting default state (AC-3.5). Contract snapshot re-baselined (+1 tool).

**Dependencies.** None.

---

### M2 — Multi-board connection manager and `board_id` routing

**Goal.** Replace the single global session with a `ConnectionManager` holding per-board
connections and per-board locks; thread `board_id` through every board-facing tool; in-memory
assignment map with one-to-one invariants.
**Satisfies:** AC-2.5, AC-2.7, AC-14.1, AC-19.1 (routing half); AC-17.5 (per-board serialization
half). CC-15 groundwork.

**Files/modules.**
- `services/connections.py` — `ConnectionManager`: `assign(board_id, connection)`,
  `handle_for(board_id)`, per-board `threading.Lock`, `clear(board_id)` on disconnect,
  invariant enforcement (one board_id ↔ one active connection).
- `server.py` / existing tools — add required `board_id` parameter to every board-facing tool;
  `connect`/`disconnect` become per-board; `_session_handle`/`_lock` globals removed.
- `services/session_runtime.py` — `SessionRecord` gains the assigned board identity per
  connection (one runtime session per board connection instead of one global).

**Data model.** None persisted; assignments in-memory (AC-2.7).

**Interfaces.** All L2 tool signatures change (board_id required). Event records now always carry
board_id. Contract snapshot re-baselined.

**Verification.** Two-fake-board unit tests: action on board B with only board A connected fails;
duplicate assignment rejected (AC-2.5); concurrent calls to two boards proceed independently
while two calls to one board serialize (AC-17.5 partial); restart clears assignments (AC-2.7).
Existing tool tests updated to pass `board_id`.

**Dependencies.** M1 (dispatch wrapper, run state).

---

### M3 — `.firm/` artifact store, profile schema v2, attachment cache

**Goal.** Create the persistent artifact layer: `.firm/` store, board-profile schema v2 with the
spec's integrity rules, pack-manifest sole ownership, attachment cache module, report writers.
**Satisfies:** AC-6.1, AC-6.2, AC-6.7; AC-18.4; AC-18.1–18.3 (module logic; conversational
integration in M6). CC-16, CC-18, CC-19 groundwork.

**Files/modules.**
- `firmstore/__init__.py`, `firmstore/store.py` — path layout owner
  (`.firm/boards/`, `.firm/packs/`, `.firm/setup/`, `.firm/safety/`, `.firm/validation/`,
  `.firm/cache/`), atomic write helpers.
- `firmstore/profiles.py` — profile v2 load/commit on top of `board_config.py`: adds
  `mcu_part_number` (required) and `schema_version`; enforces filename-stem == board_id
  (AC-6.1) and display-name uniqueness (AC-6.2); staged-commit API (`commit_core`,
  `commit_optional`, `commit_safety_ref`) used by M6.
- `board_config.py` — deprecate `pack_name` (ignored with warning; removed from
  `BoardConfig`); `packs/manifest.yaml` confirmed as sole pack-metadata owner (AC-6.7);
  loader accepts both `boards/` (legacy, read-only) and `.firm/boards/` with a one-shot
  migration helper.
- `firmstore/cache.py` — attachment cache records (board_id, probe usb_serial, uart
  usb_serial/vid/pid, confirmed_at), exact-match reuse, ignore conditions, revocation.
- `firmstore/reports.py` — setup/validation report writer skeletons (JSON + log), following the
  `runs/` JSONL conventions.
- `.gitignore` — `.firm/cache/`, `.firm/packs/files/` (AC-18.4).
- Migration script `scripts/migrate_boards_to_firm.py` (checkout command, matching the repo's
  existing "checkout command" convention): copies `boards/*.yaml` → `.firm/boards/`, injects
  `mcu_part_number` (from a provided mapping for the three tracked boards), strips `pack_name`.

**Data model / migration.** Profile v2 as above; existing tracked boards migrated; `boards/`
retained one release as read-only fallback with a deprecation note.

**Interfaces.** `FirmStore` becomes the only writer of persisted artifacts (AC-6.5 groundwork —
no tool writes files directly).

**Verification.** Unit tests: stem/id mismatch rejected (AC-6.1); duplicate display_name rejected
(AC-6.2); pack_name absent from loaded profiles and manifest remains authoritative
(AC-6.7); cache exact-match/ignore-matrix tests (AC-18.1–18.3 logic); `git check-ignore`
test for cache path (AC-18.4). Migration round-trip test on the three tracked boards.

**Dependencies.** None hard; sequenced after M2 so profile v2 lands once `board_id` routing
exists.

---

### M4 — Plan engine and user-permission store

**Goal.** The complete L1 mechanism: `*-plan` tool factory, all-NULL-first flow, immutable
plans bound to exact params, budgets, atomic replacement, enforcement checklist in dispatch,
permission store with one-time/full-session scoping. Piloted end-to-end on three actions —
`read_serial` (multi-call), `write_serial` (multi-call), `write_memory` (fixed `1,0`) — which
become hidden/locked behind their plan tools.
**Satisfies:** AC-4.1–AC-4.10; AC-5.1–AC-5.6; AC-3.2, AC-3.4, AC-3.6 (plan-path); AC-19.2,
AC-19.4. CC-2 completed for piloted tools.

**Files/modules.**
- `guardrails/plan_engine.py` — plan lifecycle: NULL-call tracking per (plan tool, run);
  populated-plan validation (required fields, flags true + non-empty text, budget rules);
  create/replace atomically; `consume_call()` decrement-at-execution-start (thread-safe, under
  the board lock); exhaustion → relock via registry callback.
- `guardrails/plan_defs.py` — declarative table per underlying tool: budget fixity, permission
  requirement, NULL-response text blocks, underlying parameter schema. Single source for M5's
  full surface.
- `guardrails/permissions.py` — `PermissionStore`: grant/check/consume/revoke keyed by
  (tool, board_id); `one-time` requires `1,0` budget (AC-5.2); full-session disclosure hook for
  NULL responses (AC-5.4).
- `kernel/operations.py` — dispatch wrapper gains the spec §3.4 enforcement checklist (plan
  exists, exact tool, board match, exact params, current run, remaining calls, session valid,
  permission active, L2 checks) executed before budget decrement.
- `tools/serial.py`, `tools/memory.py` (initial) — piloted tools moved out of `server.py`,
  registered hidden+locked with generated `read_serial-plan`, `write_serial-plan`,
  `write_memory-plan`.

**Data model.** None persisted (plans/permissions are run-scoped, AC-4.10/AC-5.6).

**Interfaces.** `PlanEngine.null_response(tool)`, `.submit(tool, fields) -> PlanResult`,
`.enforce(tool, board_id, params) -> ActivePlan`; registry unlock/relock callbacks; refusal
codes `plan/*` and `permission/*` following existing `PolicyRefusal` naming.

**Verification.** Exhaustive unit tests mapped 1:1 to AC-4.1–4.10 and AC-5.1–5.6 (parameter
drift, budget burn on failed-after-start, no burn on pre-execution reject, replacement,
restart). In-process MCP client test: tool list changes after valid plan (AC-3.2), reverts after
exhaustion (AC-3.4). Cross-board scoping tests (AC-19.2/19.4). Contract snapshot re-baselined.

**Dependencies.** M1 (registry, dispatch), M2 (board_id scoping).

---

### M5 — Revised L2 action surface

**Goal.** Migrate the whole tool surface to the spec's revised action set with plan guarding and
parameter validation; region enforcement (needs the safety map) arrives in M7 and until then
write-capable actions run under an explicit interim policy (plan-guarded + legacy artifact
checks), recorded as a temporary deviation in the contract snapshot.
**Satisfies:** AC-14.2, AC-14.3 (symbol-first half), AC-14.5 (plan+permission wiring), AC-14.7,
AC-14.9 (preserved), AC-14.11 (regression test); AC-3.1 (final always-available/always-guarded
lists per spec §3.3). CC-3 pending M7; CC-21 response-text pass.

**Files/modules.**
- `tools/session.py` — `connect`, `disconnect`, `get_board_info`, `get_state`,
  `connect_override` (+plan; manual values never rewrite persisted profiles).
- `tools/execution.py` — `halt`, `resume`, `step`, `reset_and_run` (always available);
  `reset_and_halt`, `connect_under_reset` (+plans; `connect_under_reset` fails clearly without
  reset-line support). Legacy `reset` removed.
- `tools/registers.py` — `read_cpu_register`, `read_execution_state` (always);
  `write_cpu_register` (+plan; R0–R12/FP class), `set_execution_state` (+plan+permission;
  control-flow class); register-class tables + core-supported check via pyOCD register list
  (AC-14.7); security/provisioning register names rejected in both.
- `tools/memory.py` — `find_symbol` (new: symbol search over ELF symbol table),
  `read_memory_symbol` (renamed from `read_symbol_u32`), `read_memory_address` (+plan,
  block cap 64 KiB per spec A-12), `write_memory` (+plan; symbol-first, `allow_address_fallback`
  + reason required for raw addresses — AC-14.3 first half; RAM containment lands M7).
- `tools/flash.py` — `flash_application` (+plan), `flash_bootloader` (+plan+permission)
  replacing `flash_firmware`; interim policy = existing `flash_gate` artifact identity checks;
  partition containment lands M7.
- `tools/serial.py` — finalized (from M4 pilot), validations preserved (AC-14.9).
- `tools/misc.py` — `wait(ms)` (1–60,000 ms per spec A-13).
- Shared response wrapper in `kernel/operations.py` — appends the safe-exit reminder to every
  L2 result (AC-14.2).
- `unlock_recover` retained temporarily (replaced in M8). `guardrails/convergence_watcher`
  wiring preserved on the renamed mutation tools.
- `adapters/swd_interface.py`/`swd_pyocd.py` — add `connect_under_reset` and register-list
  introspection to the backend protocol.

**Data model.** None.

**Interfaces.** Full new tool surface; plan_defs table completed for every guarded tool;
contract snapshot fully re-baselined (legacy 20-tool snapshot retired — deliberate break of
`test_extracted_server_contract.py`, see Risk R-3).

**Verification.** Per-tool unit tests with fake backend (existing monkeypatch pattern):
register-class acceptance/rejection matrices (AC-14.7), symbol-first refusal text (AC-14.3),
plan-locked defaults for `flash_bootloader`/`set_execution_state` (AC-14.5 path), safe-exit
reminder on every L2 response (AC-14.2), reset-never-unlocks regression (AC-14.11), wait bounds.
In-process MCP list test asserting the exact always-available/always-guarded sets of spec §3.3
(AC-3.1). Hardware smoke on the bench pair via updated Stage 1 harness.

**Dependencies.** M4 (plan engine), M2 (board routing).

---

### M6 — Setup, research, target/pack resolution, validation

**Goal.** The full Stage-0-as-MCP flow: `Load_setup_tool`, `Board_setup-plan` (permission-locked,
paired setup+fix allowance), `Board_setup`, `Board_fix_setup`, `Board_validate`, naming/assignment
startup flow, deterministic preflight, research handoff, target/pack candidate staging, staged
profile commits, reports, attachment-cache integration.
**Satisfies:** AC-2.1–2.4, AC-2.6; AC-6.3, AC-6.4, AC-6.5, AC-6.6; AC-7.1–7.7; AC-8.1–8.6;
AC-9.1–9.6; AC-12.2–12.5 (AC-12.1 completes in M7); AC-18.1–18.3 (integrated). CC-7, CC-8, CC-17.

**Files/modules.**
- `setup_flow/preflight.py` — deterministic inventory implementing the spec §3.7 table; reuses
  `probe_inventory`, `serial_resolver`, `pack_provision`, `firmstore/cache`; emits
  `setup_needs_user_input` payloads with friendly `choices` and `agent_prompt` (JSON-string tool
  returns; every prompt includes the "don't expose internals" instruction — AC-1.4/CC-7).
- `setup_flow/research.py` — research-prompt builder (all AC-8.1 elements), reply validator
  (requested-fields-only, part-number immutability), candidate hashing + rejected-candidate
  replay, per-fact retry budgets (3 per spec A-10), blocked-vs-research classifier.
- `setup_flow/targets.py` — exact detection → candidate validation → live-connect-before-commit;
  `setup_flow/packs.py` — staging area under `.firm/packs/files/`, sha256 + official checksum
  compare, target enumeration, promote-after-validation (extends `pack_provision.py`).
- `setup_flow/setup.py` — phase state machine (input → preflight → selection → target → pack →
  connect → enrichment → safety(placeholder until M7) → commit), resumable phase records for
  `Board_fix_setup` (AC-7.7), terminal status vocabulary of spec §3.7.
- `setup_flow/validate.py` — `Board_validate` steps 1–9 (step 8 map-consistency and step 9 gate
  stamp complete in M7), 7-result vocabulary, validation reports.
- `tools/setup.py` — MCP tool wrappers incl. `Load_setup_tool` gating (spec A-20).
- `guardrails/plan_defs.py` — `Board_setup-plan` with the paired one-setup+one-fix allowance
  (AC-7.1/7.4) and replacement-plan rules.
- `stage0_check.py` — refactored to call `setup_flow`/`validate` internals so CLI and MCP paths
  share one implementation (keeps the CLI shippable, avoids divergence).

**Data model.** `.firm/setup/<setup_id>/setup_report.json` + `.log`;
`.firm/validation/<validation_id>/validation_report.json` + `.log`; staged profile commit
ordering (core after live connect — AC-6.3; optional after validation — AC-6.4; safety ref in
M7); `packs/manifest.yaml` promotion records.

**Interfaces.** Status/continuation payload schema (status, continuation_id, agent_prompt,
choices/observed/constraints/rejected_candidates/accepted_response/validation_plan) — the
repo's first structured-return contract; documented in `docs/architecture.md`.

**Verification.** State-machine unit tests with fake inventory for every preflight row
(AC-7.2/7.3); paired setup/fix allowance and replacement-plan tests (AC-7.1/7.4/7.6); research
round-trip tests incl. dedupe and immutable fields (AC-8.1–8.6); pack staging tests with local
fixture packs (AC-9.3/9.4); commit-ordering tests over a fake backend (AC-6.3/6.4/6.6, AC-9.2);
report-presence tests (AC-7.5, AC-12.4); validation status-matrix tests (AC-12.2/12.5);
non-destructiveness assertion via backend call recording (AC-12.3). Bench: full first-time setup
+ validate on `nucleo_l476rg` and `nrf52833dk`.

**Dependencies.** M3 (store/profiles/cache/reports), M4 (plan engine + permission), M2.

---

### M7 — Safety map, double verification, fingerprints, gate, refresh

**Goal.** Layer 0: `Board_safety_setup`, `Board_safety_refresh`, the region classifier enforced
inside every write-capable action, fingerprints + freshness checks, the per-board session gate,
and validation's gate stamp.
**Satisfies:** AC-10.1–10.6; AC-11.1–11.6; AC-12.1; AC-13.1–13.5; AC-14.4, AC-14.6, AC-14.8,
AC-14.10, AC-14.3 (RAM-bounds half); AC-19.3 and remaining half of AC-19.1. CC-3, CC-6 (region
half), CC-14, CC-19.

**Files/modules.**
- `safety/regions.py` — region model (kind, range, provenance), classifier
  (`classify(range) -> kind | UNKNOWN`), prohibited-overrides-all rule, containment checks per
  action category.
- `safety/linker.py` — application/bootloader/RAM partition extraction from ELF segments +
  linker map (pyOCD `ELFBinaryFile` / pyelftools); entry-point/vector-table extraction;
  build-configuration selection hooks.
- `safety/verify2.py` — deterministic comparison of server-loaded Pack/CMSIS/SVD/target data
  against agent-supplied datasheet evidence (device variant, addresses, aliases, kind, banks,
  block identity, versions/revisions); reconciliation rules; conflict records.
- `safety/fingerprints.py` — per-source sha256 sub-fingerprints + aggregate (profile,
  part/target, pack, evidence, app linker/ELF, boot linker/ELF, geometry, schema).
- `safety/map_build.py` — `Board_safety_setup` state machine + statuses; map/manifest/report
  writers under `.firm/safety/<board_id>/`.
- `safety/refresh.py` — `Board_safety_refresh`: sub-fingerprint diff → scoped rebuild → overlap
  re-check → new aggregate → re-stamp active session; statuses incl. `refresh_scope_unclear`.
- `guardrails/gate.py` — per-(board, connection) gate: default closed, stamp on validation
  (board_id + hardware result + probe + aggregate fingerprint), clear on
  disconnect/run-end/anchor change, remedy-naming refusals per the spec §3.13 matrix.
- `kernel/operations.py` — write-capable dispatch adds gate + freshness checks (spec A-4:
  guarded reads need plan + validated session; writes add fingerprint freshness).
- `tools/flash.py`, `tools/memory.py`, `tools/registers.py`, `tools/execution.py` — interim
  policies replaced with real containment checks: flash segment/erase/entry-point/vector/MCU-id
  (AC-14.4/14.10), `register_write` peripheral-window minus prohibited (AC-14.6),
  `write_memory` RAM containment (AC-14.3), `set_breakpoint` executable region (AC-14.8).
- `setup_flow/validate.py` — step 8 (map consistency) + step 9 (stamp + open gate) completed
  (AC-12.1); `setup_flow/setup.py` safety phase un-stubbed; profile safety-ref commit (AC-6.3
  chain complete).

**Data model.** `.firm/safety/<board_id>/memory_map.yaml` (schema_version, fingerprints,
regions with kind/range/provenance), `source_manifest.json`, `safety_report.json`.

**Interfaces.** `SafetyMap.check(action_category, ranges) -> Allowed | Refusal(kind, remedy)`;
`GateManager.require_open(board_id, write=True)`; refresh/setup routing table of spec §3.11.

**Verification.** Region-classifier property tests (unknown denied, prohibited overrides,
boundary off-by-one); linker-extraction tests against the repo's tracked reference ELF/HEX
fixtures (`firmware/*/reference/build/`); verify2 agreement/conflict/reconciliation matrices
(AC-10.2); fingerprint-drift matrix mapped to the spec §3.11 routing table (AC-11.1–11.6);
gate lifecycle tests incl. restart (AC-13.1–13.5); per-action containment tests with crafted
artifacts — segment outside partition rejected pre-erase via backend call recording
(AC-14.4/14.10). Bench: rebuild-loop test (relink → refresh → flash without revalidate;
disconnect → refresh insufficient → validate required).

**Dependencies.** M6 (setup/validation flows, evidence intake), M5 (final tool surface),
M3 (store).

---

### M8 — Destructive recovery: `target_unlock`

**Goal.** Replace `unlock_recover` with the `target_unlock-plan`/`target_unlock` flow: fixed
`1,0`, mechanism research prompt when unknown, permission request with full erase-range
disclosure from the safety map, single-use bound approval, post-unlock closed gate.
**Satisfies:** AC-15.1–15.8; AC-5.7. CC-6 completed.

**Files/modules.**
- `tools/unlock.py` + `guardrails/plan_defs.py` entry — plan flow with the spec §3.15
  permission-request payload (identity, mechanism, mass-erase flag, erase ranges/banks,
  all-NV disclosure, expected loss, plan_id); approval binding + invalidation on
  target/probe/map/plan change; mass-erase excluded from full-session coverage (AC-5.7/15.5).
- `guardrails/recover_gate.py` — absorbed: typed `recover_mode` vocabulary and
  vendor-recovery-only rule carry over (AC-15.8); `manual_only` boards return the documented
  refusal inside the plan flow.
- `safety/regions.py` — erase-range rendering for the permission payload (full-chip case states
  total NV erasure — AC-15.3).
- Legacy `unlock_recover` removed; contract snapshot re-baselined.

**Data model.** Unlock attempts recorded in validation-style reports under `.firm/`.

**Verification.** Unit tests per AC-15.1–15.7 (budget rejection, payload completeness against
map fixtures, changed-field resubmission rejection, second-mass-erase re-prompt under
full-session grant, approval invalidation matrix, gate-closed-until-validate). Bench: real
recover on `nrf52833dk` (the supported vendor path), then `Board_validate` reopens the gate.

**Dependencies.** M7 (map/gate for erase ranges + post-unlock closure), M4.

---

### M9 — Batch, cancellation, cleanup, finalizers, startup hygiene

**Goal.** Finish the operation lifecycle: `action_batch`; MCP cancellation wiring; mandatory
deterministic cleanup on every exit path; flash-cancellation deferral; structured finalizer
whitelist; startup stale-helper cleanup; defined final board state; per-board busy semantics.
**Satisfies:** AC-16.1–16.5; AC-17.1–17.8; completes AC-17.5. CC-5, CC-13, CC-15.

**Files/modules.**
- `tools/batch.py` — `action_batch`: same-board precheck, nested-batch rejection, per-child
  dispatch through the standard enforcement wrapper (plans/budgets/gate checked at child
  execution time), stop-on-first-failure (spec A-14), ordered results.
- `kernel/operations.py` (v2) — `ManagedOperation`: request-id ↔ operation ↔ resources map;
  cancellation hook from the MCP request context (SDK cancellation/AbortSignal equivalent —
  verify SDK support, Risk R-4); `finally`-owned cleanup chain (stop I/O → close UART → close
  pyOCD → kill owned subprocess group → release reset lines → release board lock); flash
  operations marked non-interruptible-midway (cancel = finish, then release — AC-17.3);
  timeout path runs identical cleanup (AC-17.4).
- `kernel/finalizers.py` — structured `on_exit` whitelist (`uart_write`, `reset_and_run`),
  accepted only on long-running/stateful tools (serial, future interactive ops), best-effort,
  always followed by mandatory cleanup (AC-17.6/17.7).
- `kernel/hygiene.py` — startup scan for stale helper processes/lock files from a previous run
  (marker files under `runs/`), bounded cleanup (AC-17.8); Windows process-group semantics
  (`CREATE_NEW_PROCESS_GROUP`) vs POSIX `killpg` (Risk R-7).
- Shutdown path — defined final board state per spec A-15 (reset-and-run unless intentionally
  halted).
- `subprocess` call sites (`_run_cmd`, zephyr/pack helpers) — migrate to owned process groups.

**Data model.** Helper-process marker records under `runs/` (host-local, gitignored).

**Verification.** Batch unit tests per AC-16.1–16.5 (fake backend records child order/params).
Lifecycle integration tests: kill an in-process client mid-operation and assert port/lock/session
release within bound (AC-17.1); cancellation of a slow fake read stops it and next call succeeds
(AC-17.2); cancellation during fake flash completes flash before release with complete artifact
(AC-17.3); timeout-path cleanup parity (AC-17.4); busy semantics on one board (AC-17.5);
failing finalizer never blocks cleanup (AC-17.6); non-whitelisted finalizer rejected at call
time (AC-17.7); seeded-stale-marker startup cleanup (AC-17.8). Bench: real Stop-button test
against a client known to send `notifications/cancelled` and one known not to (documents Q-1).

**Dependencies.** M5 (final surface), M4 (child plan consumption), M7 (gate checks in batch
children); cancellation substrate started in M1.

---

### M10 — Hardening, performance, docs, full-system verification

**Goal.** Close the cross-cutting requirements, measure the performance targets, finish
documentation, and run the full hardware acceptance pass.
**Satisfies:** CC-1, CC-4 (audit), CC-9–CC-13 (measured), CC-20–CC-22; re-verification sweep of
all 122 ACs; performance numbers fed back into the spec if targets need renegotiation.

**Work.**
- Performance tests: gate/freshness overhead ≤ 250 ms (CC-10), enumeration ≤ 10 s @ 8 devices
  (CC-11), NULL-response/handshake ≤ 2 s (CC-12), no unbounded operation (CC-13 audit of every
  dispatch site).
- Security audit checklist as tests where possible: no tool accepts shell strings (CC-5), no
  agent-writable persistence path (CC-4/AC-6.5), stdio-only/no-socket assertion (CC-1).
- Text pass over every user-relayable string (CC-21/CC-22); Unicode display-name round-trip
  test.
- Docs: rewrite `docs/architecture.md` (layers, gate, plan engine, `.firm/`), README tool-surface
  section, new `docs/agent-contract.md` describing the status/continuation payload schema.
- Full-matrix hardware acceptance run on the official pair (`nucleo_l476rg`, `nrf52833dk`) +
  two-board simultaneous session for the isolation ACs (AC-19.x live).
- Retire/replace remaining extraction-era tests that froze legacy behavior (deliberate,
  documented — Risk R-3).

**Dependencies.** All prior milestones.

---

## 4. Testing Strategy

1. **Unit tests (fake backend)** — the repo's existing pattern (monkeypatched module state,
   fake `TargetSessionHandle`) generalizes into a `FakeSWDBackend` + `FakeUART` fixture pair in
   `tests/fakes/`. All plan-engine, permission, gate, region, fingerprint, and lifecycle ACs are
   deterministic unit tests. Target: every AC-4.x, 5.x, 10.x, 11.x, 13.x, 14.x, 15.x, 16.x row
   has at least one test named after it (`test_ac_4_6_param_drift_rejected`-style), so coverage
   is greppable.
2. **In-process MCP contract tests** — the `mcp` SDK memory transport drives the real server
   object: tool-list visibility transitions (AC-3.x), handshake content (AC-1.x), status
   payload schemas (AC-7.x/8.x/12.x). A regenerated `tests/contracts/` snapshot pins the new
   surface the way the extraction snapshot pinned the old one.
3. **State-machine tests with fake inventory** — preflight/setup/repair/research flows run
   against scripted hardware inventories (no hardware), covering every row of the spec §3.7
   tables and every terminal status.
4. **Lifecycle/integration tests** — spawn the stdio server as a subprocess with a fake backend
   env flag; kill/cancel/timeout scenarios for AC-17.x.
5. **Hardware acceptance suite** — extends `stage0_check.py`/Stage 1 conventions; marked
   `@pytest.mark.hardware`; run on the official board pair per milestone (M5–M10). Two-board
   isolation (AC-19.x, AC-2.5) requires both boards attached simultaneously — a new bench
   requirement.
6. **Performance tests** (M10) — timed assertions for CC-10–CC-13 on a reference workstation;
   recorded, not CI-gating, on slower hosts.

Non-machine-verifiable items (documented as manual/procedural): genuine-human-approval
legitimacy behind `user_permission` values (spec soft gate), client conversational conduct
(server can only verify its own prompt text), Q-1 client cancellation census.

---

## 5. Risks, Unknowns, Mitigations

- **R-1 — Dynamic tool list support in the pinned `mcp` SDK.** The registry needs
  add/remove/list_changed at runtime. *Mitigation:* M1 spike; fall back to a custom lowlevel
  `list_tools` handler or a justified `mcp` version bump. Note: even without list_changed,
  physical locks keep enforcement correct (AC-3.3); only discoverability degrades.
- **R-2 — Blocking pyOCD in an async server.** pyOCD is blocking and not thread-safe;
  cancellation must not corrupt an in-flight probe transaction. *Mitigation:* per-board worker
  threads with cooperative cancel points; flash marked non-interruptible (spec's own carve-out);
  cleanup always via `finally`-owned paths.
- **R-3 — Deliberate break of the extraction contract freeze.** `test_extracted_server_contract`
  and the 20-tool snapshot cannot survive the revised surface (spec A-2). *Mitigation:* replace
  with a versioned contract snapshot at each milestone that changes the surface; record the
  supersession in `docs/extraction-manifest.json` notes rather than deleting history.
- **R-4 — Client cancellation support (spec Q-1).** Codex-family clients may never send
  `notifications/cancelled`; AC-17.2 is then unreachable through that client. *Mitigation:*
  timeout path performs identical cleanup (AC-17.4); document per-client behavior; consider
  tighter default timeouts for non-cancelling clients (open question back to spec).
- **R-5 — Soft permission gate (spec Q-5).** The server cannot prove a human approved.
  *Mitigation:* implement exactly the spec's soft-gate contract (instructions + structured
  channel); track client elicitation support as a future hardening item; never claim more in
  docs than the spec's §2.2.6 honesty clause.
- **R-6 — Erase-sector containment control (AC-14.10).** pyOCD's file programmer decides erase
  scope internally; proving "no sector outside the partition" may require sector-level
  pre-computation from pack flash geometry and chip-erase prohibition flags. *Mitigation:*
  compute required sectors from segment ranges + geometry in `safety/regions.py`, force
  sector-erase mode, reject artifacts whose sector span exits the partition; verify on bench
  with boundary images. If backend control is insufficient, flag to spec owners before M7 exit.
- **R-7 — Windows vs POSIX process-group cleanup (AC-17.1/17.8).** Different primitives; the
  dev bench is Windows. *Mitigation:* platform-specific implementations behind one interface;
  CI-less POSIX check via the macOS bench noted in board YAML history.
- **R-8 — Datasheet-evidence comparison determinism (AC-10.2).** Agent-supplied evidence formats
  vary; deterministic reconciliation is genuinely hard. *Mitigation:* strict evidence schema in
  the research prompt (exact fields, widths, units); reject rather than fuzzily reconcile;
  conflicts keep actions closed (fail-safe direction is spec-correct).
- **R-9 — Spec gaps carried forward.** Q-2 (no build artifacts present — can setup complete with
  a RAM/register-only map?) blocks a concrete M6/M7 branch; Q-8 (validation serial capture
  bounds) needs a default. *Mitigation:* implement the conservative reading (setup completes,
  write-capable flash actions stay closed until partitions exist; capture bound = profile
  `read_seconds` default 3 s) and flag both as decisions needing spec-owner sign-off — not
  silent scope changes.
- **R-10 — Two-board bench availability.** AC-19.x live proof needs two boards + two probes on
  one host, which past bench notes don't show. *Mitigation:* fake-backend isolation tests carry
  correctness; schedule one two-board bench session before M10 exit.
- **R-11 — Licensing block.** README records no LICENSE decision; shipping milestones publicly
  is blocked on that human decision (pre-existing, unchanged by this plan).

---

## 6. Sequencing Rationale

1. **Kernel first (M1–M2).** Every spec feature assumes dynamic visibility, physical locks,
   per-board routing, and an interceptable dispatch path. Retrofitting those under a finished
   plan engine or safety map would force rework; building them first lets every later milestone
   be additive. The async dispatch substrate goes in at M1 specifically so the M9 cancellation
   work does not rewrite every tool a second time (highest-rework risk in the plan).
2. **Persistence before flows (M3 before M6).** Setup/validation are consumers of the store,
   profile schema, and cache; landing the schema (with migration) early also de-risks the
   `mcu_part_number` addition, which touches tracked board files.
3. **Plan engine before surface migration (M4 before M5).** The revised tool surface is
   *defined* by its guarding; migrating tools once, directly into their guarded shape, avoids an
   intermediate unguarded-new-names state. The three-tool pilot in M4 proves the engine against
   real tools before 16 more depend on it.
4. **Setup before safety map (M6 before M7).** The map consumes setup's outputs (profile,
   evidence intake, pack data, linker selection). The interim write policy in M5/M6 (plan-guarded
   + legacy artifact checks) keeps every intermediate state shippable and no less safe than
   today, while being explicitly marked temporary.
5. **Unlock after map (M8 after M7).** The permission payload's erase-range disclosure is
   map-derived; sequencing it earlier would fake the payload.
6. **Batch and lifecycle last among features (M9).** Batch reuses the finished enforcement
   wrapper for children; cancellation/cleanup testing needs the final operation set to be
   meaningful. Risk is contained because the substrate existed since M1.
7. **Hardening last (M10)** — measurement and audit only; no new behavior, so late placement
   costs nothing and catches integration drift across the whole surface.

Each milestone leaves the repo working and shippable: tests green, contract snapshot current,
stdio server bootable, and behavior at worst equal in safety to the previous state.
