# H01 batch strictness — independent plan review

Plan SHA-256: ca11a4c2775ce75c7f0ac92369679506a5c0d62e1c17193aaf8b3ed356246f72

- Reviewer: `/root/h01_batch_strict_plan_review` — `gpt-5.6-sol`, high reasoning
- Review mode: independent, read-only, no hardware
- Initial verdict on the exact plan: `BLOCK`
- Targeted amendment verdict: `RESOLVED`
- Reviewed amendment SHA-256:
  `d7aba9a5ac5c1fdadac03f0453b496d913e835f748a050ebb3b745ef65e6cf1b`
- Charter attestation: reviewer read the complete `../.codex/design_charter.md` before the plan
  review, immediately before its initial verdict, before the targeted amendment review, and
  immediately before its final verdict. It edited no file and operated no hardware.

## Initial blocking finding

1. CL-002's originally separate validation-only pass followed by
   `Tool.run(raw_arguments)` would repeat FastMCP pre-parsing and every Pydantic/model validator
   on valid calls. The plan did not provide a once-only validated invocation path or validator
   invocation-count test. This was a genuine compatibility/correctness defect in the plan.

The main model did not start implementation. It wrote only the minimal
`plan-amendments.md` A1 resolution, leaving the reviewed plan unchanged, and requested one
targeted review of that amendment rather than re-reviewing unchanged plan items.

## Amendment resolution

The reviewer found A1 resolves the block by requiring one pre-parse/model-validation pass, retaining
the validated one-level mapping for invocation, and forbidding a later
`Tool.run(raw_arguments)` validation pass.

## Numbered execution risks and test targets

1. Validation failures must retain FastMCP's
   `ToolError("Error executing tool <name>: ...")` shape and Layer-2 wrapping, not escape as a raw
   `ValidationError`.
2. Use FastMCP's one-level dump semantics exactly; do not substitute recursive `model_dump()`.
3. Verify context injection, sync-thread dispatch, async invocation, result conversion, and handler
   exception wrapping each occur exactly once.
4. Validator counters must cover direct, batch-child, malformed, and locked calls, including
   generated-plan metadata behavior.
5. Wire-level tests must prove `isError`, one recoverable structured failure, completed-prefix
   preservation, safe-exit de-duplication, and notification-state cleanup.
6. Nested notification tests must prove context-local isolation, cleanup after exceptions and
   cancellation, and exactly one outer notification for a child relock.
7. Global registration strictness is justified by the H01 contract; do not regress into per-tool
   key checks or modify generated-plan metadata/validators.
8. Preserve lock precedence, dynamic child validation, published schema truthfulness, and every
   accepted-file hash named by the plan.

