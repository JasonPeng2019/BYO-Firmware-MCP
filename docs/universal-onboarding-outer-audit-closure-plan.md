# PLAN: Universal Onboarding Outer-Audit Closure

1. Replace the nonexistent README registration helper with the supported `codex mcp add ... -- uv run --project <checkout> pyocd-debug-mcp` form.
2. Re-run docs/contract tests, Ruff, Pyright, and the full locked suite.
3. Run package/import and bounded stdio protocol smoke.
4. Create two fresh external git repositories containing only the appropriate datasheet plus a plain part/board input file. Launch GPT-5.6-Luna with an explicit MCP config pointing at this checkout and a distinct fresh artifact root per run.
5. Record truthful MCP/setup/tool coverage and diagnose any general product defect. Fix general defects through spec/plan/change/retest; do not treat unavailable hardware as product success.
6. Repeat self-audit and exact GPT-5.6-Terra diff audit after any source change.
