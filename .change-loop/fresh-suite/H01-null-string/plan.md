# Change implementation plan

## Source change list

- Source: `.change-loop/fresh-suite/H01-null-string/changes.md`
- Goal summary: Preserve caller-supplied JSON strings at every generated plan-tool MCP boundary so
  the strict plan engine sees the caller's real type and value, while retaining actual-NULL
  initialization, non-text JSON-string compatibility, strict schemas, and every existing
  plan/gate/permission contract.

## Repository context and assumptions

- Verified architecture and relevant entry points:
  `src/pyocd_debug_mcp/tools/plans.py::_field_annotation()` makes every generated plan parameter
  nullable so one universal all-NULL call reaches
  `guardrails/plan_engine.py::PlanEngine.submit()`. `register_plan_tools()` registers those
  callables with FastMCP and `forbid_unknown_tool_arguments()` rebuilds their Pydantic argument
  models. On the real `Tool.run()` path, pinned MCP `FuncMetadata.call_fn_with_arg_validation()`
  invokes `pre_parse_json()` before Pydantic; because `str | None` is not identity-equal to
  `str`, the SDK applies `json.loads("null")` and turns the caller's JSON string into Python
  `None`. Direct argument-model validation and direct handler calls preserve the string, proving
  the corruption is at this registration/invocation boundary. The same
  `forbid_unknown_tool_arguments()` helper also configures non-generated connect, artifact,
  setup, and unlock tools from `server.py`, so plan-specific behavior must be opt-in.
- Existing test/build commands relevant to the change:
  `uv sync --locked`;
  `uv run --locked --no-sync pytest -q tests/test_h01_plan_text_preservation.py`;
  `uv run --locked --no-sync pytest -q tests/test_h01_plan_text_regressions.py`; the accepted
  repository gate uses `uv lock --check`, `uv build`,
  `uv run --locked --no-sync ruff check .`, `uv run --locked --no-sync pyright`,
  `uv run --locked --no-sync pytest --collect-only -q`, and
  `uv run --locked --no-sync pytest -q`.
- Baseline preservation: the accepted uncommitted H00 repair modifies only `README.md`,
  `pyproject.toml`, `src/pyocd_debug_mcp/kernel/processes.py`, `uv.lock`, and its two H00 tester
  files. This H01 repair may not rewrite, revert, or broaden those changes.
- Charter checkpoint: the current main model reread the complete
  `../.codex/design_charter.md` after verifying the live defect and again between the change
  request and this plan. The selected boundary is the single generated-plan registration owner,
  preserves exact caller meaning, keeps the SDK's useful normal-case compatibility only where
  declared types need it, and adds no board, OS, toolchain, dependency, or hostile-input policy.

## Plan items

### CL-001 — Preserve strings at the generated plan-tool boundary

<!-- Assumption: FastMCP's JSON-string compatibility is intentional for fields that declare
non-text containers. Preserve it there, but a field whose declarative type admits text treats
every incoming Python `str` as the caller's literal JSON string, including JSON-looking `null`,
`true`, `[]`, and `{}` spellings. This is value/type preservation, not a special-case list of
placeholder words. -->

- **What to change:** Add the smallest plan-registration metadata/pre-parse policy that identifies
  generated plan fields whose `FieldDefinition.field_type` is `TEXT` or `TEXT_OR_INTEGER` and
  prevents FastMCP's compatibility pre-parser from replacing their incoming `str` values.
  Delegate all other fields to the pinned SDK's existing pre-parser. Install this policy only on
  the generated plan tools during `register_plan_tools()` and keep the strict rebuilt argument
  model/schema as the source of validation.
- **Where:** `src/pyocd_debug_mcp/tools/plans.py`, limited to the generated plan-tool metadata
  boundary and the smallest supporting type/import definitions. Do not edit the pinned MCP SDK,
  `plan_engine.py`, declarative plan definitions, or `server.py`.
- **Exact intended behavior:** A real registered plan `Tool.run()` call preserves all text-field
  strings verbatim. In particular, populated `hypothesis="null"` reaches
  `PlanEngine._validate_reasoning()` as a string and refuses with the exact field plus
  `must be concrete, not placeholder text`, never `must not be NULL`. Actual Python/JSON `None`
  still reaches the engine as `None`, so the complete all-NULL envelope still returns the normal
  initialization guide and a populated non-nullable text field set to actual NULL still receives
  the existing `must not be NULL` field error. No raw string is rewritten by a hardcoded
  placeholder table at this boundary.
- **Must remain intact:** Preserve handler signatures and published schemas, including nullable
  initialization types and `additionalProperties: false`; Pydantic type/extra rejection;
  populated non-permission `user_permission` omission/rejection; session resolution; plan
  canonicalization; reasoning, budget, permission, gate, visibility, notification, fallback, and
  action-parameter behavior. Preserve non-plan uses of `forbid_unknown_tool_arguments()` exactly.
  Add no dependency, global FastMCP monkeypatch, server-specific SDK fork, board/OS branch, or
  suppression.
- **Objective verification:** A spec-tester-owned focused test must register an actual generated
  plan tool and call its real async `Tool.run(..., convert_result=True)` boundary. After the
  all-NULL initialization, assert `"null"` and every other signed reasoning placeholder remain
  strings and produce the placeholder-reasoning refusal; inspect a capturing engine or equivalent
  direct observable to assert JSON-looking text values `true`, `[]`, and `{}` arrive unchanged.
  Assert actual NULL still initializes and still produces the existing populated-field NULL
  refusal where applicable. A direct model/handler-only test is insufficient.

### CL-002 — Retain non-text compatibility and isolate the policy

<!-- Assumption: The regression contract is behavior at the registered `Tool.run()` boundary.
Tests may use a minimal generated definition/engine double so they stay host-only and deterministic,
but must not reimplement FastMCP's pre-parser or bypass the production registration function. -->

- **What to change:** Keep the new string-preservation policy explicitly scoped to generated plan
  tools and declared text-admitting fields. For all other generated plan fields, retain
  FastMCP's existing JSON-string compatibility conversion and subsequent Pydantic/engine
  validation. Do not alter helper callers that do not opt into generated-plan text semantics.
- **Where:** The same narrow registration boundary in
  `src/pyocd_debug_mcp/tools/plans.py`; regression coverage in a tester-owned file separate from
  the spec tester's file.
- **Exact intended behavior:** A JSON object encoded as a string for a generated plan's
  `action_parameters` is still decoded to a mapping before the handler, and corresponding
  array/object compatibility remains available for any generated non-text field. Malformed or
  wrong-type values remain rejected. Unknown top-level arguments remain forbidden and published
  schemas remain unchanged. A non-plan tool passed through the existing strict-envelope helper
  retains the pinned SDK's unmodified pre-parse behavior.
- **Must remain intact:** Preserve every existing FastMCP result conversion and error wrapper;
  universal all-NULL guide behavior; direct and dynamic tool registration; all plan definitions;
  connect/setup/unlock/artifact tool behavior; H00 production/test changes and hashes; full
  repository Ruff/Pyright/pytest behavior.
- **Objective verification:** A regression-tester-owned focused test must exercise production
  `register_plan_tools()` and prove: string-encoded object input is still decoded for
  `action_parameters`; unknown arguments fail at the strict schema boundary; the generated tool's
  schema is unchanged in shape; and a minimal non-plan tool configured only through
  `forbid_unknown_tool_arguments()` still exhibits the base SDK pre-parser behavior. Both focused
  tester commands must be isolated and deterministic, then the main model must run the locked full
  repository gate.

## Out of scope / must not change

- Firmware, fresh-experiment artifacts, boards, hardware, providers, serial/UART, flash/debug/reset,
  setup state, permissions, plan budgets, action dispatch, dynamic registry semantics, or result
  wording outside the proven text-type corruption.
- `README.md`, `pyproject.toml`, `uv.lock`, `src/pyocd_debug_mcp/kernel/processes.py`, the accepted
  H00 tester files, any other production module, the MCP dependency/version, packaging, or
  generated artifacts.
- Global FastMCP monkeypatches, an SDK fork, placeholder-specific conversion branches, new
  configuration knobs, hardcoded environment behavior, or hostile-input defenses.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, or generated artifacts.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, and adjacent behavior touched by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, or gate commands.
- The doer edits only `src/pyocd_debug_mcp/tools/plans.py`; the spec and regression testers use
  separate H01-focused test files and do not modify production or each other's files.
- The pre-loop accepted H00 tracked diff and tester files remain unchanged except for the new
  explicitly authorized H01 production/test additions.
- The main model reruns the exact installed-boundary reproducer, then runs `uv lock --check`,
  Ruff, Pyright, focused H01 tests, collection, and full pytest from the locked environment.
- Every repair role rereads the complete `../.codex/design_charter.md` before analysis, before
  editing, between distinct features, before verification, and before its final verdict, and
  records the checkpoints in its final message.
