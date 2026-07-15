# BYO Server architecture

## Boundary

```text
compatible MCP client
        |
        | local stdio MCP
        v
server.py (tool schemas, validation, logging)
        |
        +--> guardrails (flash, recover, convergence)
        |
        +--> shared services (target, UART, symbols, sessions)
                    |
                    v
             pyOCD / pyserial adapters
                    |
                    v
                 one board

optional R11 wrapper --> codex exec --> the same MCP server and guardrails
```

The client decides what to request; the server decides whether a board-facing
operation is valid and permitted. Keeping flash, recover, mutation validation,
event recording, and convergence blocking below the client means every MCP
client receives the same safety contract. The extracted product has no turnkey
brain, UX shell, provider-memory system, or Codex app-server bridge.

The server is local stdio and blocking-v1. It keeps one active target handle in
the process. Ordinary use is model-agnostic. R11 is an optional Codex-specific
evaluation layer that launches `codex exec`; it neither changes nor replaces
the ordinary server boundary.

## State and artifacts

The current implementation creates one process-local `InMemorySessionStore`.
It writes global events to `runs/server-events.jsonl` and per-session data under
`runs/<session-id>/`, including `logs/events.jsonl`,
`run-metadata/session.json`, `captured-serial/`, and `applied-patches/`.

This is not Redis-backed. Restarting the server discards the in-memory session
map and watcher state; durable files remain evidence, but they are not reloaded
as live sessions. Copying current behavior was the extraction requirement, so
the older Redis architecture target remains an explicit product conflict rather
than a silent S5 change.

R11 additionally uses `runs/_r11_workspaces/` for copied bug workspaces and
writes benchmark result/artifact records under `runs/`. Its case manifests and
firmware sources remain under the checkout.

## Checkout and package boundary

Runtime roots are derived from files inside this project:

- `boards/` supplies board facts;
- `firmware/` supplies reference and bug fixtures;
- `packs/` supplies pinned pack metadata and downloaded local packs;
- `tests/cases/` supplies the frozen R11 corpus; and
- `runs/` receives session and benchmark output.

These resources are intentionally not wheel package data. The wheel contains
the `pyocd_debug_mcp` Python package only and is validated as a metadata,
entrypoint, and import artifact. Operational server, bootstrap, board, pack,
firmware, Stage 1, and R11 use is supported only from the full checkout. An
sdist or wheel unpack is not a substitute for that checkout.

## Safety and cleanup limits

The server validates board identity and arguments, gates firmware and recovery,
logs outcomes, blocks repeated mutation families through its convergence
watcher, closes target handles on explicit disconnect, and closes UART handles
on normal/finally paths.

The current product does not prove full cleanup of descendant processes,
provider children, interrupted MCP work, or every hardware-adjacent resource
after timeouts/crashes. Some subprocess paths use bounded `subprocess.run`, but
a timeout result is not process-tree cleanup evidence. Operators must disconnect
normally and audit stale processes/probe ownership after interrupted or timed-
out work. The broader cleanup guard remains open work.

## Public contract ownership

MCP tool descriptions, parameters, confirmation rules, and return/refusal/block
contracts are maintained in `src/pyocd_debug_mcp/server.py` docstrings. Tests
freeze the ordinary 20-tool schema against the source extraction commit, with
the brain-only `_brain_sync_timeouts` tool as the sole planned omission. This
document describes architecture and deliberately does not duplicate individual
tool contracts.

## Verified

The headless import closure, ordinary MCP schemas, server-owned guardrails,
in-memory/durable session behavior, checkout roots, and exclusion boundary are
non-hardware verified in this extracted project.

## Pending verification

Independent R1-R3 review, full process-tree cleanup, Redis disposition, live
MCP board/provider behavior, cross-host proof, and the long-term duplicated-
tree maintenance model remain pending. S6 verified a board-free MCP stdio
initialize/list-tools/shutdown connection but did not call a hardware tool.
