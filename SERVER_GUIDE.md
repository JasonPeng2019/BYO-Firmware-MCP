# BYO Server Guide

> `0.1.8rc1` is a mixed-product, pre-split, local-delivery preview, not a
> public production release. Its remaining external release gates are recorded
> in [docs/RELEASE_STATUS.md](docs/RELEASE_STATUS.md).

BYO Server is a headless local MCP server for safe embedded-board setup,
debugging, serial I/O, flash, and recovery through pyOCD. It runs over stdio
only. Any compatible MCP client can use it; the server, not the client, owns
plans, permissions, board routing, validation, safety containment, timeouts,
and cleanup.

## Start the server

The repository is intentionally a fresh-start distribution: it contains no authoritative board
profiles, packs, reference firmware, reviewed-device catalog, or generated `.firm` state. Its one
RX-only virtual CAN YAML is an inert example that is never selected automatically. Install the
locked environment, then register the stdio command with any MCP-compatible client:

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

## Software Classical CAN

CAN authority is selected once, before the MCP transport starts. With no CAN
variables, all seven CAN names remain registered under the normal plan-driven
visibility rules, but the runtime is off and creates no CAN thread or worker.
The backend libraries are deliberately an optional dependency group: a base
installation keeps the full non-CAN server and static seven-name CAN
registration, while configured CAN requires
`pyocd-debug-mcp[can]` (or `uv sync --extra can`). A software fixture can be
enabled with:

```text
uv sync --locked --extra can
PYOCD_CAN_MODE=available \
PYOCD_CAN_CONFIG=/absolute/path/to/fixtures/can/virtual_rx_only.yaml \
uv run --locked --extra can pyocd-debug-mcp
```

Selecting available mode without the extra fails closed at startup and names
the install remedy; it never silently falls back to off mode.

That path is stable in a source checkout. A wheel installs the same inert example
at `pyocd_debug_mcp/examples/can/virtual_rx_only.yaml`; copy/edit it in an
operator-owned location before use when its example `board_ids` do not match the
logical board. No packaged example becomes runtime authority automatically.

The source is read once, strictly parsed, hashed, then canonicalized and
snapshotted into the private run directory. Operators can pin the source with
`PYOCD_CAN_CONFIG_SHA256=<64-lowercase-hex>` and the canonical runtime contract
with `PYOCD_CAN_RUNTIME_SHA256=<64-lowercase-hex>`. A finite
`PYOCD_CAN_READ_SECONDS` may replace the profile's default read window,
but cannot exceed its maximum. Unknown non-empty `PYOCD_CAN_*` variables,
legacy FirmwareCLI fixture-proof variables, `required` mode, symlinks, malformed
profiles, and physical `socketcan` profiles all fail startup on supported POSIX
hosts. Software `virtual` and `tcp_sim` profiles can load on Linux, macOS and
Windows. SocketCAN remains Linux-only. Windows software simulation has a native
CI lane; a configured lane is not itself a passing qualification result.
Compatibility health-check declarations from a shared FirmwareCLI profile are
strictly parsed and remain part of its canonical hash, but BYO does not execute
or claim terminal/health proof from them.

The preview is software only:

- `virtual` is python-can's process-local bus. Separate programs cannot join it.
- `tcp_sim` is a localhost-only Classical CAN simulator that independent processes
  can join natively on Linux, macOS and Windows. It preserves per-sender order,
  exact identifiers and payloads, and marks own echoes as TX. It does not model
  CAN arbitration, bit timing, electrical faults or physical TX quiescence.
- `socketcan_vcan` uses a real Linux kernel virtual interface; its dedicated
  integration suite requires a pre-existing `vcan` link and mutation opt-in.
- Physical CAN profiles remain disabled.

Start a simulator with the optional dependencies installed:

```sh
uv run --locked --extra can python -m pyocd_debug_mcp.adapters.can_sim --port 43113
```

Use `fixtures/can/tcp_sim.yaml` as the profile template. The channel must match
`127.0.0.1:<port>`; port zero on the simulator command chooses and prints a free
port. Stop it with Ctrl-C. Each broker port is one simulated bus. Successful
send means local socket submission, not peer acknowledgement. Reconnect after
broker loss; an existing session never silently joins a replacement broker.

For a software-only acceptance run, including a fixture-seeded setup-full policy,
simulated SWD, public board validation and the MCP stdio path:

```sh
uv run --locked --extra can python -m unittest tests.test_can_native_sim tests.test_can_mcp_sim_e2e -v
```

These test seams do not bypass the production board/validation prerequisites.
Native Windows execution still needs a passing Windows runner result before it
can be called qualified. macOS and Linux results must name their tested versions.

Seven names are always registered: `get_can_status`, `connect_can`,
`disconnect_can`, `read_can-plan`, `read_can`, `send_can-plan`, and `send_can`.
Bus attachment requires the profile-authorized logical board, an active
setup-full debug connection, and a current validation stamp. Reads and sends
also consume an exact plan; transmit remains off unless the immutable profile
enables it and exact-allowlists the identifier. Profile duration/frame limits
and process-lifetime quotas cannot be widened by callers.

One CAN worker can be owned by one logical board at a time. A proven disconnect
allows a later sequential rebind, but process quotas do not reset. Normal debug
`disconnect` closes CAN before clearing validation or releasing SWD. If cleanup
cannot be proven, ownership is retained for bounded retry and debug authority is
left intact rather than claiming success. Process shutdown makes the same
bounded cleanup attempts and exits non-zero if the CAN generation remains uncertain.
CAN startup has a five-second caller budget covering OS spawn and readiness.
`multiprocessing.Process.start()` itself cannot be interrupted portably; a daemon
launch owner retains a stalled call and retires any late child. Cancellation and
startup deadlines no longer wait on a spawn-held lifecycle lock. Until retirement
is proven, status reports `startup-pending`/`unknown`, never a clean close. No
backend command is submitted after startup authority is revoked. This bounds the
application's startup wait, not OS process creation, interpreter exit, or stalled
filesystem I/O during evidence persistence.

Every server `read_can` now retains a private immutable JSON capture in
`.firm/runs/<run-id>/can-captures/` and returns an `artifact` reference containing
its name, SHA-256 and byte count. Capture payload disclosure follows
`evidence.artifact_payloads`, independently of `provider_payloads`. Artifact count
and byte quotas apply across reconnects; failed persistence is a failed read, and
its reserved budget is not refunded. Direct library managers without a persisted
profile snapshot return `artifact: null` and make no retention claim.

The five operational CAN routes return canonical JSON; the two `*-plan` routes
retain the standard textual plan workflow. Payload bytes are returned only when the
operator profile selects full provider payloads; otherwise frame records remain
metadata-only. Raw CAN frames do not authenticate their sender, and virtual or
`vcan` observations do not establish physical timing, wiring, arbitration, or
fault behavior.

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
uv run --locked python scripts/verify_release_artifacts.py
```

With no positional argument, the release verifier builds fresh archives, installs the wheel and sdist into
separate temporary base environments, checks the off-mode inventory and the
missing-extra diagnostic, then installs the `can` extra and opens/closes the
process-local virtual backend in its isolated worker. Pass `dist` to verify the
exact existing handoff pair instead; the command rejects stale or ambiguous
archive sets.

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
