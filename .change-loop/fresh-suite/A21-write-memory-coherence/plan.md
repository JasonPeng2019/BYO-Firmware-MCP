# Change implementation plan

## Source change list

- Source: `.change-loop/changes.md`
- Goal summary: Make the existing planned scalar `write_memory` operation lifecycle-coherent:
  after all current pre-I/O checks, briefly halt any non-halted target, perform the write, verify
  the exact value at the same address and width before reporting success, and restore only an
  execution state the server itself interrupted. Publish and test the resulting honest contract
  without changing the public call shape, permission model, containment rules, or unrelated
  memory behavior.

## Repository context and assumptions

- Verified architecture and relevant entry points:
  `src/pyocd_debug_mcp/server.py::write_memory` delegates to the handler built by
  `src/pyocd_debug_mcp/tools/memory.py::build_memory_handlers`; `MemoryToolServices` already
  supplies generic `get_state`, `halt`, `resume`, scalar read, and scalar write callables wired to
  `services/target_control.py`. The same module's `_read_coherent_scalar` is the established
  adjacent lifecycle/restoration pattern. Public plan guidance for the action is in
  `src/pyocd_debug_mcp/guardrails/plan_defs.py`.
- Existing test/build commands relevant to the change:
  focused `pytest` tests under `tests/`; adjacent
  `tests/test_a20_sleeping_symbol_read_spec.py`; repository-wide `pytest`; `ruff check .`;
  `pyright`; and `git diff --check`.
- <!-- Assumption: A successful scalar write proves that the exact requested value was read back
  at the exact mapped address and width while execution was coherently halted. It does not promise
  that subsequently resumed firmware will not intentionally overwrite that location. -->

## Plan items

### CL-001 — Add one lifecycle-coherent scalar-write primitive

- **What to change:** Add one private helper beside `_read_coherent_scalar` that owns the
  target-state query, conditional halt, scalar write, same-width scalar readback, exact comparison,
  and conditional restoration. Require configured lifecycle services. If the observed value does
  not equal the requested value, raise an explicit non-success error containing the expected and
  observed values formatted for the access width. Preserve a primary write/readback/mismatch or
  cancellation-class failure while still attempting restoration; when restoration also fails,
  report both facts in one error and chain the primary. If mutation and verification succeeded but
  restoration fails, propagate restoration failure and do not allow the caller to record success.
- **Where:** `src/pyocd_debug_mcp/tools/memory.py`, adjacent to
  `_read_coherent_scalar` and using the existing `MemoryToolServices` lifecycle/read/write
  callables.
- **Exact intended behavior:** An already `HALTED` target follows
  `get_state -> write -> readback` and remains halted on success or failure. Every other returned
  provider state follows `get_state -> halt -> write -> readback -> resume`. `get_state` failure
  propagates before any halt; halt failure propagates without a speculative resume; after a
  successful inserted halt, write, verification-read, comparison, and cancellation-class failures
  all trigger exactly one restoration attempt. Exact match returns normally. Mismatch and every
  target-access/restoration failure are non-successes.
- **Must remain intact:** Generic provider-neutral lifecycle calls; the current meaning of
  already-halted state; original exception identity/trace when restoration succeeds; both-fact
  reporting and primary chaining when restoration also fails; current widths `{8,16,32}`; no
  board/MCU/OS/firmware special cases; and no new public parameter, permission, retry, reset, or
  persistent state.
- **Objective verification:** Unit tests with recording fakes assert exact call order and count for
  `HALTED`, `RUNNING`, `SLEEPING`, and another non-halted state; exact-match success; width-correct
  mismatch text; state/halt/write/readback/restoration failures; `BaseException` restoration and
  re-raise; dual-failure message plus exception chaining; successful write plus restoration
  failure; and no resume for an initially halted target.

### CL-002 — Route both public scalar-write forms through coherent verification

- **What to change:** After all existing argument parsing, symbol/artifact resolution and
  revalidation, symbol-size/alignment validation, raw-address justification, value/range checks,
  mapped-RAM containment, plan/gate/validation checks, and handle acquisition remain satisfied,
  replace the direct backend write with the coherent helper. Keep the existing success string and
  record the existing single success event only after exact readback and any inserted-state
  restoration complete.
- **Where:** `src/pyocd_debug_mcp/tools/memory.py::build_memory_handlers.write_memory`.
- **Exact intended behavior:** Symbol-backed and explicitly justified raw mapped-RAM scalar writes
  receive identical lifecycle and verification semantics. All pre-I/O refusal branches return
  their existing refusal without calling `get_state`, `halt`, write, verification-read, or
  `resume`. A verified exact write returns the current
  `Wrote 0x... to mapped RAM at ...` response and records exactly one success event. Any lifecycle,
  backend, mismatch, cancellation-class, or restoration failure propagates as a non-success and
  records no success event.
- **Must remain intact:** The public function signature and response shape; symbol-first policy;
  ELF selection, hashing, prepared-symbol containment, and revalidation; raw-address opt-in and
  concrete reason; width/object-size/alignment checks; mapped-RAM safety enforcement; existing
  action plan, budget, validation, gate, event, and action-batch behavior; and all read-memory
  contracts.
- **Objective verification:** Handler-level tests cover both symbol and raw-address paths,
  successful response/event compatibility, no-I/O preflight refusals, mismatch/non-success, and
  production `MemoryToolServices` wiring. Existing adjacent A20 coherent-read, memory, plan,
  action-batch, and trust-model suites stay green.

### CL-003 — Publish the exact write lifecycle and recovery contract

- **What to change:** Expand the public action docstring and `write_memory` plan guidance to state
  that running/sleeping targets are briefly halted for one write plus exact readback and then
  restored; already-halted targets remain halted; success proves an immediate coherent verified
  mutation but resumed firmware may later change its own variable; failures are reported honestly;
  and the recovery is to inspect/reconnect the target and retry with the current ELF or a
  deliberately halted target.
- **Where:** `src/pyocd_debug_mcp/server.py::write_memory`, the handler docstring in
  `src/pyocd_debug_mcp/tools/memory.py`, and the `write_memory` `_PromptGuidance` in
  `src/pyocd_debug_mcp/guardrails/plan_defs.py`.
- **Exact intended behavior:** MCP tool discovery/help and initialized plan guidance teach the
  same lifecycle, success limit, failure, and recovery behavior without outside knowledge. The
  plan still asks the caller to state the confirming readback, but the server itself performs the
  authoritative immediate verification.
- **Must remain intact:** Existing symbol-first usage, raw-address restrictions, mapped-RAM scope,
  warning that a wrong memory write can crash firmware, cleanup/recovery intent, example arguments,
  tool name, plan schema, call budget, and permission classification.
- **Objective verification:** Tests inspect both published action help/docstrings and plan guidance
  for the lifecycle, verification, later-overwrite caveat, honest failure, and recovery phrases,
  while asserting the existing example, symbol-first/RAM-only guidance, plan schema, and no-new-
  permission contract remain present.

### CL-004 — Add focused adversarial and regression proof

- **What to change:** Add tester-owned focused spec and regression tests for CL-001 through CL-003.
  Keep new tests deterministic and hardware-free; use service fakes and production wiring/help
  inspection rather than a board or pyOCD process. Exercise adjacent coherent reads and established
  memory guardrails so the repair cannot silently broaden I/O or change compatibility.
- **Where:** New or narrowly extended files under `tests/`, with spec-tester and
  regression-tester ownership kept separate by the change-loop manifests.
- **Exact intended behavior:** The test gate fails on fabricated success, missing readback,
  unconditional resume, skipped restoration, swallowed cancellation, lost primary error, missing
  help, or a regression in existing pre-I/O refusal/containment behavior.
- **Must remain intact:** Test-role ownership, isolated tester manifests/commands, no production
  edits by testers, no hardware/network dependency, and no weakening or rewriting of existing
  assertions to obtain green.
- **Objective verification:** The neutral gate runs both tester-recorded commands independently in
  the same iteration and both exit zero; main verification also runs the focused tests, adjacent
  A20 tests, full repository tests where practical, Ruff, Pyright, and `git diff --check`.

## Out of scope / must not change

- Do not change firmware, fresh-experiment evidence/specifications, pyOCD itself, provider worker
  protocols, public parameters, plan permissions, action budgets, setup/validation/gate policy, or
  memory-map containment.
- Do not make write success durable across future application execution, add retries/resets, or
  require callers to pre-halt manually.
- Do not refactor `_read_coherent_scalar` except for a narrowly shared private utility proven
  simpler and behavior-preserving; its published A20 contract must remain unchanged.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, or generated artifacts.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, and adjacent behavior touched by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, or gate commands.
