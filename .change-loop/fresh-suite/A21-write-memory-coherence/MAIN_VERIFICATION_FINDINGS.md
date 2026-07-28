# Main verification findings

## MV-001 — stale adjacent A20 fixture

At the post-loop verification boundary, the exact command below returned one failure:

```text
./.venv/Scripts/python.exe -m pytest -q \
  tests/test_a20_sleeping_symbol_read_spec.py \
  tests/test_a21_write_memory_coherence_spec.py \
  tests/test_regression_a20_sleeping_symbol_read.py \
  tests/test_regression_a21_write_memory_coherence.py \
  tests/test_server_trust_model_round_1.py \
  tests/test_server_trust_model_round_3.py \
  tests/test_server_trust_model_round_4.py \
  tests/test_swd_process_isolation.py
```

Result: `1 failed, 80 passed, 1 skipped, 45 subtests passed`.

The failure is
`CoherentSymbolReadSpecTests.test_pre_io_refusals_and_raw_operations_do_not_acquire_lifecycle_behavior`.
Its A20-era fake always returns `0x1234` from scalar reads, then expects a raw public
`write_memory(... value=1 ...)` to remain lifecycle-free. A21 deliberately and correctly changes
both public scalar write forms to use lifecycle-coherent exact readback, so this assertion and fake
are stale. The production diff behaved as planned by refusing fabricated success.

Required tester correction:

- preserve the A20 proof that a refused symbol read and a raw `read_memory_address` do not acquire
  coherent-lifecycle behavior;
- remove or update only the obsolete raw-write expectation so it no longer asserts the superseded
  contract;
- do not weaken A21's exact write/readback/lifecycle proof;
- add the changed adjacent test path to the regression tester manifest and include it in the
  regression command;
- reread the complete design charter at every required boundary.

This is a test-fixture correction under already-planned CL-002/CL-004, not a production-plan or
server-source change.

## MV-002 — stale H00 clean-candidate baseline

The full repository suite then returned
`1 failed, 356 passed, 4 skipped, 186 subtests passed`. The only failure was the H00 clean-candidate
transaction: the current clean server HEAD is
`db3fb8660c8186d351508050bf622a6aaf0b50fc`, but its exact approved-baseline assertion still named
the superseded pre-commit HEAD `4e139377...`.

This is a test-only baseline fixture made stale by the external/human commit already classified in
the suite ledger. The assertion was updated to the actual clean baseline
`db3fb8660c8186d351508050bf622a6aaf0b50fc`; no production behavior, candidate-file set, build
command, or verification gate changed.
