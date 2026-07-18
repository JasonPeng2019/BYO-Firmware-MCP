# PLAN: Safety Layer v2

Specification: `docs/safety-layer-v2-spec.md`  
One-way-door decision: `decisions/ADR-0001-single-file-safety-authority.md`

## Scope discipline

- Preserve unrelated untracked P4-10 evidence.
- Do not flash, erase, deploy, commit, or push.
- Use fresh temporary/project roots in tests; do not treat old `.firm` evidence as authority.
- Keep caller-supplied ranges prohibited and all gate/permission/plan state run-scoped.

## Slice 1 — Single-file map model and repository

1. Replace `SafetyArtifacts`/`SafetyArtifactRepository` with a schema-v2 single-map model containing
   identity, semantic source digests, geometry, partitions, regions, and canonical digest helpers.
2. Make repository paths/commit/load touch only `memory_map.yaml`; delete exact legacy manifest/report
   siblings on commit/load cleanup and remove report APIs.
3. Retain typed `RegionContribution` internally, but remove persisted `FingerprintSet` and source
   manifest evidence dependencies.
4. Extend reviewed catalog evidence with explicit authoritative deployment-partition policy. Never
   reinterpret the existing full-flash ceiling as authority without the new reviewed flag; keep
   flash unavailable when application/bootloader partition authority is absent.
5. Update map-build tests first: deterministic serialization, semantic profile stability, malformed
   and old schema handling, atomic writes, prohibited/overlap checks, and single-file assertions.

Verification: `tests/test_safety_map_build.py`, `tests/test_safety_regions.py`, Ruff/Pyright affected
modules.

## Slice 2 — Universal refresh and currentness

1. Refactor `SafetyRefresher` so every call deterministically rederives a complete candidate map
   from profile plus server-owned reviewed catalog/evidence; a missing/malformed/old map follows the
   same path.
2. Report semantic change classifications but remove the old scoped mutation algorithm; make every
   safety-only refusal name refresh rather than setup.
3. Remove application/bootloader build artifacts from persisted currentness and refresh inputs.
4. Return `validation_required` based on whether the same board/connection has a live identity stamp.
5. Remove server migration/source-manifest branches and make `_run_board_safety_refresh` able to
   create the first map as well as replace an invalid one.

Verification: rewritten `tests/test_safety_refresh.py`, server safety-tool tests, missing/corrupt map
MCP tests.

## Slice 3 — Lean validation and separated gate state

1. Remove serial selection/capture and firmware-behavior assertions from `BoardValidator`; retain
   probe selection, target connection, safe silicon identity, map association, and gate stamping.
2. Refactor `GateManager`/stamp data so a live identity proof is distinguishable from the current
   map digest and refresh can update only the latter without creating identity authority.
3. Ensure restart/disconnect/connection change/identity repair/recovery clear live proof; reset,
   flash, UART, and refresh do not.
4. Collapse validation success to identity/map success while leaving UART readiness in
   `get_setup_status`.

Verification: `tests/test_setup_validation.py`, `tests/test_gate.py`, setup status and disconnect
isolation tests.

## Slice 4 — Flash-time containment and plan artifact binding

1. Remove `_require_fingerprinted_flash_artifact` and declared-build freshness checks.
2. Read target, geometry, and stable partitions directly from the single map.
3. Keep/exercise runtime ELF/HEX segment, entry, vector, target, partition, and erase-sector checks
   before backend mutation.
4. Add a run-scoped artifact-digest binding hook for flash plans: hash at populated-plan acceptance,
   verify before execution start, reject changed bytes without budget/permission/backend burn, and
   clear/replace with the plan lifecycle. Do not add staging; document that the normal workflow must
   not rebuild the selected output concurrently after execution starts.
5. Ensure ordinary new build bytes do not stale unrelated reads/writes or the live gate.
6. Require a matching ELF companion for HEX and describe target checking truthfully as live-target
   versus reviewed-map identity.
7. Add current-ELF executable evidence to `set_breakpoint` and its plan; never mark the whole stable
   application partition executable merely to preserve breakpoint availability.

Verification: plan engine/flash plan tests, `tests/test_safety_enforcement.py`,
`tests/test_revised_memory_flash_misc.py`, backend-call recording and action-batch parity.

## Slice 5 — Setup eligibility and neutral mismatch routing

1. Add run-scoped mismatch allowances keyed by board, connection, probe, expected MCU, and observed
   MCU; expose setup plan for an established profile only when validation creates that allowance.
2. Keep the no-profile initial setup path.
3. For ordinary safety failures, keep setup plan/actions hidden and physically locked.
4. Make mismatch guidance report expected/observed identity and ask the user what to do without
   recommending setup. If the user elects to keep different silicon, create a new logical board and
   profile rather than rewriting established core identity; require plan permission first.
5. Retire public `board_safety_setup` behavior or make it an internal compatibility alias that is
   unreachable for ordinary safety recovery; initial and rebuilt maps use refresh.
6. Add reviewed live identity evidence for every automatically supported catalog entry (including a
   documented masked STM32 device-family proof); missing evidence is stamp-ineligible.

Verification: setup allowance, setup tools/workflow, visibility/list-changed, mismatch and restart
tests.

## Slice 6 — Surface, documentation, and cleanup

1. Update tool descriptions and loader/handshake/NULL prompts with the exact three validation trigger
   categories and explicit non-triggers.
2. Update artifact collector/build guidance to `collect -> flash plan -> flash`; recommend refresh
   only for stable-map problems.
3. Remove manifest/report schemas, docs, runtime references, fixtures, and contract fields; rebaseline
   intended MCP snapshots.
4. Update README, agent contract, architecture, plan prompt contents, safety evidence docs, and gap
   documentation.

Verification: contract, prompt-sync, docs consistency, packaging, and stdio MCP schema smoke.

## Slice 7 — Independent review and final verification

1. Review `git diff` for authority regressions, accidental unrelated edits, unreachable remedies,
   persisted authority, and backend calls before containment.
2. Run an adversarial diff review and fix valid findings.
3. Run focused suites, Ruff, Pyright, then the complete locked pytest suite, package build/import,
   and bounded stdio startup/shutdown smoke. After a fix, rerun the failing check and then the full
   suite once.
4. Add a fresh-root in-process MCP/fake-backend end-to-end covering refresh/build, validate, flash
   plan digest binding, successful containment, and zero backend calls for rejected boundaries.
5. Launch one fresh subagent test through the MCP server (no destructive hardware): handshake,
   setup routing, validate trigger comprehension, safety refresh guidance, flash NULL-plan teaching,
   and absence of routine-build refresh guidance. Preserve a concise evidence bundle.

Done only when all in-scope software checks are green and the subagent follows the revised contract.

## Completion record — 2026-07-18

- Slices 1–6 implemented against `docs/safety-layer-v2-spec.md`.
- Final diff review found four valid issues; all were fixed: identity-changing refresh now clears
  live proof, flash safety uses the effective live target override, malformed existing profiles
  cannot enter first-time setup, and changed plan-bound artifacts invalidate/relock the plan so
  restoring old bytes cannot resurrect approval.
- Full locked suite: `972 passed, 2 skipped`.
- Ruff: green. Pyright: zero errors. Package build/import and bounded stdio smoke: green.
- Fresh-root black-box MCP subagent smoke: green for handshake, no-board routing, single-field
  refresh schema, absence of public `board_safety_setup`, NULL setup/flash plan teaching, validation
  triggers, normal build/collect/flash guidance, and authority/range constraints.
- Fresh-root in-process MCP/fake-backend end-to-end: green for map refresh, live validation,
  plan-bound artifact bytes, successful contained flash, and zero backend calls on rejected
  changed/out-of-bound artifacts.
