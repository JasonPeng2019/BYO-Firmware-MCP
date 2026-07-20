# Nonfatal operation failures must preserve the debug connection

## Problem

A live acceptance run asked for `PC` while the core was running. pyOCD correctly
rejected the register read, but the generic operation cleanup treated every
started handler exception as an unsafe session and closed the otherwise healthy
debug connection. The next ordinary action therefore failed with "not
connected" and forced a reconnect and revalidation.

This makes harmless, recoverable mistakes destructive to workflow state and
adds a failure mode to every board and backend.

## Required behavior

1. An ordinary handler error must report the real error and preserve the current
   debug connection, gate stamp, and board assignment.
2. Cancellation and timeout must continue to close the connection because an
   interrupted in-flight backend operation has an uncertain completion state.
3. A typed target-connection failure must also clear the stale connection and
   validation stamp, because reusing a lost probe/transport session cannot work.
4. A target-state refusal such as reading PC while the core is running is not a
   connection failure and must preserve the session.
5. Explicit `disconnect` remains the way to close a healthy connection.
6. The server must use typed backend errors rather than message matching to
   distinguish recoverable target state from a lost target connection.
7. The rule is toolchain-, target-, probe-, and operating-system-independent.

## Acceptance

- A failed started read operation leaves its connection and validation state
  intact.
- A cancelled/timed-out operation still clears and closes the connection.
- A typed target-connection failure clears and closes the connection.
- A typed core-register state refusal preserves the connection.
- Existing successful-operation cleanup behavior remains unchanged.
