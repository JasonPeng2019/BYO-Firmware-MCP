> STATUS: S4 R11 BENCHMARK REVIEW - PASS WITH LIVE CLIENT/HARDWARE AND
> PACKAGING PROOF DEFERRED.

# BYO Server S4 R11 Benchmark Review

## Scope and result

This review covers only the explicitly invoked S4 R11 benchmark slice. The
standalone tree now contains the retained Codex-specific R11 evaluation path,
its complete tracked corpus and result contract, the R11 and Stage 1 harness
wrappers, and the approved R10/R11 test closure. S4 is complete. Packaging,
lockfile, public command decisions, and BYO-only operator/product documentation
remain S5.

Ordinary BYO-server use remains model-agnostic: any compatible MCP client may
drive the headless server under its server-owned safety and session rules. R11
is an optional evaluation wrapper on top of that product. It specifically
launches `codex exec`, imposes a frozen case/result/scoring contract, reconciles
the reported final session to a newly created run root, and writes benchmark
artifacts. Those benchmark rules are not presented as the general BYO product
contract.

## Copied closure

- `benchmark_support.py` is byte-identical to the frozen source. Its Codex
  invocation flags, 180-second default provider budget, 1,800-second build
  budget, self-contained prompt rules, scoring rubric, canonical-session
  selection, supporting-session warnings, and artifact names are unchanged.
- All 40 tracked files under `tests/cases/` are byte-identical. They contain 18
  case manifests, 18 prompts, the frozen suites, R11 result schema, corpus
  README, and directory marker.
- `tests/harness/r11_benchmark.py` and `tests/harness/stage1_smoke.py` are exact
  thin-wrapper copies.
- `tests/test_r11_benchmark.py` and all three R11 structured-result fixtures
  are exact copies. `tests/test_r10_runtime.py` was already copied unchanged in
  S2 and remains part of the focused S4 validation set.
- No `r12_turnkey_benchmark.py`, R12 test, turnkey fixture, brain/UX module,
  Branch-C material, provider-memory fixture, skills bundle, or turnkey
  playbook was copied.

The source `benchmark_support.py` docstring calls the module shared R11/R12
support because the parent turnkey implementation imports it. S4 copied the
explicitly requested R11 implementation unchanged; that label did not pull any
R12 harness, test, fixture, or runtime dependency into the standalone tree.

## Standalone isolation

The existing resource resolvers locate the checkout from the installed module,
so the same code resolves `REPO_ROOT` to `BYO-Server/` when imported from the
standalone editable project. No production path rewrite was needed.

The generated S4 isolation test verifies:

- `benchmark_support` and every imported project module come from
  `BYO-Server/src/pyocd_debug_mcp`;
- cases, suites, and the result schema resolve under `BYO-Server/tests/cases`;
- every case workspace source resolves under `BYO-Server/firmware`;
- session output and temporary benchmark workspaces resolve under
  `BYO-Server/runs`; and
- none of those constants equals the parent repo's canonical package, case,
  firmware, or run path.

## Verification

- All 47 copied S4 files are byte-identical. The 40-file case tree and
  three-file result-fixture tree match their source digests.
- Standalone Ruff check/fix and format passed for 55 Python files.
- Standalone full Pyright JSON analyzed 55 files with zero errors, warnings, or
  information diagnostics.
- With the parent project disabled through `uv run --no-project
  --with-editable .`, all 54 focused R10/R11/isolation tests passed and the
  complete standalone suite passed all 203 tests.
- The tests cover case and suite loading, structured-result parsing, scoring,
  authoritative final verification, prompt contracts, timeouts, workspace
  copying, artifact recording, canonical-session reconciliation, platform
  build commands, and parent-root isolation without starting Codex or touching
  hardware.

## Reconciliation and limitations

- The older build-plan Stage 4 prose names Claude Code, while the frozen R11
  spec, current Feature Master, current implementation, and this explicit S4
  prompt name Codex CLI. S4 preserves the implemented Codex-specific path and
  does not claim it is provider-generic.
- The alternate nRF52840 prompts retain their exact source wording, including
  less specialized template language. S4 does not repair or reinterpret that
  source corpus during extraction.
- No live `codex exec`, MCP registration, provider credential, hardware,
  flashing, UART, or fresh-machine check was run. Historical parent proof is
  not relabeled as standalone proof.
- D0/P1-P3 remain open despite direct S4 authorization. The parent repo's
  missing workflow `frontier.py` helper also remains an unrelated baseline
  limitation.

## Verified

- The R11 module/corpus/harness/test closure, exact-copy hashes, Codex-specific
  contracts, model-agnostic BYO boundary, and standalone path isolation are
  non-hardware verified.
- R12/turnkey/UX/Branch-C/provider-memory exclusions and post-test cleanup are
  verified in the destination tree.

## Pending verification

- S5 packaging, independent lockfile, public entrypoints, packaged-resource
  behavior, and standalone BYO documentation.
- S6 clean-room integration plus independent review gates.
- Fresh live Codex, MCP, official-board, alternate-board, and cross-host proof.
- The next implementation slice is S5: finish BYO-only packaging, lockfile,
  public commands, and standalone documentation.
