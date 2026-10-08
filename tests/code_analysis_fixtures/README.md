# Code-analysis test fixtures

Fixtures for the independent `test_code_analysis_*.py` contract tests. Tests copy
a fixture into a private temporary project before use and never modify these files.
`.gitattributes` disables newline conversion so the stored bytes stay exact. Tests
that need CRLF, Unicode or other exact-byte input build it in code.

| Fixture | Use |
| --- | --- |
| `host_c/` | Real clangd host smoke in C. Tests write an absolute-path database at runtime. `twice` is defined and called in `src/main.c`, and `scale_value` is declared in `include/util.h` and defined in `src/util.c`. |
| `host_cpp/` | Real clangd host smoke in C++: overload resolution (`shapes::area(2.0)` resolves to the `double` overload) and template hover (`shapes::doubled`). |
| `arm_firmware/` | The read-only source copy of ROOT's frozen Cortex-M4 fixture. Archive `frozen-fixture.zip` has SHA-256 `f23f9c879bd1047df85d1aba8a20501a0a48ac4cc79fd01519a2039231ad2bfa`. Only sources, headers, linker script, platform XML and generated header are copied; ELF, map and object files are not. `build/compile_commands.template.json` and `byo-analysis.template.json` keep the exact exported argv, order, `-I` include form and `output`. Machine-local prefixes are replaced by `@PROJECT@`, `@PROJECT_NATIVE@`, `@ARM_GNU@` and `@ARM_GNU_NATIVE@`, and `code_analysis_support.materialize_arm_fixture` substitutes them for each host. |

## ARM fixture notes

- `include/firmware.h` asserts an ILP32 ABI (`sizeof(void*) == 4 && sizeof(long) == 4`).
  The exported argv names `arm-none-eabi-gcc`/`g++`, so clangd infers the ARM target
  from the driver name, and the toolchain headers arrive through explicit `-I`.
  Native clangd 23.1.0 `--check` is therefore clean both with the literal query
  driver and with an empty allowlist. The negative control replaces the driver
  with a generic host `clang`: the ILP32 assertion then fails (`--check` exit 3).
- The Arm compiler cannot build from Unicode paths. The ARM fixture is therefore
  materialized under an ASCII temporary path. The Unicode position and path tests
  for clangd use the host fixtures and in-code sources.
- ROOT's preserved Cppcheck 2.22.0 `-isystem`/`missingIncludeSystem` blocker
  belongs to the Cppcheck stage. These fixtures neither rewrite command data nor
  suppress it.
- Real-program tests run only when the test harness supplies these environment
  variables: `BYO_MCP_TEST_CLANGD_EXECUTABLE` and `BYO_MCP_TEST_ARM_GNU_ROOT`. If
  they are unset, the tests are skipped and reported as an unmet prerequisite, not
  as a pass. The programs are never passed through a public tool argument.
