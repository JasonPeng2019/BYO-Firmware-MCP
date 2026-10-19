# Targeted integration checks

Use the locked server environment from `BYO-Firmware-MCP`:

```text
uv sync --locked
uv run --locked python -m unittest tests.test_codex_harness tests.test_tiered_acceptance_workspace -v
uv run --locked python -m unittest tests.test_monitor_codex_e2e -v
```

The monitor E2E module runs authentic Codex CLI clients with the pinned
`gpt-5.6-luna` model and real stdio MCP server processes. It requires a logged-in
Codex CLI and `uv` on PATH. Check skipped tests explicitly when evaluating a run.
The isolated client disables shell execution, web search and subagent tools so
the model uses the live MCP surface. This prevents the source/store searches
observed when the client relied on the MCP-only prompt alone.

Each agent test creates its own monitor store and artifact root outside its toy
firmware project. `BYO_MCP_MONITOR_ROOT`, if set for a suite, is the parent of
disposable per-test directories; existing contents are preserved. The oracle and
MCP server receive the same private root and share its salt. Cleanup removes the
whole private directory and resets the in-process monitor cache. No AppData
snapshot or shared-store deletion is needed.

Set `BYO_MANAGED_TEST_EVIDENCE` to a unique ignored directory to retain raw agent
stdout/stderr, session transcripts, exact command and MCP configuration, model pin, return code,
workspace digest and a copy of the monitor store before cleanup. Captures also
survive model-pin failures and subprocess timeouts. Keep evidence outside the
disposable monitor root and never publish copied private data.

Workspace interoperability checks load the actual sibling `CodexClaudeWorkflow`
source distribution. Its `bin/attach-project` creates physical `.agent-workspace`
copies in temporary customer projects. Tests use that helper and its shipped
manual-permission implementation directly; an installed sibling
`.agent-workspace` directory is not required.
