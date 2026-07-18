# New Brain audit closure validation

Status: closed after repeated autonomous static-client hardware acceptance.

The implementation is validated against the findings in
`docs/new-brain-gap-audit.md` and the behavior requirements in
`New_Brain_Spec.md`.

| Finding | Implementation | Focused evidence |
| --- | --- | --- |
| NB-G01 | GNU/Zephyr evaluated map parsing | `tests/test_safety_linker.py` plus real generated Zephyr artifact parse |
| NB-G02 | no implicit reset after successful ordinary action | managed resource binding and UART state-preservation regression coverage |
| NB-G03 | one-open exchange, explicit reopen/clear | `tests/test_uart_capture.py`, serial tool/plan contract tests |
| NB-G04 | declarative plan index descriptions | plan-definition and prompt-content tests |
| NB-G05 | actionable handshake | handshake/MCP contract tests |
| NB-G06 | familiar-name setup overview | setup tool and server board-config tests |
| NB-G07 | public strict setup continuation | setup workflow/tool/hardware-inventory tests |
| NB-G08 | project pack manifest/runtime discovery | pack provisioning and setup pack tests |
| NB-G09 | linker-owned application region and complete base map | catalog, safety enforcement, map and refresh tests |
| NB-G10 | stateful UART guidance | plan prompt and serial schema tests |
| NB-G11 | exact static-client accepted-plan transport | `tests/test_static_client_plan_fallback.py`, batch/plan/recovery tests |

## Second-pass result

The second pass re-read every finding after implementation and found four
integration-evidence defects: stale extraction hashes, stale active MCP
contract hashes, shifted traceability line metadata, and handshake wording that
did not preserve one tested routing phrase. These were corrected and the full
quality run was repeated. No second product-behavior gap remained within the
reviewed-board boundary.

The UART/readiness assessment produced one additional usability improvement:
`get_setup_status` now exposes `uart_attachment_ready` and
`ready_for_uart_work`. General code readiness remains independent, while an
acceptance workflow that relies on terminal evidence can require the stronger
UART barrier before writing or flashing code.

## Final software evidence

- Focused setup/research/pack/linker/UART/MCP surface run: 110 passed.
- Complete suite after final readiness change: 777 passed, 63 expected legacy
  `pack_name` deprecation warnings.
- `uv run ruff check .`: passed.
- `uv run pyright`: 0 errors, 0 warnings.
- `uv build`: source distribution and wheel built successfully.
- Package import: `import pyocd_debug_mcp.server` passed.
- Bounded stdio EOF boot/shutdown: exit 0, zero stdout/stderr bytes.
- Active product contract version 20 and extraction manifest checks passed.

The actual delegated Zephyr build at
`../Jason-MCP-v2-nrf-agent/build/Jason-MCP-v2-nrf-agent/zephyr/` was parsed
again with its ELF, GNU linker map and HEX together. Extracted evidence was:
flash `[0x00000000, 0x00010000)`, RAM `[0x20000000, 0x200032c0)`, entry
`0x38a1`, vector table `0x0`, four loadable segments and two HEX ranges.

Existing real-hardware UART evidence remains in
`docs/fresh-workspace-validation.md` and its linked sibling-repository
transcripts. It proves all ON/status/OFF/status checks through one UART handle.
No new destructive operation was needed or performed for this audit.

## Third-pass static-client acceptance

The prior closure was intentionally reopened when a fresh agent accepted
`board_setup-plan` but could not acquire a new callable binding for the hidden
`board_setup` action. The remediation added exact machine-readable one-child
fallbacks without weakening dynamic visibility or child dispatch.

A second fresh agent then started from a repository containing only `.git` and
received no fallback coaching. It natively asked for the familiar name, board,
MCU, datasheet, friendly probe/UART choices, baud, and one-time setup approval.
It completed setup and every readiness barrier before writing code, authored a
Zephyr multithreaded LED console application, built it, refreshed safety,
revalidated, flashed only the application partition, and matched ON/status/
OFF/status over one COM11 handle. Evidence is recorded in
`docs/evidence/autonomous-static-client-acceptance-2026-07-17.md` and its linked
machine-readable sibling-repository artifact.

A stricter repeat subsequently required UART output emitted inside the actual
LED worker thread plus a 1200 ms quiet-after-OFF proof. It passed without any
additional server change; see
`docs/evidence/autonomous-worker-thread-log-acceptance-2026-07-17.md`.

## Remaining boundary

An unreviewed arbitrary board is not automatically promoted to writable safety
support from agent-provided address ranges. Target and pack research now has a
complete public continuation, but authoritative safety evidence must still be
reviewed and independently reconciled. This is the New Brain fail-closed
source-authority rule, not an omitted permission or setup shortcut.

## Current-tree adversarial acceptance

A final fresh no-YAML/no-source repeat on the current strict authority schema
completed without a product retry. It proved setup-first questioning, exact
PowerShell build guidance, schema-v2 region and erase-geometry reconciliation,
guarded application flashing, and worker-thread ON/OFF behavior over one UART
handle. The strict schema-v3 record causally links 19 artifacts, 21 MCP
operations, and 5 immutable reports. See
`docs/evidence/autonomous-current-tree-acceptance-2026-07-17.md`.
