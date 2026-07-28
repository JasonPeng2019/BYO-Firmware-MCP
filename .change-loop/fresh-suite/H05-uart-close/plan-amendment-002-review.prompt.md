Authorized local firmware validation. This is a host-only, read-only review limited to the named
local BYO-Firmware-MCP workspace. No hardware, remote target, or third-party system is in scope.

Resume your existing independent plan-amendment reviewer role. Read the complete
`../.codex/design_charter.md`, then read only the following material needed for PA-002:

- `.change-loop/fresh-suite/H05-uart-close/plan-amendments.md` (PA-002)
- `.change-loop/fresh-suite/H05-uart-close/state/test_report.md`
- `.change-loop/fresh-suite/H05-uart-close/state/doer.last_message.md`
- the traceback-capture portions of `tests/test_h05_uart_close_spec.py` and
  `tests/test_regression_h05_uart_close.py`
- the UART implementation diff in `src/pyocd_debug_mcp/services/uart_capture.py`

Independently decide whether PA-002 is a narrow, technically correct correction to a destructive
test oracle, while preserving the production identity/traceback/graph requirements and the design
charter. Do not edit any file, operate hardware, run the change loop, or re-review PA-001 and
unchanged CL-001 clauses.

Return exactly one of:

- `AMENDMENT_READY` followed by concise numbered execution risks/test targets; or
- `AMENDMENT_BLOCK` followed by the exact unresolved contradiction and the smallest required
  main-authored correction.

Explicitly state that you reread the complete design charter.
