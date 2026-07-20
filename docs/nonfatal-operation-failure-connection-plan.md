# Plan: preserve connections after nonfatal operation errors

Specification: `docs/nonfatal-operation-failure-connection-spec.md`

1. Classify pyOCD `CoreRegisterAccessError` as a recoverable target-state error,
   not a target-connection error.
2. Narrow managed-board cleanup to cancellation/timeout or a typed
   target-connection failure; keep reset-line release and explicit disconnect
   unchanged.
3. Add focused tests for ordinary failure, recoverable target state,
   connection loss, and cancellation.
4. Record the acceptance-discovered product gap.
5. Run the focused tests, Ruff, Pyright, then the complete suite and a diff
   audit before resuming the blocked hardware workflow.
