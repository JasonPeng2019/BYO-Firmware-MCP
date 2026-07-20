# Universal Onboarding: Project Authority Reuse

## Problem

After agent-led onboarding, a previously unknown MCU's exact pack binding exists only in the fresh
project's `.firm/packs` manifest and immutable pack copy. Some currentness paths replay that project
authority correctly, but `setup_overview` verifies generic maps with the repository registry only.
The same valid profile can therefore validate and refresh yet still be routed to refresh on every
new run instead of directly to validation.

## Required behavior

1. Every server path that decides whether a persisted generic map is authoritative must replay the
   exact pack filename, digest, PDSC leaf, target, part binding, and map authority from the active
   project's persisted pack store.
2. Repository-pinned support remains a valid source when it is the persisted authority, but it must
   not be required for an agent-resolved project-local device.
3. `setup_overview` must route a complete current generic profile/map directly to `board_validate`.
   It must not require setup, pack research, or refresh merely because the part was not known to the
   repository before onboarding.
4. The verifier must remain exact and fail closed for missing, changed, ambiguous, or differently
   bound pack bytes.
5. The project-local resolver must be supplied by the server. Callers and agents do not submit a
   path or replace persisted authority during reuse.
6. The behavior must be covered through the real refresh callback and real overview routing, not
   only by isolated helper tests.

## Non-goals

- No global pack installation or registry mutation.
- No online search during reuse.
- No relaxation of digest, PDSC leaf, target, or part matching.

