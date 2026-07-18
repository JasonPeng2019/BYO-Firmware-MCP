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

Fresh adversarial audit: pending.
