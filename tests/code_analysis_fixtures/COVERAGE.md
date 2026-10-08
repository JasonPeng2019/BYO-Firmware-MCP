# Code-analysis behavior-to-test coverage map

Independent tests for `docs/code-analysis-contract.md` rev 3 (base `e993ecd`).
Paths are under `tests/`; `Class.test` names are abbreviated without the `test_`
prefix where unambiguous. Fake-peer coverage proves the product against
`fake_clangd.py` only; the real-program rows are the native evidence and are
skipped (unmet prerequisite, never a pass) unless the harness supplies
`BYO_MCP_TEST_CLANGD_EXECUTABLE` and, for ARM, `BYO_MCP_TEST_ARM_GNU_ROOT`.

| Required behavior | Tests |
| --- | --- |
| Configuration file: absent/default, hashing, full example, errors with field and remedy, timeouts, Cppcheck isolation | `test_code_analysis_config_contract.py` `ConfigurationFileContract.*` |
| Query-driver allowlist: exact canonical compilers; globs, commands, directories rejected; literal never broadened | `ConfigurationFileContract`/`QueryDriverContract.*`; `test_code_analysis_protocol_contract.py` `QueryDriverArguments.*`, `InitializationContract.empty_query_driver_allowlist_grants_no_compiler_execution` |
| Database selection: zero, root, build, no recursive search, ambiguity, hardlink/symlink aliases, malformed, empty, explicit relative to project, no fallback, name, directory, Unicode root | `DatabaseSelectionContract.*` |
| Escaped vs literal non-BMP database: both valid JSON for shared config | `DatabaseSelectionContract.escaped_and_literal_non_bmp_paths_are_both_valid_json` |
| Escaped vs literal non-BMP database: clangd backend blocker with literal-UTF-8 re-export remedy, bytes untouched | `test_code_analysis_real_clangd.py` `UnicodeProject.raw_clangd_distinguishes_the_two_database_representations` (native characterization), `UnicodeProject.escaped_surrogate_database_is_an_explicit_problem_not_success` |
| Database entries: index/field errors, directory/file resolution, argument order, opaque command text, dedupe, conflicts, never mutated | `DatabaseEntriesContract.*` |
| Runtime resolution: PATH vs managed (no PATH substitution), cwd not searched, origin, bounded/cached `--version` probes, expired deadline | `test_code_analysis_runtime_contract.py` `*` |
| Unicode positions: accented, combining (and the mark itself), CJK, non-BMP, tab, CRLF, line end, EOF, line start; utf-8/16/32 both directions | `test_code_analysis_results.py` `UnicodePositionMapping.*`; `test_code_analysis_protocol_contract.py` `PositionEncodingNegotiation.*`; real `UnicodeProject.unicode_crlf_positions_round_trip_under_a_unicode_root` |
| Invalid and unmappable positions/ranges | `InvalidPositions.*`, `UnmappableResultRanges.*` |
| LSP initialization, failed initialize, server-initiated requests | `InitializationContract.*`, `FailedInitialization.*`, `ServerInitiatedRequests.*` |
| LSP framing (split multibyte frames, concatenated messages) and out-of-order replies | `FramingAndRouting.*` |
| Transport failures, error replies | `TransportFailures.*` |
| Document sync, URIs, definition shapes, `.clangd` boundary, compile context | `DocumentSynchronization.*`, `ResultUris.*`, `DefinitionShapes.*`, `ClangdConfigurationBoundary.*`, `CompileContext.*` |
| Envelopes, status, counts/truncation, strict limits, diagnostics/hover normalization | `test_code_analysis_results.py` `EnvelopeForEveryOperation.*`, `StatusContract.*`, `ReferencesCountsAndTruncation.*`, `StrictLimitsAndTimeouts.*`, `DiagnosticsNormalization.*`, `HoverNormalization.*` |
| Fresh vs stale, unversioned, foreign, future or absent diagnostics; forced refresh; header invalidation | `test_code_analysis_freshness.py` `DiagnosticsVersionMatching.*` |
| Concurrent saved-file changes; configuration generations | `ConcurrentSavedFileChanges.*`, `ConfigurationGenerations.*` |
| Single deadline across phases, outer grace resolver, timeouts with `$/cancelRequest`, late replies discarded | `test_code_analysis_deadlines.py` `*` |
| MCP cancellation cancels the pending LSP request | `test_code_analysis_lifecycle.py` `StdioServerLifecycle.mcp_cancellation_cancels_the_pending_lsp_request` |
| Lazy single child, idempotent shutdown, crash/exit handling, bounded stderr | `LazySingleChild.*`, `IdempotentShutdown.*`, `CrashHandling.*` |
| Owned-process shutdown and stdio EOF (markers, wedged peer bounded, stderr never on MCP stdout) | `StdioServerLifecycle.*`; real `RealStdioShutdown.stdin_eof_terminates_owned_clangd_within_the_bound` |
| Five tools: matrix routes, both profiles, registered inventory, exact closed schemas, read-only annotations | `test_code_analysis_availability.py` `RouteMatrixAndInventory.*`, `PublicRegistration.*` |
| Five tools without board setup in no board, no-setup, setup-lite, setup-full, revoked/unvalidated identity, unrelated corrupt policy x personal/professional; no board I/O, no policy mutation | `EveryTierStateWithoutBoardIo.all_tools_in_every_tier_state_and_profile` |
| Argument refusals (authority/override arguments, strict types, URI/UNC); missing program | `ArgumentRefusals.*`, `MissingProgram.*` |
| JSON preserved at the monitoring check-in boundary (separate block) | `CheckInBlockSeparation.*` |
| Real clangd: identity, definition, references, hover, genuinely empty fresh diagnostics, saved error then fix | real `ProgramIdentity.*`, `HostC.*`, `HostCpp.*` |
| Real ARM firmware: literal query driver clean; empty allowlist uses exact argv; generic host driver never clean ARM | real `ArmFirmware.*` |

Existing suites kept as regression checks (not owned here): `test_tiered_core_matrix`,
`test_monitor_wiring`, `test_monitor_stdio_lifecycle`, `test_process_cleanup`,
`test_sidecar_runtime`.

Not covered here: four-platform execution, installed-product and managed-runtime
manifest mapping (packaging prerequisite), Cppcheck stage behavior, and hardware.
