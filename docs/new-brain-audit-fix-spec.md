# New Brain adversarial audit fix specification

Source: `New_Brain_Spec.md`. Scope is major day-to-day correctness, portability, and agent usability.

## Accepted behavior

1. **Flash cancellation boundary** (`kernel/operations.py`, `tools/flash.py`): flash remains interruptible through queuing, plan/digest checks, containment, and handle lookup; it becomes non-interruptible only immediately before backend flash mutation. A cancelled queued flash never reaches the backend.
2. **UART authorization parity** (`server.py`, plan definitions): `serial_exchange` requires the same live identity/map gate as `write_serial`. Eligible serial `on_exit` finalizers are exact nullable plan-bound action parameters; pre-start refusals never run them.
3. **Recoverable incomplete setup** (`server.py`, setup workflow/surface): a parseable existing profile with incomplete setup can receive a fresh authorized `mode=repair` setup plan and deterministically rerun current phases. Malformed profiles remain fail-closed because identity cannot be safely reconstructed.
4. **Optional UART setup** (`preflight.py`, setup plan/actions/overview): setup explicitly records whether UART is used. Serial ID and baud are required only when true; no-UART profiles use the reviewed device default internally and skip UART inventory/cache requirements.
5. **One-to-one startup routing** (`server.py`, setup tools): multiple names and connections produce a friendly assignment request, not executable ambiguous routes. Agent-supplied server-ID mappings are strict, one-to-one, run-scoped, and never shown to the user.
6. **Symbol object bounds** (`tools/memory.py`): symbol access refuses a width larger than a known positive symbol size; zero-size linker symbols are not silently treated as 32-bit variables.
7. **Batch board ownership** (`kernel/operations.py`, registry/batch): one batch reserves its board across all children; nested child dispatch reuses that ownership while unrelated same-board operations wait. Child plans, budgets, safety, and cleanup remain unchanged.
8. **Local-only build fallback** (`server.py`, `zephyr_build.py`): no board-specific Zephyr target is inferred from MCU identity. The helper performs no Python/workspace/SDK provisioning by default; provisioning requires one explicit opt-in.
9. **Custom-probe support** (`server.py`): a reviewed MCU/device can use any recognized pyOCD probe provider that successfully connects. Unknown providers remain blocked; the selected provider is persisted.
10. **Portable ELF evidence** (`safety/linker.py`): stable-map partitions remain authority. Linker partition/RAM/vector symbols are optional corroboration, not flash prerequisites. PT_LOAD/entry data drives containment; vector location falls back to the lowest load address and must have actual ELF bytes.
11. **Safe flash timeout completion** (`kernel/operations.py`): a timeout after backend flash mutation begins waits within the bounded flash-operation ceiling for transaction completion and cleanup, just like cancellation.
12. **Legacy identity preservation** (`server.py`, profile repository): a matching legacy profile is established identity. Repair uses constrained legacy migration after matching display/target/live silicon checks; it never shadows the legacy profile through unrestricted core creation.
13. **Validated Cortex-M vectors** (`safety/linker.py`): vector bytes must encode an aligned build-RAM initial stack pointer and Thumb reset handler in executable ELF content; ELF entry must likewise be executable Thumb metadata.
14. **Live fresh-workspace contract** (`scripts/run_fresh_workspace_e2e.py`): the runner first copies the exact `setup_overview` route, includes optional-UART schema fields, and validates with the assigned probe.

## Rejected or narrowed findings

- **custom-partition-authority-leak — rejected:** the normative design explicitly assigns deployment policy to server-owned reviewed board/device evidence and says custom PCBs may reuse reviewed MCU/device support. The resolver does not accept caller policy or ranges. Removing every partition would contradict the specified writable custom-device route; changing the reviewed policy itself requires a new authority decision, not an audit patch.
- **unbound-uart-finalizer — narrowed:** finalizers are currently unbound and must be fixed. The claim that they run after `before_execution` refusal is false: `ManagedOperation.run_finalizer` requires `handler_started_at`, which is set only after preconditions pass.
- **malformed-profile repair — narrowed:** parseable incomplete profiles get repair. A malformed profile cannot prove same identity, so automatic overwrite would violate the higher-priority no-rewrite rule.

## Acceptance

Focused regression tests prove every accepted behavior without hardware. Existing authority/containment tests remain green, exact public contracts are regenerated, then full pytest, Ruff, Pyright, wheel/import, and bounded stdio checks pass.
