# Product tests

**Latest maintained suite:** the standalone second-pass cleanup in
`MCP_Server_Dev/development/fixture-second-pass/source`, branch
`cleanup/fixtures-second-pass`. Use its latest committed fixture source when
preparing the next product candidate; the retained w23c checkout is historical
product evidence, not the newest test suite. The product campaign stays paused.

Run maintained source, regression and quality checks in GitHub Actions for this
campaign, after the independent prebuild review gate. Required execution is
ACTIONS_PENDING until those matching-source checks actually run. The commands
below are CI step commands, from the Firmware MCP directory:

```sh
uv sync --locked --group dev
# Set this to an existing native clangd; no compiler or board is needed.
export BYO_MCP_TEST_CLANGD_EXECUTABLE=/absolute/path/to/clangd
uv run --no-sync python -m pytest -q --junitxml=results.xml
```

PowerShell uses `$env:BYO_MCP_TEST_CLANGD_EXECUTABLE = 'C:\path\clangd.exe'`.
The default `mvp` marker selects existing necessary feature assertions and serious
harm checks. `python -m pytest -m 'not live'` runs all retained source regressions;
name a module after changing its fixture. A skip or zero selected tests is not
feature evidence. Native clangd, managed runtime, ARM, workspace, OS and real
client prerequisites are explicit; unavailable prerequisites are UNRUN. Use
`BYO_MCP_TEST_WORKSPACE_ROOT` for the paired workspace source when testing it.
On WSL, keep the dependency environment on the Linux filesystem; the source may
stay on the Windows volume. Mounted-volume cold imports exceeded a provider
startup deadline in verification; the same lock on Linux storage passed without
changing that deadline.
Real client checks are marked `live` and run separately with `-m live` only when
local client/model execution is explicitly authorized.

The paired Installer's explicit native dispatch additionally selects the retained protection,
ownership, recovery, UART, symbol-event and full-record privacy regressions with
the default MVP filter disabled. These use the locked companion helper, real
policy/store/dispatch code and simulated physical effects. They retain JUnit,
failed inputs and the public-event/full-record payloads; their execution remains
ACTIONS_PENDING until the new frozen candidate runs. `test_model_pin.py` is the
separate M18 zero-request check of the maintained live fixture's exact model and
effort assertion; it runs in the default MVP selection and never starts a client.
The installed public MCP driver also measures project files before startup,
after the manual-permission reset, after monitor traffic and across clean EOF.
It keeps the compiled capsule and attributes the exact permission startup files
separately. This compiled check does not rewrite the historical false only-README
oracle or substitute for actual native-client passivity.

The second pass preserves fourteen direct process-ownership checks from the
old bc01-ci controls in `test_process_callers.py` and
`test_process_release_identity.py`, with only their two needed child fixtures.
They run through ordinary pytest in the four-host native regression step;
failure inputs/streams use `BYO_TEST_FAILURES`. The observer/recorder self-tests
and their unused framework have been removed, not parked in an optional suite.

Push/PR now runs preparation; one explicit bundle dispatch retains the full
native/installed scope. Cheap source, full fixture-format and risk checks
precede packaging. Linux regression commands use an explicit shell and quoted
directory change. The compiled passivity driver now checks the plain-text
handshake as text, including the actual server run ID, while retaining its
baseline, monitoring, privacy and EOF assertions. Previous failing evidence
keeps its original attribution; these source changes are not an execution PASS.

Keep tests of necessary product behavior, ordinary mistakes and serious harm.
Keep only their needed helpers. Do not add tests of runners, observers, evidence
validators or fixture receipt publication. Update the table when behavior changes.
Use actual product assertions; a fake backend cannot establish physical results.

Failures use the standard traceback and JUnit report. Assertion values and child
stderr identify the failed operation. Analysis and tiered context directories
are retained on an exception and copied to `BYO_TEST_FAILURES` (default
`.test-failures`) before teardown, so temporary-host shutdown cannot erase the
inputs. Passing directories are removed. Installed checks retain failed labs, command streams,
exit status and gate identity. CI group logs stream directly to files so timeout
output survives; cleanup failures are additional fields, never replacements for
the original command failure. Record the revision, dependency lock and command
once per run. No host process-history or nested-test ledger is required.
Pytest sets owned temporary runtime, monitor and managed-evidence roots before
collection unless explicitly supplied. Failed session roots are printed and
retained; passing ones are removed. `BYO_TEST_FAILURES` selects the destination
for failing unittest inputs copied before teardown.

Builds and Rust execution remain in Actions. Installer `quality.yml` owns neutral
release Python checks; native Rust/OS behavior and real built-package smoke remain
per target. The noncompiling Installer release checks use the existing group in Actions:
`python release/scripts/final_acceptance_checks.py --group release-tests --evidence <new-directory>`.
Installed drivers require a genuine built bundle and their documented inputs.
Future CI publication must refresh the paired source exports/locks and existing
CI input index for each changed candidate. Those records must match the exact
export and checkout; a dirty tree is not the already verified frozen input.
The ARM builder in `tests/arm_corpus.py` is consumed only by the installed CI
driver; it is never a prerequisite of this source suite. Real clients and boards
need separate local authority; this cleanup runs neither.

| Required behavior | Retained tests / driver | Evidence and limitation |
| --- | --- | --- |
| M01 install/start/status/doctor/uninstall, preserve user files | Installer `test_installed_e2e.py`; Rust `install` / `project`; installed `doctor_gate` and project/global uninstall | Genuine built package required; Rust source unchanged; current compilation/installation UNRUN |
| M02 project init/reinit, modes, client config preservation | Rust `project`; companion `test_attach_project`, `test_workspace_contracts`, `test_firmware_mcp_skill_suite`, `test_mode_catalog`; `test_tiered_acceptance_workspace` | Owned source projects; native integration still requires built launcher |
| M03 advertised client startup and integration | `test_monitor_codex_e2e`; installed forwarding/public MCP drivers | Real signed-in client checks stay local; pre-existing Claude/hook activation gaps remain |
| M04 update/failure recovery/rollback | Rust staged activation/update; installed lifecycle and update-preservation gates | Distinct real versions needed for genuine upgrade claim; current artifact UNRUN |
| M05 MCP without a board | `InitializationContract`, `TransportFailures`, `RealStdioShutdown`; sidecar runtime; installed `mcp-no-board` | Source MCP + real clangd; compiled startup needs actual package |
| M06 intended probe/serial, absent device, provider collision | `test_probe_inventory`, `test_server_assignment_connect`, `test_setup_overview_no_probe`, `test_serial_resolver_identity` | Controlled discovery/backend, no physical enumeration claim |
| M07 no-setup/lite/full, grants and corrupt state refusal | `test_tiered_core_policy`, `test_tiered_core_routing`, `test_tiered_core_manual_permissions`, tiered acceptance product modules | Real repositories/router with simulated effects; single-use grant preserved |
| M08 board debugging/ELF/symbol disambiguation | `test_breakpoint_tools`, `test_duplicate_elf_symbols`, SWD backend/process tests | Actual symbol handling, controlled board; safe live checks remain a pre-existing gap |
| M09 intended board/artifact and permission before effects | `test_lite_application_flash`, `test_lite_protective_overlaps`, full plan/permission tests | Public dispatch to fake physical backend; changed image refused; physical flash unverified |
| M10 complete UART read/write/exchange, missing port, cancellation | `test_uart_capture_evidence`, `test_vi06_uart_ownership` | Controlled serial bytes and errors; live benign protocol requires hardware setup |
| M11 five semantic tools, saved error/fix, EOF cleanup | `ProgramIdentity`, `HostC`, `RealStdioShutdown`; installed semantic queries | Real native clangd; ARM/C++ and other hosts require respective explicit inputs |
| M12 target-aware installed static analysis/refusal | Rust `cppcheck_acceptance`; installed portable/compiled clients and ARM gates | Genuine Cppcheck/ARM/native bundle required; all original Rust tests preserved, current compiled PASS not claimed |
| M13 timeout/cancel/recovery, exact owned-child cleanup | `VersionProbeTests`, `test_vi02a_async_queue_deadline`, `test_process_cleanup`, `test_swd_process_isolation`, `test_process_callers`, `test_process_release_identity`, attachment transaction tests | Real processes and async dispatch; historical Linux probe fixture correction retained; new process-module location needs Actions execution; public expiry/retry assertions retained |
| M14 passive monitoring, unchanged results, secret redaction | `test_monitor_behaviour`, `test_monitor_passivity`, `test_monitor_redaction` | Real monitor; refusal fixture now isolates its store from developer history |
| M15 project/runtime isolation, moved/copied projects | `test_artifact_runtime_destination`, Rust copy/lease tests, installed relocation and forwarding | Real file destinations/source repositories; moved installed package requires actual build |
| M16 utilities/configuration and bundled tool choice | artifact collector tests, native-build/pack/remote product checks, managed runtime tests | Source validation preserved; native/installed behavior requires actual tool input |

The November 7 before snapshot is outside discovery at workspace
`.archive/test-fixtures-before-20261107`; the comparison and actual run reports
are in `MCP_Server_Dev/development/tce`. Frozen historical receipts are recovery
material, never current PASS. Removed self-tests did not establish additional
product features. Product source, all Rust tests and companion tests are preserved.

November 7 verification: the default 41 checks passed on Windows (83.10s) and
Linux (231.79s), with no skips in either selection. Installer's eight retained
Python scripts passed before and after. Of the 33 selected companion checks, 32 passed initially; the remaining
doctor case passed with the existing native Codex executable on PATH (offline
parsing only). Its `.cmd`-only PATH limitation remains. All 27 selector-chain checks
passed on Windows, and the two affected actual-child checks passed on Linux
using native-filesystem dependencies. The broader Windows sweep and its
interrupted/failed stages are recorded individually in the execution report;
they are not presented as one uninterrupted green run. Builds, native installed
packages, macOS, real clients/models and boards were not exercised here.
