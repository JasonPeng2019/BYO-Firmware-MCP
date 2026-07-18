# Claude board-free agent-contract smoke ? PASS

Claude Code 2.1.76 ran `claude-sonnet-4-5-20250929` at medium effort through an isolated config directory and a checkout-scoped strict MCP definition. After two preserved auto-mode blockers, the successful run used default permission mode with an exact five-tool allowlist (Read, Write, and the three required advertised MCP tools).

The MCP call sequence was exactly `initialization_handshake`, `setup_overview` with the literal no-board answer, and one all-NULL `board_setup-plan`. The server returned `setup_no_board`; no setup, validation, connection, safety, or hardware action ran. The model's user-facing explanation contains no JSON or internal identifiers. Maximum logged context was 17952 of an effective 180,000-token window.
