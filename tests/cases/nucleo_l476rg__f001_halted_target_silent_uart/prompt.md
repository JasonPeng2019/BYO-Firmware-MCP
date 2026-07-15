You are investigating a firmware-debug problem on `{board_id}`.

The board is not currently producing the expected UART confirmation, but you
must determine whether this is a firmware/code problem or an
observability/runtime-state problem before changing code.

Rules:

- connect with `connect(board_id="{board_id}")`
- do not use a hard-coded probe UID
- treat the current workspace as read-only
- the runner already prepared the target state; do not rebuild or reflash unless
  the MCP evidence proves the prepared state is wrong
- do not edit source unless the evidence clearly proves a code bug
- do not read `.codex/skills`, `.claude/`, `superpowers/`, or repo markdown
  files; this benchmark prompt is already the full task contract
- do not run local shell discovery commands such as `rg`, `ls`, or
  `Get-ChildItem`; use the MCP tools directly to distinguish runtime state from
  firmware defects
- do not use `unlock_recover` unless the evidence clearly justifies it
- use MCP tools to distinguish firmware state from board/runtime state
- use this exact flow unless one step demonstrably fails:
  1. `connect`
  2. `get_state`
  3. if the target is halted, use `reset(halt_after=false)` or `resume()` before
     judging the UART path
  4. `read_serial(expected_text="{uart_substring}", reset_on_open=true)`
  5. `read_symbol_u32(elf_path="{symbol_artifact}", symbol_name="{symbol_name}")`
  6. if the only problem was halted/runtime state and no code change was needed,
     classify it as an observability/runtime-state fault and return the
     structured result immediately

Healthy target observables after the fix:

- UART contains `{uart_substring}`
- symbol `{symbol_name}` resolves and reads back `{symbol_value_u32_hex}`

Return your final answer in the structured schema exactly.
