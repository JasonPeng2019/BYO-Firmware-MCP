# H01 Plan Review

Plan SHA-256: 21b059867ac578caf99acb7f4410e47494ecc8aeaa1317c16dfa8d6051801cc8

- **Reviewed plan SHA-256:** `21b059867ac578caf99acb7f4410e47494ecc8aeaa1317c16dfa8d6051801cc8`
- **Reviewer:** `/root/h01_null_plan_review` — OpenAI Codex (`gpt-5.6-sol`)
- **Verdict:** **PASS**

## Charter attestation

I reread the complete `../.codex/design_charter.md` before analysis and again immediately before
this verdict (SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`).
The plan is charter-aligned: it repairs caller-value corruption at the single generated-plan
registration owner, delegates unchanged non-text compatibility to the pinned SDK, preserves
strict correctness guards, and adds no environment-specific behavior, dependency, hardware
policy, hostile-input defense, or speculative framework.

## Execution risks and test targets

1. **Keep the policy instance-local.** Install the override only on metadata belonging to tools
   created by `register_plan_tools()`. Preserve the existing `arg_model`, output metadata,
   conversion path, and error wrapping; do not mutate the SDK `FuncMetadata` class or change
   `forbid_unknown_tool_arguments()` behavior for its non-plan callers.
2. **Select fields from the declarative top-level call envelope.** Protect every
   `definition.call_fields` entry whose type is `TEXT` or `TEXT_OR_INTEGER`, including
   `user_permission` on permission-bearing tools. Do not confuse nested `definition.action_fields`
   with top-level fields or exempt the outer `action_parameters` object from SDK parsing.
3. **Prove type and value preservation at the real boundary.** Through registered async
   `Tool.run(..., convert_result=True)`, demonstrate that bare JSON-looking Python strings
   `"null"`, `"true"`, `"[]"`, and `"{}"` reach the handler/engine unchanged. For `"null"` in
   each reasoning position, assert the named `must be concrete, not placeholder text` refusal,
   not `must not be NULL`. Retain controls showing actual `None` still initializes the universal
   envelope and still triggers the existing populated non-nullable-field NULL error.
4. **Exercise the delegation half, not only preservation.** Pass a string-encoded
   `action_parameters` object through the same registered tool and assert it becomes a mapping;
   include a nested JSON-looking text value to prove outer decoding does not rewrite nested
   caller text. Malformed JSON, a decoded wrong top-level type, and unknown top-level arguments
   must retain their existing failures.
5. **Compare schemas against the current strict registered contract.** Assert nullable field
   unions, defaults/required shape, and `additionalProperties: false` remain unchanged. The
   comparator must include the existing strict-envelope rebuild; comparing against raw
   `FastMCP.add_tool()` output would create a false baseline.
6. **Use a non-plan control with the same vulnerable union shape.** A minimal tool configured only
   by `forbid_unknown_tool_arguments()` should retain the pinned SDK behavior for a JSON-looking
   string, proving the new policy did not leak globally or alter connect/setup/unlock/artifact
   invocation semantics.
7. **Preserve the accepted H00 working tree byte-for-byte.** Snapshot and recheck the six existing
   H00 files. Review-time SHA-256 values are:
   `README.md` `15a3c471426302805e866563bba78a2c0482b00133fce4fdbd262c8cd6763f1a`;
   `pyproject.toml` `357b4bf783b0226d04d33035fc78fd63535bb279bf20b7e25be11637a335a454`;
   `src/pyocd_debug_mcp/kernel/processes.py`
   `5f74cd9be7aeea3b2b72d97d9c0b00ad3120205902aacd17a96ca53ebac42435`;
   `uv.lock` `1b0ea27f91dddbd00c215b8d9da487d7960e1fb4f1e1afa4c07bc4811c7ff0cf`;
   `tests/test_h00_repository_contract.py`
   `2f2dd37fe76b642b40fbd7177bef9ce12775eedc925b229489573615b33d597a`;
   and `tests/test_h00_repository_regressions.py`
   `56b72f516554016b9616db2828f437dfca4cb29767adeafa0e987bdd4afbf29b`.
8. **Gate the installed behavior, not a model-only surrogate.** After both focused suites pass,
   rerun the installed-boundary reproducer and the complete locked Ruff, Pyright, collection,
   build, lock, and pytest gate specified by the plan.
