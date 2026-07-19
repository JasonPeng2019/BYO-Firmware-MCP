# Universal onboarding: selected-board routing among multiple probes

## Problem

A fresh project may intentionally configure one board while other debug probes remain attached.
`setup_overview` currently requires one familiar board name for every visible probe. That makes an
unrelated attached probe a hard blocker and prevents the intended part-number-and-datasheet setup
journey.

## Required behavior

1. A caller may name only the logical boards it intends to use in the current project/run.
2. When more than one debug connection is visible, every named board must still be mapped explicitly
   to one server-returned connection choice. The server must never guess the mapping.
3. Each selected connection may map to at most one named board and each named board to exactly one
   selected connection. Unselected visible connections remain unassigned and confer no authority.
4. More named boards than visible connections is a clarification error. Fewer names than visible
   connections is valid once the named boards have explicit assignments.
5. With exactly one visible connection and one named board, the existing unambiguous automatic
   mapping remains valid.
6. Startup guidance asks for the board or boards the user wants to work with, not an exhaustive
   inventory of every attached probe.
7. Existing `no board`, profile reuse, repair, refresh, validation, and run-scoped assignment rules
   remain unchanged.

## Acceptance

- One named board plus two visible probes returns friendly assignment choices, then routes setup when
  one exact choice is supplied.
- The unrelated probe is absent from run assignments.
- Invalid, duplicate, stale, missing, or over-subscribed assignments fail before hardware access.
- Focused setup/handshake/contract tests and the full suite pass.

