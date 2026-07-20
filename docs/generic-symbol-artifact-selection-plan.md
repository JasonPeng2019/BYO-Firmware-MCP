# Generic symbol artifact selection — implementation plan

1. Replace implicit reference-image fallback with explicit/current-run artifact selection and
   digest revalidation in the memory handlers.
2. Add optional `elf_artifact` parameters to find/read symbol tools, responses, and documentation.
3. Add nullable `elf_artifact` to `write_memory`, its plan schema, containment preflight, and action
   handler; require and digest-bind it for symbols while raw addresses require null and no binding.
4. Add focused restart/no-binding, explicit-selection, byte-change, no-backend-call, plan-binding,
   and raw-address-null tests. Synchronize intended MCP/plan contracts.
5. Run focused suites, Ruff, Pyright, full pytest, package/import, and stdio smoke. Run fresh Terra
   high/fast read-only diff review and fix valid findings until clean, then resume the blocked Luna
   session and repeat live by-name reads.

## Adversarial-audit correction

6. Reverify the plan-bound artifact after containment has resolved the symbol and before plan
   consumption, then carry that resolved symbol through the request-scoped managed operation so the
   handler cannot reinterpret later path contents.
7. Normalize malformed-ELF failures into the existing pre-backend plan refusal and add focused
   mutation-during-containment, prepared-symbol, and malformed-ELF tests.
