# Firmware analysis ownership and verification plan

ROOT scope update, 2026-10-08: Windows x86_64 validation is sufficient for
acceptance. Run no further Linux or macOS checks; those platforms remain
unverified. Both runners, the five tools and all other quality/behavior gates
remain. Historical four-platform planning and evidence is retained as history.

Revision 3, 2026-10-08. Inputs are the supplied spec/approach/principles/model
assignments, this lane's baseline
`a5321d76fc980fa0577c9beb11dc216dc3e3a68f`, and the
[shared contract](code-analysis-contract.md). This plan reserves future writers;
it does not dispatch them or authorize implementation outside a native assignment.
ROOT must accept the contracts, confirm repository contexts and give each worker
its exact files, inputs and completion conditions before parallel implementation.

ROOT owns this documentation-only integration correction after retiring the
contracts writer. It resolves independent findings R1/R2 by making both query
fallbacks and the release-notes/migration obligation explicit. No production
ownership or accepted schema/result semantics changes.

## Stage 0 outcome and dispatch prerequisites

The configuration syntax/selection, static-result and MCP-result meanings,
adapter/service responsibilities, logical runtime boundary and required tests are
defined. No broad implementation has begun. No board, main checkout or sibling
repository was accessed. The native invocation identifies `mcp-contracts`, Codex
`gpt-6.1-sol`, effort `max`, service tier `default`, explicit `permission_mode=yolo`.
The lane inbox initially contained no assignment event; subsequent ROOT messages
were received and acknowledged through the native helper. Exact receipt and
decision evidence is recorded in the verification document and native result.
Automatic hook execution is unproven; ROOT owns that launch diagnosis and any
fix before dependent workers. ROOT subsequently reports a fixed adapter and
fresh automatic-hook proof for future workers (event `90193ede13cd47ffa19af2acc2cea63f`);
this does not prove this original invocation's hooks. No harness/hook files were changed here.

| Prerequisite | Current disposition / ROOT action |
| --- | --- |
| Contract and ownership acceptance | ROOT reviews this committed candidate and distributes exactly its revision/commit. Changes affecting public semantics go through ROOT before dependent edits. |
| Native tool versions/digests | **Unselected**: initially neither tool was on PATH; ROOT later authorized read/execute access to official Windows development candidates clangd 23.1.0 / Cppcheck 2.22.0. Both version probes and ASCII ARM controls ran, but the Cppcheck Unicode-path case failed. Preserve the failure; packaging resolves candidate compatibility, LSP and four-target evidence before any version pin. |
| Physical runtime layout/manifest | **Unresolved packaging prerequisite**: logical resolver/context boundary is fixed. ROOT and packaging must accept exact manifest entries, vendor data/resource paths and relocation evidence before packaging or consumers hardcode a layout. |
| Cppcheck diagnostic-ID/library-model policy | Explicit `missingInclude` enablement and `missingInclude`/`missingIncludeSystem` blocked classification are required by ROOT and reproduced in lane smoke. Complete the eventual pinned-version table and each supported firmware fixture's library setup; no generic vendor-support claim. |
| Command representation/header/config precedence | Native smoke must prove command-only/both-field behavior, inferred header context, standard position encoding, fresh versioned diagnostics and effective `.clangd` selection. Unsupported behavior is an explicit configuration/incomplete result. |
| Other repository contexts | Workspace/installer reservations below incorporate ROOT's original-card decisions; their live source/CI were not inspected here. ROOT confirms native repository contexts and authoritative commands before launching those lanes. No retargeting an active server runtime. |
| Independent validation | Reserve named test files first; give test engineers a separate native worktree and the exact combined candidate. Review is a different assignment. |

Missing program versions/layout do not block this documentation-stage deliverable;
they explicitly block the dependent packaging/support claims. If a native smoke
proves a contradiction requiring changed semantics, escalate through
`.agent-workspace/manager-notify.py --severity blocking`, include the reproduction
and decision needed, and stop at a safe boundary. Do not quietly edit the contract
or choose another model/version to turn a gate green.

## File ownership

There is one writer per path. The following server paths are exact reservations.
Unlisted files need ROOT's explicit ownership allocation before edits. Directory
reservations below cover only new analysis fixtures; they do not authorize writes
to unrelated tests. Existing generic inventory tests need no special-case changes.

| Owner | Exact paths in the Firmware MCP repository | Output / completion condition |
| --- | --- | --- |
| `mcp-contracts` (this lane) | `docs/code-analysis-contract.md`; `docs/code-analysis-implementation-plan.md`; `docs/code-analysis-contract-verification.md`; `docs/contracts/byo-analysis.schema.json` | Reviewable contracts, evidence and this plan; local commit; valid native `RESULT.json`. No production edits. Future contract changes are ROOT-controlled ownership transfers. |
| `mcp-clangd` | New `src/pyocd_debug_mcp/code_analysis_config.py`; `src/pyocd_debug_mcp/code_analysis_runtime.py`; `src/pyocd_debug_mcp/adapters/clangd.py`; `src/pyocd_debug_mcp/services/code_analysis.py`; `src/pyocd_debug_mcp/tools/code_analysis.py` | Shared parser and resolver behavior, owned protocol adapter, service and thin handlers; focused implementer checks pass. |
| `mcp-clangd` | `src/pyocd_debug_mcp/server.py`; `src/pyocd_debug_mcp/sidecar.py`; `src/pyocd_debug_mcp/kernel/operations.py`; `src/pyocd_debug_mcp/kernel/registry.py`; `src/pyocd_debug_mcp/capabilities/matrix.py`; `src/pyocd_debug_mcp/tools/handshake.py` | Five always-visible tools, explicit runtime handoff, correct budgets/check-in blocks and actual lifespan cleanup. No safety-policy change or LSP parsing in server. |
| `mcp-clangd` | `tests/test_clangd_adapter_unit.py`; `tests/test_code_analysis_service_unit.py`; `tests/test_sidecar_runtime.py` | Implementer-owned focused tests and necessary existing context regression extension. Independent files remain separate. |
| `mcp-clangd` | `README.md`; `SERVER_GUIDE.md`; `docs/client-contract.md`; `docs/server-reference.md`; `docs/architecture.md` | User setup/limitations and final public tool documentation, including saved-only inputs, truthful results and recovery. This stage does not change current-behavior docs. |
| `mcp-tests` | `tests/test_code_analysis_config_contract.py`; `tests/test_code_analysis_runtime_contract.py`; `tests/test_code_analysis_protocol_contract.py`; `tests/test_code_analysis_results.py`; `tests/test_code_analysis_availability.py`; `tests/test_code_analysis_freshness.py`; `tests/test_code_analysis_deadlines.py`; `tests/test_code_analysis_lifecycle.py`; `tests/test_code_analysis_real_clangd.py` | Independently designed config, protocol, service, availability/lifecycle and real-program acceptance tests, tied to integrated candidate. |
| `mcp-tests` | `tests/fake_clangd.py`; `tests/code_analysis_support.py`; `tests/code_analysis_fixtures/` (new analysis fixtures only) | Scriptable owned fake LSP peer, reusable MCP client/assertions, host and ARM fixture sources/expected outcomes. No production or implementer-test edits. |
| `mcp-review` | No write ownership | Read-only plan/code/test/evidence review and actionable findings; fixes go to the owning implementer. |

`RESULT.json` and native `.agent-workspace` inbox/outbox records are lane reporting
artifacts, not product source. Do not commit bootstrap overlays, `.codex`, `.venv`,
scratch, monitor output or caches. Current source/plan-tool helpers require no
unrelated changes. Any later package/module declarations or CI wiring are allocated
by ROOT to their owning production file writer before a tester requests edits.

The following exact reservations are **outside this lane**. ROOT must confirm
their repository-local contexts before use; none was inspected here.

| Future owner | Supplied/proposed authored paths | Responsibility |
| --- | --- | --- |
| `cppcheck-workspace` | `.agent-workspace/internal/verify_backend.py`; proposed focused `.agent-workspace/internal/code_analysis.py`; `.agent-workspace/skills-src/common/verify/SKILL.md`; `.agent-workspace/skills-src/common/query-codebase/SKILL.md`; `.agent-workspace/internal/query_backend.py` in the host workspace layout | Python static step/config/scope handling; required Cppcheck after normal/local builds; authored verify and semantic query guidance. Label C/C++ `bin/query` fallbacks as text search; preserve text/Kconfig/map-symbol/no-MCP uses and syntax. Confirm helper path against original card; regenerate views through live tooling. |
| `cppcheck-workspace` | `tests/fixtures/code-analysis/` recursively in the authored workspace repository | Common language-neutral parity corpus, expected outcomes and SHA256 handoff manifest. Python implementer chooses concrete filenames inside this ROOT-reserved directory. |
| `cppcheck-workspace-tests` | `tests/test_cppcheck_acceptance.py`; `tests/test_cppcheck_real_program.py`; `tests/fixtures/cppcheck-acceptance/` recursively in the authored workspace repository | Original-card independent test reservations; not the implementer's common corpus. No proposed replacement paths or implicit transfer. |
| `cppcheck-installer` | `launcher/src/workflow.rs`; new `launcher/src/code_analysis.rs`; `launcher/tests/fixtures/code-analysis/` recursively in installer | Native verification parity and exact ROOT-transferred common corpus copy/consumption. In `tool_query`, label C/C++ `byo workflow tool query` fallbacks as text search; preserve text/Kconfig/map-symbol/no-MCP uses and syntax. No hidden workspace Python. Packaging may not edit `workflow.rs` without transfer. |
| `cppcheck-installer` until acceptance | `launcher/src/main.rs` | Own necessary module declaration for `code_analysis.rs` through Cppcheck-stage acceptance; no concurrent packaging writer. |
| `cppcheck-installer-tests` | `launcher/src/code_analysis/acceptance_tests.rs`; `launcher/tests/cppcheck_acceptance.rs`; `launcher/tests/fixtures/cppcheck-acceptance/` recursively; `release/scripts/test_code_analysis_installed.py` | Original-card independent Rust acceptance, independent fixtures and installed verification script. Module declarations/public access go through the production owner. |
| Packaging implementer after explicit transfer | `launcher/src/main.rs`; `release/scripts/build_release.py` in ROOT-confirmed installer/release context | ROOT transfers `main.rs` diagnostics ownership after Rust-stage acceptance. Existing release builder owns manifests/digests/SBOM/licenses/archive wiring; allocate additional actual inventory files after live inspection. |
| Packaging implementer | New `docs/code-analysis-release-notes.md` in installer | ROOT allocated this path after live-context inspection found no existing release-notes file. Prepare new-candidate notes for mandatory Cppcheck, configuration migration and on-disk-only clangd; keep older releases unchanged and publication separately authorized. |
| Installed independent test roles / ROOT | Existing `cppcheck-installer-tests` installed script above and `mcp-tests` real-clangd/fixture files; new archive/CI files need a ROOT allocation | Run real installed Windows x86_64 coverage independently of packaging implementation/review. Do not take write ownership of another tester's script or invent replacement file reservations. |
| `cppcheck-workspace-review`, `cppcheck-installer-review`, packaging reviewer | No write ownership | Independent review of each stage and final installed evidence. |

The ownership decisions above come from native ROOT events
`230a68f2257e4b598a9cd0b570032c63` and
`8ad72be2b6554ab6977e7763bab50733`. A helper path marked proposed still needs
original-card/live-context confirmation; accepted tester/corpus paths are not
replacements. ROOT supplies authoritative regeneration, Rust registration and
release/CI commands from those repositories. `main.rs` has one writer at a time:
Rust module declaration first, packaging diagnostics only after ROOT's explicit
transfer. Server runtime-resolution changes stay with `mcp-clangd` unless transferred.

ROOT's R1/R2 integration decision additionally confirms both query-runner paths
above and the exact new installer release-notes path. Independent workspace and
installer acceptance tests verify text-search labeling and preserved query uses;
the installed tester and final reviewer verify release-note/migration accuracy.

## Roles and candidate/fixture handoff

Contracts/reviews use Codex `gpt-6.1-sol` at `max`; implementation/packaging use it
at `xhigh`. All Codex launches require service tier `default` and explicit YOLO.
Each fresh independent test deployment starts with Claude Code `claude-opus-5-5`
at `high`, permission bypass. Only an actual usage-limit or OAuth failure from
that deployment permits ROOT to choose Codex `gpt-6.1-sol` at `high` with the same
explicit defaults. Preserve failure evidence and invocation identity, stop the
old process and use a fresh native invocation/branch for a provider switch. No
automatic fallback, sticky fallback, generic startup-error substitution or Qwen
assignment. This plan does not launch any agent or prove provider authentication.

Before fixture preparation, ROOT supplies the accepted contract commit and the
file map above. Test engineers derive cases from required behavior and realistic
failures. Fixture handoff contains sources, intended build/ABI/defines/includes,
platform XML, export procedure, expected source positions/outcomes, generated
header state and optional library-model setup/license. Do not check machine-local
absolute compiler paths into portable fixtures; materialize paths per test host.

Use one portable semantic fixture corpus for both verification runners. At
minimum assign case IDs for clean/defect, missing exe/db/platform, ambiguity,
duplicate/conflicting commands, header/directory/source/zero scope, command-only
quoting, both command fields, local override, information/style, coverage-ID
blockers, malformed XML, exit 1 without findings, exit 0 with findings, timeout,
internal failure and non-ASCII/spaced paths. ROOT transfers the accepted fixture
bytes by commit/artifact and records a corpus digest. The Python implementer
owns the common corpus in `tests/fixtures/code-analysis/`; ROOT transfers the
same bundle to the Rust implementer's `launcher/tests/fixtures/code-analysis/`.
Independent engineers derive additional fixtures in their separate reserved
directories and port harnesses without changing accepted common outcomes.
Corpus changes require a new digest and affected
reruns. Version-dependent raw diagnostic IDs are recorded separately from semantic
case outcomes, never used to hide a failed analyzer result.

A test worktree receives other lanes only through integration. ROOT integrates
accepted implementation and test commits, records the exact combined commit and
dirty-state status, and supplies it to both independent reviewer and test engineer.
Include lockfile digest, program executable/data versions/digests, fixture digest,
build/OS/architecture and installed archive/runtime digest. Review and testing can
examine the same frozen candidate in parallel. Early tests of stubs or fixture
preparation are not final acceptance evidence. Byte changes invalidate affected
evidence; rerun relevant checks on the final candidate after production fixes.

## Gates and test coverage

| Gate / owner | Required cases and evidence | Acceptance boundary |
| --- | --- | --- |
| G0 contracts / ROOT + reviewer | Match supplied decisions to live wiring, strict JSON syntax plus semantic rules, selected database identity, single writers and explicit packaging prerequisites. | Approve contract revision and reservations before dispatch; no claim of product behavior from this stage. |
| G1 unit/contract / independent test engineers | Unknown/wrong/null/duplicate fields, finite numbers/bools, defaults and path roots; missing/ambiguous/same-file/equal-bytes-distinct DBs; canonical entries, command quoting/conflicts; source/directory/header/zero scope; Python/Rust identical records/classification. | Shared fixtures agree, including normal/local override and error/status precedence. Missing configuration/tool/report never passes. |
| G2 protocol / `mcp-tests` | Split headers/body, concatenated/multibyte frames, concurrent out-of-order replies, server requests/refused edits, required capabilities, initialization/encoding; URI decoding and readable/unreadable target ranges; Location/LocationLink/null and hover variants. | Fake peer proves actual framing/conversion/failure handling, with no exposed generic methods or MCP stdout contamination. |
| G3 freshness/concurrency/deadlines / `mcp-tests` | Saved changes, same-file serialization/different-file interleaving, monotonic versions; stale/unversioned/silent/wrong-URI/version diagnostics; forced unchanged revision; source/db/config changes during calls; dependency invalidation; finite request budgets including queue/probe/init; MCP cancellation, late replies, crash/EOF/wedged process. | No false-clean file or mixed revision, explicit affected-waiter errors, no hidden retry, bounded completion/cleanup. Measure monotonic timings, allowing documented cleanup grace rather than resetting phase budgets. |
| G4 availability/compatibility / `mcp-tests` + workspace/installer testers | All five names in discovery and direct calls for no board, no-setup/lite/full, revoked and unvalidated identity, unrelated bad policy, both monitoring profiles; missing clangd/config; unknown override args; check-in boundary, nested dispatch behavior, strict JSON analysis block and separate prompt. Both `bin/query` and `byo workflow tool query` retain text/Kconfig/map-symbol/no-MCP uses and label C/C++ pattern results as text search. | Matrix equals full public inventory; analysis performs no policy write/read, permission/unlock, inventory/probe/hardware call. Existing tools keep response/prompt behavior; text fallback is never semantic proof. |
| G5 real-program compatibility / independent testers + packaging | Real clean/defective C/C++; exact Cppcheck argv/XML/exit, target model and library setup; explicit `missingInclude` group, both include IDs blocked, normal information retained, without-group regression and Unicode-path reproduction. Real clangd overloaded/template definitions, cross-file references/hover, Unicode/CRLF, saved changes and explicit empty diagnostics. Header-context, project/user `.clangd` conflict and literal driver allowlist checks. | Passing `--version` or ASCII-only controls never selects a program version. Retain commands, expected/observed outcomes, diagnostics, executable/data hashes and cleanup evidence. A real ARM firmware fixture must prove ABI/includes/defines; synthetic parsing smoke is partial evidence. |
| G6 repository regression / owning test engineers | Locked environment, focused suites plus relevant full repository suites, formatting/type checks and current inventory/monitor/stdio/sidecar/process tests. Workspace/Rust/release authoritative commands supplied from their own guidance/CI. | All required checks pass on combined candidate; actual failures/skips are listed. Mocks do not replace native or installed gates. |
| G7 installed-product / ROOT-assigned independent test roles | Windows x86_64. No analysis tools on PATH, spaces/Unicode/relocation, real host and cross-target fixture; doctor, queries and verify/local override; bundled cfg/resource headers/dependencies/licenses/digests/SBOM; no source/cache/report archive contamination; update/uninstall preserve config. | Windows artifact evidence proves the active immutable runtime, not launcher/cwd. Missing required Windows artifact/program/data proof remains incomplete. Linux/macOS are unverified, not required gates; run no further checks there. |
| G8 lifecycle / `mcp-tests` + installed tester | Start lazy, no persistent LSP/indexing child for status (bounded version probes are allowed); repeated/idle shutdown, real MCP EOF and cancellation/signal paths, child crash and descendants, all waiters released; marker retention on unconfirmed kill, cleanup on next startup; board cleanup before monitor delivery. | No leftover owned child/blocked waiter; lifecycle is exercised through actual `RegistryFastMCP` and `server.main`, not solely direct service shutdown. Windows ownership needs real Windows evidence; POSIX is unverified and outside this run's acceptance scope. |
| G9 final acceptance / ROOT + independent reviewer/test engineer | Resolve review findings, integrate fixes, rerun affected gates, compare final commit/artifact/corpus identity to reports. Check installer `docs/code-analysis-release-notes.md` describes mandatory Cppcheck, accurate configuration migration and on-disk-only clangd against the delivered candidate. | Product readiness only after G1-G8, required notes/migration and review pass on final candidate. Missing/inaccurate notes prevent acceptance. This contract-stage result does not satisfy future gates. Publication remains separately authorized. |

G1 maps to supplied acceptance checks 1-2; G5 includes 3-4; G2-G3 include 4-5;
G4 is 6; G3/G8 are 7; G7 is 8. Tests record setup failures as blockers or skips
with unmet prerequisite, never as a passing clean analysis.

Native write-preservation checks must distinguish permitted disposable analysis
data from protected project inputs. The service's reports/metadata use
`.firm/code-analysis/`; stock clangd's selected-database-local
`.cache/clangd/index/` shards and generated `.gitignore` are also permitted.
Check every other project file, including generated headers, source, database,
`.clangd` and board policy, for unchanged bytes. Retain the initial stricter
native tree-oracle failure and its explicit contract clarification; final
independent tests and review must validate the corrected boundary together.

### Server commands and local scope

Repository README specifies `uv sync --locked`; `uv.lock` pins MCP 1.28.1.
Active tests import `unittest`; preserved Sentry evidence names full discovery,
Ruff and Pyright as prior checks. Historical results are not evidence for this
candidate, and no lane-local CI directory exists. Candidate checks for the server:

```text
uv sync --locked
uv run --locked python -m unittest discover -s tests -p "test_code_analysis_*.py" -v
uv run --locked python -m unittest discover -s tests -p "test_clangd_adapter_unit.py" -v
uv run --locked python -m unittest discover -s tests -p "test_tiered_core_matrix.py" -v
uv run --locked python -m unittest discover -s tests -p "test_monitor_wiring.py" -v
uv run --locked python -m unittest discover -s tests -p "test_monitor_stdio_lifecycle.py" -v
uv run --locked python -m unittest discover -s tests -p "test_sidecar_runtime.py" -v
uv run --locked python -m unittest discover -s tests -p "test_process_cleanup.py" -v
uv run --locked python -m unittest discover -s tests -p "test_*.py"
uv run --locked ruff check src tests
uv run --locked pyright src
```

The two implementer test files and `test_code_analysis_*.py` do not yet exist;
zero tests is an unmet gate. Real smoke tests must require/log explicit program
identity and report missing programs as skipped/blocked; acceptance jobs must
reject required skips. Supply selected candidate native programs as test-harness
fixtures, not public tool executable overrides. Restrict full-suite runs to
hardware-free cases/fixtures under this task's authority; manual hardware runners
and live board checks remain skipped. Isolate artifact/monitor/temp/process-marker
roots inside the test workspace so imports and subprocesses cannot modify the
operator's project or shared state. Do not run archived snapshot suites as if
they were active tests. External provider or installed-host prerequisites must
be explicitly recorded.

## Integration slices

1. ROOT accepts G0 and allocates files/contexts. Test engineers prepare their
   reserved fixtures while implementers work, with no production writes.
2. Implement shared parsing/runtime resolution seams and verify G1 against the
   accepted fixture semantics. Cppcheck runners can proceed in their own native
   contexts after these interfaces are agreed. Review each slice independently.
3. Implement owned clangd transport/service and fake-peer gates, then five-tool
   registration/check-in/deadline/lifecycle wiring. Pass G2-G4/G8 and relevant G6
   before accepting the server stage. Provide real G5 program smoke rather than
   reporting fake success as semantic-tool readiness.
4. Packaging selects versions/layout only after G5 compatibility, extends the
   existing runtime/release inventory through its single writer, then satisfies
   G7 on Windows x86_64. Program-specific data and diagnostic classifications
   join the pinned evidence; release archives exclude project state.
5. ROOT integrates the final candidate, independently reviews and validates it
   through G9, including Python/Rust parity and installed behavior. Record any
   unsupported fixture and missing check explicitly. No publishing under this card.

Every failure report includes candidate/artifact identity, fixture and program
versions/digests, exact argv/command, exit, stdout/stderr/full report paths,
expected vs actual and reproduction. Implementers fix production code; independent
test engineers rerun after integration. No weakening tests, silently changing
contracts or unbounded retry loop. A worker's passing local suite is evidence for
that slice, not completion of the combined installable product.
