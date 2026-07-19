# Validation Probe Guidance Specification

## Problem

After `setup_overview` creates a run-scoped board-to-probe assignment, its validation route contains
both `board_id` and `probe_id`. However, `load_setup_tool(board_validate)` currently returns a
`next_call` containing only `board_id`, even though `board_validate` rejects calls without the
assigned `probe_id`. A conforming agent that copies the server-generated call therefore encounters a
predictable avoidable error immediately after fresh setup.

## Required behavior

1. In the production server, loading `board_validate` after `setup_overview` must return an exact
   executable `next_call` containing the assigned `board_id` and `probe_id`.
2. The probe value must come only from the current server run's assignment store. It must not be
   caller-authored, persisted, inferred from a profile, or treated as durable identity authority.
3. If there is no current assignment, loading validation must fail closed with a structured route
   back to `setup_overview`; it must not advertise an incomplete validation call.
4. Existing assignment checks at validation execution remain authoritative and unchanged.

## Non-goals

- Persisting probe assignments.
- Allowing validation without a current setup route.
- Changing validation, gate, permission, plan, or safety-map authority.
