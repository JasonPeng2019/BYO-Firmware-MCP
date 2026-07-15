You are debugging a firmware benchmark case on `{board_id}`.

The current workspace contains the buggy firmware source that produced the
flashed image on the target.

Rules:

- connect with `connect(board_id="{board_id}")`
- do not use a hard-coded probe UID
- operate only inside this workspace for code edits
- do not read `.codex/skills`, `.claude/`, `superpowers/`, or repo markdown
  files; this benchmark prompt is already the full task contract
- if you change code, rebuild with `{build_command}`
- after a successful rebuild, flash the rebuilt artifact with
  `flash_firmware(path="build/firmware.hex")`
- do not use `unlock_recover` unless the evidence clearly justifies it
- use MCP tools to gather evidence before editing
- inspect only `src/src/main.c` unless the first verification pass proves the
  fault is elsewhere
- use this exact flow unless one step demonstrably fails:
  1. `connect`
  2. `read_serial(expected_text="{uart_substring}", reset_on_open=true)`
  3. `read_symbol_u32(elf_path="{symbol_artifact}", symbol_name="{symbol_name}")`
  4. if UART is wrong but the symbol already reads `{symbol_value_u32_hex}`,
     fix the UART text in `src/src/main.c`
  5. rebuild, flash, and re-run the UART and symbol checks
  6. return the structured result immediately after verification

Target observables for the repaired state:

- UART contains `{uart_substring}`
- symbol `{symbol_name}` resolves and reads back `{symbol_value_u32_hex}`

Return your final answer in the structured schema exactly.
