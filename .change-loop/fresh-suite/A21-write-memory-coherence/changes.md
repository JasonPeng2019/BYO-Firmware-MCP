# A21 public `write_memory` lifecycle-coherence repair request

## Authorized scope

Authorized local firmware-server repair. The production target is only this local
`BYO-Firmware-MCP` repository. Hardware evidence comes from the user-owned STM-A board assigned to
the local A21 experiment. No remote or third-party target is in scope.

## Current production identity

- Git HEAD: `db3fb8660c8186d351508050bf622a6aaf0b50fc`
- Working tree at request creation: clean
- Current 81-file production `src/` tree SHA-256:
  `1c27e5ec597ed5fe4fe7c7787f69b6efd56be924fb9ac4119568a8095f9e846a`
- The A21 evidence was captured from recorded HEAD `4e1393775167166146c6ee1a0ce310c9747ca3bf`
  plus tracked diff `a520dfee72bb4aac5a0b9f53f1847bb834a73729`. An external/human commit
  subsequently folded that work into the current clean HEAD without changing the production
  `src/` bytes.

## Independently verified defect

The public planned `write_memory` action accepts a symbol-backed mapped-RAM write while the target
is running or sleeping and returns a successful completion message such as:

`Wrote 0xA21C0DE2 to mapped RAM at g_boot_id.`

On the same fresh validated MCP lifetime, immediate coherent `read_memory_symbol` returned
`0x00000000`, and a subsequent explicit halt followed by another coherent read still returned
`0x00000000`. The requested value was not observably present even though the action reported
completion.

The control case on the same board, artifact, resolved address, width, server implementation, and
public plan route proves the provider can perform the mutation while the target is explicitly
HALTED:

- original `g_boot_id`: `0xA21065D3` at `0x200000FC`;
- test write while HALTED: `0xA210C076`;
- immediate raw read: `76 C0 10 A2`;
- immediate symbol read: `0xA210C076`;
- restored original while still HALTED;
- immediate raw read: `D3 65 10 A2`;
- immediate symbol read: `0xA21065D3`;
- resumed to the original `SLEEPING` execution state;
- final coherent read remained `0xA21065D3`.

Primary evidence:

- `../fresh-experiments/A21_20260726-052146/.agent-workspace/evidence/manager-validation/write-memory-halted-running-control-v2/report.json`
- `../fresh-experiments/A21_20260726-052146/.agent-workspace/evidence/manager-validation/write-memory-halted-restore-classification-20260728T185828Z/report.json`
- `../fresh-experiments/A21_20260726-052146/.agent-workspace/PARALLEL_CHECKPOINT.md`

## Expected production behavior

For both symbol-backed and explicitly justified raw mapped-RAM scalar writes:

1. Complete all existing parse, artifact, symbol, width, address, mapped-region, gate, plan,
   validation, and safety checks before acquiring target lifecycle behavior.
2. Query the target execution state.
3. If it is already HALTED, leave it HALTED. Otherwise, insert a halt for the bounded mutation.
4. Perform the exact backend scalar write.
5. Before returning success, read the same address and width while still halted and require an
   exact value match.
6. If the server inserted the halt, resume execution before returning or raising. Never resume a
   target that was already halted.
7. Return the existing public success shape only after exact readback and any required execution
   restoration succeed.
8. On state, halt, write, verification-read, mismatch, or restoration failure, do not record or
   return success. Preserve the primary failure; if restoration also fails, report both facts and
   chain the primary failure.

Success guarantees an immediate coherent verified mutation before execution is restored. It does
not claim that subsequently executing firmware cannot deliberately overwrite its own variable.

## Public guidance

Update the public `write_memory` action description and its plan guidance so an agent needs no
outside knowledge:

- state that a running/sleeping target is briefly halted for one coherent write plus exact
  readback and then restored;
- state that an already halted target remains halted;
- state what success proves and what later firmware execution may still change;
- state that target-access, write, readback-mismatch, or restoration failures are reported
  honestly; and
- provide the narrow recovery: inspect/reconnect the target and retry with the current ELF or
  deliberate HALTED state.

Do not add another permission prompt, require the caller to pre-halt manually, introduce a new
public parameter, or specialize behavior for STM32, pyOCD, A21, `g_boot_id`, an address, a board,
an OS, or a firmware image.

## Required automated proof

Add focused neutral tests that independently prove:

- already-HALTED write order is state → write → exact readback, with no halt/resume;
- RUNNING, SLEEPING, and another non-HALTED provider state use
  state → halt → write → exact readback → resume;
- symbol and raw-address forms both use the same coherent path;
- exact match returns the existing success text and records one success event;
- readback mismatch is an explicit non-success containing expected and observed values;
- state/halt/write/readback failures never fabricate success and restore execution when needed;
- `BaseException`/cancellation-class failures still attempt restoration and re-raise the primary
  when restoration succeeds;
- a simultaneous primary and restoration failure reports both and chains the primary;
- restoration failure after a verified write is still a non-success;
- pre-I/O validation/refusal branches do not halt, write, read, or resume;
- already-HALTED failures never resume;
- production wiring supplies lifecycle operations; and
- public tool and plan help publish the exact contract and recovery.

Run the focused tests, adjacent A20 coherent-read tests, existing memory/plan/trust-model tests,
full repository tests where practical, Ruff, Pyright, and `git diff --check`. Do not commit, push,
deploy, flash, or alter fresh-experiment firmware/evidence.
