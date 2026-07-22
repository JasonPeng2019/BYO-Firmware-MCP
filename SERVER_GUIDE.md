# BYO Server Guide

BYO Server is a headless local MCP server for safe embedded-board setup,
debugging, serial I/O, flash, and recovery through pyOCD. It runs over stdio
only. Any compatible MCP client can use it; the server, not the client, owns
plans, permissions, board routing, validation, safety containment, timeouts,
and cleanup.

## Start the server

The repository is intentionally a fresh-start distribution: it contains no board profiles, packs,
reference firmware, reviewed-device catalog, or generated `.firm` state. Install the locked
environment, then register the stdio command with any MCP-compatible client:

```text
uv sync --locked
uv run --project <absolute-path-to-BYO-Server> --locked pyocd-debug-mcp
```

The client-specific registration wrapper varies, but the command and arguments do not. Stdout is
reserved for MCP framing. Each project should set `BYO_MCP_ARTIFACT_ROOT` when it needs state outside
its default project `.firm` directory. Setup creates project-local profiles and verified pack
metadata only after the client supplies the user's exact part number, datasheet, and researched target
or pack candidate.

Generic command-line utilities are available without any bundled board data:

```text
uv run --locked pyocd-pack-repair --help
uv run --locked pyocd-native-build --help
uv run --locked pyocd-collect-artifacts --help
```

After setup validation, `get_setup_status` returns advisory, provider-neutral
build guidance. The client inspects the project's own build metadata, resolves
its actual toolchain and target, and supplies exact argv to
`<server-python> -m pyocd_debug_mcp.native_build ... -- <command>`. The server
does not choose an SDK, compiler, provider, or target. Compatible local tools
are preferred; ordinary acquisition is allowed when none is usable. Build
guidance never grants memory authority.

Plan fields, budgets, and permission modes are listed in
[`docs/plan-tool-contract.md`](docs/plan-tool-contract.md), which is derived
from the same definitions used by the live MCP schemas. Setup resolves the
current UART port from the selected stable identity and computes the datasheet
SHA-256 itself; clients never need to bind a COM path or run a hash command.

## Tool surface

Call `initialization_handshake` first. The live `tools/list` response is the
authoritative advertised surface; visibility can change after plan/setup calls.
A visible tool is never proof of authorization.
After a plan is accepted, dynamic clients should use its newly exposed direct
action. Clients with a static function binding can use the exact returned
single-child `action_batch` fallback; it follows the identical guarded dispatch
path and is never permission to invent hidden calls.

Always-advertised operational tools cover:

- connection and inspection: profile-only `connect`, `disconnect`, `get_board_info`,
  `get_state`, `read_cpu_register`, `read_execution_state`, `find_symbol`, and
  `read_memory_symbol`;
- ordinary execution: `halt`, `resume`, `step`, `reset_and_run`,
  `remove_breakpoint`, and bounded `wait`;
- setup and safety: familiar-name `setup_overview`, `load_setup_tool`,
  setup-first `board_setup-plan`, strict `continue_setup`,
  application/bootloader-aware `board_safety_refresh`, `board_validate`,
  and the non-authoritative `get_setup_status` readiness barrier;
- orchestration: `action_batch`; and
- the `*-plan` tools for guarded actions.

Guarded actions are registered but hidden until their exact plan unlocks them:

- connection/execution: `connect_override`, `connect_under_reset`,
  `reset_and_halt`, `write_cpu_register`, and `set_execution_state`;
- memory/register/debug: `read_memory_address`, `write_memory`,
  `register_write`, and `set_breakpoint`;
- serial and flash: `read_serial`, `write_serial`, single-open
  `serial_exchange`, `flash_application`, and
  permission-locked `flash_bootloader`;
- destructive recovery: `target_unlock`, which requires fresh one-time
  approval and leaves the validation gate closed; and
- setup mutation: `board_setup` and `board_fix_setup`, exposed only through
  the setup loader and plan workflow.

Normal `connect(board_id)` resolves only the named project profile and its
profile-matched probe. It accepts no probe UID, target, external board-config,
or launch-environment override. If an exceptional manual connection is truly
needed, initialize `connect_override-plan` and use the hidden run-scoped
`connect_override`; it never rewrites the profile. `action_batch` uses the same
strict child schema and cannot smuggle those fields through normal `connect`.

Memory reads are contained to the exact bytes the backend will access. Mapped
RAM, flash, ROM, CPU-system, and peripheral reads remain available, including
deliberately prohibited security/provisioning spans when they are authoritatively
mapped. Unknown or unmapped spans and write-only peripheral reads are refused
before target I/O. Every mutation remains refused for prohibited regions. This
applies to both raw-address reads and symbol-resolved reads; use the named
safety-setup remedy for an incomplete map, or choose a different mapped address
for a write-only peripheral.

After the handshake, ask the user only for familiar board names and pass them
to `setup_overview`. The normalized phrase `no board` is a literal sentinel
that must be passed alone, never treated as a candidate profile name. Every
matching YAML routes to validation first; unknown names route to setup, and
validation may return a specific repair. The response supplies bounded
`load_call`, `next_call`, or plan-template objects with every server-known ID
already filled. Copy those fields into MCP calls; never ask the user to invent
them or to hash a datasheet. `load_setup_tool` then returns guidance only for
the requested setup tool. If setup or validation returns a friendly choice,
relay its prose and copy its exact `accepted_response`; do not scrape labels,
invent a target, or ask the user for internal IDs.

Exact schemas and status payload behavior are in
[docs/client-contract.md](docs/client-contract.md) and the live MCP descriptions.

## Safety model

Each logical board has one live connection and one operation boundary.
Same-board calls serialize while different boards can execute concurrently.
Guarded requests are scoped to their run, board, session, exact parameters,
plan budget, and permission.

When multiple SEGGER J-Link probes are used from one server process, the server
detects the selected probe provider dynamically. The first live J-Link session
uses the provider's normal DLL instance; each additional simultaneous J-Link
session gets an isolated temporary DLL instance, as required by pylink. Closing
the normal owner releases that fast path immediately, even if isolated sessions
remain live. This allocation does not depend on board names, probe serials,
target types, USB locations, or host-specific library paths.
If provider closure cannot be confirmed, the server keeps the affected DLL/session
reservation and reports the close failure; later J-Links remain safely isolated.
Restart the server after resolving the probe/USB fault to reclaim the normal fast path.

Only successful `board_validate` opens the in-memory gate. Writes recheck the
current aggregate safety fingerprint on every call and apply typed containment
before backend mutation. Disconnect and restart clear live assignments, plans,
permissions, and gates. Files under `.firm` are durable evidence only and can
never restore authority.

Target recovery and bootloader flash are destructive operations with stronger
approval rules. Never treat conversational approval, a report, tool visibility,
or a prior run as current authorization.

## Build and import check

The distributable package can be checked without operating hardware:

```text
uv build
uv run --locked python -c "import pyocd_debug_mcp; import pyocd_debug_mcp.server"
```

## More detail

- [Architecture and state ownership](docs/architecture.md)
- [MCP client contract](docs/client-contract.md)
- [Plan-tool contract](docs/plan-tool-contract.md)

## Runtime guarantees

`InMemorySessionStore` is the process-local session implementation; durable reports are
evidence only and cannot restore live authority. The MCP server is provider-neutral over stdio.

For project build dependencies, the client inspects the project's own metadata and available host
resources, prefers a compatible existing SDK/toolchain/library, and uses the project's ordinary
installation or network acquisition path when none is usable. The server does not prescribe vendor
locations, select a provider, or manage a build-environment fallback. Device-support packs used as
debug authority follow the separate verified-pack onboarding contract.
