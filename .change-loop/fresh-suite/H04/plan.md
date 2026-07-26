# Change implementation plan

## Source change list

- Source: `.change-loop/fresh-suite/H04/changes.md`
- Goal summary: Make the existing attachment cache observable and exercisable for every setup that
  selects a stable probe/UART pair, including an automatically proven built-in UART, while keeping
  the cache strictly non-authoritative and preserving all profile, safety, live-identity, plan,
  permission, and gate contracts.

## Repository context and assumptions

- Verified architecture and relevant entry points: `FirmLayout` and `FirmStore` own project-local
  `.firm` paths and atomic writes in `src/pyocd_debug_mcp/firmstore/store.py`;
  `AttachmentCache` owns the JSON hint schema, authority-key rejection, atomic persistence, and
  resolution in `src/pyocd_debug_mcp/firmstore/cache.py`; `PreflightDecision` distinguishes an
  external adapter that needs confirmation from a directly proven UART in
  `src/pyocd_debug_mcp/setup_flow/preflight.py`; `SetupWorkflow._run_attempt` currently invokes
  its cache callback only when `cache_confirmation_required` is true in
  `src/pyocd_debug_mcp/setup_flow/setup.py`; `_confirm_setup_cache` and `_get_setup_status` wire
  cache persistence and public readiness in `src/pyocd_debug_mcp/server.py`; and
  `get_setup_status` publishes that mapping through `src/pyocd_debug_mcp/tools/setup.py`.
- Deterministic structural evidence:
  `python ../../.agent-workspace/bin/query refs AttachmentCache --lang python --path
  MCP-Trial-3/BYO-Firmware-MCP/src/pyocd_debug_mcp`,
  `python ../../.agent-workspace/bin/query refs cache_confirmation_required --lang python --path
  MCP-Trial-3/BYO-Firmware-MCP/src/pyocd_debug_mcp`, and
  `python ../../.agent-workspace/bin/query refs _get_setup_status --lang python --path
  MCP-Trial-3/BYO-Firmware-MCP/src/pyocd_debug_mcp`.
- Existing verification conventions: repository tests are Python `unittest` modules under
  `tests/`; static checks are `ruff check src tests` and `pyright src`; the neutral loop will run
  the two tester-authored focused commands in the same iteration.

## Plan items

### CL-001 — Persist every stable selected attachment as a non-authoritative hint

<!-- Assumption: `cache_confirmation_required` continues to mean that an external, not-provably-
mapped adapter needs the existing explicit selection/confirmation route. It must not be repurposed
to mean whether an already selected stable pair deserves a cache hint. -->

- **What to change:** Decouple cache persistence from the external-adapter confirmation predicate.
  After preflight is ready, invoke the existing cache-persistence callback whenever a UART endpoint
  was selected; retain `_confirm_setup_cache`'s stable probe and stable UART identity checks so no
  record is written for incomplete or unstable identities. Reuse `AttachmentCache.confirm()` and
  `FirmStore.atomic_write_json`; do not add a second writer or schema.
- **Where:** `src/pyocd_debug_mcp/setup_flow/setup.py` (`SetupWorkflow._run_attempt`) and, only if a
  small naming/comment clarification is needed, the existing callback boundary and
  `_confirm_setup_cache` in `src/pyocd_debug_mcp/server.py`.
- **Exact intended behavior:** A preflight-ready setup with a stable selected probe and stable
  selected built-in/provably-mapped UART writes or idempotently retains the existing
  `.firm/cache/attachments.json` record just as the confirmed external-adapter route does. A setup
  with no selected UART or missing stable USB identity writes no cache record. Repeating the same
  setup is idempotent rather than duplicating a record. Cache persistence may record only stable
  attachment hints and cannot persist plan, permission, gate, target, profile, pack, safety, or
  live-validation authority.
- **Must remain intact:** External adapters that are not provably mapped still require the same
  friendly public confirmation/continuation flow; directly proven built-in UARTs must not acquire
  a new user prompt; preflight decisions, setup phases, one-time allowances, atomic-write behavior,
  cache schema version, authority-key rejection, and setups that do not require UART remain
  unchanged.
- **Objective verification:** Focused automated tests drive preflight-ready workflow decisions for
  (a) a stable selected direct UART, (b) a confirmed external UART, (c) no UART, and (d) an
  unstable UART. Assert callback/write counts, exact stable record content, idempotence, and that
  only (a) and (b) create the cache record. Retain or add a control proving the external adapter
  still stops for confirmation before becoming preflight-ready.

### CL-002 — Publish a portable, explicitly non-authoritative cache diagnostic

<!-- Assumption: The additive public field is named `attachment_cache` and contains only portable
project-relative metadata and diagnostics. It does not expose an absolute host path or become an
input/authority token. -->

- **What to change:** Give the cache owner a canonical project-relative reference for its existing
  artifact and add an `attachment_cache` object to every available `get_setup_status` result.
  Document that object in the public tool docstring. The object must identify
  `record_kind="attachment_cache"`, `authority="non_authoritative_hint_only"`,
  `record_path=".firm/cache/attachments.json"` (derived from `FirmLayout`, never hardcoded to a
  host), `present`, a finite state (`missing`, `valid`, or `corrupt`), the current board's
  resolution reason when available, and an actionable remedy. A valid diagnostic may also report
  record count and whether resolution reused the hint, but must not expose it as authority.
- **Where:** `src/pyocd_debug_mcp/firmstore/store.py` and/or
  `src/pyocd_debug_mcp/firmstore/cache.py` for the single-owned portable reference/inspection
  boundary; `src/pyocd_debug_mcp/server.py` (`_get_setup_status`) for public aggregation; and
  `src/pyocd_debug_mcp/tools/setup.py` (`get_setup_status` description and unavailable fallback)
  for the zero-guessing contract.
- **Exact intended behavior:** The status response labels the exact run-local cache artifact before
  an agent touches the filesystem. Missing is reported honestly with a remedy to complete setup
  using a stable selected pair. Valid reports the cache resolution outcome without changing any
  readiness predicate. Corrupt/unreadable reports the parse/validation failure and tells the agent
  it may remove only the non-authoritative hint file and repeat setup; it never claims the record
  is current or silently regenerates it during a read-only status call. The unavailable fallback
  preserves its existing readiness-false fields and includes an honest unavailable cache
  diagnostic without inventing a path owned by a missing service.
- **Must remain intact:** Existing top-level setup-status keys and meanings remain backward
  compatible; the path stays project-relative and works for arbitrary project roots, hosts, ports,
  probes, and boards; `get_setup_status` remains read-only and never opens a connection, writes a
  cache, or opens a gate; cache identities and absolute paths do not become public authorization.
- **Objective verification:** Automated status tests assert the exact additive object for missing,
  valid, and corrupt cache artifacts; assert the record path is derived correctly under a
  non-default temporary project root and contains no absolute-root prefix; assert a status call
  does not change file bytes or create a missing file; and assert the tool's unavailable fallback
  still reports all prior readiness fields false plus an honest cache-unavailable diagnostic.

### CL-003 — Degrade safely from missing or corrupt hints to verified direct mapping

- **What to change:** Separate cache inspection/resolution failure handling from direct stable
  probe-to-UART matching in `_get_setup_status`. Evaluate the independently verified direct
  identity match even when the cache is missing, malformed, contains forbidden persisted-authority
  keys, or otherwise cannot be loaded. Feed the cache error into the `attachment_cache`
  diagnostic, not into profile/safety/live-validation authority.
- **Where:** `src/pyocd_debug_mcp/server.py` (`_get_setup_status`) and the existing
  `AttachmentCache` validation boundary in `src/pyocd_debug_mcp/firmstore/cache.py` only if a small
  read-only inspection helper avoids duplicated parsing.
- **Exact intended behavior:** With exactly one current UART whose stable USB identity matches the
  validated probe, `uart_attachment_ready`, `resolved_uart`, and `ready_for_uart_work` may be true
  from that direct observation whether the hint file is missing or corrupt. With no unique direct
  match, a missing/corrupt cache cannot make UART ready. Cache corruption never changes
  `configuration_ready`, `live_session_ready`, `identity_capability`, or
  `ready_for_flash_planning`; no cached value can create or restore a gate. An authority-shaped
  cache document is rejected and diagnosed as corrupt rather than consumed.
- **Must remain intact:** A valid exact cache match may still resolve a current port; revoked,
  changed, missing-identity, and multiple-match reasons retain their meanings; profile, safety-map,
  connection, validation-stamp, and flash-readiness calculations remain the sole existing
  authorities; diagnostic exceptions remain truthful and actionable rather than swallowed or
  reported as success.
- **Objective verification:** Tests cover corrupt JSON, an authority-shaped JSON record, a valid
  cache pointing at stale hardware, and a valid exact cache. Cross each with one unique direct
  stable match versus no/multiple direct matches. Assert readiness comes only from valid cache
  resolution or the independent unique direct match; assert corrupt/forbidden caches report
  `state="corrupt"` and never change configuration/live/flash readiness or any plan/gate state.

### CL-004 — Protect adjacent setup/cache compatibility with focused regressions

- **What to change:** Add adversarial regression coverage around callers and persistent artifacts
  touched by CL-001 through CL-003, using temporary project roots and fake inventories only. Keep
  spec-tester and regression-tester ownership separate as required by the loop.
- **Where:** Tester-owned focused modules under `tests/`; no production test hook and no hardware
  fixture.
- **Exact intended behavior:** Existing v1 attachment-cache files continue to load; repeated
  confirmation stays idempotent; revocation and hardware-changed outcomes remain non-authoritative;
  status is additive; no board, probe family, serial port, OS, part, or H04-specific constant enters
  production code; and all tests run without connected hardware or network.
- **Must remain intact:** All unrelated uncommitted H00-H03 production changes and their tests;
  existing setup allowance/continuation fixes; setup report writing; direct built-in UART
  resolution; public JSON serialization; static typing and lint contracts.
- **Objective verification:** The spec tester exercises every CL item with one isolated command.
  The regression tester traces and exercises `AttachmentCache`, `SetupWorkflow`, public
  `get_setup_status`, external-adapter confirmation, and existing setup-status compatibility with a
  disjoint test module/command. Both commands must exit zero in the neutral gate in the same
  iteration; `ruff check`/`pyright` coverage for changed production modules must also remain green
  in at least one tester command.

## Out of scope / must not change

- H04 experiment specs, evidence, fixtures, firmware, hardware state, or the installed
  `.h01-venv-batchstrict` runtime; the fresh test agent will retest only after neutral acceptance
  and a deliberate runtime reload.
- Profile, pack-manifest, safety-map, validation-stamp, plan, permission, gate, flash, reset, UART
  I/O, or destructive-operation contracts.
- Cache schema migration, a second cache format, provider/plugin redesign, external-adapter policy
  redesign, or automatic deletion/regeneration of a corrupt file during status reads.
- Any board-, part-, probe-, port-, operating-system-, or test-specific branch or constant.
- Unrelated pre-existing H00-H03 source/test changes and all runtime history outside
  `.change-loop/fresh-suite/H04`.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, generated package
  installation, deployment, or physical hardware actions.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, and adjacent behavior touched by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, or gate commands.
