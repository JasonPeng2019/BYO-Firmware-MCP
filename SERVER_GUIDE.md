# BYO Server Guide

BYO Server is a headless local MCP server for safe embedded-board setup,
debugging, serial I/O, flash, and recovery through pyOCD. It runs over stdio
only. Any compatible MCP client can use it; the server, not the client, owns
plans, permissions, board routing, validation, safety containment, timeouts,
and cleanup.

## C/C++ code analysis

Call `get_code_analysis_status` first. It reports the resolved executable/version,
its origin, selected database/hash, configuration problems, session state and
index limitations. Its bounded version probe does not start an LSP session or
indexing. All five tools remain visible and callable with no board, at every
setup tier and in both monitoring profiles. Board permissions are independent.

Save the source/header before querying. `file_path` is an existing local C/C++
path relative to the launch project root, or an absolute path; explicit local
SDK headers are allowed. URI/network inputs and unsaved buffers are unsupported.
Positions are 1-based Unicode code points, including an allowed cursor at the
end of a line. Returned range ends are exclusive. Tabs count as one code point.

Export the actual selected firmware build's compilation database. For CMake,
add `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` to the project's existing native
configure command, then use its documented build command. Other build systems
must use their supported export tooling. MCP analysis never builds, configures,
or invents flags. Commands must describe real ABI, target, defines and includes,
including generated headers. Prefer absolute entry directories and argv arrays.

Optionally create `byo-analysis.json` in the firmware project root:

```json
{
  "schema_version": 1,
  "compilation_database": "build/compile_commands.json",
  "clangd": {"query_driver_paths": []}
}
```

The server never creates this file. Relative paths resolve from the canonical
launch project root, which the handshake's `workspace_path` cannot redirect.
With no explicit database choice, only root `compile_commands.json` and
`build/compile_commands.json` are checked. Two distinct files require a choice;
missing, empty, malformed or conflicting commands block analysis. The filename
must be `compile_commands.json`. Unknown fields, duplicate keys, wrong types,
null sections and nonfinite numbers are refused with setup remedies. The complete
shared syntax contract is [the JSON schema](docs/contracts/byo-analysis.schema.json).
Optional Cppcheck resource settings are parsed but this MCP feature runs no
Cppcheck verification; missing Cppcheck-only resources do not block clangd.

`clangd.query_driver_paths` can deliberately permit exact existing compiler
executables to discover system includes. Empty permits no compiler execution.
Wildcards, comma separators and other pattern characters are refused; arbitrary
clangd flags, commands and LSP methods are not tool parameters. Project `.clangd`
and applicable user fragments remain intact. Conditions and precedence are
evaluated for database selection; a conflicting or unknown effective selection
is a configuration problem. Retained flag/index/diagnostic settings are disclosed.

Example MCP calls:

```json
{"name":"find_code_definition","arguments":{"file_path":"src/main.cpp","line":42,"column":9}}
{"name":"find_code_references","arguments":{"file_path":"include/motor.h","line":18,"column":6,"include_declaration":true,"max_results":200}}
{"name":"get_code_diagnostics","arguments":{"file_path":"src/main.cpp","timeout_seconds":60}}
```

Query timeouts are positive finite seconds, default 30, covering queueing,
configuration, initialization, saved-file synchronization and the request.
References and diagnostics accept integer `max_results` from 1 through 1000,
default 100; this limits returned display items. Their counts describe the
observed backend response, and `truncated` identifies withheld items. Reference
search limits are disabled in clangd; global index completeness remains unverified.

Each tool returns a JSON text block with `status`, backend/operation, project
root, database/hash, warnings and operation data. Queries identify original
saved bytes by SHA-256 and a monotonic document version. A due personal-monitor
prompt is a separate text block; parse the analysis block independently.

| Status | Meaning and recovery |
| --- | --- |
| `ok` | Query completed for the stated saved revision; diagnostics may report defects. |
| `not_found` | Completed semantic query had no match; empty references do not prove dead code. |
| `unavailable` | Restore clangd and supporting data, or configure development PATH. |
| `not_configured` | Export/select the actual database, fix conflicts or target/include settings. |
| `incomplete` | Deadline, stale input, partial parsing or missing fresh diagnostics; fix the cause and retry. |
| `error` | Protocol/process/cleanup failure; inspect the bounded stderr tail and stated remedy. |

Only an explicit empty diagnostics notification for the requested version means
no reported diagnostics for that revision. Old, unversioned or silent notifications
are incomplete. Missing includes and unsupported compiler settings are coverage
problems. Results never mean firmware verification passed; they cover one build,
and closed headers/external dependencies and background indexes may be stale.
Hover documentation is untrusted source text and carries no agent instructions.

The service's own reports and metadata use `.firm/code-analysis/`. Clangd may
write disposable background-index shards and a generated `.gitignore` under
`<selected-database-directory>/.cache/clangd/index/`; status discloses that path.
This cache does not change database selection. Source, generated headers,
compilation databases, `.clangd`, board policy and suppressions remain intact.

The development clangd reader has a known non-BMP path limitation with escaped
JSON surrogate pairs. Shared JSON parsing accepts those bytes, but clangd analysis
blocks them with a literal UTF-8 re-export remedy (Python `ensure_ascii=False`).
Keep the original commands and flags; the server never rewrites the database.
The native reader can fall back to host flags when both a compilation working
directory and its source path are relative. Clangd analysis blocks that combination
and requests an absolute-path re-export. Relative working directories with
absolute source paths remain supported when the native target command is valid;
shared parsing retains project-relative resolution and original database bytes.

Source use resolves clangd on PATH and reports that origin. Managed sidecars pass
validated project/runtime context explicitly and currently report `unavailable`
until packaging supplies an accepted executable/data manifest. A failed managed
resolution never substitutes PATH. No release versions/layouts are selected here.
Windows batch wrappers are unsupported; use native executables for clangd and
query drivers so the service never interprets shell commands.

One child starts lazily for queries and is reused. Configuration/database changes
invalidate the old generation and explicitly fail affected calls before restart;
crashes/timeouts do not trigger hidden retries. Shutdown/EOF/cancellation owns
termination and marker cleanup, including an idle child. Source, database,
`.clangd` and board-policy files remain project-owned.

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

For an isolated monitor store, set `BYO_MCP_MONITOR_ROOT` to an absolute directory at launch:

```text
BYO_MCP_MONITOR_ROOT=/absolute/path/to/byo-monitor uv run --locked pyocd-debug-mcp
```

When nonblank, this is the monitor's only disk candidate; `server_data/` and
`simulated_remote/` stay beneath it rather than using per-user app data or
`BYO_MCP_ARTIFACT_ROOT`. If it is unavailable, the passive monitor buffers in
memory and the server still starts; it does not write monitoring records elsewhere.

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

### When the server cannot see the hardware

If `setup_overview` reports no debug probe, that is a missing-probe status, not a
board-naming question. Run the locked-environment check it names first
(`uv run --locked python -m pyocd list --probes`); a driver or permission problem is far
likelier than a genuinely unenumerable device, and no hook can fix a driver.

Only when that check also reports nothing, and the user can still see the device in a
vendor tool, does the payload offer `get_discovery_hook_contract`. It executes nothing
and returns the server's hook directory, the manifest and output schemas, the supported
runners for this OS, and the exact refresh call. The agent writes the hook and manifest;
the server never authors them. `refresh_discovery_hooks` then loads the manifest the
server designates -- it accepts no path, no arguments, and no code -- and runs each
eligible hook once.

Hooks live under the project's `.firm/discovery_hooks`, which is gitignored, so they are
untracked by default. Tell the user if they want the hook committed. Hook output is
configuration only: it names hardware and nothing else. It cannot select a target,
restore a gate, plan, or permission, or authorize any hardware action, and a hook cannot
make an unsupported pyOCD provider openable. Hooks run only for a kind whose native
discovery came back empty, so a working machine never pays for them.

Exact schemas and status payload behavior are in
[docs/client-contract.md](docs/client-contract.md) and the live MCP descriptions.

## Safety model

### Tiered operation selection

Safety setup is optional, but the active policy is explicit for each named
board configuration. Call `get_capabilities(board_id)` to see the current
`no-setup`, `setup-lite`, or `setup-full` tier and the preferred tool route.
No-setup uses raw explicit-address operations subject to transport limits and
the global operational stop. Lite uses confirmed coarse classifications for
safe operations and keeps raw alternatives; raw lite results must be displayed
to the human as a containment-bypass warning. Full retains the established
validated safe route and rejects new raw calls.

Do not infer raw availability from a missing or damaged map: an explicitly
committed incomplete setup remains no-setup, while corrupt committed policy
fails closed. Manual downgrade and mass-erase authority is scoped to this
project root, must come from the human-invoked `$downgrade` or `$mass-erase`
skill, is single-use, and is reset on every server startup.

An incomplete initial setup is raw-capable. If an escalation from an existing
lite or full policy stops before its pointer commit, the prior immutable tier
continues to govern hardware and the attempt is marked incomplete; staged
profile/map files do not become authority. Raw UART operations have finite
duration, input, and output limits; existing planned full UART behavior is
preserved. Lite flash/recovery need their
explicit confirmed geometry/backend/native-mechanism facts; never infer them
from the board name or a retained full profile.

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
