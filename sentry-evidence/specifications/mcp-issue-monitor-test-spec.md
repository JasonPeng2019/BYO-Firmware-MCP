# MCP Issue Monitor — Remaining Test and Verification Specification

## Goal

Complete the remaining server-side verification for the MCP issue monitor without
using physical hardware. The client-side workspace skill remains a separate
implementation item and must not be declared complete by this test plan.

## Test environment

- Create a new sibling folder named `Sentry_Test` under `FirmCLI_Sentry`:
  `FirmCLI_Sentry/Sentry_Test`.
- The subagent performing the test must use `Sentry_Test` as its working directory
  for the entire test session. Do not run the subagent from the repository root or
  from `MCP_Server/BYO-Firmware-MCP`.
- Put a project-local Codex configuration at
  `FirmCLI_Sentry/Sentry_Test/.codex/config.toml`. Do not rely on the user-level
  Codex configuration or an existing MCP registration.
- Configure that file to launch the server source from
  `FirmCLI_Sentry/MCP_Server/BYO-Firmware-MCP`, while keeping the server process
  and test client rooted in `Sentry_Test`:

  ```toml
  [mcp_servers.byo_firmware]
  command = "uv"
  args = ["run", "--project", "<absolute-path-to-FirmCLI_Sentry>/MCP_Server/BYO-Firmware-MCP", "--locked", "pyocd-debug-mcp"]
  cwd = "<absolute-path-to-FirmCLI_Sentry>/Sentry_Test"
  startup_timeout_sec = 180
  tool_timeout_sec = 120
  ```

  Replace the placeholders with absolute paths before starting the subagent. Use
  forward slashes in TOML paths. The server source must resolve to the existing
  `MCP_Server/BYO-Firmware-MCP` checkout; do not copy or modify the server into
  `Sentry_Test`.
- Keep all test-created project files, transcripts, raw test output, and temporary
  MCP configuration under `Sentry_Test`. The test may read the server source from
  `MCP_Server`, but must not write test artifacts there.
- Use the repository’s `unittest` suite and the existing monitor test helpers.
- Use a temporary monitoring store for every test; never use the developer’s real
  monitoring store.
- Use `NullTransport` for local-only tests and `TestTransport` for delivery tests.
- Physical probes and boards are being used by other tests. Do not connect to,
  reserve, reset, flash, halt, resume, inspect, or otherwise touch real hardware.
  Do not invoke hardware-affecting MCP tools, pyOCD, J-Link, UART, or board locks.
  Unit tests may use fakes and the existing `NullTransport`/`TestTransport` helpers,
  but must not reach a real probe or board.
- Use repeated `server_health_check` MCP calls instead of hardware tool calls to
  exercise the 100-call, 200-call, and 500-call monitor boundaries. The health
  check is the only MCP load-generation tool permitted for these boundary tests.
- Capture the exact command, exit code, test counts, skips, duration, and output.

## Workspace isolation checks

Before running the tests, record:

- the absolute path of `Sentry_Test`;
- the absolute server project path used in `.codex/config.toml`;
- the MCP server name discovered from the project-local configuration;
- the initial contents of `Sentry_Test`.

After the tests, verify that:

- the subagent and its MCP client were launched from `Sentry_Test`;
- no test output or monitoring artifact was written into the server checkout;
- no project state was written outside the configured test workspace, except the
  explicitly isolated per-user monitoring store used by the monitor tests;
- the final report and raw logs remain under `Sentry_Test`.

## Boundary tests using the health-check tool

`server_health_check` takes no arguments, requires no board, and performs no
hardware action. It is still counted as a normal monitored tool call.

Physical hardware is concurrently occupied by other tests. Generate every monitored
call needed for the boundaries below with `server_health_check` only, apart from a
required workspace handshake and the final `submit_routine_checkin`. Do not substitute
flash, debug, memory, register, reset, UART, or other hardware-oriented MCP calls.

### 100-call usage-snapshot boundary

1. Start a clean server session with the shipped snapshot cadence of 100.
2. If a workspace handshake is required, call it once and then make 99
   `server_health_check` calls; otherwise make 100 `server_health_check` calls.
3. Verify that the 100th monitored call produces exactly one `usage_snapshot`
   record.
4. Verify that the snapshot contains cumulative total, per-tool counts,
   per-outcome counts, error counts, coverage, ledger state, delivery state,
   environment, and build capability.
5. Verify that the snapshot causes the expected segment roll and delivery handoff.
6. Verify that no hardware action, plan, permission, or board lock was required.

Repeat the sequence through 200 calls and confirm snapshots remain cumulative and
monotonic rather than resetting or becoming per-window deltas.

### 500-call routine-check-in boundary

1. Start another clean session with the shipped check-in cadence of 500.
2. Account for the required handshake, then make enough `server_health_check`
   calls to reach exactly 500 monitored calls. Do not use hardware tool calls.
3. Verify that the 500th call also produces the 500-call usage snapshot.
4. Verify that the 500th response contains the server-prompted routine check-in
   request.
5. Submit `submit_routine_checkin` with a valid narrative and verify a distinct
   `checkin` record and summary are created.
6. Verify that the check-in has no issue signal, severity, or grouping identity.
7. Verify that the prompt is consumed once and is not repeated until the next
   500-call boundary.

## Remaining coverage requirements

### Summary completeness

Add one end-to-end assertion over the delivered usage snapshot that checks all
required fields together:

- run identity and start time;
- uptime;
- cumulative per-tool, per-outcome, and per-error counts;
- exercised and never-exercised advertised tools;
- ledger record count, chain head, hardening, and verification state;
- store/workspace binding;
- transport, delivery anchor, and undelivered-file state;
- narrative/build capability;
- environment metadata.

### Under-reporting honesty

Add tests and documentation checks for all three tiers:

1. Cumulative counts plus the staleness block defeat casual omission of snapshots.
2. Post-hoc edits are detectable only with an off-box witness after transport
   cutover.
3. Source-level forgery by the machine owner is neither prevented nor detected.

The tests must not claim stronger guarantees than the specification allows.

### Delivery and persistence

Verify that:

- a report or summary remains on disk when delivery fails or the queue drops the
  handoff;
- boot recovery resends undelivered report and summary bodies;
- ACK deletes the complete local file, never a partial file;
- an ACK-deleted predecessor does not create a tamper finding;
- counters survive workspace binding, disconnect, and other run-scoped state
  transitions;
- closeout uses one shared deadline and never waits twice for the full budget.

### Health-check passivity

Verify that health checks:

- require no board or action plan;
- do not open hardware connections;
- do not send reports or check-ins by themselves;
- do not hold a board execution lock;
- still appear in live counters and coverage as ordinary monitored calls.

### Test-quality audit

For every acceptance test, confirm that it exercises the behavior it names. In
particular:

- do not test snapshot delivery by calling private summary builders directly;
- do not cite nonexistent tests in the coverage map;
- do not accept a test that passes when the implementation is patched out;
- synchronize with background delivery rather than relying on sleeps or immediate
  reads;
- distinguish skipped Codex E2E tests from tests that actually ran.

## Verification commands

Run and preserve raw output for:

```text
$env:PYTHONPATH = "<absolute-server-project>;<absolute-server-project>/src"
uv run --project "<absolute-server-project>" --locked python -m unittest tests.test_monitor_behaviour \
  tests.test_monitor_classification \
  tests.test_monitor_counters_trail \
  tests.test_monitor_delivery \
  tests.test_monitor_ledger \
  tests.test_monitor_narrative_tools \
  tests.test_monitor_passivity \
  tests.test_monitor_redaction \
  tests.test_monitor_thrash_block \
  tests.test_monitor_wiring \
  tests.test_monitor_stdio_lifecycle
```

```text
$env:PYTHONPATH = "<absolute-server-project>;<absolute-server-project>/src"
uv run --project "<absolute-server-project>" --locked python -m unittest discover -s "<absolute-server-project>/tests" -p "test_*.py"
uv run --project "<absolute-server-project>" --locked ruff check "<absolute-server-project>/src" "<absolute-server-project>/tests"
uv run --project "<absolute-server-project>" --locked pyright "<absolute-server-project>/src"
```

Run these commands from `Sentry_Test`. The discovery command deliberately has no
explicit `-t`: this checkout's `tests` directory is not an importable package
under an absolute start directory when a separate top-level is forced.

The final report must state the number passed, failed, and skipped; identify any
Codex E2E tests that were skipped; and include the paths to the raw logs.

## Completion gate

Server-side verification is complete only when all boundary tests, remaining
coverage requirements, quality-audit checks, static checks, and full-suite runs
pass with reproducible logs. Overall specification completion still requires the
separate client-side workspace skill and its templates.
