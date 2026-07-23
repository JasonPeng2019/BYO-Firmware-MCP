# Verify and, only where needed, fix four reported MCP server correctness bugs

Use `.change-loop/design_charter.md` as the governing design standard. Inspect current code and tests before asserting that any issue exists. Do not edit unrelated behavior. For each item, record whether the bug exists, the evidence, and the smallest general fix if needed.

1. Ambient overrides in `connect_under_reset`: `PYOCD_BOARD_CONFIG` and `PYOCD_PROBE_UID` are outside action parameters and may invisibly override a correct agent plan. Ensure the action cannot be redirected by ambient environment state. Resolve toward an explicit, truthful, general contract rather than assuming a sanitized launch environment.

2. Overstrict build-output discovery: valid Zephyr/NCS multi-image builds may produce multiple ELF/MAP files. Do not treat multiple candidates as a generic build failure. Report candidate artifacts and require/select an explicit artifact using the existing interface patterns, without guessing which image is correct.

3. False final-reset reporting: after programming, a failed or unobservable final reset must not be reported as `running`. Distinguish observed running, observed halted, and reset-state unconfirmed. Never claim running unless observed.

4. Stale session after recovery: mass erase/recovery may invalidate the debug session. Close and invalidate the session after recovery, then require or perform a clean reconnect before later validation/debug operations.

Preserve public compatibility where it does not conflict with truthful reporting. Update tool contracts/docs and focused tests for every confirmed bug. Avoid hardware-specific branches, environment-specific constants, speculative abstractions, commits, deployment, and physical hardware actions. Verification must be automated and must not require a connected board.
