# MCU and toolchain de-bias loop report

Safety cap: six fresh-audit rounds. Terminate earlier only when a fresh audit yields no valid new
criticism. Findings that cannot be fixed honestly without a real backend/parser remain explicitly
deferred rather than hidden behind fake abstractions.

## Round 1 — complete

The complete audit triage, specification, and plan are in
[`debias-round-1-spec.md`](debias-round-1-spec.md) and
[`debias-round-1-plan.md`](debias-round-1-plan.md).

Implemented packaged reviewed-board data, explicit safe-read/attach facts, target-neutral recovery
with a live capability check and legacy read alias, generic-first configured serial fallbacks, and
provider-neutral native-build/collector guidance with a labeled Zephyr fallback.

Verification on 2026-07-17:

- `uv run --locked pytest` — 970 passed, 2 skipped.
- `uv run --locked ruff check .` — passed.
- `uv run --locked pyright` — 0 errors.
- `uv build` — passed; an isolated built-wheel import loaded both packaged registries.
- One bounded test run exercised `collect_build_artifacts` over in-process MCP for a generic native
  bundle and an executed Zephyr fallback bundle; both produced coherent ELF/map roles.

## Round 2

The fresh audit found one valid [TOOLCHAIN] issue: Python prefix/wildcard spelling heuristics were
being treated as part-to-target evidence. The triage, spec, and reviewed plan are in
[`debias-round-2-spec.md`](debias-round-2-spec.md) and
[`debias-round-2-plan.md`](debias-round-2-plan.md).

The heuristic and normalized auto-detection path were removed. Automatic setup now uses only the
exact reviewed catalog mapping; a staged pack may provide that target but cannot redefine it, and
legacy profiles are not target-mapping authority. Focused setup target/inventory/live-commit tests
passed (24 tests), including broad-prefix refusal and connection-before-profile-commit. Ruff and
Pyright are green.

## Round 3

The fresh audit found one valid [TOOLCHAIN] issue: unrecognized probe descriptions defaulted to
CMSIS-DAP. The triage, specification, and adversarially reviewed plan are in
[`debias-round-3-spec.md`](debias-round-3-spec.md) and
[`debias-round-3-plan.md`](debias-round-3-plan.md).

Probe inventory now uses pyOCD's registered runtime provider identity first. The configured CLI
fallback resolves a validated executable/argv without a shell, uses provider-qualified IDs before
configured legacy text aliases, and preserves `unknown` rather than guessing. Board selection and
active-session inventory retain the observed provider identity, and reviewed setup rejects unknown
providers before connect or profile commit.

Verification on 2026-07-17:

- One in-process MCP run exercised the generic API provider, configured CLI fallback, and unknown
  CLI provider through `setup_overview`; the payloads reported `futureprovider`, `stlink`, and
  `unknown`, and the exact configured fallback argv was observed.
- A focused setup test proved `unknown` is refused before backend connect or profile commit.
- `uv run --locked pytest tests/test_probe_inventory.py tests/test_setup_hardware_inventory.py
  tests/test_server_resource_binding.py -q` â€” 32 passed.
- `uv run --locked ruff check .` â€” passed.
- `uv run --locked pyright` â€” 0 errors.

One-line summary: no MCU code changed; pyOCD API inventory remains the generic provider-identity
path, while its CLI inventory is a labeled, parameterized compatibility fallback that no longer
misclassifies unfamiliar probes.

## Round 4

The fresh audit produced 28 findings. The complete triage, accepted specification, and reviewed plan
are in [`debias-round-4-spec.md`](debias-round-4-spec.md) and
[`debias-round-4-plan.md`](debias-round-4-plan.md). Most findings were either target-specific
reviewed data/test evidence, already-correct generic-first fallbacks, or real architectural limits
that cannot be fixed honestly without a second backend/ISA/image/transport implementation.

One new easy [MCU] defect was accepted: fresh setup labeled every silicon identity register
`FICR.INFO.PART`. Live profile commit now uses the neutral `silicon_id` label while preserving the
exact reviewed address, expected value, and mask.

Verification on 2026-07-17:

- An in-process MCP client called `board_setup-plan` all-NULL, loaded the setup tool, submitted a
  valid permitted plan, observed dynamic `board_setup` visibility, and executed `board_setup` with
  fake physical I/O and a temporary FirmStore. The committed profile contained the neutral label
  plus exact unchanged reviewed identity evidence.
- `uv run --locked pytest tests/test_server_resource_binding.py tests/test_setup_tools.py -q` â€” 28
  passed.
- `uv run --locked ruff check .` â€” passed.
- `uv run --locked pyright` â€” 0 errors.

One-line summary: the last executable Nordic register-name assumption was removed from fresh setup;
target-specific reviewed addresses remain data, and larger backend/ISA/parser/transport limitations
remain explicitly deferred rather than hidden behind fake abstractions.

## Round 5

The fresh audit produced 30 findings. The complete triage, accepted specification, and reviewed
plan are in [`debias-round-5-spec.md`](debias-round-5-spec.md) and
[`debias-round-5-plan.md`](debias-round-5-plan.md). The repeated backend/ISA/image/transport and
host-bootstrap findings remain the architectural limits already deferred in Round 4; reviewed
device data, fixtures, and acceptance scripts remain target-specific evidence rather than generic
runtime policy.

One mixed [MCU]/[TOOLCHAIN] defect was accepted: a provider-neutral AP#1 exception claimed an nRF52
and J-Link interpretation. The live pyOCD mapper now gives target-neutral causes and directs the
model to exact setup/validation evidence, with typed recovery only when the server identifies it.

Verification on 2026-07-17:

- One in-process MCP client called visible profile-only `connect` first through an ordinary
  non-J-Link provider session and then through the parameterized J-Link UID-retry fallback. Both
  calls traversed the real target-control service and error mapper, returned identical neutral
  guidance, and the fallback was observed retrying without a UID.
- `uv run --locked pytest tests/test_connections.py tests/test_target_control.py -q` — 29 passed.
- Focused Ruff — passed; focused Pyright — 0 errors.

One-line summary: nRF52-specific error guidance was removed; the existing J-Link compatibility
fallback remains parameterized and now reports the same provider-neutral failure as the generic
path.

## Round 6 — loop termination

The sixth fresh audit and its classifications are recorded in
[`debias-round-6-audit.md`](debias-round-6-audit.md). It found the same real architectural limits:
one pyOCD/SWD/Cortex-style backend, UART text transport, ELF/GNU-oriented safety evidence, and
CMSIS-Pack target support. It also repeated target-specific reviewed data, fixtures, and configured
fallbacks that are specific by design rather than generic runtime policy.

No new criticism survived triage as an honest small runtime fix. Cosmetic interfaces with one
implementation were rejected as fake abstractions; unsafe inferred target facts were rejected; and
second-backend/parser/transport work remains explicitly deferred until it can be implemented and
tested for real.

Termination condition: the configured six-round safety cap. One-line summary: no MCU code was kept
as a newly accepted fallback, no additional toolchain path needed demotion, and the remaining
target-specific material is reviewed evidence, compatibility fallback data, or an explicit
capability boundary.

## Final integrated verification — 2026-07-17

- `uv run --locked pytest` — 978 passed, 2 skipped.
- `uv run --locked ruff check .` — passed.
- `uv run --locked pyright` — 0 errors, 0 warnings.
- `uv build` — source distribution and wheel built successfully.
- An actual stdio MCP client initialized the module server, listed tools, and observed both
  `initialization_handshake` and the live generic `collect_build_artifacts` tool.
- The active versioned contract was intentionally rebased to de-bias round 6, and the preserved M10
  traceability proof locations were refreshed without changing their assertions or hardware state.
