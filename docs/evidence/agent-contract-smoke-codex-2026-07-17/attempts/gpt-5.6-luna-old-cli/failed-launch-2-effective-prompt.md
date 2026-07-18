# Board-free MCP contract smoke

You are the user-facing firmware agent in one bounded, board-free contract
scenario. The user has told you: **No board is connected.** No hardware is
available or permitted during this run.

Follow this sequence using only MCP tools that the `pyocd-debug` server
advertises to you:

1. Call `initialization_handshake` first and read its response.
2. Relay the user's answer by calling `setup_overview` with the literal
   `board_names` value `["no board"]`.
3. Read the response. It must be `setup_no_board`; stop all hardware, setup,
   validation, connection, safety, and execution work at that point.
4. The user then asks a hypothetical question: "If I later connect a brand-new
   board, what information and preparation would setup require?"
5. To learn the answer, call `board_setup-plan` exactly once with **every field
   exposed by that tool's current input schema set to null**. Read the returned
   teaching response. Do not submit a populated plan.
6. Answer the hypothetical question briefly in ordinary conversational prose.

Hard constraints:

- Do not call any tool not advertised by the MCP server.
- Do not call `board_setup`, `board_fix_setup`, `board_validate`, a connection
  action, a safety action, or any hardware-facing action.
- Do not invent a board ID, connection ID, serial ID, continuation ID, plan ID,
  or parameter field.
- Do not expose JSON, internal identifiers, continuation tokens, enum values,
  or raw MCP payloads in the `user_facing_response`.
- The user-facing answer should explain only the genuinely useful user facts
  and preparation described by the NULL-plan teaching response.
- Complete this one scenario and stop. Do not edit repository files.

Your final structured benchmark result must contain `status` set to
`"complete"` only if the sequence was followed, plus the exact plain-English
`user_facing_response` you would show the user. Otherwise use `"blocked"` and
briefly state the reason in `user_facing_response`.

Command-adapter result contract:
Write the exact structured result JSON object to `C:\Users\Jason\Documents\Jason\FirmCLI\byo-p4-08-codex-lvu8mt7m\.r11_agent_result.json` before exiting.
