You are debugging a firmware benchmark case on `{board_id}`.

The current workspace contains the buggy firmware source that produced the
flashed image on the target.

Rules:

- connect with `connect(board_id="{board_id}")`
- do not use a hard-coded probe UID
- operate only inside this workspace for code edits
- if you change code, rebuild with `{build_command}`
- do not use `unlock_recover` unless the evidence clearly justifies it
- use MCP tools to gather evidence before editing

Target observables for the repaired state:

- UART contains `{uart_substring}`
- symbol `{symbol_name}` resolves and reads back `{symbol_value_u32_hex}`

Return your final answer in the structured schema exactly.
