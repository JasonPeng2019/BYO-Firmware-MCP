# H01 Post-Implementation Adversarial Review

- **Reviewer:** `/root/h01_null_plan_review` - OpenAI Codex (`gpt-5.6-sol`)
- **Plan SHA-256:** `21b059867ac578caf99acb7f4410e47494ecc8aeaa1317c16dfa8d6051801cc8`
- **Verdict:** **PASS**

## Charter checkpoints

I reread the complete `../.codex/design_charter.md` before analysis and again immediately before
this verdict (charter SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`).
The repair is aligned with correctness, simplicity, generality, and single-owner neatness: it
preserves the caller's exact value without weakening validation, adds no environment/hardware
branch or hostile-input guard, and changes only the generated-plan registration boundary.

## Findings

1. **Correct and general value preservation.** `_PlanToolMetadata.pre_parse_json()` delegates to
   the SDK and restores only original `str` values for declaratively text-admitting top-level plan
   fields. It therefore preserves `"null"`, `"true"`, `"[]"`, `"{}"`, and arbitrary future
   strings without a placeholder table, while actual `None` remains `None`.
2. **Non-text behavior remains intact.** `action_parameters` and every other non-text field still
   use the SDK parser and the existing strict Pydantic model. The regression test proves
   string-encoded object decoding, nested text preservation, strict unknown-argument rejection,
   and unchanged non-plan helper behavior.
3. **Metadata, schema, output, and error paths are preserved.** The replacement metadata copies
   `arg_model`, `output_schema`, `output_model`, and `wrap_output`, inherits the SDK conversion
   methods, and is installed only after the existing strict-schema rebuild. Real
   `Tool.run(..., convert_result=True)` tests cover result conversion, initialization output,
   placeholder refusal wording, and actual-NULL field refusal.
4. **Implementation is narrow and understandable.** The production diff is confined to
   `src/pyocd_debug_mcp/tools/plans.py`; it adds one small metadata specialization and one
   registration helper. It does not alter the plan engine, definitions, server wiring,
   dependencies, permissions, gates, budgets, action dispatch, or hardware behavior.
5. **Private-SDK coupling is bounded, not eliminated.** Importing/subclassing FastMCP
   `FuncMetadata` is a version-sensitive internal seam, but it is localized beside the module's
   pre-existing `_tool_manager` coupling, does not monkeypatch SDK globals, and is exercised
   through the locked MCP `1.28.1` boundary. Any MCP upgrade must rerun these focused tests.
   This is a maintenance risk, not a defect in the accepted locked repair.
6. **Tester ownership and focused evidence are sound.** Manifest snapshots match the current two
   tester files. The neutral gate is green: spec `3 passed, 36 subtests passed`; regression
   `3 passed`. Coverage reaches the real registered async boundary rather than a direct model or
   handler surrogate.
7. **Accepted H00 bytes and scope are preserved.** All six H00 file SHA-256 values exactly match
   `PRE_IMPLEMENTATION_BASELINE.json`; the only new production path is the authorized
   `tools/plans.py`, and the only H01 test paths are the two role-owned files.

## Non-blocking verification note

The reviewed neutral report contains the two required focused suites, not the separate complete
repository lock/build/Ruff/Pyright/collection/full-pytest gate. That broader gate remains required
before a final repository-wide correctness claim. This evidence boundary does not expose a
subpar H01 implementation or justify blocking the diff.
