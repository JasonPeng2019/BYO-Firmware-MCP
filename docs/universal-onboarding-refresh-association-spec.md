# Universal onboarding: refresh completes profile/map association

## Problem

Generic onboarding can persist a profile and then require `board_safety_refresh` to create or repair
its first stable map. Refresh commits `memory_map.yaml` but does not set the profile's canonical
`safety_ref`. The map and live identity can both validate, yet setup status stays incomplete and the
next startup incorrectly routes to repair forever.

## Required behavior

1. After a successful deterministic safety refresh, the server associates the same board profile
   with that board's canonical `memory_map.yaml` path.
2. The association is server-derived; the caller supplies neither a path nor authority.
3. Refresh changes no MCU identity, target, pack binding, geometry, partition, permission, or live
   identity evidence.
4. The reference update occurs for both initial-map creation and later map repair, and is idempotent
   when already correct.
5. A blocked refresh does not change the profile reference.
6. A successful refresh followed by validation makes a complete generic profile route directly to
   validation on the next `setup_overview`, without another setup or research pass.

## Acceptance

- A profile with no `safety_ref` receives the canonical reference only after a successful refresh.
- Existing correct references remain unchanged.
- Focused refresh/setup tests, full tests, and a new fresh Luna-medium nRF run pass.

