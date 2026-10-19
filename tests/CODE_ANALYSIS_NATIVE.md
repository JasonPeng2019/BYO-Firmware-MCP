# Native analysis prerequisites

These focused source tests exercise staged development inputs. They do not
certify an installed runtime, a release, hardware, or the full test campaign.

Before importing the test modules, supply isolated writable evidence, artifact,
monitor and runs roots. Keep native vendor files and original firmware inputs
outside those roots and retain their byte hashes before and after each run.

| Tests | Explicit prerequisites |
| --- | --- |
| `test_code_analysis_managed_runtime_native.py` | Windows x86_64; `BYO_MANAGED_TEST_INPUT_RECORD` and its exact `BYO_MANAGED_TEST_INPUT_RECORD_SHA256`; `BYO_MANAGED_TEST_EVIDENCE` |
| `test_code_analysis_service_unit.RealClangdTests` | `BYO_TEST_CLANGD` selecting a real native executable; `BYO_TEST_ANALYSIS_EVIDENCE` for retained results |
| Its ARM/query-driver case | `BYO_TEST_ARM_FIXTURE` selecting a genuinely compiled C/C++ corpus with `byo-analysis.json` and `build/compile_commands.json` |
| `test_code_analysis_real_clangd.py`, native cases in `test_code_analysis_runtime_contract.py` and `test_code_analysis_results.py`, and Windows acceptance/regression modules | `BYO_MCP_TEST_CLANGD_EXECUTABLE` selecting a real Clangd executable with its original resources |
| Their ARM/query-driver cases | `BYO_MCP_TEST_ARM_GNU_ROOT` selecting the full Arm GNU tree, including `bin/arm-none-eabi-gcc.exe`, `bin/arm-none-eabi-g++.exe`, GCC's versioned headers and newlib/C++ headers; this is distinct from `BYO_TEST_ARM_FIXTURE` |
| `OfficialSTM32U5PackIntegrationTests` | `BYO_MCP_OFFICIAL_U5_PACK` selecting the exact filename, length and SHA pinned in `test_trusted_input_admission.py` |

Managed native tests copy the pinned payload into an inventoried private
`RuntimeFixture`, retaining vendor resources, manifest and provenance. They
resolve through production code with analyzer PATH lookup unavailable. The
separate `RealClangdTests` exercise supported development PATH resolution and
real stdio; their negative PATH test deliberately checks rejection of an
incidental current-directory executable. Neither suite substitutes resolver
success for native queries.

The semantic tests' `make_service(executable=...)` constructor seam selects the
supplied real executable and runs the actual adapter/native queries. Its
controlled `path` origin is not proof of PATH discovery or managed admission.
Runtime-selection tests separately use the production resolver and preserve
their origin assertions. `AnalysisStdioServer` installs the supplied executable
through the gated server test seam, then uses real MCP stdio and owned children.

Use a short owned toolchain root on Windows. Arm GNU 14.3.1's MinGW compiler
can fail to open a present C++ multilib header when its internal search path,
including unnormalized `../` segments, exceeds Windows' path limit. Validate
the actual C++ fixture with its recorded flags before relying on clean analysis.
If relocation is necessary, copy the complete official tree into an isolated
short directory and verify every copied file's length and SHA against the
original input record. Preserve the failed long-root command and its diagnostics;
do not edit vendor headers or replace ABI assertions. The extended-root
acceptance test records both compiler verdicts with their argv and raw output.

For these six modules, run each original filename using
`uv run --locked python -m unittest discover -s tests -p <exact-filename> -v`.
Set `BYO_WINDOWS_ACCEPTANCE_EVIDENCE`, `BYO_WINDOWS_REGRESSION_EVIDENCE` and
`BYO_WINDOWS_ACCEPTANCE_SCRATCH` as well as the common isolated roots. On Windows,
point `TEMP` and `TMP` at an owned short root before imports. Keep both native
gates enabled; POSIX-only cases remain host-inapplicable skips on Windows.

The existing ARM semantic oracle queries the `heartbeat` call at `src/main.c`
line 4, column 37, and references its definition in `src/heartbeat.cpp` at
line 3, column 10. A harness-generated corpus must place those symbols there.
If supplied data disagrees, retain the failed data and commands, create an
owned copy, correct that copy and genuinely compile/link it again. Preserve the
original test assertions and original input hashes. Retain the new source,
compilation database, compiler identities, build argv/results and ARM ELF hashes.

On Windows, `OwnedSpawns` reads original child birth identity through the
retained `Popen` handle. A valid fast `--version` child can already have exited
when observed. Live-process absence remains an independent cleanup check;
the marker must still match the original PID/birth and original parent, and
must be removed after owned cleanup. The native observer control forces this
terminal-before-observation boundary and also proves a retained marker fails
the cleanup assertion.

Capture verbose unittest output and every case outcome. Missing prerequisites
and host-inapplicable cases remain explicit skips; they are never passes.
