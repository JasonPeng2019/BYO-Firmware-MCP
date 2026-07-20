# PLAN: UART capture-duration correctness

Specification: `docs/uart-capture-duration-spec.md`

1. Add focused UART-helper coverage proving a no-sentinel capture accumulates later chunks rather
   than returning on the first bytes, and proving an unmatched sentinel is not truncated by the
   intermediate reopen-window setting.
2. Simplify `capture_uart_output`: remove the no-sentinel early return and let the final (or only)
   open use the remaining overall deadline; preserve expected-text and byte-limit early exits.
3. Run the focused UART tests, Ruff, and Pyright, then rerun the live 15-second capture in the same
   Luna session.
4. Review the server diff with a fresh read-only GPT 5.6 Terra high/fast run. Vet every finding,
   fix valid ones, repeat until clean, then run the complete software suite before continuing the
   hardware acceptance matrix.

