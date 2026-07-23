# Provider worker protocol

`byo-firmware-mcp` version 0.2.0 uses one provider-neutral, newline-delimited
JSON worker protocol. The built-in pyOCD worker and a trusted external provider
recipe use this same boundary; an external worker is not a server plugin.

## Launch and readiness

The server starts the recipe's `worker_argv` directly, without a shell. Standard
output is reserved for protocol frames and diagnostics belong on standard error.
The first frame must be exactly:

```json
{"version": 4, "ready": true}
```

The parent owns the child process, cancellation marker, and board-local session.
It can terminate an unpromoted worker on cancellation and closes a detached
session only when that board disconnects or a typed failure invalidates it.

## Requests and replies

Each request is one JSON object followed by `\n`:

```json
{"version": 4, "request_id": 7, "operation": "get_state", "arguments": {}}
```

Replies must preserve the version and request ID, and return either the typed
operation result or a typed provider error recognized by the parent. The parent
strictly validates session metadata, live identity, physical regions, flash
readback records, recovery descriptors, and memory values before presenting
them to an MCP client. An invalid frame is a connection failure, never success.
When a connection operation's reset release or session close cannot be
confirmed, the error includes a strict `cleanup_diagnostics` list with the
named stage, `unconfirmed` status, provider error type/message, and recovery
instruction. The original provider failure remains in `primary`; disconnect,
power-cycle if needed, reconnect, and revalidate before further operations.

## Evidence expectations

An `open` or `connect_under_reset` result supplies provider route, runtime token,
and live identity facts. `physical_memory_regions` supplies current session-bound
access facts. `flash` supplies exact parsed-byte readback evidence. `recover`
supplies the selected mechanism, command acceptance, verification state, and
observed session postcondition. The server binds these facts to its guard, plan,
and safety-map checks; a worker must not infer authorization from an argument.

## External-provider recovery

Use `get_setup_overview` with a trusted `provider_recipe` containing a
`provider_id`, `inventory_argv`, and `worker_argv`. The inventory result returns
namespaced connection IDs. Supply the same recipe during setup. If a worker
cannot report the required live identity or physical evidence, the server reports
the unavailable fact and does not claim direct-provider correctness.
