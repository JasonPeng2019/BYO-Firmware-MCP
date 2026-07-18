# Static-client plan execution gap specification

## Acceptance failure

A fresh autonomous client completed the setup handshake, supplied the requested familiar
board name, exact `nrf52840dk` board type, exact `nRF52840` MCU, authoritative local
datasheet, friendly probe/UART selections, baud rate, and one-time setup approval. The
server accepted `board_setup-plan` and emitted `notifications/tools/list_changed`, but the
client's callable function bindings remained fixed at their startup snapshot. Consequently
the newly visible `board_setup` action could not be invoked and the end-to-end workflow
stopped before setup, source generation, flash, and UART validation.

Dynamic visibility remains the preferred MCP behavior. The product also needs a safe,
exact execution route for clients which observe a static tool binding for a whole turn.

## Required behavior

1. Every accepted plan returns one machine-readable `plan_accepted` JSON document. It
   contains the plan id, preferred direct action call, and an exact one-child
   `action_batch` fallback built from the accepted plan snapshot.
2. The child arguments are exactly the accepted envelope `board_id` plus the canonical
   `action_parameters`. They never contain permission values, grants, authorization state,
   or caller-supplied wrapper data.
3. The fallback is only transport compatibility. Its child traverses the same registry
   lock, exact-plan binding, permission, validation, gate, fingerprint, timeout, board-lock,
   budget, event, and cleanup path as a direct call.
4. NULL, malformed, rejected, disclosure-only, stale, replaced, or exhausted plans never
   return a usable execution fallback.
5. A setup plan separately returns a paired `board_fix_setup` fallback. It may be used only
   after `board_setup` produces the eligible repair/continuation state. Setup and repair
   must never be combined into one optimistic batch.
6. `target_unlock` receives a fallback only after the unchanged disclosure has fresh
   one-time approval. Disclosure and pre-approval responses contain none.
7. Guidance tells dynamic clients to use the newly exposed direct action. A static client
   may submit only the exact server-returned one-child fallback unchanged; it must never
   invent a hidden child name or arguments.
8. Every stored YAML name routes to `board_validate` first. Validation determines whether
   the remedy is attachment correction, retry, safety setup/refresh, profile repair, or
   first-time setup. A stored profile is never rewritten merely because validation failed.
9. Every non-setup NULL plan guide includes setup-first routing and forbids planning the
   requested hardware action until the board is ready.
10. Startup/setup guidance consistently allows the universal all-NULL setup guide before
    loading. `load_setup_tool(board_setup-plan)` is required only before the populated setup
    plan.

## Validation requirements

- Snapshot the stdio tool list once, ignore `tools/list_changed`, accept a plan, and execute
  its exact fallback successfully.
- Cover no-permission, one-time, full-session, setup primary/paired repair, and approved
  recovery plans with fake typed backends.
- Mutate fallback board, action, arguments, wrapper fields, and stale plan state and prove
  refusal occurs before handler execution or authority consumption.
- Prove direct and fallback calls have identical results, events, budgets, permissions,
  safety enforcement, failure behavior, and cleanup.
- Prove all matching YAML profiles route to validation first.
- Repeat the fresh-workspace autonomous nRF52840 DK setup, Zephyr multithreaded console
  blinky creation, application flash, and one-open UART ON/status/OFF/status validation.

