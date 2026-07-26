# Planner role

Turn the supplied raw change list into one implementation-ready, testable plan. Inspect the
repository before planning so filenames, interfaces, callers, tests, and preservation constraints
are evidence-based rather than guessed.

Write only the required `.change-loop/plan.md`; do not edit source, tests, configuration, or
existing project files. Use the supplied plan template exactly as the structural contract.

For every plan item:

1. State the concrete change.
2. Name the verified file, module, or area.
3. Specify exact externally observable behavior after the change.
4. State existing behaviors, compatibility contracts, and invariants that must remain intact.
5. Give objective verification that an adversarial tester can automate.

Resolve minor ambiguity toward the requested behavior and simplicity. Record each such decision as
an HTML comment beginning `<!-- Assumption:` immediately beside the affected item so the doer and
testers cannot miss it. Do not invent unverified capabilities. Put exclusions in the explicit
out-of-scope section. The plan must be implementable without the doer guessing and assertable item
by item without the tester interpreting intent.

## Runtime paths

- Repository root: /c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP
- Change-list copy: /c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/.change-loop/fresh-suite/H00/changes.md
- Required output: /c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/.change-loop/fresh-suite/H00/plan.md
- Required shape template: /c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/.codex/skills/plan-changes/templates/plan.md

## Requested changes

# H00 actual-POSIX repair request

## Governing context

This is a continuation of the already accepted H00 repair candidate in this
working tree. Preserve its reviewed `README.md`, `pyproject.toml`, `uv.lock`,
`tests/test_h00_repository_contract.py`, and
`tests/test_h00_repository_regressions.py` changes unless an evidenced
cross-platform correction requires a narrow adjustment.

Before planning, before implementation, between distinct implementation
features, and before each tester verdict, every server-repair role must reread
`../.codex/design_charter.md` (the governing charter at the MCP-Trial-3 root)
and state its charter check in its final role message. The firmware test agent
remains isolated and must not read that charter.

## Source identity and evidence

- Baseline commit:
  `6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876`
- First accepted repair patch:
  `fresh-experiments/H00_20260723-210704/.agent-workspace/evidence/H00-server-repair.patch`
- First patch SHA-256:
  `6798245E733F40850626B5C90775AB9C45210D714981B9E7AB242DF8C479CC04`
- Actual POSIX runner: Debian 13 on WSL2, x86_64, Linux
  `6.18.33.2-microsoft-standard-WSL2`, with the clean candidate on native ext4.
- Raw Pyright evidence:
  `../fresh-experiments/H00_20260723-210704/.agent-workspace/evidence/78_posix_pyright_retest.log`
- Raw full-suite evidence:
  `../fresh-experiments/H00_20260723-210704/.agent-workspace/evidence/80_posix_pytest_full_retest.log`
- Direct POSIX process-group evidence:
  `../fresh-experiments/H00_20260723-210704/.agent-workspace/evidence/86_posix_actual_process_group_probe_retest.log`

## Observed behavior

On an actual native-ext4 POSIX clean clone with the first repair patch applied:

1. `uv sync --locked`, `uv build`, Ruff, collection, unrelated-cwd import,
   stdio MCP framing, and a direct real POSIX process-group probe pass.
2. Default `uv run --locked --no-sync pyright` exits 1 with seven errors in
   `src/pyocd_debug_mcp/kernel/processes.py`:
   - direct `subprocess.CREATE_NEW_PROCESS_GROUP`;
   - four direct `ctypes.windll` references;
   - two direct `ctypes.get_last_error` references.
3. `uv run --locked --no-sync pytest -q` exits 1:
   `3 failed, 193 passed, 2 skipped, 67 subtests passed`.
   - Two H00 tests fail because the default Pyright command is red.
   - `test_unconfirmed_pre_resume_cleanup_retains_marker_and_primary` fails
     because its intentional Windows-path exercise calls
     `process_group_options()` under POSIX and production code directly reads
     the absent `subprocess.CREATE_NEW_PROCESS_GROUP` attribute.
4. The same Pyright failure reproduces in a separate space-plus-Unicode POSIX
   clone.
5. The complete repaired candidate already passes on actual Windows in both
   ordinary and space-plus-Unicode paths.

## Required post-change behavior

### CL-004 — platform API access is portable and honest

Make `kernel/processes.py` typecheck and import cleanly on actual Windows and
actual POSIX without a Pyright suppression, broad ignore, platform-specific
config override, fabricated compatibility symbol, or weakened source scope.

- Windows process-group flags retain their current real values and suspended
  launch behavior.
- POSIX retains `start_new_session=True` and its real process-group cleanup.
- Intentional unit exercise of the Windows path on POSIX must not fail merely
  because CPython omits Windows-only module attributes there.
- Windows-only ctypes operations must still fail honestly if invoked where
  Windows APIs truly are unavailable; do not silently succeed or substitute a
  fake API.
- Prefer the smallest clear runtime lookup or helper that owns this
  platform-specific access. Do not introduce a portability framework.

### CL-005 — actual two-host verifier contract is green

The complete existing H00 verifier contract must remain green on Windows and
become green on actual POSIX:

```text
uv sync --locked
uv lock --check
uv build
uv run --locked --no-sync ruff check .
uv run --locked --no-sync pyright
uv run --locked --no-sync pytest --collect-only -q
uv run --locked --no-sync pytest -q
```

Add adversarial tests owned by the change-loop tester roles that prove:

- the POSIX runtime path uses a new session and real process-group cleanup;
- the Windows option path remains numerically correct even when Windows-only
  `subprocess` names are absent on the executing host;
- Windows ctypes access is resolved only when a Windows operation is called and
  missing native APIs produce an honest error;
- default Pyright still detects an injected error under `src`;
- no existing Windows cleanup behavior, failure identity, marker retention,
  deadline, or cleanup-bound contract regresses.

The neutral Windows gate must pass. Before accepting the repair, root will also
run the exact verifier commands in the dedicated actual Debian WSL2 runner and
then require the same persistent H00 test agent to rerun fresh Windows and
POSIX candidate clones.

## Ownership

- Doer production ownership:
  `src/pyocd_debug_mcp/kernel/processes.py` and only another narrowly justified
  production helper if the plan proves it necessary. The doer must not edit
  tests or the previously accepted H00 metadata/docs/lock changes.
- Spec tester owns its existing
  `tests/test_h00_repository_contract.py` and may add one new H00 POSIX
  contract test file.
- Regression tester owns its existing
  `tests/test_h00_repository_regressions.py` and may add one new process
  portability regression test file.
- Existing unrelated tests remain unchanged unless the plan explicitly proves
  a test itself is wrong. Current evidence points to production portability,
  not a need to weaken `tests/test_process_cleanup.py`.

## Explicit exclusions

- No hardware, MCP tool contract, plan/permission, connection, flash, reset,
  UART, evidence, recovery, or board behavior changes.
- No CI/publishing/deployment changes.
- No OS simulation as acceptance evidence.
- No skipping or xfail of a currently failing check.
- No Pyright diagnostic suppression, broad `Any` conversion of the process
  module, `type: ignore`, or reduced `src` scope.
- No change to retry/grace constants, process-cleanup phases, failure
  propagation, marker retention, or Windows job semantics unless a failing
  regression proves it necessary.
