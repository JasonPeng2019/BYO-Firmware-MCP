# H04 production repair request: durable attachment-cache surface

Authorized local firmware validation. The defect was reproduced against the local
`pyocd-debug-mcp` runtime and the single user-owned NUCLEO-L476RG assigned by H04. No remote or
third-party target is involved.

## Observed

In the isolated run
`../fresh-experiments/H04_20260725-100800/cases/req011_uart_positive_root`, setup used
`requires_uart=true`, a stable server-returned probe route, a stable server-returned UART route,
and baud 115200. Setup completed and two fresh validation/status replays returned `setup_ready`
with `resolved_uart`, but:

- no distinct attachment-cache record existed under the case's `.firm` root; and
- no public response exported a cache record path or labeled it as non-authoritative.

The setup report recorded `cache_outcome.reason="no_record"` even though both selected identities
were stable. This prevents the catalog's isolated cache tamper/replay control from selecting a
public target without guessing an internal filename.

Primary evidence:

- `../fresh-experiments/H04_20260725-100800/.agent-workspace/evidence/req011_uart_positive/setup_state.json`
- `../fresh-experiments/H04_20260725-100800/.agent-workspace/evidence/req011_uart_positive/validate_status_state.json`
- `../fresh-experiments/H04_20260725-100800/.agent-workspace/evidence/req011_uart_positive/validate_status_state_repeat.json`
- `../fresh-experiments/H04_20260725-100800/.agent-workspace/evidence/req011_uart_positive/firm_file_inventory.json`
- `../fresh-experiments/H04_20260725-100800/.agent-workspace/evidence/req011_uart_positive/cache_surface_search.txt`

## Expected

For every setup whose selected probe and UART endpoint both have stable identities:

1. persist the existing non-authoritative attachment hint record atomically, including the normal
   built-in/provably-mapped UART case rather than only the external-adapter confirmation case;
2. expose through the public setup-status response a project-relative cache record path plus an
   explicit non-authoritative classification and honest present/missing/corrupt/resolution state;
3. never let the cache create or restore plan, permission, gate, target, profile, pack, safety, or
   live-identity authority;
4. preserve direct stable-identity UART resolution, setup readiness, and every existing external
   adapter confirmation rule;
5. on missing cache, continue using independently verified direct mapping where available and
   report a non-authoritative cache miss honestly;
6. on corrupt cache, do not silently treat it as current, do not open a gate from it, and return an
   actionable diagnostic that removal/re-setup may rebuild only the hint record.

The behavior must be generic across hardware, operating systems, ports, and part numbers. Do not
hardcode this H04 board, COM12, ST-Link, STM32, Windows, or any test-only path.

## Exclusions

- Do not alter firmware, fixtures, H04 specifications, experiment evidence, or the installed
  runtime directly.
- Do not weaken profile, pack, safety-map, validation, plan, permission, flash, or live-identity
  gates.
- Do not persist run-scoped authority.
- Do not redesign unrelated setup flows or add a new plugin/provider abstraction.
- Do not commit, push, deploy, or flash hardware.
