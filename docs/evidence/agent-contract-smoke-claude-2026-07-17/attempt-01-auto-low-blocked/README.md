# Claude board-free agent-contract smoke ? BLOCKED

The single permitted Claude Sonnet 4.5 low-effort run was executed once and was not retried. Claude connected to the checkout-scoped BYO stdio MCP server and discovered the three intended tools, but Claude Code disabled `auto` permission mode through its runtime circuit breaker, fell back to `default`, and denied `initialization_handshake` before it reached the server. No setup, validation, connection, or hardware action ran.

The current command adapter then raised because the model result file was absent and did not expose its captured stdout/stderr, so raw transcript assertions are not evaluable. Both failures are retained as evidence rather than converted into a pass. The maximum logged context count was 10,716 of an effective 180,000-token window.
