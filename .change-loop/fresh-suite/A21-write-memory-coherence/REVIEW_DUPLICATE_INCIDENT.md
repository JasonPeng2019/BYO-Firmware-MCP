# Non-authoritative duplicate review incident

The required priority/Fast Codex reviewer
`A21-write-memory-plan-reviewer-001` (thread
`019faa22-939a-7a92-9704-f614a62f365d`) started at `2026-07-28T19:10:17Z`.
Its controller command did not contain the runtime name, so a process query scoped to
`A21-write-memory-coherence` failed to detect it. A second collaboration reviewer was then
mistakenly launched before the first controller completed.

The earlier-started Codex review is the sole authoritative review recorded in `plan-review.md`.
The duplicate review did not edit the plan, production source, or tests; its
`plan-review-agent.md` artifact is retained only for audit and is not an additional gate or input
to implementation. No further plan review will be launched.

During reconciliation, the manager also found that the runtime `changes.md` had inherited stale
content from an earlier incorrectly scoped prepare invocation. Before implementation, it replaced
that file byte-for-byte in substance with the already reviewed A21 `request.md`. The directly
authored and reviewed `plan.md` did not change, so its reviewed SHA-256 remains
`d2a1d29a7b8932133fa959bf35733906086cee3121240aa0594e15c0615fc626`.
