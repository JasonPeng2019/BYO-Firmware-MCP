# A21 write-memory coherence plan review

- Reviewer model/reasoning: gpt-5.6-terra / medium
- Charter SHA-256: `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`
- Reviewed plan SHA-256: `d2a1d29a7b8932133fa959bf35733906086cee3121240aa0594e15c0615fc626`
- Verdict: **PASS**

## Execution risks and test targets

NO_BLOCKING_RISKS.

1. **Inserted-halt ownership and provider-neutral states.** Test exact ordered calls for `HALTED`, `RUNNING`, `SLEEPING`, `RESET`, and an opaque non-halted provider state. Only `HALTED` may omit halt/resume; every successfully inserted halt must receive exactly one restoration attempt.
2. **Failure preservation including cancellation.** Test `get_state`, halt, write, readback, mismatch, and resume failures. For write/readback/mismatch `BaseException` failures, restoration must still be attempted; when it succeeds, re-raise the original object/trace. When restoration also fails, expose both facts and use the primary failure as `__cause__`.
3. **No fabricated success.** Test exact same-address/same-width readback and width-formatted expected/observed mismatch text. A readback mismatch or restoration failure after a matching write must neither return the existing success text nor record a success event.
4. **Preflight and compatibility boundary.** Cover symbol and justified raw-address routes, successful response/event compatibility, and every relevant refusal branch to prove no lifecycle/read/write/resume call occurs before existing parsing, artifact/revalidation, symbol/width/alignment, value/range, containment, plan/gate, and validation checks complete.
5. **Published contract and scope.** Inspect the registered MCP `write_memory` docstring, handler docstring, and initialized plan guidance for halted/running/sleeping lifecycle, immediate verification limitation, honest failures, and recovery, while retaining existing symbol-first, RAM-only, example, schema, budget, and permission guidance. Keep tests hardware-free and preserve separate tester ownership/manifests.