# Server A implementation specification

Authority: `Server_A_functionality.md`.

## Product boundary

The checkout contains two separately composed MCP servers:

- **Server A (`pyocd-turnkey`)** is the user-facing stdio turnkey brain.
- **Server B (`pyocd-debug-mcp-http`)** is the guarded hardware manager on a loopback
  streamable-HTTP endpoint.

`pyocd-turnkey` reuses a separately managed singleton Server B or starts one for this Client A
session, starts Server A on stdio, and passes the verified private Server B URL to each middleman.
A Server B started by the launcher terminates on Client A EOF; explicit daemon mode is opt-in.
Server B owns an OS lease so separate processes cannot create separate physical-board owners.
Client A may register the same loopback HTTP endpoint alongside Server A whenever it needs direct
Server B setup/guardrail/hardware tools; the Server A handshake returns the exact endpoint. This
keeps the two MCP identities separate while one launcher owns their lifetimes.

## Server A surface

Each of `bug_fix`, `complex_implementation`, and `complex_task` has one matching `load_*` tool.
The load tool returns bounded purpose, parameter, memory-tier, permission, and green-check guidance
and unlocks only that agentic tool for the Server A session. A locked call refuses and names its
load tool.

First calls require the complete context defined by the authority document. A follow-up for the
same tool/task may provide `tool_summary`, `task`, and `continue_instruction`; Server A restores the
immutable full context from in-memory session state. Nothing is persisted as authority.

## Middleman boundary

Provider choice is operator-configured, not hard-coded. The configuration names the provider and
must match the outer Client A provider identity. A provider wrapper is launched once per
agentic call with explicit argv and communicates through newline-delimited JSON over stdio:

- Server A sends `{ "type": "prompt", "prompt": "..." }`.
- Server A independently verifies the endpoint's versioned Server B identity and guarded surface.
- Before prompts, the wrapper proves that it initialized MCP at that exact Server B URL, received
  the same product contract, listed tools, and read the live `server_run_id`. Server A independently
  probes the endpoint and accepts only the exact matching readiness frame documented in README.
- The wrapper then returns exactly one decision JSON object per line.

The wrapper process persists for the complete tool call and is terminated on every exit path.
Every call also receives a dedicated temporary artifact root that is recursively removed on every
exit path. `green_check_script` supplies a safe filename, script text, and explicit command template
with exactly one `{script}` placeholder (plus optional `{python}`); Server A materializes it only in
that root, executes the validated argv, and removes the root on wrap-up. It never deletes a
caller workspace file merely because a parameter named it. Ordinary source files named in context
are never deleted.
This small protocol allows any provider CLI to be adapted without putting vendor flags in Server A.
The dynamic `complex_task` schema uses one narrowly isolated FastMCP compatibility adapter. The MCP
SDK is pinned to the exact tested version because the public API cannot express unbounded top-level
`step_n` parameters.

## Controller

The controller owns fixed workflows, contiguous top-level `step_1` through `step_n` fields, prompt rendering, exact decision validation,
iteration accounting, permission finalization, failed-strategy carry-forward, and terminal result
formatting. Schema-invalid decisions are discarded, consume one iteration, and receive a compact
correction prompt.

`return_text_to_user` surfaces the exact text through MCP elicitation and keeps the same middleman
session when Client A supports elicitation. If Client A declines or lacks elicitation, Server A
returns the text as structured MCP content and a delta-call remedy. That fallback continuation
restores the last action result and the complete ordered failed-strategy and warning lists;
diagnostic logging is never treated as a user-facing channel.

`finish_task` is unavailable until the current call has passed its deterministic green check.
Server A, not the middleman, runs the explicit green-check script with validated argv, a finite
timeout, and literal stdout comparison. A value may be a complete normalized line or the exact
value after a conventional `label:`/`label=` prefix; arbitrary substrings and stderr do not pass.

## Server B boundaries

All board-affecting operations share one process-wide execution lock as required by the authority
document. Metadata-only MCP work remains concurrent. Target control resolves the explicit backend
recorded by each board profile through `pyocd_debug_mcp.target_backends`; pyOCD is only the bundled
provider. Additional MCU families may register fail-closed reviewed setup support through
`pyocd_debug_mcp.reviewed_board_support`. Artifact evidence is selected through
`pyocd_debug_mcp.artifact_evidence`, so a backend/toolchain can add self-addressing formats without
central gate edits. Caller-defined allowed ranges or raw load addresses remain forbidden.

## Packaging

The installed hardware entry point must fail early with a clear checkout-resource remedy when the
project-owned board/pack/evidence roots are absent. Runtime authority is not silently packaged into
the wheel.
