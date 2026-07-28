# A21 write-memory plan adversarial review

- Reviewed plan SHA-256: `d2a1d29a7b8932133fa959bf35733906086cee3121240aa0594e15c0615fc626`
- Plan SHA-256: d2a1d29a7b8932133fa959bf35733906086cee3121240aa0594e15c0615fc626

Plan SHA-256: d2a1d29a7b8932133fa959bf35733906086cee3121240aa0594e15c0615fc626
- Plan SHA-256: d2a1d29a7b8932133fa959bf35733906086cee3121240aa0594e15c0615fc626
- Reviewer: `A21-write-memory-plan-reviewer-001`
- Persistent reviewer thread: `019faa22-939a-7a92-9704-f614a62f365d`
- Model: `gpt-5.6-terra`; reasoning: `medium`; service tier: `priority/Fast`

## Overall assessment

The plan is a sound, scope-contained execution contract for the verified fabricated-success
defect. It places one provider-neutral lifecycle/readback primitive next to the established
coherent-read helper, preserves all pre-I/O refusals and public call shape, and makes success
conditional on immediate same-address, same-width exact readback plus restoration of only state
the server interrupted. No blocking plan defect was found.

## Execution risks and objective test targets

1. **Restoration must be conditional, complete, and ordered.** A helper that resumes an
   initially halted target, resumes after a failed halt, or returns before its inserted-halt
   restoration would alter execution state or fabricate completion. Test exact recording-fake
   order/count for `HALTED`, `RUNNING`, `SLEEPING`, and an unfamiliar non-halted state; cover
   state and halt failures with no write/read/resume, and every post-halt write/readback/mismatch
   failure with exactly one resume attempt.
2. **Primary failures and cancellation-class failures can be lost while reporting restoration.**
   Test write, readback, and mismatch failures with successful restoration preserve their original
   exception identity/trace, including `KeyboardInterrupt` or another `BaseException`. Test dual
   primary/restoration failure includes both type/message facts and chains the primary; test a
   restoration-only failure after an exact readback is non-success.
3. **Verification must be exact rather than a successful backend-call proxy.** Test 8-, 16-, and
   32-bit writes with recording fakes to prove the same address and width are read back, an exact
   match alone reaches the unchanged success text and one success event, and each width's mismatch
   reports width-formatted expected and observed values with no success event.
4. **Both public write forms must reach the one helper only after existing policy checks.** Test
   symbol-backed and justified raw mapped-RAM success routes, plus invalid width/value, symbol,
   artifact, raw-fallback reason, object-size/alignment, containment, and gate/plan refusal
   branches. Assert those refusals make no lifecycle, backend write, or verification-read call;
   inspect production `MemoryToolServices` wiring and MCP/plan help for the promised lifecycle,
   honest-failure, later-overwrite, and recovery contract.

## Nonblocking implementation clarifications

- Treat all states other than case-insensitive `HALTED` as states the server must interrupt only
  temporarily; do not introduce a state allowlist or provider-specific branch.
- Keep error composition local to the new write helper and preserve the adjacent A20 read helper's
  published behavior. The only success event must stay after helper completion, so a verified
  mutation followed by failed restoration is never reported as success.
- Documentation may state immediate coherent proof, not durable application state after a resumed
  firmware loop; recovery wording must not add a permission prompt or pre-halt requirement.

## Review integrity

The reviewer verified the required plan hash and reread the complete authoritative design charter
immediately before this record. It did not edit the plan, production source, or tests. The
controller status and complete JSONL are retained beside this file.
