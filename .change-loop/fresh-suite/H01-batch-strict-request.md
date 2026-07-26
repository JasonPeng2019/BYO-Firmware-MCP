# Authorized local firmware validation — H01 strict MCP boundary repair

## Verified production defect

This is a host-only, no-board repair request for the local `BYO-Firmware-MCP` repository. No
remote or third-party target is in scope. Do not operate hardware.

The H01 raw stdio test submitted request `190` to the attested installed server:

```json
{
  "jsonrpc": "2.0",
  "id": 190,
  "method": "tools/call",
  "params": {
    "name": "action_batch",
    "arguments": {
      "actions": [
        {
          "arguments": {
            "baudrate": 115200,
            "board_id": "h01_absent_board",
            "expected_text": "H01_C",
            "on_exit": null,
            "port": null,
            "read_seconds": 0.15,
            "reset_on_open": false
          },
          "tool_name": "read_serial"
        }
      ],
      "board_id": "h01_absent_board",
      "extra_top_level": true
    }
  }
}
```

The server silently discarded `extra_top_level`, dispatched the child, and returned
`isError: false` with a `batch_failed` body whose child failure was the expected absent-board
connection refusal. The outer malformed request therefore executed past its schema boundary.

Expected behavior: an unknown argument at any registered MCP tool boundary is rejected as a tool
error before the tool handler executes. In particular:

1. an extra `action_batch` top-level field is rejected before any child dispatch;
2. an extra field on the `BatchChild` envelope is rejected before any child dispatch;
3. an extra field inside `BatchChild.arguments` is rejected by the selected child's normal
   registered MCP argument schema before that child's handler, plan consumption, or provider path;
4. direct and batch-dispatched calls use the same strict child argument schema;
5. the exact unchanged server-returned one-child fallback still reaches normal child dispatch,
   preserves the structured `batch_failed` details on child refusal, and exposes that refusal as
   an MCP tool error rather than an `isError: false` success result.

The adjacent direct controls are independently verified from the registered runtime metadata:
`action_batch`, `read_serial`, and `initialization_handshake` currently have no
`extra="forbid"` argument-model policy and their published schemas omit
`additionalProperties: false`. `BatchChild` itself already declares `extra="forbid"`. The
registered FastMCP boundary, not the standalone handler/model, is therefore the required repair
surface.

## Evidence and identity

- Run: `../fresh-experiments/H01_20260724-044242`
- Exact request: `../fresh-experiments/H01_20260724-044242/.agent-workspace/evidence/requests.jsonl`
  request `190`, label `batch-top-extra`
- Exact response: `../fresh-experiments/H01_20260724-044242/.agent-workspace/evidence/responses.jsonl`
  response `190`
- Terminal failure:
  `../fresh-experiments/H01_20260724-044242/.agent-workspace/evidence/terminal_failure.json`
  SHA-256 `afdb55e01c86c53e1f3b9009fbc510567118d41866613ee834b625bfecc6cb90`
- Preserved terminal bundle:
  `../fresh-experiments/H01_20260724-044242/.agent-workspace/history_pre_H01_batch_strict_repair_20260725T030602Z`
- Preservation manifest SHA-256:
  `9a009511bb7236fdd045228e43e50cd03102e5b60b849eb7742906c07e25bd9e`
- Baseline commit: `6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876`
- Accepted pre-repair production diff SHA-256:
  `1bfb1569f7a6e174cb094c80255b587d7f4d961648b5802f90d95ddca1bcf65c`
- Accepted composite identity:
  `6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876+1bfb1569f7a6e174cb094c80255b587d7f4d961648b5802f90d95ddca1bcf65c`

No board, serial port, probe, provider, firmware, setup state, or hardware permission was involved.
The child stopped at the synthetic board's absent active-connection check.

## Required repair properties

- Enforce strict unknown-argument rejection at the one registered FastMCP boundary that owns tool
  input parsing, rather than adding board-, action-, or H01-specific key checks.
- Publish schemas that truthfully include `additionalProperties: false` at each statically
  expressible strict object level.
- Preserve the existing special generated-plan parsing and permission validators, live registry
  locks, plans, gates, budgets, permissions, finalizers, timeouts, board serialization, list
  notifications, and safe-exit wrapping.
- Validate malformed input before handler execution. For a locked hidden tool, preserve the
  established registry-lock precedence; once unlocked, reject its unknown argument before its
  handler and without consuming the active plan.
- Preserve `BatchChild`'s existing strict envelope and validate a child's dynamic `arguments`
  through that named tool's ordinary registered argument model, not a duplicate schema.
- Preserve successful batches and their ordering. On a child refusal, stop at that child, retain
  the existing structured `batch_failed` payload (`index`, `tool_name`, error type/message,
  completed prefix), and make the MCP result an error.
- Include real registered FastMCP boundary tests. Direct handler-only or Pydantic-model-only tests
  are insufficient.
- Automated tests must prove:
  - outer batch extra: error, injected key named, zero child dispatch;
  - child envelope extra: error, injected key named, zero child dispatch;
  - child `arguments` extra: error before child handler/plan consumption;
  - direct extra on an unlocked child tool: the same schema rejection and no handler execution;
  - registry lock still wins for a locked hidden child call containing an extra field;
  - `initialization_handshake` rejects an extra field and its zero-argument schema is strict;
  - relevant `tools/list` schemas advertise `additionalProperties: false`, including the
    `BatchChild` definition;
  - exact valid fallback dispatches once, returns the unchanged absent-board child refusal in the
    structured batch failure, relocks as before, and is an MCP error;
  - a valid successful batch remains successful and ordered;
  - plan all-NULL, literal-string preservation, NULL semantics, budgets, dynamic visibility,
    notifications, and existing full repository behavior remain green.

## Exclusions

- Do not edit the fresh H01 run, harness, sealed specification, amendments, evidence, firmware, or
  fixture.
- Do not add arbitrary caps, allowlists, board/OS constants, or adversarial-input hardening.
- Do not change plan budget behavior, generated-plan text/NULL handling, hardware authorization,
  plan/permission/gate semantics, or unrelated tool behavior except for the general correction
  from silently ignoring unknown MCP arguments to rejecting them.
- Do not commit, push, deploy, flash, erase, connect, or operate hardware.

