# Codex board-free contract smoke transcript

Provider: Codex CLI 0.142.2  
Model: `gpt-5.4`  
Effort: medium  
Outcome: pass

## MCP sequence

1. `initialization_handshake`
2. `setup_overview` with the literal no-board sentinel; server returned the no-board terminal status and no routes.
3. `board_setup-plan` once with every advertised plan field null.

No populated plan, real setup, validation, connection, safety, or hardware action ran. The full request/response records are in `mcp-tool-timeline.json`, and the provider event stream is in `raw-codex-events.jsonl`.

## User-facing response

If you later connect a brand-new board, setup would first need one unique familiar name for that board, then its exact board type, the full package-level MCU part number, and the path to an authoritative local datasheet PDF. It would also need the specific connected probe and UART choice routed by the server, plus a UART baud rate if serial setup is part of the profile. Before downloading any large SDK or toolchain, setup would first check the project, parent folders, environment paths, and normal vendor install locations for a compatible local copy.
