# MCP Issue Monitor — Remaining Work

- Q: is this still relevant? has this already been tested?
- The server-side monitor is substantially implemented, but the full specification is not yet fully verified.

## Implementation

- [ ] Add and ship the required client-side, tool-agnostic workspace skill.
- [ ] Include the skill’s enumerated signal criteria and exact issue/check-in templates.

## Tests and verification

- [ ] Test the real snapshot trigger and delivery path at the 100-call boundary.
- [ ] Add one complete test for every required summary field (including uptime, environment, and verification state).
- [ ] Complete tests for the AC-104 under-reporting tiers.
- [ ] Close the remaining adversarial-review gaps around ACK deletion, counter transitions, and closeout timing.
- [ ] Reconcile the coverage map with tests that actually exist.
- [ ] Run the full `unittest` suite and static checks in a clean environment.
- [ ] Preserve raw test output and explicitly record which E2E tests ran versus skipped.

## Release readiness

- [ ] Review and update stale implementation, coverage, and handoff documents.
- [ ] Commit the currently untracked monitor source, tests, and documentation.
- [ ] Perform a final spec-to-implementation review before calling the feature complete.
