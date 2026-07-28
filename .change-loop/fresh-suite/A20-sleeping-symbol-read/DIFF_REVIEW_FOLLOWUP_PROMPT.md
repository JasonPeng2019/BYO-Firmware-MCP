Authorized local firmware validation. Continue the same read-only A20 diff-review session in the
same BYO-Firmware-MCP repository. This is host-only review and authorizes no hardware action.

The main model accepted both of your earlier findings. The existing persistent doer and testers
have now corrected them. Reread the complete `../.codex/design_charter.md` and verify SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`, then inspect only:

- the current A20 hunks in `src/pyocd_debug_mcp/tools/memory.py` and
  `src/pyocd_debug_mcp/server.py`;
- `tests/test_a20_sleeping_symbol_read_spec.py`;
- `tests/test_regression_a20_sleeping_symbol_read.py`;
- `.change-loop/fresh-suite/A20-sleeping-symbol-read/state/test_report.md`;
- plan SHA-256 `a974e693dd8b16d03d993b27b0c16f1113891482de58e67160608b2cc6da0a07`.

Verify specifically that cleanup now handles and preserves non-`Exception` primary/restoration
failures without changing successful or already-HALTED behavior, and that the published help
fully documents parameters plus an example without schema drift. Recheck the earlier nonblocking
risks only where reached by these corrections.

Remain strictly read-only: do not edit files, run the server, operate hardware, commit, push,
deploy, replan, or launch another agent. Return `VERDICT: ACCEPT` if both earlier actionable
findings are closed and no new actionable issue is introduced; otherwise return
`VERDICT: NEEDS_FIX` with exact file/line and evidence. State the verified hashes and that no edits
or hardware actions occurred.
