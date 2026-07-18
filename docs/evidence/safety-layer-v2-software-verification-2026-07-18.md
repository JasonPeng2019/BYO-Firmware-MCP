# Safety Layer v2 software verification — 2026-07-18

Scope: non-destructive software verification of `docs/safety-layer-v2-spec.md`. No flash, erase,
target recovery, deployment, commit, or push was performed.

Host/tool context: Windows, Python 3.12.13, uv 0.11.19, pytest 9.1.1, Ruff 0.15.21,
Pyright 1.1.411, and MCP SDK 1.28.1.

## Results

- Focused Safety Layer v2 selection — **PASS**, 236 passed.
- `uv run --locked pytest` — **PASS**, 972 passed, 2 skipped, 79 expected legacy warnings.
- `uv run --locked ruff check .` — **PASS**.
- `uv run --locked pyright` — **PASS**, zero errors or warnings.
- `uv build` — **PASS**, source distribution and wheel created.
- Package import — **PASS**.
- Bounded stdio MCP smoke — **PASS**, 39 tools; `board_safety_setup` absent;
  `board_safety_refresh` accepts only `board_id`; initialization guidance names routine-build and
  validation-trigger rules.
- `git diff --check` — **PASS** (line-ending conversion notices only).

## Independent final-diff review

The diff reviews identified four valid defects, all fixed before the final suite:

1. identity-changing refresh could preserve an old live identity proof;
2. flash safety could inspect the saved target instead of an effective live target override;
3. malformed saved profiles could be mistaken for absent profiles during setup-plan acceptance.
4. an artifact that changed after plan approval was refused without budget burn, but restoring its
   old bytes could resurrect the same plan instead of requiring the specified replacement plan.

Focused regression coverage was added for all four before the final full run. Resource-binding
failure now invalidates and relocks the plan without consuming budget or permission.

## Fresh-root MCP model smoke

A separate testing subagent launched the server over stdio with an isolated temporary artifact root
and performed no hardware calls or populated plans. It passed all checks:

- initialized and read `initialization_handshake`;
- confirmed the 39-tool surface and absence of `board_safety_setup`;
- confirmed `board_safety_refresh(board_id)` is the sole public safety-maintenance shape;
- received `setup_no_board` with no routes for the literal no-board answer;
- read schema-derived all-NULL `board_setup-plan` and `flash_application-plan` guidance;
- correctly identified refresh as maximum map recovery but not a routine-build step;
- identified exactly the three validation trigger categories;
- followed build → collect artifacts → flash plan → flash-time containment;
- identified plan-bound artifact-byte checking and the absence of caller ranges or persisted
  authority.

Overall subagent result: **PASS — no returned-contract gap observed**.

## Fresh-root in-process MCP/fake-backend gate

`tests/test_safety_v2_e2e.py` composes the real single-map repository, refresh engine, lean
validator, run-scoped gate, flash plan engine, flash containment policy, and MCP registry around a
recording fake backend. Through an in-process MCP client it proves:

- refresh creates a fresh schema-v2 `memory_map.yaml` as the only safety file;
- validation supplies the live identity proof and map stamp;
- a changed artifact after populated-plan acceptance is refused without budget or backend burn and
  the invalidated plan cannot be resurrected by restoring old bytes;
- a replacement plan for the restored artifact allows one contained application flash through the
  fake backend;
- an out-of-partition HEX plus matching ELF is rejected with no additional backend call.

Result: **PASS**, including the full focused 236-test Safety Layer v2 verification selection.
