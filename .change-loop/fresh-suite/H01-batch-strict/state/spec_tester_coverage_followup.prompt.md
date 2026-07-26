Authorized local firmware validation. This is a host-only follow-up in the named BYO-Firmware-MCP workspace; no board, remote target, or hardware action is in scope.

Resume your persistent H01 spec-tester role. The neutral suite is behaviorally green, but the main-model post-gate audit found three explicit plan/amendment proof gaps in your owned `tests/test_h01_strict_mcp_boundary.py`. Strengthen only that file, without production edits, other-test edits, dependency/lock changes, runtime-control changes beyond your required command/manifest, or assertion weakening:

1. H01-BS-A1 rule 6 / CL-004 requires deterministic cleanup after cancellation, not only an ordinary exception. Add a no-sleep deterministic test in which a nested or direct registered dispatch is cancelled while inside the call path, then the same continuing task issues an independent visibility-changing call and proves notification ownership/nesting state was restored.
2. H01-BS-A1 rule 6 requires a real MCP transport proof that an exact nested child relock emits exactly one `notifications/tools/list_changed`. Extend the in-process ClientSession/wire fixture or add a dedicated wire test that actually observes the notification count/order; a direct fake-session-only assertion is insufficient for this wire claim. Also prove a later wire request does not inherit state after a failed/cancelled call where the transport permits that proof.
3. H01-BS-A1 rules 4-5 and CL-001 require the actual generated-plan metadata path to preserve populated non-permission-plan rejection and literal JSON-looking text while pre-parse/model validation occurs once. Add a deterministic actual `register_plan_tools`/generated-plan registered-boundary fixture (or equivalently production-faithful generated-plan fixture) with an observable metadata/validator counter and handler/engine evidence. Cover accepted populated permission behavior, literal text, and once-only valid dispatch. Do not replace this with a generic ordinary tool counter; that is already covered.

Read the full plan, amendment, plan review, and complete `../.codex/design_charter.md` before analysis; reread the full charter immediately before editing, between these distinct proof features, before verification, and before final verdict. Record all checkpoints.

Use the same portable state records:
- spec command: `uv run --locked --no-sync pytest -q tests/test_h01_strict_mcp_boundary.py`
- manifest: `tests/test_h01_strict_mcp_boundary.py`

Run verification through this exact WSL environment prefix so no repository `.venv` is created or mutated:
`wsl -d H00-POSIX --cd /mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP -- env UV_PROJECT_ENVIRONMENT=/root/mcp-trial-3-h01-gate-venv uv run --locked --no-sync ...`
At minimum run the isolated spec suite and `ruff check tests/test_h01_strict_mcp_boundary.py`. Do not operate hardware.