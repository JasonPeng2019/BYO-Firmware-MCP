# Firmware analysis implementation contract

Contract revision: 2, 2026-10-08. Prepared in the `mcp-contracts` lane against
Firmware MCP commit `a5321d76fc980fa0577c9beb11dc216dc3e3a68f`.

This is the server stage's contract for ROOT acceptance and subsequent lane
handoff. It implements the supplied specification's decisions on paper; it does
not register tools, change verification behavior, select native program versions,
or establish release readiness. ROOT owns integration, launch and fallback
decisions. The [implementation and test plan](code-analysis-implementation-plan.md)
reserves writers and gates; [verification](code-analysis-contract-verification.md)
records what this lane actually checked.

Revision 2 incorporates ROOT's native decisions on explicit Cppcheck
`missingInclude` enablement and original ownership reservations, plus the
Windows development smoke and its Unicode-path failure. Project JSON schema
version remains 1. No native version or four-platform runtime is accepted.

## Authority and live repository evidence

Source paths in the evidence table use the `src/pyocd_debug_mcp/` prefix unless
another prefix is shown. Evidence refers to the baseline commit, so subsequent
changes may move the cited lines.

| Live location | Observed convention and required integration |
| --- | --- |
| `server.py:364` | `_project_root` comes from `BYO_MCP_ARTIFACT_ROOT`, otherwise launch cwd; capture it once for source use. The handshake cannot change it. |
| `sidecar.py:40,202,236` | `RuntimeContext` contains validated project/runtime roots; `_install_runtime_context` clears ambient runtime overrides and changes cwd; `_serve` currently calls `run_server()` without passing the context. Add an explicit analysis-context handoff. |
| `tools/artifacts.py:15` | A factory returns thin typed handlers producing JSON strings. Use the same factory pattern with an injected analysis service. |
| `server.py:7694` and `kernel/registry.py:63` | Discovery/remote-probe tools use ordinary registration, `structured_output=False`, and default visible/unlocked registry entries. Follow this pattern for all five analysis tools. |
| `tools/plans.py:25` | `forbid_unknown_tool_arguments` modifies generated argument models and publishes `additionalProperties: false`. Apply it to every new tool. |
| `capabilities/matrix.py:19,196` and `tests/test_tiered_core_matrix.py` | Public inventory is explicit, including profile-specific rows. Add five metadata rows with family `code-analysis`, `personal_only=False`. |
| `server.py:3690` | `LAYER2_ACTIONS` is assembled from hardware handler groups. Keep the analysis handlers in their own group. |
| `kernel/registry.py:426,456` | Calls have passive observation; the check-in helper currently mutates the first text block. Pass the tool name into that helper and append a separate block for exactly the five analysis names. |
| `kernel/operations.py:297,539,881` | Managed operations record monotonic start time and cancellation; argument timeout grace is 5 seconds. The default 30-second outer timeout currently ignores analysis timeout arguments. |
| `kernel/processes.py:460` | `popen_owned` returns a child and marker, with Windows Job/POSIX group ownership. Confirm termination before marker removal. A session child must not belong to one query's cleanup resources. |
| `kernel/registry.py:663` and `server.py:8127` | Stdio has a private protocol stdout and cancels active operations in `finally`; `main` disconnects boards before monitor closeout. Active-operation cancellation alone cannot close an idle analysis child. |
| `tools/handshake.py:143` | `workspace_path` labels activity records. Add code-analysis guidance without redirecting project authority. |
| `uv.lock`, `pyproject.toml` | Locked MCP SDK is 1.28.1; tests use `unittest`. Its installed FastMCP constructor accepts a lifespan context manager, and low-level `Server.run` enters that lifespan. Register lifespan at construction, not by merely changing settings afterward. |

No lane-local `AGENTS.md` exists. The supplied principles and detailed charter,
and the repository's `design_charter.md`, govern this task. Their portability
guidance does not expand this task beyond the supplied product scope. Workspace
and installer locations quoted below come from the supplied specification only;
their live files were not inspected or edited from this server lane.

## Project configuration and selected build

The only project analysis configuration is optional `PROJECT/byo-analysis.json`.
Never create it automatically. The
[JSON Schema](contracts/byo-analysis.schema.json) is the syntax contract; the
semantic rules below are equally normative for the Python workspace runner,
Rust launcher and Python service. Defaults apply in memory, not by writing a file.

| Field | Type/default | Semantic validation |
| --- | --- | --- |
| `schema_version` | Required JSON integer `1` when the file exists | Reject bool, string, other version or missing field; explain the field and supported version. |
| `compilation_database` | Optional nonempty path string | Existing readable file named exactly `compile_commands.json`; relative to canonical project root. |
| `cppcheck` | Optional object, default `{}` | Reject unknown nested fields and wrong types. |
| `cppcheck.platform_file` | Optional path string | Required and readable target platform XML for full firmware verification. Its absence does not by itself block clangd. |
| `cppcheck.suppressions_file` | Optional path string | When supplied, require a readable file for Cppcheck. No generated suppressions. |
| `cppcheck.enable_style` | Boolean, default `false` | No string/numeric coercion. |
| `cppcheck.timeout_seconds` | Number, default `120` | Positive and finite; reject bool, NaN, infinity and overflow to infinity. |
| `clangd` | Optional object, default `{}` | Reject unknown nested fields and wrong types. |
| `clangd.query_driver_paths` | List of exact path strings, default `[]` | Canonical existing compiler executables; no globs, command strings or shell expansion. |

An absent file uses version-1 defaults and database discovery. An existing file
must be a JSON object: reject malformed JSON, duplicate keys, `null` sections,
unknown fields and non-JSON numbers with the offending field and a remedy.
Syntax/schema errors affect all consumers. Analyzer-specific resource checks
affect that analyzer: a missing Cppcheck platform blocks verification; an invalid
clangd driver blocks semantic queries. Report other configured resource problems
in status rather than pretending they affect the other backend. JSON Schema
cannot replace strict parser-type and finite-number checks.

Example, written deliberately by the project owner:

```json
{
  "schema_version": 1,
  "compilation_database": "build/compile_commands.json",
  "cppcheck": {
    "platform_file": "config/cppcheck-platform.xml",
    "suppressions_file": "config/cppcheck-suppressions.txt",
    "enable_style": false,
    "timeout_seconds": 120
  },
  "clangd": {"query_driver_paths": []}
}
```

### Database algorithm

1. Canonicalize the launch-owned project root. Resolve configured paths from it,
   never runtime cwd. Do not expand shell variables or infer a board directory.
2. If explicitly configured, select that file or report its failure. Never fall
   back from a broken explicit choice. Otherwise inspect only
   `PROJECT/compile_commands.json` and `PROJECT/build/compile_commands.json`.
   Collapse aliases of the same existing filesystem file, including symlinks
   and hardlinks where identity is available. Zero files means missing; two
   distinct files mean ambiguous even if their bytes are equal. A malformed
   existing candidate remains a configuration problem, not an excuse to ignore it.
3. Read the selected file as a stable snapshot, record its canonical path and
   SHA-256 of original bytes, and parse its array of command objects. An empty
   array cannot configure a useful analysis. Reject malformed entries with index
   and field. Recheck identity/hash before returning an analysis result.
4. Resolve an entry's relative `directory` from the canonical project root;
   resolve `file` from that entry's canonical working directory. Validate the
   working directory and source. Absolute paths keep their meaning. Preserve
   include paths, defines, argument order, optional `output` and other entry data.
5. Accept a nonempty `arguments` list of strings or a nonempty `command` string.
   Prefer `arguments` if both exist, as in the compilation database format.
   Command text remains opaque to the shared selector: no whitespace splitting,
   shell execution or invented flags. Native readers must demonstrate matching
   handling of entries containing both fields and command-only quoting in the
   parity smoke gate before that representation is declared supported.
6. Group by canonical source identity. Identical working directory and identical
   command representation can be deduplicated; argument order is significant.
   Different working directories, argv or command strings are conflicting.
   Do not guess equivalence between an argv entry and a different command-string
   entry: require a consistently exported database for one configuration. A
   conflicting command anywhere blocks selection for both analyzers, including
   when the requested scope contains only a different unit.
7. Both backends receive this selected database identity. The Cppcheck scope
   filter may write a private database; it preserves working directories and
   command data and reports the original database path/hash separately. Clangd
   receives the selected original database directory; no recursive search or
   generic-host fallback is permitted as authoritative analysis.

The relative-directory convention applies the supplied project-root rule;
absolute `directory` entries remain preferred. Cross-language fixtures must
prove that both native programs interpret the resulting entries consistently.
If the original database cannot satisfy this convention without silently using
different commands, return a configuration problem and request re-export with
absolute directories/argv. Do not mutate the project database.

This covers one selected build, not all board, macro or ABI combinations. CMake
guidance adds `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` to the project's existing
native configure command; build with its documented native command. Other build
systems must export commands through their supported tooling. Queries and skill
guidance do not configure or build implicitly. These decisions follow the supplied
specification and the [compilation database format](https://clang.llvm.org/docs/JSONCompilationDatabase.html).

### Cppcheck scope

Resolve `verify [path]` from the project root. `.` selects all database units; a
source selects its canonical unit; a directory selects units underneath it using
path components, not text prefixes. A header selects all units in the selected
build and explicitly reports that expansion. Zero selected units is `blocked`.
Report requested path, actual scope kind, selected unit paths and count. Native
build/local-override handling and existing memory obligations run before static
analysis so generated inputs can exist. Build failure remains failure even if
static analysis succeeds.

### clangd configuration and driver boundary

Read configuration and database identity on every call, including status. Session
identity includes canonical project/database paths, database/config hashes and
presence, resolved program identity and driver paths. Track relevant `.clangd`
and applicable clangd user configuration changes; preserve their bytes.

Validate `CompileFlags.CompilationDatabase` with actual fragment conditions and
precedence. A path selecting another database, `None`, or effective ancestor
discovery selecting another build is `not_configured`. An unknown effective
selection cannot establish authoritative coverage. Do not disable configuration
globally, rewrite `.clangd` or override a proven conflict silently. Retained
compile-flag changes, external/background index settings and inferred header
context must be disclosed. The pinned-program smoke must prove database
selection in the presence of both project and user fragments. The
[clangd configuration rules](https://clangd.llvm.org/config) govern their precedence.

Build `--query-driver` only from validated exact paths. Its native syntax is an
allowlist of patterns, so also verify that comma separators and pattern
metacharacters cannot broaden a literal path. If a literal cannot be represented
exactly by the chosen version, report the entry and remedy rather than widening
it. Empty allowlist grants no compiler execution. This configuration intentionally
permits those exact compilers to discover includes. Do not expose compiler,
executable, flag or protocol overrides in MCP arguments.

A source query requires its database command. A header requires an explicitly
reported inferred context from this database, with the originating unit when
known. Generic host flags are `not_configured`. Unknown target flags, missing
generated headers or known partial parsing produce configuration warnings or
`incomplete`, never firmware-coverage claims. Respect existing library models;
supported vendor fixtures need a documented and tested model setup before any
claim that those projects are supported.

## Runtime resolution boundary

Freeze the logical interface, not an untested native version or guessed physical
bundle layout. Add `code_analysis_runtime.py` as the single Python resolver for
both programs. Workspace/Rust consumers implement the same resolution behavior
and conformance cases in their own authorized repositories; MCP must not import
hidden workspace Python.

| Boundary value | Required meaning |
| --- | --- |
| `AnalysisRuntimeContext.project_root` | Canonical, fixed launch-owned project root. |
| `runtime_root` | Canonical active immutable runtime from validated sidecar context, or `None` for source/development. |
| `managed` | Explicitly derived from launch mode, never a tool argument or a guess from executable location. |
| Resolved program | Program name, canonical executable, executable identity/hash, version/probe outcome, origin `managed_runtime` or `path`, required data locations and problems. |

Sidecar must deliver these values before constructing/using the service, through
an explicit function/constructor argument. A small `configure_analysis_runtime`
handoff before importing `server.py` is compatible with its current import-time
composition; `server.main` may also accept an explicit context and preserve its
zero-argument development entry. Do not infer installed roots from cwd,
`sys.executable`, installer directory or a new ambient environment variable.

Managed launches resolve manifest-listed executables/data beneath the validated
active runtime. Missing/unusable bundled files are visible failures, with no PATH
substitution. Source/development can use a supplied managed runtime first, then
PATH; expose fallback origin and actual paths. Tool calls never install/download.
Packaging must supply the manifest mapping and preserve each vendor's required
resource/configuration layout. Its exact manifest extension, relative paths,
versions, digests and license inventory are **packaging prerequisites**, pending
successful compatibility smoke and ROOT acceptance; dependent packaging work
cannot guess them from this document. The adapter consumes resolved paths and
does not own discovery. User config survives update/uninstall.

Use owned bounded `--version` probes, caching by canonical executable identity
and changed bytes/metadata. Status's clangd probe has a 5-second spawn/probe
budget plus confirmed child cleanup; it never initializes LSP or indexes. A
nonzero/timeout probe is unavailable evidence, not proof of usability. A passing
version probe does not replace real analysis smoke. Cppcheck records its probe
and run identity in the verification evidence.

## Cppcheck static-result contract

The existing final line and exit convention stay `VERIFY: PASS`/`VERIFY: FAIL`.
Static analysis has its own `status` enum; only `pass` permits full verification
to pass, and earlier failures cannot be erased. This is not a new MCP tool or skill.
Project settings stay separate from the compiled workflow pack's `CompiledVerify`
schema; add no fields there and preserve verify syntax and loader invocation policy.

Use explicit argv with error checking and
`--enable=warning,performance,portability,information,missingInclude` (append `style` only when
enabled), `--error-exitcode=1`, `--xml`, `--xml-version=2`,
`--project=<selected-or-filtered-db>`, `--platform=<explicit-target-xml>` and only
configured `--suppressions-list=<file>`. No default `--force`, premium/MISRA checks,
automatic suppressions or repair/retry. Required library data/setup is documented
and exercised with the pinned executable. The
[Cppcheck manual](https://cppcheck.sourceforge.io/manual.html) documents project
import and platform setup; the [CLI reference](https://github.com/cppcheck-opensource/cppcheck/blob/main/man/cppcheck.1.xml)
is the reference for flags, not a substitute for compatibility evidence.

The explicit `missingInclude` group is required by ROOT's native smoke decision
(events `218d1604ed7c4ac8a029be315fff9988` and
`fbfc317836b64e2cacccbea3767cb367`). With the candidate Windows Cppcheck 2.22.0,
`information` alone omits missing-header diagnostics and exits 0. This lane's
ASCII control independently reproduced that gap and the corrected XML/exit
behavior. The implementation must not infer coverage from that silent exit.

| State | Evidence/rule |
| --- | --- |
| `pass` | Exit 0, valid complete XML-v2 report, nonzero selected units, usable target settings, no failing findings and no known coverage blockers. Information is still shown. |
| `findings_failed` | Valid report explains error/warning/performance/portability findings, or style when enabled. Findings fail even if process exit is 0. Exit 1 must be explained by report evidence. |
| `blocked` | Missing executable/database/platform, invalid project selection or known target/library/include/analysis-limit coverage problems. Give setup action and retain any findings. |
| `execution_error` | Spawn failure, timeout, internal failure, invalid analyzer input, malformed/missing usable report or unexplained nonzero exit. An exit 1 alone is insufficient. |

If conditions coexist, execution failure takes precedence over coverage blockers,
then findings, then pass; configuration failures discovered before execution are
blocked. Preserve every diagnostic regardless of final classification. Information
severity alone does not classify a coverage problem: maintain a tested diagnostic
ID table against the eventual pinned version. Unknown/anomalous diagnostic IDs
that may signal lost coverage must not be silently accepted; record the issue
and resolve its classification with version-specific evidence.

Required initial diagnostic cases, backed by the development smoke:

| ID / evidence | Classification obligation |
| --- | --- |
| `missingInclude` | `blocked`, including severity `information` and exit 1; absent generated/project header prevents promised coverage. |
| `missingIncludeSystem` | `blocked`, including severity `information` and exit 1. Do not silently waive it because the native message mentions standard-library modeling. |
| `nullPointer` | Error finding: `findings_failed` with valid XML and explained exit 1. |
| `checkersReport` with normal active-checker counts | Display as information; it does not by itself fail an otherwise complete exit-0 run. A critical-error report is not clean evidence and must be explained/classified. |

Exit 1 explained solely by a known coverage-blocking ID is `blocked`, not an
unexplained-exit execution error. Test both include IDs with the explicit enable
group and keep the without-group false-negative case as a regression fixture.
These cases do not finish the pinned-version classification table.

Both runners emit the same static-step record fields (JSON evidence file even
when their normal display remains text):

| Field | Meaning |
| --- | --- |
| `status`, `backend` | Enum above; backend `cppcheck`. |
| `code`, `message`, `remedy` | Required for non-pass; stable code and actionable explanation. |
| `project_root`, `compilation_database`, `compilation_database_sha256` | Canonical root and original selected database identity; unresolved database values are `null`. |
| `executable`, `version`, `executable_origin` | Resolved program evidence or `null` before resolution. |
| `platform_file`, `scope` | Explicit target or `null`; scope `{requested_path, kind, translation_units, selected_count}` with actual expansion. |
| `argv`, `timeout_seconds`, `process_exit_code` | Actual argv, positive finite budget, exit code or `null` when no process completed. |
| `diagnostics`, `coverage_reasons`, `warnings` | Full parsed findings, explicit incomplete-coverage reasons, explanatory warnings; no dropped records. |
| `report_directory`, `display_truncated` | Unique evidence path and whether the terminal summary was shortened. |

`scope.requested_path` preserves the requested string (default `.`); `kind` is
`all`, `source`, `directory` or `header_all`; `translation_units` contains
canonical selected source paths in stable database order, and `selected_count`
matches their count. `diagnostics` contains objects with `id`, `severity`,
`message`, `verbose_message` (string or null), `inconclusive` (boolean) and
`locations` (ordered `{file, line, column}` objects). File paths are resolved
against the analyzed entry context when known; unavailable line/column values
are null, otherwise 1-based integers. If an XML location cannot be resolved,
preserve its reported path and warn; full XML is always retained. `coverage_reasons`
and `warnings` are lists of strings. Unspawned argv is `[]`; unresolved program
fields and uncompleted exit codes are null. Signal termination has a null exit
code and an explicit reason, never a findings-only classification. Compare
cross-runner semantic fields with temporary path/run-ID substitutions recorded
by the shared fixture harness; do not require identical absolute temp paths.

After build handling, one monotonic deadline covers spawn and analysis, followed
by bounded child/group cleanup. Save complete stdout, stderr, XML report, actual
argv and structured result under `.firm/code-analysis/reports/<unique-run>/`.
Private filtered databases can live in that run's private working area. Do not
change input databases. Concise summaries include file/line/diagnostic ID and full
report path. Evidence-write failure cannot be a pass.

## MCP surface and result envelope

Five ordinary tools, independent of `board_id`, with saved-files-only descriptions:

| Name / `operation` | Arguments (exact names/defaults) |
| --- | --- |
| `get_code_analysis_status` / `status` | None. |
| `find_code_definition` / `definition` | `file_path: str`, `line: int`, `column: int`, `timeout_seconds: float = 30`. |
| `find_code_references` / `references` | Position arguments above; `include_declaration: bool = false`, `max_results: int = 100`. |
| `get_code_hover` / `hover` | Position arguments above. |
| `get_code_diagnostics` / `diagnostics` | `file_path: str`, `max_results: int = 100`, `timeout_seconds: float = 30`. |

Timeouts are positive finite numbers excluding bool. Integers/bools/strings use
strict validation; reject invalid positions and limits outside `1..1000` without
coercion. Unknown arguments use the existing helper. SDK/schema refusals remain
normal MCP validation errors before service execution; completed service calls
return the envelope below. No root, runtime, executable, tier, command, flag,
unsaved-buffer or LSP-method argument is accepted.

Paths are existing local source/header paths, relative to project root or absolute;
explicit external SDK headers are allowed. Reject URI/network input. Return
local file URI results with correct percent decoding and Windows drive handling;
never fetch a non-local result URI. Preserve it with unavailable position mapping.

Positions are 1-based Unicode code points. A cursor at line end (`length + 1`)
is valid; beyond it or outside the document is invalid. Range ends are exclusive.
Keep the exact decoded saved text used for LSP synchronization and conversion,
preserve CRLF, and hash original bytes. No universal-newline conversion may alter
offsets. Use lossless decoding; if the source cannot be decoded for the supported
backend, explain it rather than inventing positions. Test accented, CJK, non-BMP,
combining characters, tabs, CRLF and line-end/EOF positions. Standard LSP encoding
negotiation is separate from this public coordinate convention.

Handlers return one JSON string; registration uses `structured_output=False` and
annotations `readOnlyHint=True`, `destructiveHint=False`, `idempotentHint=True`.
The MCP response keeps the analysis JSON in one intact text block; any due
monitor check-in is a separate block, for these five names only.

```json
{
  "status": "not_found",
  "backend": "clangd",
  "operation": "definition",
  "project_root": "/project",
  "compilation_database": "/project/build/compile_commands.json",
  "compilation_database_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "source": {
    "path": "/project/src/main.cpp",
    "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "document_version": 3
  },
  "index_completeness": "unverified",
  "data": {"locations": [], "returned_count": 0, "observed_count": 0, "truncated": false},
  "warnings": []
}
```

This is a completed empty definition query. Every envelope has these common fields.
Database/hash are `null`
when unresolved. Omit `source` for status and before a query's source snapshot
exists; after reading, `source.document_version` can be `null` until synchronized.
Successful queries require a positive integer version and exact source hash.
`warnings` is a list of explanatory strings. `index_completeness` remains
`unverified` in v1; per-file idle cannot establish global index currency.

Operational failures (`unavailable`, `not_configured`, `incomplete`, `error`) add
`code`, `message`, `remedy`; completed `not_found` is not an execution failure.
Stable code groups are
`analysis/config-invalid`, `database-missing`, `database-ambiguous`,
`database-invalid`, `database-conflict`, `clangd-config-conflict`,
`driver-invalid`, `compile-context-missing`, `executable-unavailable`,
`invalid-argument`, `timeout`, `source-changed`, `configuration-changed`,
`diagnostics-unversioned`, `diagnostics-missing`, `capability-unsupported`,
`protocol-error`, `child-exited` and `cleanup-failed`, all prefixed `analysis/`.

| `status` | Rule |
| --- | --- |
| `ok` | Completed request for stated saved revision; diagnostics may contain issues. It never means verification passed. |
| `not_found` | Completed empty definition/references/hover result. No proof of unused/dead code. |
| `unavailable` | No usable clangd; restore supported runtime or PATH only for development. |
| `not_configured` | Missing/ambiguous/invalid compilation settings or header context; export/select/fix the actual build. |
| `incomplete` | Deadline, source/config change, missing fresh diagnostics, required behavior unsupported or known partial result. Do not describe it as clean. |
| `error` | Execution/protocol/crash/cleanup failure; include bounded stderr tail. A later independent call can start fresh. |

Status resolves all available information without starting LSP. If the executable
is unusable return `unavailable`; otherwise configuration problems yield
`not_configured`; otherwise `ok`. Preserve all problems in `data` regardless of
primary status. `data` includes `executable`, `version`, `executable_origin`,
`runtime_root`, `configuration_problems` (objects with `code/message/remedy`,
optional `field`), `session_state` (`not_started`, `initializing`, `ready`,
`restarting`, `failed`, `closed`) and `index_limitations`. Report an existing
session failure/state without starting a replacement. Status can mark a stale
session invalid, but it does not restart it.

### Operation data

| Operation | `data` shape and normalization |
| --- | --- |
| Definition | `locations` from Location, Location array, LocationLink array or null; all forms preserve target URI/range and link selection/origin ranges when present. |
| References | `locations`, `returned_count`, `observed_count`, `truncated`; limit after observing the backend list. Do not claim the observed count is every possible reference. |
| Hover | `contents` normalized as `{kind: "markdown" or "plaintext", value: string}` or null, optional mapped range. Normalize legacy MarkedString forms without interpreting instructions. Documentation is untrusted source text. |
| Diagnostics | `diagnostics`, counts/truncation; each includes severity (LSP 1..4 or null), message, code (string/integer/null), source (string/null), range and related locations. Explicit version-matched empty notification is `ok` with zero counts. |

Definition may also emit counts as in the example; it has no caller-supplied
limit. Normalized locations carry `uri`, `path` (canonical local path or null),
`position_mapping` (`available` or `unavailable`), mapped `range` or null,
`raw_lsp_range` and `lsp_position_encoding`. A mapped range is
`{start:{line,column}, end:{line,column}}`; raw LSP coordinates keep their
zero-based `line/character`. Preserve link ranges using the same paired
mapped/raw convention. If target bytes cannot be read/mapped, keep the raw
range/encoding and warning; do not substitute guessed columns. Diagnostic ranges
use the synchronized source text; related locations use the location convention.

References/diagnostics `max_results` limits display only. Counts describe the
observed backend response before slicing; retain backend order. `truncated` is
true exactly when the service withheld observed items for the requested limit.
If the backend itself is known to have capped a result, additionally return
`incomplete` and describe that cap. Do not make a global completeness assertion.

## Adapter/service interface and lifecycle

Use synchronous, thread-safe service entry points to fit the current managed
dispatch path. Public handlers own schemas/JSON serialization; the service owns
configuration, saved revisions, deadlines, session state and normalization; the
adapter owns child/protocol/encoding mechanics. These names are the handoff API,
not implemented definitions:

```text
code_analysis_config.load_analysis_config(project_root) -> AnalysisConfigSnapshot
  config presence/hash, selected database path/hash, canonical command entries,
  cppcheck options, exact drivers, typed problems with code/message/remedy
code_analysis_runtime.resolve_analysis_program(context, name, deadline)
  -> ResolvedAnalysisProgram
adapters.clangd.ClangdAdapter(program, config_snapshot, marker_store)
  start(deadline, cancellation), request(method, params, deadline, cancellation),
  notify(method, params), wait_diagnostics(uri, version, deadline, cancellation),
  cancel(request_id), shutdown(deadline), capabilities, position_encoding,
  stderr_tail; continuous reader routes pending replies/notifications
services.code_analysis.CodeAnalysisService(context, adapter_factory)
  status(), definition(...), references(...), hover(...), diagnostics(...),
  shutdown(); the four query signatures match the MCP argument table
tools.code_analysis.CodeAnalysisToolServices(service)
tools.code_analysis.build_code_analysis_handlers(services) -> dict[name, handler]
```

No generic request API crosses the tool boundary. Typed failures can carry
partial evidence internally; the service chooses the public envelope/status.
Private dataclasses may stay in their owning module: no plugin framework or
board-session abstraction is needed.

1. One lazily started child per server/project/configuration. `status` and server
   boot create no child. Serialize initialization/restart; different documents
   share the transport and unique request IDs. Use `popen_owned` with explicit
   binary stdin/stdout/stderr pipes; continuously drain a bounded stderr tail.
   Never attach the persistent child to per-operation resources or MCP stdout.
2. Implement Content-Length byte framing, split/concatenated frames, serialized
   writes, UTF-8 JSON and a continuous reader routing out-of-order replies and
   notifications. EOF or malformed transport resolves every pending waiter with
   a typed failure. Own/close pipes, threads and process markers once.
3. Initialize with canonical `rootUri`/workspace folder and selected database
   directory (`initializationOptions.compilationDatabasePath`), required query
   and synchronization capabilities and standard position encodings. Negotiate
   `general.positionEncodings` (`utf-8`, `utf-16`, `utf-32` supported); honor the
   server selection, default to standard UTF-16 if omitted, reject unsupported
   selections. Send `initialized` only after successful initialize. Do not rely
   solely on deprecated clangd `offsetEncoding`.
4. Handle required server requests explicitly: return the fixed workspace
   folder/configuration, answer message/progress requests without execution;
   advertise only capabilities actually supported. Reject unsupported dynamic
   registration cleanly. `workspace/applyEdit` returns `applied: false`; unknown
   requests receive MethodNotFound. Never run a server-supplied command.
5. Read saved bytes, hash/decode losslessly, then serialize synchronization and
   query for that canonical document. `didOpen` and full-content `didChange`
   use monotonic document versions for the session. Same-file calls cannot mix
   revisions; versions cannot be reused after close/reopen in that session.
6. Diagnostics advertise `publishDiagnostics.versionSupport: true` and wait for
   the exact URI/version in the current session generation. If needed send a
   new full-content `didChange` with top-level `wantDiagnostics: true`, even for
   unchanged saved bytes. Old, unversioned, silent or other-file notifications
   are not empty success. Cache only current explicit versioned notifications.
7. One deadline starts at managed operation entry (`started_at + requested
   timeout`), covering configuration work, queueing, probe, initialization,
   synchronization and query. Outside managed dispatch, start at service entry.
   The outer resolver uses the valid requested/default query budget plus
   `ARGUMENT_TIMEOUT_GRACE_SECONDS` (currently 5), allowing existing cleanup;
   invalid inputs still reach strict validation. Do not reset budgets at each
   phase or mark analysis non-interruptible. Honor managed cancellation while
   queued/waiting; send `$/cancelRequest` for a pending request and wake it.
8. Timeout is `incomplete`, with no success retry. Discard late replies. If the
   transport is wedged, terminate the owned group, fail all affected calls
   explicitly and leave state `failed`; only the next independent query starts
   fresh. After a crash, complete every pending waiter. Do not restart from a
   reader callback or hide an error with a retry loop.
9. On config/database/program change, close admission to the old generation;
   explicitly cancel/drain its active calls with `configuration-changed`, then
   cleanly terminate and allow the new generation. Capture generation with each
   waiter/diagnostics key. Recheck source and configuration hashes before return;
   changed bytes yield `incomplete`, not a result for the new bytes. Track known
   on-disk dependencies/configs and invalidate diagnostics when they change.
   Always disclose that closed headers/global background index may still be stale.
10. Register an idempotent service shutdown in the actual FastMCP lifespan's
    `finally`, shielded from server cancellation, and call it from `server.main`
    cleanup. Attempt LSP `shutdown`/`exit` within the existing 5-second argument
    grace, reserving `MAX_OWNED_PROCESS_CLEANUP_SECONDS` (currently 1.5) for hard
    group termination. Wake all waiters and join/close transport resources;
    retain recovery markers if termination cannot be confirmed. Preserve board
    disconnect/plan cleanup before monitor delivery. Add a registry transport
    hook only if real EOF/cancellation tests show lifespan cannot cover a path.

The [clangd extensions](https://clangd.llvm.org/extensions) support explicit
database directory and forced diagnostics. The standard
[LSP initialization](https://github.com/microsoft/language-server-protocol/blob/gh-pages/_specifications/lsp/3.17/general/initialize.md),
[position](https://github.com/microsoft/language-server-protocol/blob/gh-pages/_specifications/lsp/3.17/types/position.md),
[diagnostics](https://github.com/microsoft/language-server-protocol/blob/gh-pages/_specifications/lsp/3.17/language/publishDiagnostics.md),
[definition](https://github.com/microsoft/language-server-protocol/blob/gh-pages/_specifications/lsp/3.17/language/definition.md)
and [references](https://github.com/microsoft/language-server-protocol/blob/gh-pages/_specifications/lsp/3.17/language/references.md)
contracts govern protocol semantics. Required version behavior and program
compatibility must still be proven by smoke tests before a version is pinned.

## Availability and exclusions

All five names have `TierRoute(name, "metadata", "code-analysis")`, no plan,
prerequisite, board guard, unlock or Layer 2 envelope. Both personal/professional
profiles retain them. Missing program/settings changes a response, not discovery.
Prove visibility and direct calls with no board, no-setup, setup-lite, setup-full,
revoked/unvalidated identities and an unrelated corrupt policy. Assert that no
hardware backend, probe inventory, board-policy reader/writer or unlock operation
is consulted by analysis. Check-in observation/deadlines remain in place.

Only local caches/reports may be written under `.firm/code-analysis/`. No source,
database, `.clangd`, policy or library suppression is rewritten. No rename,
formatting, code action, build, flashing, download or editor buffer API is exposed.
Update/uninstall and archive exclusion require installed-product checks in the
packaging stage. Existing releases stay unchanged; release publication needs
later explicit authorization.
