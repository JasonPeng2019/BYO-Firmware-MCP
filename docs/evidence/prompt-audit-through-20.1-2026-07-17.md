# Prompt audit through 20.1 — 2026-07-17

## Result

Every prompt in `codex_prompts.md` from 1.1 through 20.1 has an implemented
software result or the exact fail-closed hardware disposition required by that
prompt. This audit did not execute Prompt 20.2, repeat an application flash, or
attempt recovery. Prompt 20.1 is now represented by a reproducible preparation
script and a machine-readable 122-AC/22-CC acceptance plan whose test node IDs and
assertions are checked by an automated test.

The current inventory positively matches `nucleo_l476rg` probe
`066FFF514988525067233337` and UART `COM12`. It does not contain the required
`nrf52833dk`. The other Nordic adapter was previously identified from live silicon
as an nRF52840 (`FICR.INFO.PART=0x00052840`) and is explicitly forbidden as a
substitute. Consequently, nRF52833 and destructive recovery criteria remain
blocked until the designated recoverable nRF52833 DK and a firmware backup are
available. That is the safe terminal result required by Prompts 8.3, 11.3, 14.3,
15.3, and 18.3 when required hardware is unavailable.

## Prompt-by-prompt disposition

| Prompt | Disposition | Inspected implementation/evidence |
| --- | --- | --- |
| 1.1 | Complete | kernel registry/run state/operations, handshake composition, MCP tests |
| 1.2 | Complete | hidden direct-call lock, restart defaults, list-changed, contract and quality checkpoint |
| 2.1 | Complete | board-keyed connection manager and board-threaded dispatch/event/session paths |
| 2.2 | Complete | routing, duplicate assignment, isolation, serialization/concurrency, restart and contract tests |
| 3.1 | Complete | FirmStore layout/atomic writes and profile v2 ownership/validation |
| 3.2 | Complete | interruption, malformed/mismatch, Unicode duplicate, exact-part and legacy fallback tests |
| 4.1 | Complete | attachment cache, immutable reports, migration, ignore rules and round trips |
| 4.2 | Complete | M3 integrated validation and rollback-safe migration evidence |
| 5.1 | Complete | declarative plan definitions and run/board/session-scoped plan engine |
| 5.2 | Complete | adversarial NULL, binding, budget, replacement, concurrency and relock tests |
| 6.1 | Complete | generic permission store, ordered dispatch enforcement and three generated pilot plans |
| 6.2 | Complete | M4 MCP visibility/permission/budget lifecycle and active contract |
| 7.1 | Complete | revised session/execution/register modules and backend protocol extensions |
| 7.2 | Complete | schema, register-class, permission, reset, routing and non-persistence checkpoint |
| 8.1 | Complete | revised memory/flash/serial/breakpoint/misc surface and legacy retirement |
| 8.2 | Complete | exact M5 visibility/schemas/reminders/bounds/contracts and integrated validation |
| 8.3 | Complete conditional result | Nucleo passed; required nRF52833 blocked by positive silicon mismatch; `m5-hardware-smoke-2026-07-17.json` |
| 9.1 | Complete | deterministic preflight and resumable setup/fix state machine |
| 9.2 | Complete | complete routing/choice/attempt/report/plain-prose checkpoint matrix |
| 10.1 | Complete | research, exact target resolution, staged pack validation and commit ordering |
| 10.2 | Complete | target/pack/research failure and round-trip matrix with artifact ownership checks |
| 11.1 | Complete | validation steps/statuses, setup MCP tools, A-20 redirects, shared Stage 0 internals |
| 11.2 | Complete | M6 state-machine, status, cache, reports, non-destructive and CLI validation |
| 11.3 | Complete conditional result | Nucleo setup/validation accepted; nRF52833 blocked; `m6-hardware-acceptance-2026-07-17.md` |
| 12.1 | Complete | typed safety ranges, linker extraction and strict double-verification schema |
| 12.2 | Complete | boundary/property/artifact/agreement/conflict/determinism checkpoint |
| 13.1 | Complete | canonical fingerprints, map build/persistence and scoped refresh routing |
| 13.2 | Complete | drift combinations, stable rebuild, unclear scope and failed-promotion preservation |
| 14.1 | Complete | live bound gate, validation stamp, actual action containment and freshness checks |
| 14.2 | Complete | M7 AC tests, crafted artifacts, zero-backend-call refusal and contract cleanup |
| 14.3 | Complete conditional result | Nucleo real map/flash/refresh/gate proof passed; nRF52833 blocked; `m7-hardware-acceptance-2026-07-17.md` |
| 15.1 | Complete | unchanged-plan recovery disclosure/approval, typed vendor operation and reports |
| 15.2 | Complete | adversarial recovery binding, single consumption, closed gate and legacy removal |
| 15.3 | Complete blocked result | no destructive call on wrong Nordic silicon; `m8-hardware-recovery-2026-07-17.json` |
| 16.1 | Complete | standard-dispatch sequential batch with full precheck and no authorization bypass |
| 16.2 | Complete | adversarial bounds/recursion/board/drift/budget/permission/failure/concurrency tests |
| 17.1 | Complete | managed operation/resource lifecycle, cancellation, timeout and idempotent cleanup |
| 17.2 | Complete | real stdio subprocess EOF/cancel/fake-flash/reuse/busy/final-state checkpoint |
| 18.1 | Complete | allowlisted finalizers, startup hygiene and owned cross-platform process groups |
| 18.2 | Complete | M9 lifecycle/finalizer/hygiene audit, contracts and full software checkpoint |
| 18.3 | Complete conditional result | real MCP cancellation and Nucleo release/reconnect passed; unavailable clients/nRF entries recorded in `m9-hardware-lifecycle-2026-07-17.json` |
| 19.1 | Complete | measured performance, security assertions, relay/Unicode tests and rewritten documentation |
| 19.2 | Complete | focused hardening validation, static audits, packaging/smokes and machine-readable Task 20 remainder |
| 20.1 | Complete | `prepare_m10_acceptance.py`, frozen acceptance plan, current inventory/fixture/version checks and bounded run order |

## Prompt 20.1 artifacts

- `scripts/prepare_m10_acceptance.py` regenerates the plan without changing product
  semantics or performing hardware mutations.
- `docs/evidence/m10-task20-acceptance-plan-2026-07-17.json` maps all 122 acceptance
  criteria and all 22 cross-cutting constraints to inspected test nodes, the actual
  assertion expressions, and self-contained hardware/manual procedures.
- `tests/test_m10_acceptance_plan.py` proves criterion completeness, test-node and
  assertion existence, fixture and pack hashes, current fail-closed inventory,
  explicit identity requirements, machine-readable result paths, and the bounded
  non-repeating destructive run order.
- The run order is software once; per-board setup/safety/validation/actions while
  reusing the preserved Nucleo M7 flash proof; at most one nRF52833 application flash
  and one approved recovery; non-destructive lifecycle cancellation reusing preserved
  M9 flash evidence; then simultaneous-board isolation.
- The clean, currently nonexistent result root is
  `C:\Users\Jason\Documents\Jason\FirmCLI\M10-Final-Acceptance\2026-07-17_run1`.

The exact non-mutating preparation command was:

```powershell
uv run --locked python scripts/prepare_m10_acceptance.py --output docs/evidence/m10-task20-acceptance-plan-2026-07-17.json --result-root 'C:\Users\Jason\Documents\Jason\FirmCLI\M10-Final-Acceptance\2026-07-17_run1' --nucleo-probe-id 066FFF514988525067233337 --nucleo-serial-id COM12 --nucleo-artifact-source 'C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run1'
```

Prompt 20.2 and Prompt 20.3 are deliberately outside this audit and remain
unexecuted.
