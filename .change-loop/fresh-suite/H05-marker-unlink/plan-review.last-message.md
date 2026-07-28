Plan SHA-256: `2b1107796f7a5437304e4a440279b589fbdd2f7c8933ac599d08aa5f5a82186a`

Verdict: `READY`

1. Required nested-path test: invoke the real `close → call("close") → _invalidate` path, with confirmed termination and one `ProcessMarkerStore.remove` `OSError`. Assert the original typed `TargetConnectionError` escapes, retains the `OSError` as direct cause, retains the marker, and makes exactly one initial removal/termination attempt.

2. Required retry test: after restoring removal, second `close()` must remove only the retained marker—no extra provider request and no extra termination—and clear `_marker`.

3. Required diagnostic distinction: a provider/protocol close error that does not invalidate the client must remain non-fatal if final termination and marker removal succeed. Separately, an invalidation that already removed the marker must also remain a successful outer close. These controls distinguish the intended failure from ordinary graceful-close diagnostics and complete invalidation.

4. Required fail-closed distinction: unconfirmed termination must still raise and retain the marker without attempting marker removal. Preserve existing expired/racing-deadline cleanup behavior.

No charter conflict, unnecessary complexity, scope expansion, missing preservation contract, or ambiguous state transition found. The change is correctly constrained to `_WorkerClient.close`, reuses existing ownership/marker contracts, and adds no retry loop or environment-specific behavior.

The current production diff is the separate accepted `tools/misc.py` cancellation repair; it is not part of CL-001.