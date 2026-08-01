# Alternate hardware discovery plan

## Outcome

Add two discovery-only fallback pathways:

1. a debug-probe hook that tells the server which pyOCD-supported debugger is present
   and the exact UID pyOCD must use to open it; and
2. a UART hook that tells the server which local serial endpoint is present and how
   to identify it again when UART work begins.

Hooks do not replace the MCP tools or hardware-control code. Setup, validation,
connection ownership, pyOCD sessions, serial I/O, plans, permissions, containment,
timeouts, reporting, and cleanup remain server-owned.

The fallback must work on Windows, macOS, and Linux and with every probe provider
registered by the installed pyOCD, including provider plug-ins.

## Codebase findings this plan must account for

Discovery is currently repeated in several independent paths:

- `probe_inventory.list_connected_probes_cli()` runs pyOCD inventory.
- `server._validation_inventory()` builds setup and validation choices from pyOCD
  plus `pyserial`.
- `server._resolve_probe_uid_for_connect()` performs a separate pyOCD-only lookup.
- `server._assigned_probe_uid_for_connect()` rechecks the assigned probe against a
  new validation inventory.
- setup preflight filters that inventory to the exact planned connection.
- setup performs a real pyOCD attach before committing a profile.
- `server._resolve_serial_port_for_session()` performs a new pyserial-only lookup
  when UART tools run.
- `get_setup_status()` resolves the attachment cache against another fresh inventory.
- `serial_resolver.py` already has an operator-supplied, vendor-specific UART helper
  registry, but it only maps ports already returned by pyserial and is not a generic
  inventory hook.

A correct implementation must update all of these paths. Merging hooks only into
`setup_overview` would allow setup choices that later connect and UART operations
could not resolve.

`setup_overview` also stops before creating a setup route when a requested board has
no visible debug connection. Therefore alternate-path guidance must begin in
`setup_overview`; adding guidance only to setup preflight's `setup/no-probe` result
is insufficient.

## Pre-existing defects this work must fix

Two defects already exist in the paths this work touches. They are not caused by
discovery hooks, but hook guidance cannot be attached correctly until the first is
repaired, and the second becomes load-bearing once `connection_id` carries real
meaning.

### Zero visible probes is reported as an assignment problem

`setup_overview` compares the number of requested board names against the number of
visible debug connections *before* it tests for zero connections. With one requested
board and no attached probe, that comparison fails and the call returns
`setup_assignment_clarification_required` — asking the user to clarify which board is
which — and never builds a route.

Consequences:

- The user-facing message is wrong today, with no hooks involved. A missing debugger
  is reported as a naming ambiguity.
- The branch that handles the no-probe case correctly, inside the unknown-board setup
  route, is unreachable for any named board and is therefore dead code.
- This is exactly where the probe hook-contract call must be returned. Attaching
  fallback guidance to the apparently correct branch would leave it unreachable too.

Required fix: test for zero connections explicitly and first, return a typed
no-probe/fallback-available status from there, and delete the unreachable branch
rather than maintaining two competing no-probe messages. This fix is self-contained,
independently verifiable, and must land before the hook work so that later steps have
a correct place to attach guidance.

The zero-UART path behaves differently and needs no repair: an empty UART inventory
does not short-circuit, so the conditional UART hook-contract call attaches to the
existing "attach and identify the board's UART connection" user fact.

### `connection_id` is minted in four places with inconsistent normalization

`stable_connection_identity()` casefolds the probe UID into `probe:<uid>`. The
`setup_overview` inventory row builder, the live-connection match helper, and the
UID-less active-connection path each construct the same shape differently, and only
some casefold.

No failure results today, because every comparison helper casefolds both sides
defensively before comparing. The defect is that the identifier is not a single
value, so any new comparison written without that defensive normalization will be
subtly wrong.

Required fix: mint this identifier in exactly one place and have all four sites call
it. Behavior must not change; this is a precondition for the opaque run-scoped
selection records, which make `connection_id` meaningful rather than merely
comparable.

### Not a defect: unbounded child-output capture

The owned-subprocess helper collects a child's entire output with no ceiling. An
audit of its callers found no current exposure: the build path inherits its streams
instead of capturing them, and every capturing caller runs a bounded, small-output
enumeration command. This is therefore a requirement on new code — hooks must not use
that helper — and not a repair to existing behavior.

## Boundaries

- Hooks are read-only discovery programs.
- A probe hook returns a pyOCD provider and selector UID. The existing pyOCD backend
  still opens and controls the probe.
- A UART hook returns an endpoint that the existing pyserial code opens.
- Hook output grants no connection, validation, memory, flash, recovery, or UART
  authority.
- A listed probe is not considered usable until the existing setup or validation
  path opens it and verifies the live probe identity.
- If pyOCD cannot open a hook-discovered UID, report an open/backend failure. Do not
  ask the agent to write a second discovery hook or silently fall back to another
  physical probe.
- If pyserial cannot open a resolved UART path during a UART action, report that
  action failure. Discovery alone is not evidence that the port can be opened.
- With no hooks configured, behavior remains unchanged.

## Agent-facing workflow

Add two always-visible, non-authorizing MCP tools:

### `get_discovery_hook_contract`

Input: `kind`, exactly `probe` or `uart`, plus the opaque run-scoped `retry_id`
returned by the failed discovery response. Permit `retry_id=null` only for
non-executable contract inspection.

Return:

- the exact project-local hook root selected by the server;
- the manifest and output schemas;
- the supported runners;
- the current operating-system name;
- the installed pyOCD provider IDs for probe hooks;
- an example using the server's Python interpreter;
- the exact `refresh_discovery_hooks` call bound to that retry context; and
- plain guidance that the hook is discovery-only.

The response must never ask the user to invent paths, UIDs, provider IDs, JSON, or
commands. A capable local agent inspects the host, writes the hook and manifest, and
uses the returned retry call. The user is asked only about friendly physical
ambiguity, such as which of two visible adapters belongs to a board.

### `refresh_discovery_hooks`

Input: the opaque run-scoped `retry_id`; no arbitrary executable, argv, or code. It
reloads only the server-designated project manifest. A missing, expired, or
wrong-kind retry ID is refused without running a hook.

Behavior:

- strictly validate the manifest and hook paths;
- hash the manifest and hook files into a run-scoped configuration snapshot;
- execute each eligible hook once as a diagnostic;
- return per-hook status and friendly discovered rows; and
- provide the exact original `setup_overview` or setup/validation retry call captured
  under `retry_id`.

Refreshing hooks consumes no setup plan or hardware permission because it performs
discovery only. It opens no debugger or UART. If a hook file changes after refresh,
the server refuses to execute it until `refresh_discovery_hooks` is called again.
Restart clears the loaded snapshot but not the project files.

Discovery failures must return a machine-readable `hook_contract_call` containing
the exact kind and retry ID. Retry contexts are memory-only, bounded in count and
lifetime, scoped to the originating server run and setup request, and cleared after
successful replay. They grant no hardware authority.

### Failure routing

`setup_overview` and setup/validation failures must give the agent deterministic
guidance:

- Native probe inventory produced no probe:
  return a typed fallback-available status with the locked-environment diagnostic
  `uv run --locked python -m pyocd list --probes`, followed by the exact
  `get_discovery_hook_contract(kind="probe")` call if native discovery remains empty.
- Native UART inventory is empty and the workflow actually requires UART:
  return the exact `get_discovery_hook_contract(kind="uart")` call.
- Native inventory is merely ambiguous:
  keep the current friendly-selection flow; do not create a hook unnecessarily.
- A hook fails, times out, or returns invalid output:
  name that hook's friendly ID, failure class, and exact repair/retry call.
- A hook returns an unregistered pyOCD provider:
  explain that discovery succeeded but the installed pyOCD cannot control that
  provider; a discovery hook cannot fix it.
- A hook finds a probe but pyOCD cannot open it:
  distinguish `probe found` from `pyOCD open failed` and give driver, competing
  process, firmware, and physical-target checks. Do not loop back to discovery.
- The client cannot create local files or run local discovery:
  relay a concise manual remedy and stop without fabricating hook output.

For an unknown-board route with no native UART rows, `setup_overview` must include a
conditional UART hook-contract call alongside the question of whether UART is
required. If the user says UART is required, the agent resolves the UART fallback
and reruns `setup_overview` before submitting a populated setup plan. This avoids
spending the setup call and permission on a deterministic `setup/no-uart` failure.
If a previously visible endpoint disappears only after execution starts, preserve
the current budget/paired-allowance semantics and tell the agent whether a complete
replacement plan and fresh permission are required after refreshing discovery.

Update the live descriptions for `setup_overview`, `load_setup_tool`,
`board_setup-plan`, and validation guidance so agents follow this sequence. Document
the same sequence in `docs/client-contract.md`, `docs/architecture.md`,
`SERVER_GUIDE.md`, and the README.

## Project-local hook configuration

Support agent-authored project hooks and optional preinstalled operator hooks.

The server returns the exact project hook root; clients must not guess it. Add a
`FirmStore` layout entry for configuration-only discovery hooks. Hook files are not
safety evidence and cannot restore a gate, plan, permission, assignment, or session.

Use one strict manifest:

```json
{
  "schema_version": 1,
  "hooks": [
    {
      "id": "local-probe-fallback",
      "kind": "probe",
      "platforms": ["windows", "macos", "linux"],
      "runner": "server-python",
      "entrypoint": "probe_fallback.py",
      "argv": [],
      "timeout_seconds": 10
    }
  ]
}
```

Supported runners:

- `server-python`: execute `sys.executable ENTRYPOINT ...`; this is the portable
  default for agent-authored hooks.
- `executable`: execute an operator-installed absolute executable directly.

Never invoke a shell. Do not accept a command string. Resolve project entrypoints
inside the returned hook root and reject traversal, symlink escape, NUL bytes,
unknown fields, duplicate IDs, invalid platforms, invalid timeouts, and non-files.

Retain an optional environment-selected operator registry for centrally managed
hooks at `BYO_MCP_DISCOVERY_HOOK_REGISTRY`, but give project hooks and operator hooks
distinct IDs and provenance. Define an explicit precedence rule: identical hardware
identities merge; one source never silently replaces a conflicting source.

Load manifests only through `refresh_discovery_hooks`, not implicitly at import time.
This allows an agent to create and use a fallback during the current MCP run without
requiring a server restart.

## Hook output contracts

Each hook writes one UTF-8 JSON object to stdout. Human diagnostics go to stderr.
Both streams are size-bounded.

Probe output:

```json
{
  "schema_version": 1,
  "probes": [
    {
      "provider": "stlink",
      "unique_id": "0668FF514988525067213913",
      "description": "STM32 ST-LINK",
      "usb_location": "optional diagnostic location"
    }
  ]
}
```

Rules:

- `provider` must be registered by the installed pyOCD.
- `unique_id` must be the exact selector accepted by pyOCD for that provider.
- The server treats `(provider, unique_id)` as the hardware selection key.
- The hook may not return a target, board ID, pack, debug mode, frequency, memory
  range, permission, or operation.
- `usb_location` is diagnostic only and never becomes identity or authority.

UART output:

```json
{
  "schema_version": 1,
  "uart_endpoints": [
    {
      "port_path": "COM3",
      "description": "Florence controller FTDI UART",
      "serial_number": "FTDI1234",
      "vid": 1027,
      "pid": 24577,
      "location": "optional diagnostic location"
    }
  ]
}
```

Rules:

- Windows `COMx`, macOS `/dev/cu.*` or `/dev/tty.*`, and Linux `/dev/tty*` paths are
  opaque platform paths; do not rewrite them except for existing Windows `\\.\`
  normalization at open time.
- `serial_number + vid + pid` forms the existing durable, non-authoritative UART
  attachment key.
- An endpoint missing any stable USB field is session-local. It may be selected for
  the current run but is not persisted in `AttachmentCache`.
- A hook may not return baud rate, test data, expected output, board assignment, or
  permission.

## Cross-platform hook guidance

The contract tool and operator documentation must give the agent practical,
platform-specific discovery options without requiring one particular utility:

- Windows: prefer a vendor CLI or Windows PnP/SetupAPI data. A Device Manager row is
  insufficient unless the hook can derive the exact pyOCD selector UID. UART hooks
  may use PnP metadata but must return the actual `COMx` path.
- macOS: prefer a vendor CLI or IOKit/`system_profiler` USB data. Prefer `/dev/cu.*`
  for ordinary outbound UART use, while accepting `/dev/tty.*` when the hardware
  requires it.
- Linux: prefer a vendor CLI, `/sys/bus/usb/devices`, or `udevadm` for probes and
  `/dev/serial/by-id` for stable UART resolution, while returning the current usable
  `/dev/tty*` path.

Agent-authored Python hooks may invoke local vendor or OS utilities with argv arrays
and `shell=False`. The MCP server itself still invokes only the declared hook
entrypoint. Guidance must tell the agent to prefer stable serial/UID fields over USB
location and never infer a pyOCD UID from a friendly label.

## Unified inventory and identity model

Add a shared inventory service used by every setup, validation, reconnect, status,
and UART-resolution caller.

It returns:

- merged probe rows;
- merged UART rows;
- native discovery diagnostics;
- hook diagnostics; and
- source provenance for each row.

Merge rules:

- Preserve exact provider and UID values needed by pyOCD.
- Deduplicate probes only by the existing stable-equality policy applied within the
  same provider. Do not broadly strip punctuation or merge unrelated providers.
- Deduplicate stable UARTs by `(serial_number, vid, pid)`.
- Deduplicate session-local UARTs only within one inventory snapshot by normalized
  port path and source.
- If native and hook rows describe the same stable device, retain one selectable row
  with both provenance sources.
- Conflicting rows remain separate friendly choices.
- Hook results supplement native results; they never delete native rows.

### When hooks execute

The merge rules above govern what happens once both native and hook rows exist; they
do not say when a hook is allowed to run. Decide that explicitly, per hardware kind,
independently for probes and UARTs:

- Run a kind's configured hooks only when that kind's native discovery for the
  current snapshot returned zero rows.
- If native discovery for that kind returned any row, do not execute that kind's
  hooks at all, not even to supplement.
- Evaluate this per snapshot, not once at server startup: a device that is natively
  visible on one refresh and only hook-visible on the next must still be found.

This makes the fallback strictly opt-in by hardware state rather than an always-on
second detection pass. It matters most for UART: `_resolve_serial_port_for_session()`
runs immediately before every UART action, so an always-run policy would launch hook
processes inside every `read_serial`, `write_serial`, and `serial_exchange` call, and
inside the UART finalizer, on every board, forever — not only on the machines that
need the fallback. Gating by native-empty keeps hook execution, and therefore its
timeout cost, confined to the case the feature exists for.

Refactor the current assumption that `connection_id.removeprefix("probe:")` is always
the pyOCD UID. A run-scoped selection record must map the opaque setup
`connection_id` to:

- provider;
- exact pyOCD `unique_id`;
- stable identity;
- provenance and hook-source hash; and
- identity scope.

`_assigned_probe_uid_for_connect()`, setup live attach, `board_validate`, normal
connect, and reconnect checks must resolve this record from a fresh unified
inventory. If the same stable row is absent or the hook hash changed, clear the
assignment and rerun setup routing rather than selecting a different probe.

Keep active server-owned connections in validation inventory exactly as today so an
already-open probe does not disappear merely because pyOCD omits it from listings.

## UART selection and reuse

The UART hook must participate in more than setup choices:

- setup overview and preflight;
- external-adapter confirmation;
- attachment-cache confirmation;
- `get_setup_status()` readiness;
- `_resolve_serial_port_for_session()` immediately before every UART action; and
- UART finalizers.

At UART-open time, rerun unified inventory and resolve the selected stable identity
to its current path. Never persist or blindly reuse `COM3` or `/dev/tty*`.

For stable hook endpoints, use the existing `AttachmentCache`. For session-local
endpoints, add a run-scoped board-to-UART selection that is cleared on disconnect,
restart, hook refresh, or disappearance. If more than one row could satisfy the
selection, fail and route back to friendly setup selection.

The existing `PYOCD_SERIAL_FALLBACK_REGISTRY` must not become a competing third
system. Adapt its two vendor parsers into the unified inventory/hook layer, preserve
backward compatibility for one release, document precedence, and deprecate the old
environment variable only after equivalent behavior and migration tests exist.

## Probe-provider coverage

Use pyOCD's loaded provider registry as the source of truth for selectable provider
IDs. The static `probe_families.json` remains useful for friendly labels and legacy
CLI text matching, but must not limit hook support.

Test with the built-in providers and a fake registered plug-in provider. A provider
that is listed by a hook but not registered by the server's installed pyOCD is
diagnostic-only.

Do not change the existing native-only safety condition for the J-Link UID-less retry
unless separately proven safe. A discovery hook must not cause pyOCD to retry without
the selected UID and accidentally open a different probe.

## Process and operation integration

Add `discovery_hooks.py` for manifest parsing, snapshots, invocation, output parsing,
and typed diagnostics. Reuse owned-process creation and cleanup, but add truly capped
stdout/stderr capture: checking size only after `communicate()` is not enough because
the current runner buffers without a limit.

Requirements:

- per-hook timeout and aggregate inventory timeout;
- cancellation and descendant cleanup on all platforms;
- UTF-8 decoding with replacement only in diagnostics, never silently repairing JSON;
- deterministic hook order;
- bounded row count, field lengths, stdout, and stderr;
- no stdin and no interactive windows;
- no shell;
- process exit code, timeout, parser failure, and cleanup failure kept distinct; and
- operation timeout calculations updated for the configured number of eligible hooks.

The last requirement applies beyond the probe-discovery tools that already carry an
inventory timeout allowance. Because UART hooks execute inside
`_resolve_serial_port_for_session()`, every tool that reaches it needs the same
allowance added to its own budget: `read_serial`, `write_serial`, `serial_exchange`,
and the `on_exit` UART finalizer. A tool's timeout is otherwise computed from its own
arguments (for example a bounded `read_seconds`) and can be smaller than a single
hook's configured timeout, which would cancel the action before a hook already running
inside it can finish. Compute this allowance from the per-kind hook counts described
under "When hooks execute" above, so it stays zero when no hook for that kind is
configured or none is eligible to run.

Run hooks outside same-board state locks where possible, then bind one immutable
inventory snapshot inside the operation. Concurrent setup for different boards may
share a short-lived inventory result, but one operation must not mix rows from
different snapshots.

## Implementation sequence

### Precursor fixes

Land these before step 1. They are independently verifiable against the current test
suite and do not depend on any hook code.

- P1. Make `setup_overview` return a typed no-probe status from an explicit
  zero-connection test placed before the name/connection count comparison, and delete
  the branch this makes unreachable. Ship this as its own change so the corrected
  message is verifiable without the hook feature.
- P2. Mint the probe `connection_id` in one shared helper and route all four current
  construction sites through it, with no behavior change.

### Hook implementation

1. Add strict hook models, project/operator registry loading, source hashing, capped
   execution, and unit tests.
2. Add `get_discovery_hook_contract` and `refresh_discovery_hooks`, including exact
   agent retry guidance and static-client-compatible tool registration.
3. Replace lossy native probe listing results with a typed result that retains exit
   code, timeout, stdout/stderr summary, parsed rows, and command provenance.
4. Add the unified inventory service and adapt the legacy serial helper providers.
5. Introduce opaque run-scoped probe selection records and refactor all places that
   derive a UID directly from `connection_id`.
6. Integrate unified probe inventory into `setup_overview`, preflight, validation,
   normal connect, assigned-probe rechecks, status, and disconnect invalidation.
7. Integrate unified UART inventory into setup, confirmation/cache logic, status,
   UART actions, and finalizers; add run-scoped session-local UART selections.
8. Add typed no-probe, no-UART, hook-failure, unsupported-provider, disappeared
   selection, and pyOCD-open-failure responses with exact agent guidance.
9. Update live MCP descriptions and all client/architecture/operator documentation.
10. Run focused simulated tests, the full unit suite, Ruff, Pyright, and build/import
    checks. Real-hardware checks are optional smoke tests and are not required for
    implementation or acceptance.

## Tests

### Precursor fix tests

- With one requested board name and zero visible debug connections, `setup_overview`
  returns the typed no-probe status, not an assignment-clarification status, and
  clears provisional assignments exactly as the sibling early returns do.
- The same call with two requested names and one visible connection still returns
  assignment clarification, proving the count comparison was narrowed rather than
  removed.
- The deleted unknown-board no-probe branch has no remaining reachable caller.
- The shared `connection_id` helper produces the identical value the four former
  construction sites produced for the same probe UID, including the decimal
  leading-zero and mixed-case cases the comparison helpers currently absorb.

### Registry and process tests

- Project and operator registries, platform filtering, path containment, symlink
  escape, duplicate IDs, unknown fields, invalid runners, timeouts, cancellation,
  large output, malformed UTF-8/JSON, nonzero exits, and cleanup failures.
- Windows path-with-spaces, macOS executable/Python entrypoint, and Linux executable
  permissions.
- Hook-file change after refresh is detected before execution.
- Run the subprocess and fixture suite in a Windows/macOS/Linux CI matrix.

### Inventory and identity tests

- Native-only, hook-only, merged, deduplicated, conflicting, disappeared, and changed
  probe/UART rows.
- Decimal J-Link UID equality without over-normalizing other providers.
- Two providers with the same textual UID remain distinct.
- Active opened probes remain visible to validation.
- Static-label and registered plug-in provider coverage.

### Workflow tests

- `setup_overview` with zero native probes returns hook guidance before attempting to
  build a setup route.
- `setup_overview` with zero UART rows supplies conditional hook guidance; an agent
  that learns UART is required resolves it before submitting the populated plan.
- An agent can fetch the contract, create/refresh a hook, rerun setup overview, select
  the friendly row, complete setup, validate, disconnect, and reconnect.
- Existing-profile validation uses a hook-discovered assigned probe.
- Hook discovery followed by pyOCD open failure reports the correct terminal remedy
  and never stamps the gate.
- Multiple boards and multiple hook probes preserve one-to-one assignments.
- UART-required and UART-disabled setups route differently.
- Stable UARTs survive port-path changes through cache resolution.
- Session-local UARTs work only for the current run and require reselection after
  invalidation.
- Hook UART paths are used by read, write, exchange, and finalizer operations.

All required workflow tests use fake hook processes and mocked pyOCD/pyserial
boundaries. They must simulate:

- native pyOCD discovery returning no probes while a probe hook returns a valid UID;
- native pyserial discovery returning no UART while a UART hook returns an endpoint;
- successful pyOCD open using the hook-returned UID;
- pyOCD open failure after successful hook discovery;
- UART open success and failure after successful hook discovery;
- hook timeout, malformed output, disappearance, and identity change; and
- reconnect and port-path changes without physical hardware.

These simulations reproduce the alternate-path behavior even on a development
machine where native discovery works normally.

### Safety and compatibility tests

- Hook output cannot inject targets, packs, connection policies, board IDs, flash
  ranges, permissions, or hardware actions.
- No hook configuration preserves current behavior and schemas.
- Existing `PYOCD_SERIAL_FALLBACK_REGISTRY` fixtures retain behavior during migration.
- Normal pyOCD and pyserial inventory remains preferred and fully functional.
- Plans, permissions, validation stamps, assignment invalidation, and memory/flash
  containment behave identically for native- and hook-discovered hardware.

## Acceptance criteria

- A requested board with no attached debug probe is reported as a missing probe, not
  as a board-naming ambiguity, and that response carries the probe hook-contract call.
- The probe `connection_id` has one construction site, and comparison helpers no
  longer depend on defensive normalization to mask differing forms.
- After native discovery failure, the MCP response tells the agent exactly how to
  obtain the hook contract, install/refresh a project-local hook, and retry setup.
- A hook-discovered debugger from any installed pyOCD provider that pyOCD can open by
  the returned UID can complete the same setup, validation, connect, debug, and flash
  paths as a natively listed debugger.
- A hook-discovered UART can complete the same guarded serial paths as a pyserial
  inventory row and is re-resolved before use.
- Discovery-hook failure, backend-open failure, and hardware-selection ambiguity are
  distinct and actionable.
- Hook code and output never grant hardware authority.
- Windows, macOS, and Linux behavior is covered by automated tests and documented
  agent guidance.
- The full existing automated test suite remains green.

## Optional hardware smoke testing

Real hardware is not required for development, CI, or acceptance. When suitable
hardware is available, an optional smoke test may confirm that one hook-discovered
probe UID can be opened by pyOCD and one hook-discovered UART path can be opened by
pyserial. A working native setup is sufficient: tests can deliberately suppress the
native inventory result and feed the same physical device through the hook.

Unavailable hardware or an unavailable host OS must not block completion. Record
optional smoke-test results separately from the required automated test result.
