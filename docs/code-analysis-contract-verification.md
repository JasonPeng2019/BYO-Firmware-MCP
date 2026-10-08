# Contract-stage verification record

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

| Check | Actual command / result |
| --- | --- |
| Lane and authority | `git rev-parse --show-toplevel`, `git branch --show-current`, `git rev-parse HEAD`, `git status --short`: assigned worktree/branch and baseline matched. Initial untracked `.agent-workspace/` and `.codex/` were preserved. No lane-local `AGENTS.md`. |
| Invocation/binding | Read lane-local `.agent-workspace/invocation.json` and `harness-hook-binding.json`: Codex `gpt-6.1-sol`, `reasoning_effort=max`, `service_tier=default`, `permission_mode=yolo`; lane/run IDs match. Inbox had no queued assignment event to acknowledge. No bootstrap or launch was attempted. |
| Locked environment | `uv sync --locked`: exit 0; lane `.venv`, Python 3.12.13, MCP 1.28.1. `uv.lock` and project metadata unchanged. |
| Live lifecycle API | `.venv/Scripts/python.exe -` using `inspect.signature/getsource`: FastMCP accepts `lifespan`; its constructor binds the low-level lifespan wrapper; low-level `Server.run` enters lifespan and cancels handlers on closed transport. This supports the planned integration but does not exercise future clangd shutdown. |
| Existing baseline tests | `.venv/Scripts/python.exe -`, loading the three patterns below into one `unittest.TestSuite`: **12 run, 0 failures, 0 errors, 0 skipped**, exit 0. Test temp/monitor/marker roots were isolated under lane scratch. |
| Contract syntax and local links | `.venv/Scripts/python.exe -` with `jsonschema.Draft202012Validator.check_schema/validate` and a local Markdown link/JSON-block scan: exit 0; schema valid, 3 valid and 12 invalid config cases behave as expected, 2 JSON examples parse, 4 local links resolve across 3 documents, fences balance. This validates syntax/examples, not the future semantic parser. |
| Task-owned diff | Final staged `git diff --cached --check`, production-path unchanged check and four-path staging review are recorded in the native result. |
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
| Real clangd and Cppcheck compatibility smoke/version pin | Neither program was found by `Get-Command` or `shutil.which`. No native version/digest is selected. Packaging must provide successful real compatibility evidence. |
| New analysis unit/protocol/availability/freshness/deadline tests | This lane is contracts and test-plan only; production modules/tools and reserved independent tests are future implementation. |
| Cross-target ARM native smoke | No native programs/selected fixture compiler were provided. ABI/include/define and optional library-model support remain G5 prerequisites. |
| Workspace/Rust verification parity, build and skill regeneration | Sibling repositories are outside lane authority and were not inspected. Their owners must use ROOT-confirmed native contexts, fixtures and authoritative commands. |
| Four-target installed E2E, archive/SBOM/license/relocation/update/uninstall | No candidate analysis runtime/archive exists here. Windows, Linux/glibc 2.28, macOS x86_64/arm64 each remain required G7 rows. |
| Full repository suite, Ruff and Pyright | No production/test Python changed. Three relevant existing suites were run for live-contract evidence; broad/type/style checks are planned implementation gates, not performed for this documentation-only change. |
| Hardware/manual tests, provider launches, release publication | Outside this assignment; none attempted. Native result validity is not proof of other providers, product readiness or publication. |

No observed server behavior required a policy decision changing the supplied
specification: missing context handoff, budgets, prompt-block compatibility and
idle-child cleanup are explicit planned integration work. Native versions and
physical packaging layout remain the allowed unresolved packaging prerequisites;
ROOT acceptance is required before dependent dispatch. Any future smoke-proven
contradiction requires native escalation, not a silent contract change.
