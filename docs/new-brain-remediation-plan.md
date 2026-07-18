# New Brain usability remediation plan

This plan addresses every gap recorded in `docs/new-brain-gap-audit.md` without
weakening Layer 2 safety or turning natural-language agent output into authority.

## Phase 1 — Build and UART correctness

1. Extend the linker-map parser for GNU ld/Zephyr evaluated symbol rows and
   `PROVIDE`, preserving malformed-definition errors.
2. Parse a real Zephyr ELF/map/HEX and add a representative regression fixture.
3. Remove unconditional successful-action reset from managed cleanup.
4. Make UART reopen and input clearing explicit opt-ins.
5. Add a one-handle, bounded, plan-bound multi-step serial conversation.

Checkpoint: parser and UART focused tests pass; a successful ordinary action
does not implicitly reboot the board; abnormal cleanup remains deterministic.

## Phase 2 — Agent discoverability

1. Render all plan index descriptions from `PlanDefinition`.
2. Include purpose, use cases, NULL protocol, exact binding, budgets,
   permissions and setup-first routing.
3. Enrich initialization guidance with current descriptions and explicit
   profile/setup/repair routing.
4. Add `setup_overview` so familiar user names become server-owned routes and
   generated IDs without exposing internals to the user.

Checkpoint: in-process MCP list and handshake tests prove an agent can discover
the correct first and next calls without guessing hidden tools.

## Phase 3 — Complete the setup continuation loop

1. Add a run-scoped public `continue_setup` handler.
2. Bind it to board plus continuation; accept only the last returned friendly
   choice or an exact official-source target/pack response.
3. Retain target candidate failures/dedupe and package retry limits.
4. Pass accepted selections into the paired repair attempt.
5. Load promoted `.firm` packs at runtime and read the project-local manifest.
6. Clear continuation selections/candidates on completion, revoke or disconnect.

Checkpoint: choice, target and pack response schemas reject extra or stale data;
accepted responses redirect to `board_fix_setup` and grant no authority.

## Phase 4 — Restore source ownership in automatic safety

1. Keep catalog deployment policy as a ceiling, not an application partition.
2. Include required reviewed prohibited/peripheral/CPU/physical/RAM classes.
3. Require linker/ELF evidence to create an application partition.
4. Fail setup readiness and validation when base classification is incomplete.

Checkpoint: a build cannot widen the deployment ceiling, while a real Zephyr
map can narrow it into an executable application partition.

## Phase 5 — Documentation and validation loop

1. Update architecture, agent contract and README with setup overview,
   continuation and state-preserving UART use.
2. Rebaseline versioned MCP contracts intentionally.
3. Run focused parser/UART/setup/plan/contract tests, ruff and pyright.
4. Run full pytest, package/import, contract and bounded stdio checks.
5. Re-read every gap and New Brain section; create a follow-up audit only if a
   behavior remains missing rather than declaring similarity by name.

