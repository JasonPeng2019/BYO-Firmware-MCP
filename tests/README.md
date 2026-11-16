# Necessary product tests

Run the maintained source suite with standard pytest in GitHub Actions. Installer
`build-matrix.yml` has an explicit `build_kind=cleanup` route that checks the exact
pinned Firmware and companion sources on Linux x64, Windows x64, macOS arm64 and
macOS x64. That route does not build products or resume a campaign. The companion
is a separate shipping implementation and retains its own workflow assertions.

The suite keeps one useful owner per behavior. Helpers exist for retained tests;
there are no optional legacy drivers or fixture self-test banks. The whitelist
and full file/function audit accompany the November 16 cleanup evidence in the
coordinating repository. Source tests do not establish genuine installed,
client/model or connected-board acceptance.

| Behavior | Main retained owners |
| --- | --- |
| Discovery, native/vendor fallback, hook refresh, exact provider selection | `test_discovery_hook_workflow.py`, `test_discovery_hook_registry.py`, `test_serial_resolver_identity.py`, `test_remote_probes.py`, `test_vi09_selector_chain.py` |
| Setup and policy publication, tiers, scoped grants, refusal before effects | `test_setup_overview_no_probe.py`, `test_tiered_acceptance_*.py`, `test_connection_promotion_transaction.py`, `test_setup_allowance.py` |
| Artifact provenance, maps and symbols | `test_artifact_runtime_destination.py`, `test_trusted_input_admission.py`, `test_trusted_artifact_pack_boundaries.py`, `test_duplicate_elf_symbols.py` |
| Registered execution/register bindings and masked writes | `test_duplicate_elf_symbols.py`, `test_lite_protective_overlaps.py` |
| Flash, protected ranges, recovery and rollback | `test_lite_application_flash.py`, `test_generic_builtin_safety_map.py`, `test_vi03_fresh_recovery_grant.py`, `test_validation_honesty.py` |
| Debug worker isolation, breakpoint and replacement ownership | `test_swd_process_isolation.py`, `test_swd_pyocd_breakpoints.py`, `test_swd_pyocd_jlink_multisession.py` |
| UART capture, exact attachments and identity | `test_uart_capture_evidence.py`, `test_vi06_uart_ownership.py`, `test_vi07_uart_provenance.py`, `test_vi08_attachment_cache_refusal.py` |
| clangd semantics and saved-input changes | `test_code_analysis_real_clangd.py`, `test_code_analysis_managed_runtime_native.py` |
| Protocol/error replies, Unicode positions and config recovery | `test_code_analysis_protocol_contract.py`, `test_code_analysis_results.py`, `test_code_analysis_config_contract.py` |
| Managed native runtime and owned process cleanup | `test_code_analysis_managed_runtime_contract.py`, `test_code_analysis_portability.py`, `test_code_analysis_posix_ownership.py`, `test_code_analysis_windows_acceptance.py`, `test_process_*.py` |
| Cancellation and queue deadlines | `test_final_supported_batch_cancellation.py`, `test_vi02_operation_resource_ownership.py`, `test_vi02a_async_queue_deadline.py` |
| Passive, private, durable monitoring with exact public output | `test_monitor_*.py` |
| Sidecar and local permission helper contracts | `test_sidecar_runtime.py`, `test_manual_permission_helper_runtime.py` |

Real clangd owns normal language semantics. `fake_clangd.py` is a small protocol
peer for error replies and explicit Unicode negotiation, not a second analyzer.
Native compiler or genuine bundle prerequisites are explicit skips when absent;
a skipped body is unverified. Existing frozen failures remain evidence.

`conftest.py` and `fixture_directory.py` retain scoped failing inputs before
teardown. JUnit and normal traceback/capture identify the failed test and step;
small command logs keep exit status and stdout/stderr. Cleanup failures must not
replace the original assertion, and natural EOF is checked before owned teardown.
No real client/model or physical board is used by the cleanup route. Live tests
remain separately qualified local acceptance under an explicit campaign resume.
