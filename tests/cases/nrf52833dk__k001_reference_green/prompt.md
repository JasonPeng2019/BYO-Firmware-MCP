You are validating a firmware benchmark case on `{board_id}`.

Use the `pyocd-debug` MCP server tools to determine whether the currently
tracked baseline firmware is healthy.

Rules:

- connect with `connect(board_id="{board_id}")`
- do not use a hard-coded probe UID
- treat the current workspace as read-only
- the runner already prepared the target state; do not rebuild or reflash unless
  the MCP evidence proves the prepared state is wrong
- do not edit source files
- do not read `.codex/skills`, `.claude/`, `superpowers/`, or repo markdown
  files; this benchmark prompt is already the full task contract
- do not run local shell discovery commands such as `rg`, `ls`, or
  `Get-ChildItem`; use the MCP tools directly for verification
- do not use `unlock_recover` unless the evidence clearly justifies it
- use MCP tools rather than guessing from static files alone
- use this exact verification flow unless one step demonstrably fails:
  1. `connect`
  2. `read_serial(expected_text="{uart_substring}", reset_on_open=true)`
  3. `read_symbol_u32(elf_path="{symbol_artifact}", symbol_name="{symbol_name}")`
  4. return the structured result immediately

Target observables to confirm:

- UART contains `{uart_substring}`
- symbol `{symbol_name}` resolves and reads back `{symbol_value_u32_hex}`

Return your final answer in the structured schema exactly.
