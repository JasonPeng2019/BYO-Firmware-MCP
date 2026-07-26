# H00 Server Repair Request

## Server revision and gate

- Failing suite test: `H00 — Clean-clone reproducibility`
- Server commit under test: `6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876`
- Run evidence:
  `../fresh-experiments/H00_20260723-210704/.agent-workspace/`
- Structural result: `VALID SERVER_FAILURE`

## Verified defects

### 1. The locked development environment cannot run the repository's test suite

Observed from a clean clone at the exact commit:

```text
uv sync --locked                                      -> exit 0
uv run --locked --no-sync pytest --collect-only -q    -> exit 2
error: Failed to spawn: `pytest`
Caused by: program not found
```

Evidence:

- `../fresh-experiments/H00_20260723-210704/.agent-workspace/evidence/09_ordinary_pytest_collect.log`
- `../fresh-experiments/H00_20260723-210704/.agent-workspace/evidence/10_ordinary_pytest_full.log`
- `../fresh-experiments/H00_20260723-210704/.agent-workspace/evidence/31_pytest_declaration_search.txt`

Expected: `uv sync --locked` installs every declared tool needed by the documented clean-clone
verification path, and the complete test suite can be collected and executed without an undeclared
manual installation or lockfile drift.

Minimal regression: in a new clean clone, `uv sync --locked`, followed by
`uv run --locked --no-sync pytest --collect-only -q` and the complete suite, exits zero and records
the full collected count.

### 2. The declared repository Pyright check is red

Observed:

```text
uv run --locked --no-sync pyright -> exit 1, 19 errors
```

The errors are in test scaffolding while `pyright src` is clean. The repair must define an explicit,
honest typechecking scope appropriate to the shipped package and make the documented/default
Pyright command reproducible. Do not suppress production-source errors or add broad ignore rules
merely to obtain green.

Evidence:

- `../fresh-experiments/H00_20260723-210704/.agent-workspace/evidence/08_ordinary_pyright.log`
- `../fresh-experiments/H00_20260723-210704/.agent-workspace/evidence/26_unicode_pyright.log`

Expected: the repository declares its typecheck scope explicitly, the default documented Pyright
command exits zero on a clean clone, and production code remains fully checked.

Minimal regression: after locked sync, `uv run --locked --no-sync pyright` exits zero; a tester
injects or otherwise proves that a real type error under `src/pyocd_debug_mcp` is still detected.

### 3. Clean-clone verification commands are not documented as one reproducible contract

Observed: README documents locked installation but not the exact build/import/Ruff/Pyright/pytest
verification sequence, and pytest is absent from the declared development group.

Expected: a concise contributor/verifier section gives exact locked commands for build,
unrelated-cwd installed import, Ruff, Pyright, and complete tests, including common failure/recovery
guidance. It must be host/path neutral.

Minimal regression: documentation assertions confirm the exact commands are present and agree with
project metadata.

## Exclusions and rejected attributions

- Do not change MCP hardware, plan, permission, setup, flash, debug, safety, serial, or runtime
  behavior. H00 exercised host packaging/testability only.
- Do not add board-, MCU-, OS-, shell-, username-, checkout-path-, toolchain-, or CI-provider-specific
  logic.
- Do not repair the test agent's Unicode console-print command. The server itself installed,
  imported, built, and started from the Unicode clone; the cp1252 failure arose from printing a
  non-ASCII module path in an ad hoc evidence command and is not isolated as a server defect.
- Do not claim or simulate POSIX coverage. The current fixture lacks a usable POSIX runner; that is
  an infrastructure requirement for H00 retest, not a server-code defect.
- Do not weaken, skip, or delete existing tests; do not hide production type errors.
- Do not perform unrelated cleanup.

## Design-charter constraints

Reread `../.codex/design_charter.md` before planning, after planning, before implementation,
between distinct features, before verification, after risky diffs, and before acceptance. Record
each check in this runtime's `DESIGN_CHARTER_CHECKS.md`.

The repair must be the simplest effective repository-contract correction: declare the real test
dependency, explicitly scope typechecking without concealing production defects, and document the
portable clean-clone verification path. It must introduce no environment-specific constants,
speculative framework, defensive limits, or unrelated product behavior.

