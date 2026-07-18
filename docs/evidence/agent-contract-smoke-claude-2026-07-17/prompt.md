# Board-free MCP interaction-contract smoke

Complete this one bounded scenario using only MCP tools advertised by `pyocd-debug`. Do not use hardware, a probe, UART, setup execution, validation execution, safety execution, connect, or any real board action.

1. Call `initialization_handshake` once and read its returned guidance.
2. The simulated user's literal conversational answer is **no board**. Call `setup_overview` with the literal one-element board-name list containing `no board`.
3. Read the returned status. On `setup_no_board`, stop all hardware-path, setup, and validation work. Do not try another spelling, create a board profile, or call a routed setup action.
4. The user then asks only: "What would setup require later for a hypothetical new board?" Call the advertised `board_setup-plan` exactly once with every one of its top-level plan fields null, solely to read its NULL-plan teaching. Do not submit a populated plan. Do not call `board_setup`, `board_fix_setup`, `board_validate`, any safety tool, any connection tool, or any hardware-facing action.
5. Give the user a short ordinary-English explanation of what future setup would require. The user-facing prose must contain no JSON, plan object, internal ID, board ID, connection ID, continuation token, permission enum, or invented parameter name. Do not claim setup or validation ran.

Only request MCP tools present in the server's advertised list. Built-in Read may read this prompt and built-in Write may write only the harness result file.

After the scenario, write one JSON object to the absolute result path in the command-adapter contract, with exactly these fields: `scenario_status` (`completed`, `failed`, or `blocked`), `setup_status` (exact observed status or null), `stopped_hardware_path` (boolean), `null_plan_read_only` (boolean), `populated_plan_submitted` (boolean), `real_setup_validation_or_hardware_called` (boolean), `user_facing_response` (the same ordinary-English response), and `notes` (array of short strings). Do not perform extra scenarios or retries.
