# Change implementation plan

## Source change list

- Source: `/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/.change-loop/fresh-suite/A20-sleeping-symbol-read/changes.md`
- Goal summary: Make the public scalar `read_memory_symbol` action obtain a coherent value from a
  non-halted target by temporarily halting and restoring execution, while preserving its existing
  interface, validation, safety, isolation, and truthful error behavior.

## Repository context and assumptions

- Verified architecture and relevant entry points:
  `src/pyocd_debug_mcp/tools/memory.py::MemoryToolServices` injects the target operations used by
  `build_memory_handlers()`. Its `read_memory_symbol` handler currently performs all artifact,
  symbol, width, size, and memory-map validation before directly invoking
  `read_target_memory`. `src/pyocd_debug_mcp/server.py` constructs this service from
  `services/target_control`, then registers the handler with FastMCP using the handler docstring as
  public tool help. The target-control service exposes generic `get_state`, `halt`, `resume`, and
  `read_memory` operations through the process-isolated provider. The older
  `services/symbols.py::read_symbol_u32` demonstrates the repository's state-observe,
  halt/read/finally-resume pattern, but cannot be reused directly because it resolves its own
  symbol and only supports 32-bit reads.
- Verified constructor/test surface: production constructs `MemoryToolServices` once in
  `src/pyocd_debug_mcp/server.py`; test constructors exist in
  `tests/test_server_trust_model_round_1.py`,
  `tests/test_server_trust_model_round_3.py`, and
  `tests/test_server_trust_model_round_4.py`.
- Existing verification commands relevant to the change:
  `python -m pytest -q`, `python -m ruff check src tests`, `python -m pyright`, and
  `git diff --check`. The neutral gate will run narrower tester-owned commands first, then the
  main model will run the repository-wide checks before accepting the repair.

## Plan items

### CL-001 — Coherent scalar symbol snapshot with truthful state restoration

- **What to change:** Extend `MemoryToolServices` with the three generic target lifecycle
  operations needed by this handler (`get_state`, `halt`, and `resume`) and wire them to
  `services/target_control` in production. Add one small, local coherent-scalar-read helper in the
  memory tool module and use it only after all existing `read_memory_symbol` pre-I/O validation.
  Keep the control flow explicit rather than adding a general transaction framework.
- **Where:** `src/pyocd_debug_mcp/tools/memory.py` and the production service construction in
  `src/pyocd_debug_mcp/server.py`.
- **Exact intended behavior:** Query the target state immediately before the scalar read. If the
  normalized state is `HALTED`, read once and do not halt or resume. Otherwise, halt once; only
  after halt succeeds, read once; and attempt one resume in guaranteed cleanup before returning.
  A successful read is returned only after any required resume also succeeds. State lookup or
  halt failure prevents the read and propagates through the existing Layer-2 error translation.
  Read failure after a successful inserted halt still attempts resume and preserves the primary
  read failure. Resume failure after a successful read becomes an honest failure rather than
  returning the value. If read and resume both fail, the raised error retains the primary failure
  and also includes the resume failure type and message, so neither fact is lost.
- **Must remain intact:** The public `read_memory_symbol(board_id, symbol, width=32,
  elf_artifact=None)` arguments and success text; supported widths; symbol lookup and scalar
  checks; artifact hashing/re-verification; memory-map authorization before target I/O; board and
  session routing; process-provider deadlines; Layer-2 safe-exit/error wrapping; and existing
  event semantics. `find_symbol`, raw address/block reads, writes, explicit execution controls,
  flash, and the legacy symbol helper do not acquire new behavior.
- **Objective verification:** Tester-owned fakes must assert exact call order and counts for:
  initially halted (`state, read` only); running and sleeping (`state, halt, read, resume`);
  state failure; halt failure; read failure with successful resume; read success with resume
  failure; and dual read/resume failure with both messages retained. Tests must assert that no
  fallback zero is fabricated, a legitimate zero returned by the coherent read remains valid,
  success is not recorded/returned when restoration fails, and all pre-I/O refusals still perform
  no state or target operation. Regression tests must cover the production `target_control`
  wiring and prove raw address reads/writes and explicit halt/resume behavior are unchanged.

<!-- Assumption: Target state names are provider-defined strings normalized case-insensitively.
`HALTED` is the only state that requires no intervention. For every other state, the existing
generic `resume` operation restores execution eligibility; reproducing an exact low-power state is
firmware behavior after resume and is neither exposed nor safely inferable by this server. -->

<!-- Assumption: A resume is attempted only after the inserted halt returns successfully. If halt
itself raises, whether hardware changed state is unknowable, so issuing another state-changing
command would guess rather than restore verified state. The halt failure is reported honestly. -->

### CL-002 — Public help discloses the coherent-read lifecycle

- **What to change:** Replace the affected handler's one-line docstring with concise public help
  that states what the tool reads, when to use it, the meaning of its parameters and return, the
  temporary halt/resume behavior for a non-halted target, and recovery guidance for artifact,
  symbol, target-access, and restoration failures.
- **Where:** The `read_memory_symbol` handler docstring in
  `src/pyocd_debug_mcp/tools/memory.py`; `src/pyocd_debug_mcp/server.py` already publishes that
  docstring through `mcp.add_tool`.
- **Exact intended behavior:** MCP tool listing/help tells an agent before invocation that a scalar
  read from a running or sleeping target briefly halts the core for one coherent read and attempts
  to restore execution before return; an already halted target remains halted. It states that
  failure to read or restore is reported as failure, never as invented data, and gives the
  appropriate retry/reconnect/current-ELF recovery without adding an approval gate.
- **Must remain intact:** Tool name, schema, structured-output setting, return formatting, safe-exit
  reminder, registry placement, and all unrelated tool descriptions.
- **Objective verification:** An automated registry/handler test must inspect the published
  description (or the handler docstring used by registration) and assert the temporary-halt,
  restoration, already-halted, and honest-failure contract. Existing tool-schema and registry
  tests must continue to pass.

## Out of scope / must not change

- No firmware, fixture, SDK, experiment evidence, board profile, pack, or hardware changes.
- No board-, MCU-, probe-, OS-, path-, toolchain-, low-power-state-, counter-, or application-
  specific branch.
- No arbitrary retry, zero-value heuristic, fabricated fallback, polling loop, new user approval,
  or broad target-state abstraction.
- No coherent-stop behavior added to raw-address reads, block reads, writes, or unrelated tools.
- No change to flash, permission, safety-map, plan, session, or provider-process semantics.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, or generated artifacts.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, interfaces, and adjacent behavior touched
  by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- Focused tests, the repository-wide test suite, Ruff, Pyright, and `git diff --check` exit 0 under
  main-model verification.
- The doer does not modify tester-owned files, manifests, or gate commands.
