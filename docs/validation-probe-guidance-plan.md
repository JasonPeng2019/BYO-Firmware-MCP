# Validation Probe Guidance Plan

1. Add a read-only board-to-connection lookup to the run-scoped assignment store.
2. Let setup-tool loading receive the current assigned connection and include its probe identity in
   validation guidance; return a closed reroute when the assignment is missing or not a probe.
3. Wire the production server to the assignment store without changing validation's execution-time
   assignment check.
4. Add focused unit and server-integration coverage proving the returned call is complete and a
   missing assignment cannot produce misleading validation guidance.
5. Run focused tests, Ruff, Pyright, the full locked suite, and an adversarial diff review before a
   fresh hardware-agent retry.
