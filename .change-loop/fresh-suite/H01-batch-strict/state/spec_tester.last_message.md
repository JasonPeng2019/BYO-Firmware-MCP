Strengthened only `tests/test_h01_strict_mcp_boundary.py`.

Added proofs for:
- CL-004/H01-BS-A1: deterministic no-sleep cancellation cleanup, followed by an independent visibility-changing dispatch in the continuing task.
- CL-004/H01-BS-A1: real in-process MCP transport observation of exactly one nested relock `notifications/tools/list_changed`, plus clean subsequent wire behavior after failure.
- CL-001/CL-002/H01-BS-A1: actual `register_plan_tools` path covering populated non-permission rejection, literal JSON-looking text preservation, parsed non-text compatibility, required permission acceptance, and once-only metadata pre-parse/engine dispatch.

Verification through the requested WSL environment:
- Ruff: passed.
- Spec suite: 9 passed, 7 subtests passed.

State records remain exactly:
- `uv run --locked --no-sync pytest -q tests/test_h01_strict_mcp_boundary.py`
- `tests/test_h01_strict_mcp_boundary.py`

No remaining untestable ambiguity identified. Charter checkpoints completed: pre-analysis (with plan/review/amendment), immediately before each distinct feature edit, pre-verification, and pre-final verdict.
