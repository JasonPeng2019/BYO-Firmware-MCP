# Contract-stage verification record

ROOT scope update, 2026-10-08: Windows x86_64 validation is sufficient for this
run's acceptance. Run no further Linux or macOS checks; those platforms are
unverified and are not completion gates. The earlier four-target requirements
below record the original contract-stage scope, not the current requirement.
No historical failure, skipped check or missing proof is relabeled as passing.

Date: 2026-10-08. Baseline:
`a5321d76fc980fa0577c9beb11dc216dc3e3a68f`.
Lane: `mcp-contracts`; branch: `lane/cppcheck-clangd/mcp-contracts-run1`.
Native run identity: `515efec0f2cc443a8bac40781795751d`.

This records documentation-stage evidence only. It does not claim that the
proposed tools, Cppcheck changes or packaged native programs exist. The source
tree was unchanged; only the four contract/plan/schema/verification paths named
in the ownership plan are committed. Native reporting overlays and local
environments/scratch remain outside that commit.

## Actual checks

### ROOT clarification after independent integrated review, 2026-10-08

The frozen `9aabd167` review reproduced clangd's native
`<selected-database-directory>/.cache/clangd/index/` writes while source,
configuration, database, generated headers and policy inputs remained intact.
The independent real-program test required every project write to be under
`.firm/code-analysis/`; that was broader than the supplied specification's
permission for local analysis caches. ROOT explicitly clarified the contract to
allow only the documented native index shards and generated `.gitignore` there,
while protecting all other project files. This is a recorded oracle correction,
not a claim that the original strict tree test passed. Background indexing and
the original selected database remain required; no native LLVM fork, database
redirection or filesystem-policy workaround was selected.

The independent reviewer assessed both interpretations and the alternative
native storage-root patch. Its report remains **REVISE** for five other confirmed
production defects. The exact report and boundary appendix are hash-bound in
ROOT's run evidence `root-review-delivery-audit.json`; repaired-candidate testing
and affected review must assess the clarified contract and corrected oracle
together. This clarification supplies no installed or four-platform evidence.

| Check | Actual command / result |
| --- | --- |
| Lane and authority | `git rev-parse --show-toplevel`, `git branch --show-current`, `git rev-parse HEAD`, `git status --short`: assigned worktree/branch and baseline matched. Initial untracked `.agent-workspace/` and `.codex/` were preserved. No lane-local `AGENTS.md`. |
| Invocation/binding | Read lane-local `.agent-workspace/invocation.json` and `harness-hook-binding.json`: Codex `gpt-6.1-sol`, `reasoning_effort=max`, `service_tier=default`, `permission_mode=yolo`; lane/run IDs match. Inbox was initially empty; later native assignments were acknowledged/resolved through helpers as recorded below. No bootstrap or launch was attempted. |
| Locked environment | `uv sync --locked`: exit 0; lane `.venv`, Python 3.12.13, MCP 1.28.1. `uv.lock` and project metadata unchanged. |
| Live lifecycle API | `.venv/Scripts/python.exe -` using `inspect.signature/getsource`: FastMCP accepts `lifespan`; its constructor binds the low-level lifespan wrapper; low-level `Server.run` enters lifespan and cancels handlers on closed transport. This supports the planned integration but does not exercise future clangd shutdown. |
| Existing baseline tests | `.venv/Scripts/python.exe -`, loading the three patterns below into one `unittest.TestSuite`: **12 run, 0 failures, 0 errors, 0 skipped**, exit 0. Test temp/monitor/marker roots were isolated under lane scratch. |
| Windows development native smoke | ROOT explicitly authorized read/execute-only access to clangd 23.1.0 / Cppcheck 2.22.0 outside this lane. Both version probes exited 0. ASCII Cppcheck clean/defect/include controls and clangd ARM `--check` produced the results below; Unicode-path Cppcheck failed. No installed runtime or vendor firmware build was tested. |
| Contract syntax and local links | `.venv/Scripts/python.exe -` with `jsonschema.Draft202012Validator.check_schema/validate` and a local Markdown link/JSON-block scan: exit 0; schema valid, 3 valid and 12 invalid config cases behave as expected, 2 JSON examples parse, 4 local links resolve across 3 documents, fences balance. This validates syntax/examples, not the future semantic parser. |
| Task-owned diff | Final staged `git diff --cached --check`, baseline-relative production unchanged check and allowed four-path staging review are recorded in the native result. Revision-1 checkpoint is `3d982b12aebb5e1af0176b8527d0a2cc81b3affa`; final revision-2 commit is identified in `RESULT.json`. |
| Native result | Lane-root `RESULT.json` is generated from the identity-bound native template; final `.agent-workspace/result-stop-check.py --json` validates it after the local commit. |

The baseline runner used `unittest.defaultTestLoader.discover("tests", pattern=p)`
for exactly these existing active files, then `TextTestRunner(verbosity=2)`:

```text
test_tiered_core_matrix.py
test_sidecar_runtime.py
test_process_cleanup.py
```

Its complete log and summary are retained locally at
`scratch/code-analysis-contracts/baseline-unittest.log` and
`scratch/code-analysis-contracts/baseline-summary.json`; they are excluded from
the product commit. The tests establish current inventory/profile matching,
sidecar explicit-root behavior and owned-process cleanup conventions. They do not
test new analysis tools, independent test cases or Python/Rust parity.

Primary protocol/configuration references in the shared contract were checked
against their official web pages/source on this date. These are documentation
checks, not executable compatibility evidence.

## Native receipt and ROOT decisions

| Native event | Received instruction / disposition |
| --- | --- |
| `8a25c42db2074483ad029d8e30e55581` | ROOT reports 39 existing baseline checks passed; this is ROOT-reported evidence, separate from this lane's 12 checks. Keep package versions unresolved and report hook proof honestly. |
| `218d1604ed7c4ac8a029be315fff9988` | ROOT reports Windows version/ARM smoke and the information-only missing-header gap. |
| `fbfc317836b64e2cacccbea3767cb367` | ROOT confirms `missingInclude` enablement and authorizes the two exact external development executables read/execute only. Contract requires that group and both coverage-blocking IDs. |
| `230a68f2257e4b598a9cd0b570032c63` | Original independent test paths, Python corpus owner and Rust-first `main.rs` ownership restored. This event was blocked while the precise shared-corpus path was missing; the later ownership decision resolves that question. |
| `2abd627dc8fc4a4d89a865b4fcdf35f0` | ROOT received hook warning; ROOT owns diagnosis/fix before dependent launches. No automatic-hook success claim or harness edits. |
| `9287e7d4ff7f452a88982f4172944326` | ROOT received Unicode failure; retains Unicode acceptance and unselected versions, and directs contract completion without reducing product gates. |
| `8ad72be2b6554ab6977e7763bab50733` | ROOT assigns implementer corpus directories `tests/fixtures/code-analysis/` and transferred `launcher/tests/fixtures/code-analysis/`; independent fixture directories remain separate. This resolves the blocking path decision. |
| `90193ede13cd47ffa19af2acc2cea63f` | ROOT reports adapter fix, 45 focused launch/hook/operator checks passed, and eight actual automatic feedback messages in fresh `native-hook-proof` lane for assignment `cb5d21cc9d1944918fef542448985bb6`. This resolves future launches; it does not prove this original invocation's automatic hooks. |

All acknowledgments/completions use `.agent-workspace/lane-queue.py`; no queue was
hand-edited. Native notices record receipt, the hook limitation, native failure
and the resolved corpus decision. Final native result evidence names terminal
assignment states. The blocked history of event `230a...` is preserved and linked
to the completing decision `8ad...`, not rewritten as if no block occurred.

### Hook proof limit

`.codex/hooks.json` registers PostToolUse/Stop wrappers; native `provider_started`
records `features.hooks=true` and `--dangerously-bypass-hook-trust`. The actual
provider transcript reports these warnings (items `item_0`/`item_2` and
`item_1`/`item_3`), preserved exactly in `.agent-workspace/provider-transcript.jsonl`:

```text
Codex is ignoring 1 unrecognized configuration setting. Check for typos or deprecated settings.
  session-flags: `projects."C:\\Users\\Jason\\Documents\\Jason\\FirmCLI_Sentry\\MCP_Server\\BYO-Firmware-MCP\\.harness-runtime\\worktrees\\0b0fe5341a4143ad8ac9dea7bbdbecc1\\mcp-contracts"` is ignored.
`--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run without review for this invocation.
```

Available logs contain configuration/launch evidence, not proof that automatic
hooks executed or delivered the assignments. Manual inbox receipt and any direct
`result-stop-check`/`hook-dispatch --boundary stop` ALLOW are explicitly manual
helper checks. ROOT acknowledged notice `7c7346c6499b4db68f764ec061d93ec0` and
owns native launch diagnosis. No launch/hook files were modified.

ROOT's later event `90193...` reports its paired adapter fix using a top-level TOML
inline `projects` table and fresh native automatic-hook evidence. Those 45 checks
and eight feedback messages were not rerun/observed from this lane; preserve their
ROOT-reported provenance. The original ignored-setting warning and absence of
local automatic-hook proof remain historical facts. Future-worker launch proof
is ROOT-reported resolved, rather than an outstanding fix in this lane.

## Windows smoke results and limitations

ROOT reports official acquisition/archive SHA256 checks; this lane did not repeat
archive verification. It computed the executable hashes below. These are
**development candidates**, with neither version selected for product packaging.

| Program | Actual version output / executable SHA256 |
| --- | --- |
| clangd | `clangd version 23.1.0`, `Features: windows+grpc`, `Platform: x86_64-pc-windows-msvc`; `dbd52c13d21ef9d284f4f0627efe76c81adf2fe0998f230127f554a54fc9dde7` |
| Cppcheck | `Cppcheck 2.22.0`; `1962c7fa80a83c5b447262ac967231fd801e48228a9b91a641864a72b74532d2` |

The synthetic C fixture's database uses explicit ARM cortex-m4/thumb,
`--target=arm-none-eabi`, C11, freestanding and signed-char flags, plus project
platform XML with 32-bit pointer/int/long. Clangd's clean case asserts pointer/long
sizes. Commands were hand-authored for this isolated smoke: no build was run and
no real exported firmware/vendor/library configuration is claimed supported.

All native calls used owned processes, null stdin, captured complete streams,
5-second version / 20-second analysis bounds and lane-local markers. Exact argv,
source/database/platform and reports are retained under
`scratch/code-analysis-contracts/native-smoke*/`; summary is
`scratch/code-analysis-contracts/native-smoke-summary.json`. Zero markers remained.

| Fixture / flags | Actual process result | Contract consequence |
| --- | --- | --- |
| ASCII clean ARM, required enable group | Exit 0; normal `checkersReport` information | Clean control, with information retained. |
| ASCII null-pointer defect, required group | Exit 1; XML `nullPointer`, severity error | Findings cannot pass. |
| ASCII absent generated header, required group | Exit 1; XML `missingInclude`, severity information | Required coverage `blocked`. |
| ASCII absent system header, required group | Exit 1; XML `missingIncludeSystem`, severity information | Required coverage `blocked`. |
| ASCII absent generated header, without `missingInclude` group | Exit 0; only normal checker information, no include diagnostic | Reproduces the false negative; required group is mandatory. |
| Existing clean source under `firmware café`, required group | Exit 1; XML `missingFile` error and critical checker report | **Failed compatibility gate.** Source exists; candidate mishandles this path. Unicode acceptance is retained; ROOT diagnoses candidate before a pin. |
| clangd clean ARM `--check`, explicit database directory, ASCII path | Exit 0, successful parsing/static assertions | Partial real-program evidence; no LSP query/versioned diagnostics or four-target readiness claim. |

Reproduction starts with
`.venv/Scripts/python.exe scratch/code-analysis-contracts/run_native_smoke.py`;
the Unicode run exits 1 at the failed clean-case assertion. The separate `--ascii`
control completed the five Cppcheck calls above, then its initial harness assertion
incorrectly required zero information records in the without-group report.
That harness assertion failed (exit 1); it was corrected to assert absence of
include IDs, keeping valid `checkersReport` information. A subsequent
`.venv/Scripts/python.exe -` checked the retained XML and ran the remaining owned
clangd check, exit 0. Successful Cppcheck samples were not repeated. This correction
does not waive the original Unicode failure or report the initial scripts as passes.

## Failures, corrections and limits

Initial read-only inspection had a PowerShell brace-expansion parse error,
`rg` errors for an absent `.github` directory/literal wildcard, and a console
encoding failure reading the Unicode charter. Explicit paths, bounded searches
and UTF-8 console output corrected those inspections. The global Python's `mcp`
namespace did not contain `mcp.server.fastmcp`; it was not used as SDK compatibility
evidence. The locked lane environment then supplied MCP 1.28.1 for introspection
and the passing baseline checks. These were inspection/setup failures, not hidden
test passes or production fixes.

| Unrun check | Reason / consequence |
| --- | --- |
| Complete native compatibility/version pin | Initial PATH resolution found neither program. Later authorized Windows smoke is partial and has a failed Unicode case; native versions/layout remain unselected prerequisites. Real LSP, relocation/data and all four-target compatibility are still required. |
| New analysis unit/protocol/availability/freshness/deadline tests | This lane is contracts and test-plan only; production modules/tools and reserved independent tests are future implementation. |
| Real cross-target firmware ARM smoke | Synthetic ARM parsing/static-assert smoke ran. An exported real firmware build with correct ABI/includes/defines and optional library models remains a G5 prerequisite. |
| Workspace/Rust verification parity, build and skill regeneration | Sibling repositories are outside lane authority and were not inspected. Their owners must use ROOT-confirmed native contexts, fixtures and authoritative commands. |
| Four-target installed E2E, archive/SBOM/license/relocation/update/uninstall | No candidate analysis runtime/archive exists here. Windows, Linux/glibc 2.28, macOS x86_64/arm64 each remain required G7 rows. |
| Full repository suite, Ruff and Pyright | No production/test Python changed. Three relevant existing suites were run for live-contract evidence; broad/type/style checks are planned implementation gates, not performed for this documentation-only change. |
| Hardware/manual tests, provider launches, release publication | Outside this assignment; none attempted. Native result validity is not proof of other providers, product readiness or publication. |

ROOT resolved the material missing-include argv gap and corpus ownership question
through native messages. Context handoff, budgets, prompt-block compatibility and
idle-child cleanup remain explicit implementation work. Native versions/physical
layout and the Windows Unicode failure remain packaging prerequisites; automatic
hook proof/launch diagnosis belongs to ROOT. Required gates were preserved.
ROOT acceptance of these final bytes is required before dependent dispatch.
