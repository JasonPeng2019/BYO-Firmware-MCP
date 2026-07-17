> STATUS: S2 CORE RUNTIME REVIEW - PASS WITH PARENT-REPOSITORY BASELINE LIMITATION.

# BYO Server S2 Core Runtime Review

## Scope and result

This review covers only the explicitly invoked S2 core-runtime slice. The
standalone tree now contains the headless MCP server dependency closure,
focused server/service tests, a frozen source-contract fixture, and import
isolation checks. S2 is complete. No board profiles, firmware, pack data,
bootstrap scripts, Stage 0/1 flow, R11 harness, lockfile, or standalone product
documentation was added; those remain later slices.

## Dependency closure

- 25 package modules were extracted: server, adapters, guardrails, five shared
  services, and their transitive support modules.
- 22 module files are byte-for-byte source copies. Six copied source tests are
  also hash-identical.
- `pack_provision.py` was pulled into S2 because the unchanged SWD adapter
  imports it. The `packs/` content and pack-data tests remain S3.
- `brain/`, `ux/`, `services/codex_app_server.py`, and
  `services/codex_activity.py` are absent. The import-closure test imports every
  extracted module from the standalone package and rejects those surfaces.
- No parent-relative import, symlink, runtime fallback, environment, cache,
  output directory, or lockfile remains in the destination.

## Intentional divergences

Only three runtime files differ from their recorded sources:

- `server.py` removes `_brain_sync_timeouts` plus its three unused timeout
  update imports. All 20 ordinary decorated tool AST hashes and FastMCP
  description/parameter-schema hashes match the frozen source.
- `timeouts.py` retains the external/setup/startup constants, pyOCD operation
  budgets, `ServerTimeoutConfig`, `default_server_timeout_config`, and
  subprocess timeout diagnostic normalization. Provider, turnkey client,
  timeout-clamp/routing, and brain-sync update APIs are removed.
- `runtime_resources.py` retains checkout/package and R11 benchmark resource
  resolution. Turnkey skills, preloaded-skills, and playbook resolvers are
  removed.

The current source uses `InMemorySessionStore`. This extraction preserves that
implementation; it does not introduce the Redis design mentioned in older
planning material. No unrelated source defect was repaired.

## Verification

- Standalone Ruff check/fix and format passed for 33 Python files.
- Standalone full Pyright JSON analyzed 33 files with zero errors, warnings, or
  information diagnostics.
- Standalone pytest passed all 75 focused server, R10 runtime, target-control,
  UART, symbol, source-contract, and import-closure tests.
- The destination manifest parses and verifies all 38 recorded destination
  hashes, including 34 S2 entries. It reports zero symlinks and zero prohibited
  or generated destination paths after cleanup.
- The repository Python-change gate passed Ruff and full Pyright JSON (159
  files, zero diagnostics). The default ladder also passed Ruff and mypy for
  the 72 root source files.
- Parent-repository pytest remains blocked during collection by the pre-existing
  missing `.codex/skills/firmcli-workflow-core/scripts/frontier.py`. This slice
  did not restore or work around that unrelated workflow defect.
- No hardware, live probe, UART device, provider, or fresh-machine check was
  run. Pre-existing Codex, Claude, and root MCP processes were not used by the
  tests and are audited separately at closeout.

## Verified

- S2 runtime closure, exclusions, exact-copy hashes, approved splits, ordinary
  MCP contract parity, standalone imports, focused behavior tests, static
  checks, manifest integrity, and cleanup are non-hardware verified.
- Root source files and unrelated `markdowns/UX_design/` content were not
  changed by S2.

## Pending verification

- D0 and P1-P3 remain open because direct slice authorization did not decide the
  long-term duplicate-tree, naming, packaging, licensing, or plan-freeze model.
- The next implementation slice is S3: board/firmware/pack/bootstrap and
  Stage 0/1 support. Its data-dependent tests cannot pass from the S2 tree yet.
- S4-S6, independent review, clean-room installation, exact-board hardware,
  live-provider, and fresh-machine verification remain pending.
