# Change implementation plan

## Source change list

- Source: `.change-loop/fresh-suite/H04-stale-datasheet-plan/changes.md`
- Goal summary: Make a populated `board_setup-plan` bind the exact bytes of its selected local PDF
  by opting `datasheet_path` into the plan engine's existing generic artifact-digest mechanism, so
  stale or replaced PDF bytes invalidate the plan before either setup action while stable PDFs
  preserve the current setup flow.

## Repository context and assumptions

- Verified architecture and relevant entry points:
  - `src/pyocd_debug_mcp/guardrails/plan_defs.py` owns the declarative `PlanDefinition` for
    `board_setup` / `board_setup-plan`; it already declares `datasheet_path`, paired
    `board_fix_setup`, setup parameter validation, permission, budget, and safety contracts.
  - `src/pyocd_debug_mcp/guardrails/plan_engine.py` already owns the general
    `ArtifactDigestBinding` lifecycle: plan submission resolves and hashes the configured artifact;
    guarded primary and paired execution re-resolve/re-hash it; a missing, unreadable, replaced, or
    changed artifact invalidates and relocks the plan before budget/permission consumption; the
    accepted-plan payload automatically explains the byte binding.
  - `src/pyocd_debug_mcp/server.py` routes both `board_setup` and `board_fix_setup` through
    `_enforce_guarded_invocation` / `PlanEngine.enforce`.
  - `src/pyocd_debug_mcp/tools/setup.py` owns the setup handlers and paired workflow. It should not
    need setup-specific digest state when the plan definition uses the shared binding mechanism.
- Existing test/build commands relevant to the change:
  - focused plan/setup and H04 suites run with
    `.h01-venv-batchstrict/Scripts/python.exe -m pytest` followed by the tester-owned isolated
    spec or regression test paths;
  - deterministic static checks available in the repository include `git diff --check`,
    `.h01-venv-batchstrict/Scripts/python.exe -m compileall -q src`, Ruff, and BasedPyright.
- <!-- Assumption: The intended plan boundary is byte identity, not merely path-string identity.
  This follows the public immutable-plan contract, the H04 REQ-008 oracle, and the charter's duty
  to stop a fallible agent from acting on stale verified assumptions. -->
- <!-- Assumption: Reusing the generic run-scoped digest binding is preferred over copying the PDF
  or adding setup-specific plan state because it is already used by other artifact-bearing plans
  and is the simplest general mechanism that preserves current setup behavior. -->

## Plan items

### CL-001 — Bind setup datasheet PDF bytes at plan acceptance

- **What to change:** Configure the existing `board_setup` plan definition to use
  `datasheet_path` as its artifact binding field and `.pdf` as its contract-derived accepted
  suffix. Do not add a second digest implementation, setup-specific cache, board/vendor allowlist,
  or arbitrary file limit.
- **Where:** `src/pyocd_debug_mcp/guardrails/plan_defs.py`, specifically the existing
  `PlanDefinition` for `board_setup` / `board_setup-plan`. The shared behavior remains owned by
  `src/pyocd_debug_mcp/guardrails/plan_engine.py`; change it only if an automated test proves the
  existing generic primary/paired binding lifecycle is insufficient for this definition.
- **Exact intended behavior:**
  - A populated setup plan resolves the selected local `datasheet_path`, requires it to be a
    regular readable `.pdf`, hashes its bytes, and activates only after that binding succeeds.
  - The accepted public plan response truthfully states that `datasheet_path` bytes are bound and
    that a change requires a replacement plan.
  - Immediately before either `board_setup` or the paired `board_fix_setup` begins, guarded
    dispatch rechecks the same resolved path and SHA-256.
  - If the file is missing, unreadable, replaced at another resolved path, or byte-changed, the
    server uses the existing `plan/artifact-changed` behavior: invalidate the plan, relock both
    setup actions, perform no setup workflow/hardware action, consume no plan execution or
    permission allowance, persist no completed/ready profile authority, and direct the caller to
    submit a replacement plan for the current bytes.
  - If the PDF is unchanged, primary and paired setup behavior, including research continuation,
    applicability proof, evidence capture, validation, safety creation, and successful readiness,
    remains unchanged.
- **Must remain intact:** Exact setup action schema and path string; `max_calls=1`,
  `max_calls_buffer=0`; one-time/full-session permission semantics; the single paired-fix
  allowance; action visibility/locking and assignment checks; existing H04 wrong-family refusal
  and correct-family acceptance; durable datasheet applicability/provenance; behavior of all
  unrelated artifact-bound and non-artifact plans; OS/board/vendor/MCU generality; and the charter
  boundary that validates correctness without hostile-input hardening or paternalistic limits.
- **Objective verification:** Automated tests independently assert:
  1. the `board_setup` definition exposes `artifact_binding_field="datasheet_path"` and
     `artifact_binding_suffixes=(".pdf",)`, and an accepted setup plan's public payload includes
     the standard byte-binding reminder;
  2. changing one byte after plan acceptance makes primary `board_setup` fail through
     `plan/artifact-changed`, leaves both primary and paired actions locked/invalidated, preserves
     execution and permission budget, does not enter the setup workflow, and accepts a newly
     submitted plan for the new stable bytes;
  3. changing one byte after an incomplete primary call but before `board_fix_setup` makes the
     paired call fail before workflow execution and prevents completed/ready authority;
  4. unchanged PDF bytes preserve successful primary and paired setup controls;
  5. wrong-family/correct-family datasheet applicability tests, generic artifact-binding tests,
     strict MCP boundary tests, and adjacent setup/plan regressions pass.

## Out of scope / must not change

- Do not redesign the plan engine, setup workflow, PDF parser/applicability proof, pack resolution,
  profile/safety schemas, permissions, or action budgets when the declarative binding is sufficient.
- Do not add an immutable-copy subsystem, public digest parameter, board/vendor/MCU/OS special case,
  hostile-input guard, arbitrary size/count cap, dependency, or broad documentation rewrite.
- Do not edit firmware, experiment fixtures/evidence, or fresh-test specifications.
- Do not operate hardware or perform flash, erase, reset, unlock, deployment, commit, or push.
- Preserve all pre-existing uncommitted H00–H04 repairs and tester-owned files.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, or generated artifacts.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, and adjacent behavior touched by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, or gate commands.
