# Fresh-workspace automation implementation plan

This plan implements `docs/fresh-workspace-automation-spec.md`. Setup is a hard
milestone: no code-generation, build, flash, or UART behavior is tested until
the clean-root setup acceptance passes.

## Phase 1 — Reproduce and freeze the failure

1. Add an MCP stdio integration fixture using an empty artifact root and the
   `nrf52840dk` identity.
2. Assert the current failure modes: locale-sensitive empty target inventory,
   generic 30-second setup timeout, missing profile after timeout,
   profile-dependent override refusal, and closed safety/validation.
3. Add a code-phase spy and assert it is never called after any setup failure.
4. Use logical ID `nf_board`, seed a poisoned legacy profile, and assert the
   clean-root test cannot pass through checkout fallback.

Checkpoint: focused tests fail for the intended missing behavior and do not
perform a hardware write.

## Phase 2 — Trusted board catalog and direct inventory

1. Add `setup_flow/board_catalog.py` with immutable typed entries for
   `nrf52840dk`, `nrf52833dk`, and `nucleo_l476rg`.
2. Store separate family/product and package evidence, target, probe family,
   identity registers, UART attachment facts, physical geometry expectations,
   reviewed provenance, prohibited regions, and a maximum deployment policy in
   repository-owned data. Reference artifacts are test vectors only.
3. Validate the catalog at import/test time: unique aliases, exact field sets,
   absolute in-repository evidence paths, non-overlapping ranges, and existing
   reference artifacts.
4. Replace target-table subprocess parsing with pyOCD's built-in target API plus
   pinned-manifest targets. Ensure owned subprocess text decoding is UTF-8.
5. Extend setup inputs with `board_type`, `probe_uid`, stable UART USB identity,
   current port, datasheet path/hash, and exact MCU evidence; preserve the user's
   familiar logical name separately.
6. Add strict packaged device-support evidence, official-document evidence, and
   deployment-policy resources. Validate them with `verify2`, document hashes,
   revisions, and sections. The nRF acceptance resource is
   `Nano_BLE_MCU-nrf52840_PS_v1.1.pdf`.

Checkpoint: catalog and locale-adversarial inventory tests pass. No persistence
or hardware mutation is introduced yet.

## Phase 3 — Bounded profile-free bootstrap

1. Add an atomic probe reservation/lease service and a temporary bootstrap
   connection service accepting a catalog entry,
   exact probe UID, and finite timeouts. It owns/cleans the pyOCD handle and does
   not register a persistent assignment.
2. Read the catalog identity register and live memory map. Reject target, MCU,
   flash/RAM geometry, probe-family, and one-to-one assignment mismatches before
   staging a profile.
3. Make `ProfileRepository` the primary normal-connect resolver, with explicit
   legacy compatibility fallback. Refactor connection composition so
   `connect_override` may construct a
   run-scoped `BoardConfig` from its reviewed plan values when no profile exists.
   Keep that object in memory; never call a FirmStore writer.
4. Keep ordinary `connect` profile-backed and retain duplicate-assignment rules.
5. Promote the verified lease atomically into `ConnectionManager` before final
   validation; never stamp a pseudo-connection.
6. Resolve planned runtime bounds from `PlanDefinition`; define explicit bounds
   for non-plan tools and assert displayed/enforced parity for every tool.
7. Add cancellation checkpoints before each phase/commit and wait for setup
   worker cleanup before returning timeout/cancellation.

Checkpoint: fake and real-subprocess tests prove bootstrap attach and override
without a profile, immediate cleanup/reuse, no persistence by override, and
correct timeout/cancellation behavior.

## Phase 4 — Transactional profile and safety setup

1. Change setup phase inputs so the plan binds logical ID, familiar name,
   board type, exact MCU, probe UID/connection, UART, and mode.
2. During connection phase, compare supplied identity, catalog identity, and
   live identity; then stage and atomically commit the core plus optional profile
   facts through `ProfileRepository`.
3. Add a catalog safety builder that reconciles strict catalog/device and
   official-document evidence, live pyOCD geometry, pinned pack or built-in
   target identity, and the repository-owned deployment policy. Use existing
   `verify2`, linker, region, fingerprint, and map builders.
4. Build and atomically commit a base safety map automatically inside setup.
   Commit `safety_ref` only after the map is complete and conflict-free.
5. During setup, validate only stable UART attachment/open/close behavior; do not
   require application output from unknown existing firmware.
6. Run final non-destructive validation on the promoted persistent connection,
   confirm the probe/UART attachment cache,
   and return `setup_completed`. On failure preserve the last valid profile/map
   and point repair to the first unverified phase.

Checkpoint: every supported fixture completes from an empty root under fake
hardware; conflicts, interrupted writes, and silicon mismatch fail closed.

## Phase 5 — Readiness barrier and orchestrator

1. Add `get_setup_status(board_id)` with `configuration_ready`,
   `live_session_ready`, and `ready_for_code`. Require exact persistent
   connection/gate identity; never consume persisted evidence as authority.
2. Add `scripts/run_fresh_workspace_e2e.py`, a real stdio MCP client with strict
   schemas and finite phase timeouts. It writes machine-readable evidence.
3. The runner is setup-only and accepts no callback or arbitrary argv. An
   external orchestrator reads its evidence, reconnects/revalidates after a new
   Server Run, checks readiness, and only then launches the coding agent.
4. Add setup-only mode, used by hardware acceptance before any app
   source exists.

Checkpoint: adversarial status and runner tests prove no code callback occurs on
all non-success statuses, disconnect, restart, fingerprint drift, or identity
change.

## Phase 6 — Post-code guarded continuation

1. Add a separate fixed post-code workflow that discovers a completed application
   build and proves every partition/segment/entry/vector/HEX range/erase sector
   is within the unchanged deployment policy. A user build can never widen it.
2. Preserve the existing gate, plan, permission, containment, erase-sector,
   finalizer, and cleanup paths; do not add a privileged shortcut.
3. Add a plan-guarded single-session serial exchange and use it to capture exact
   `BLINK ON`, `BLINK OFF`, and status responses before disconnect.

Checkpoint: fake backend end-to-end test passes from setup-ready evidence through
flash/UART/cleanup, with zero backend mutations on every injected refusal.

## Phase 7 — Validation order

1. Run focused catalog, inventory, timeout, connection, setup, safety, readiness,
   and orchestration tests.
2. Run ruff and pyright on affected code.
3. Run the complete pytest suite, package/import checks, contract snapshots, and
   bounded stdio startup/shutdown.
4. Create a brand-new hardware artifact root with no source directory. Run
   setup-only for logical ID `nf_board`, supplied `nrf52840dk`, live nRF52840,
   documented QIAA package, probe `683377322`, stable J-Link UART identity/current
   `COM11`, and `Nano_BLE_MCU-nrf52840_PS_v1.1.pdf`.
5. Require a recorded `setup_completed` and `ready_for_code=true` before pointing
   the runner at any app repository.
6. Only after step 5, use the existing subagent-authored Zephyr app, refresh
   safety, flash the application partition, send `blink on`, `blink off`, and
   `blink status`, capture UART prints, and cleanly disconnect.
7. If hardware identity or any setup criterion fails, stop and report blocked;
   never convert it to a pass or bypass MCP with direct pyOCD.

## Deliverables

- Updated spec and this plan, including adversarial-review amendments.
- Catalog, bootstrap connection, automatic safety setup, readiness tool, and
  bounded runner implementations.
- Focused and integration tests mapped to FA-1 through FA-12.
- Updated contracts, README, architecture, and agent contract.
- Setup-first software/hardware evidence with exact commands and identities.

## Adversarial review disposition

The independent review's valid findings are incorporated above: schema-v2
profile-first connection, split readiness with persistent assignment, deployment
policy authority, non-semantic setup UART checks, single-session serial exchange,
no arbitrary runner execution, exact stable setup inputs, explicit permission,
phase cancellation, probe leases, separate package evidence, packaged trusted
sources, poisoned-legacy acceptance, and one authoritative timeout resolver.
