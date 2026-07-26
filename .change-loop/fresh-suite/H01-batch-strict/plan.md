# Change implementation plan

## Source change list

- Source: `.change-loop/fresh-suite/H01-batch-strict/changes.md`
- Goal summary: Make every registered MCP tool reject unknown arguments at its published FastMCP
  schema boundary, make that validation precede unlocked plan/handler execution while preserving
  physical-lock precedence, and report an `action_batch` child refusal as an MCP error without
  losing the existing structured batch-failure details or successful-batch behavior.

## Repository context and assumptions

- Verified architecture and relevant entry points:
  `src/pyocd_debug_mcp/kernel/registry.py::RegistryFastMCP.add_tool()` is the one registration
  path used by server tools and `RegistryFastMCP.call_tool()` is the common direct and
  batch-child dispatch path. `call_tool()` currently checks the registry lock, constructs a
  deferred plan guard, and reaches `Tool.run()`, where FastMCP first pre-parses and validates the
  generated Pydantic argument model. Because the default generated model ignores extras, the
  registered `action_batch`, `read_serial`, and `initialization_handshake` schemas omit
  `additionalProperties: false`. Even after making their models strict, the current deferred
  guarded-action check would see an unlocked action's unknown key before `Tool.run()` validates
  it. `src/pyocd_debug_mcp/tools/batch.py::BatchChild` already has
  `ConfigDict(extra="forbid")`; `build_batch_handlers()` validates the outer child list once,
  dispatches each child through `mcp.call_tool`, catches the first child exception, and currently
  returns its `batch_failed` payload as a normal successful MCP result. Each nested
  `RegistryFastMCP.call_tool()` independently snapshots `registry.list_revision` and sends
  `notifications/tools/list_changed` in its own `finally`; an exact fallback whose child consumes
  and relocks a plan can therefore notify once from the child and again from the enclosing batch
  request unless notification ownership is coalesced at the outer public request.
- Existing strict-plan behavior:
  `src/pyocd_debug_mcp/tools/plans.py::forbid_unknown_tool_arguments()` rebuilds selected
  registered argument models and, for generated non-permission plans, may add a separate
  pre-validation permission rule. `register_plan_tools()` also installs plan-specific FastMCP
  metadata that preserves literal textual JSON-looking strings. The general registration policy
  must compose with those later specializations rather than replace their model, metadata, or
  validators.
- Existing test/build commands relevant to the change:
  `uv run --locked --no-sync pytest -q tests/test_h01_strict_mcp_boundary.py` and the analogous
  isolated regression-test command in a verified project environment;
  `uv lock --check`; `uv build`; `uv run --locked --no-sync ruff check .`;
  `uv run --locked --no-sync pyright`; `uv run --locked --no-sync pytest --collect-only -q`;
  and `uv run --locked --no-sync pytest -q`. The focused tester commands must use separate,
  tester-owned H01 files and must not mutate dependencies or the lock.
- Baseline preservation:
  accepted pre-repair production files and SHA-256 values are
  `README.md` `15a3c471426302805e866563bba78a2c0482b00133fce4fdbd262c8cd6763f1a`,
  `pyproject.toml` `357b4bf783b0226d04d33035fc78fd63535bb279bf20b7e25be11637a335a454`,
  `src/pyocd_debug_mcp/kernel/processes.py`
  `5f74cd9be7aeea3b2b72d97d9c0b00ad3120205902aacd17a96ca53ebac42435`,
  `src/pyocd_debug_mcp/tools/plans.py`
  `2427ce3f0cb3986cc0ca89107d9e2d41a6edde8e6b3fef681c7eda4070ffee09`,
  and `uv.lock` `1b0ea27f91dddbd00c215b8d9da487d7960e1fb4f1e1afa4c07bc4811c7ff0cf`.
  Accepted existing tester files must also remain byte-identical:
  `tests/test_h00_repository_contract.py`
  `2f2dd37fe76b642b40fbd7177bef9ce12775eedc925b229489573615b33d597a`,
  `tests/test_h00_repository_regressions.py`
  `56b72f516554016b9616db2828f437dfca4cb29767adeafa0e987bdd4afbf29b`,
  `tests/test_h01_plan_text_preservation.py`
  `8eae1c0177b86d6d5d71d01cb35c120a1a34730ee9ddb91451b243a061e0883b`, and
  `tests/test_h01_plan_text_regressions.py`
  `2a98f72110c53e4e33b227864b1bee6e9ecbf82542b39ad0309428e0f2107b77`.
- Charter checkpoint: the current main model reread the complete
  `../.codex/design_charter.md` after the evidence-backed request and again immediately before
  writing this plan. Unknown-key rejection is a correctness guard against a compliant but
  fallible caller receiving different semantics than it requested, not adversarial-input
  hardening. One registration/dispatch policy is simpler and more general than per-tool key
  lists; accurate MCP failure signaling satisfies the no-silent-failure rule.

## Plan items

### CL-001 — Make registered MCP schemas and runtime parsing uniformly strict

<!-- Assumption: H01's public contract that unknown fields are rejected applies to every server
tool registered through `RegistryFastMCP`, not only generated plans or the three controls that
first exposed the issue. Central registration strictness is therefore the narrowest general fix;
adding explicit `action_batch`, `read_serial`, or handshake key lists would leave the same defect
in every other ordinary tool and violate the charter's one-home/generalizability rules. -->

- **What to change:** During `RegistryFastMCP.add_tool()`, configure the exact generated argument
  model owned by that newly registered tool with Pydantic's `extra="forbid"` policy, force its
  model rebuild, and republish `tool.parameters` from that rebuilt model so runtime behavior and
  `tools/list` JSON Schema agree. Keep registration failure explicit if the just-added tool cannot
  be recovered from FastMCP's manager. Do not create a second per-tool allowlist or replace
  FastMCP/Pydantic validation.
- **Where:** `src/pyocd_debug_mcp/kernel/registry.py`, at the common
  `RegistryFastMCP.add_tool()` registration boundary. The doer may factor a tiny private helper
  in that module only if it removes duplication needed by CL-002; do not move or rewrite the
  generated-plan-specific metadata/permission logic in `tools/plans.py`.
- **Exact intended behavior:** Every tool registered through `RegistryFastMCP`, including a
  zero-argument tool, rejects any unknown top-level argument with an MCP tool error that names the
  injected key. Its advertised input object schema has `additionalProperties: false`.
  `action_batch` also advertises a strict outer object, and its `$defs.BatchChild` retains
  `additionalProperties: false`; the dynamic `BatchChild.arguments` value remains the named
  child's ordinary argument object and is not falsely advertised as one fixed tool schema.
  A valid request with exactly the declared arguments has unchanged values and result.
- **Must remain intact:** Preserve all function signatures, aliases, defaults, nullable fields,
  FastMCP pre-parse compatibility, output conversion, tool descriptions, visibility registration,
  and registered count. Preserve `forbid_unknown_tool_arguments()`'s special populated
  non-permission-plan validator and `_PlanToolMetadata` literal-string semantics. Do not special
  case action names, boards, hosts, or H01 inputs; add no dependency, cap, sandbox, or
  attacker-oriented validation.
- **Objective verification:** The spec tester must instantiate a real `RegistryFastMCP`, register
  representative zero-argument, ordinary typed, generated-plan, and batch tools, and exercise
  their actual registered `Tool.run()` or public `call_tool()` boundary. Assert unknown keys raise
  a tool error naming the key, valid calls retain defaults/types/results, every root schema has
  `additionalProperties: false`, and the batch child definition is strict. Assert generated
  all-NULL guidance, populated-plan permission rules, JSON-looking textual-plan preservation,
  and string-encoded non-text compatibility still behave as the accepted H01 tests require.

### CL-002 — Validate unlocked arguments before plan guards and handlers

<!-- Assumption: The existing physical registry lock deliberately precedes schema validation so a
stale/static caller cannot use schema differences to substitute for authorization. After that
lock succeeds, malformed argument shape must be rejected before plan comparison, budget
consumption, finalizer resolution, managed handler start, or provider work. -->

- **What to change:** Add a validation-only step in `RegistryFastMCP.call_tool()` after
  `registry.require_unlocked()` and tool lookup but before constructing or executing the guarded
  plan invocation, finalizer, managed dispatch, or handler. Reuse the registered tool's existing
  FastMCP metadata pre-parser and Pydantic argument model; do not call the function and do not
  duplicate field logic. Preserve the raw argument mapping for the existing timeout, plan-binding,
  and execution path, so this step changes only rejection ordering and performs no normalization
  visible to those contracts. Ensure validation errors continue through the normal MCP
  `ToolError`/Layer-2 safe-exit path.
- **Where:** `src/pyocd_debug_mcp/kernel/registry.py::RegistryFastMCP.call_tool()` and at most one
  small private validation helper in the same module.
- **Exact intended behavior:** A locked hidden action called with an extra field still returns the
  established registry-lock prerequisite refusal because authorization is checked first. The
  same action, once unlocked by a valid plan, rejects an extra field as a schema error before the
  plan guard or handler, does not consume/relock/replace the plan, and emits no list-change
  notification. A batch child's extra argument follows this exact common child path and is
  rejected before its handler; direct and batch paths therefore cannot silently strip different
  inputs. Wrong JSON types receive the same schema-first treatment after unlock. Valid planned
  calls still reach the existing immutable-binding check and execute once.
- **Must remain intact:** Preserve unknown-tool behavior; lock-before-schema precedence;
  registry revision tracking and notifications; guarded plan comparison; budget/permission/gate
  enforcement; managed operation ownership, timeouts, board serialization, finalizers, context
  injection, output conversion, and exception wrapping. The validation-only step must not execute
  a handler, consume a plan, send a notification, bind hardware resources, or parse the caller's
  values differently from the later real `Tool.run()` call.
- **Objective verification:** Through a real `RegistryFastMCP.call_tool()` test context or the
  smallest production-faithful harness, the spec tester must record handler and guard counters.
  Assert: locked+extra yields the lock error with zero validation-dependent execution; after
  unlock, direct+extra yields a schema error naming the key with guard/handler/budget counters
  unchanged; valid exact arguments invoke the guard and handler once; and a batch child argument
  extra produces the same schema classification with the child handler and plan consumption at
  zero. Include a no-op list-notification/state control or equivalent registry revision assertion.

### CL-003 — Report failed batches as failed MCP results without losing structure

<!-- Assumption: `status="batch_failed"` plus a populated `failure` object is an operation
refusal/failure, not a successful tool result. Preserve that payload as machine-readable error
content and stop at the first failing child; only `batch_completed` is an MCP success. -->

- **What to change:** Keep `build_batch_handlers()`'s current sequential loop and payload
  construction. When a child exception populated `failure`, raise the normal MCP `ToolError` with
  the exact serialized `batch_failed` body (and one safe-exit reminder) instead of returning it as
  a successful result. Continue returning the existing `batch_completed` body for all-success
  batches. Do not discard the completed prefix or collapse the child error into prose.
- **Where:** `src/pyocd_debug_mcp/tools/batch.py`, limited to the result boundary and the smallest
  required error import/type handling.
- **Exact intended behavior:** On the first child refusal, later children do not run; the MCP
  result has `isError: true`; its text still contains one parseable JSON object with
  `status="batch_failed"`, shared `board_id`, the ordered completed prefix, and `failure.index`,
  `failure.tool_name`, `failure.error_type`, and the original child refusal message. The exact
  server-returned one-child fallback for `read_serial` still dispatches once through the child's
  normal lock/plan/gate path, preserves the absent-active-connection refusal, relocks/invalidates
  according to the existing plan engine, and emits its existing one visibility notification.
  A fully successful valid batch remains `isError: false`, `status="batch_completed"`, ordered,
  and otherwise byte-semantically unchanged.
- **Must remain intact:** Preserve outer and child structural prevalidation, same-board
  enforcement, nested/unknown-tool rejection, child ordering, first-failure stop, completed-prefix
  results, JSON sorting/compactness, safe-exit de-duplication, batch timeout aggregation, board
  reservation, and ordinary child dispatch. Do not add retries, caps, rollback, hardware-specific
  behavior, or new batch authority.
- **Objective verification:** The spec tester must exercise the registered MCP boundary and assert
  outer extra and child-envelope extra fail with their exact key and zero child dispatch; a
  child-argument extra yields a structured error and zero child handler execution; and the exact
  one-child absent-board fallback yields an MCP error containing the unchanged structured child
  refusal. The regression tester must use deterministic fake children to prove successful
  multi-child ordering/results, completed-prefix retention on a later failure, first-failure stop,
  safe-exit exactly once, and unchanged batch timeout/board-reservation behavior.

### CL-004 — Coalesce nested dispatch discovery notifications

<!-- Assumption: A client issued one outer `tools/call`, so one discovery revision caused by its
batch child must produce one `notifications/tools/list_changed`, not one per internal
`RegistryFastMCP.call_tool()` stack frame. The outer call remains responsible for reporting the
aggregate revision; direct non-nested calls retain their existing behavior. -->

- **What to change:** Make `RegistryFastMCP.call_tool()` track nested dispatch ownership with the
  smallest concurrency-safe, instance-local/context-local mechanism. Keep a revision snapshot for
  the outer public call, suppress duplicate sends from inner batch-child frames, and send exactly
  one notification from the outermost frame if the advertised registry changed anywhere in the
  nested call. Always restore nesting state on success, refusal, timeout, cancellation, and
  validation error.
- **Where:** `src/pyocd_debug_mcp/kernel/registry.py`, limited to common call nesting and the
  existing revision-change notification decision. Do not add batch/action/board name branches if
  ordinary nested dispatch depth can express the contract.
- **Exact intended behavior:** A direct plan activation or direct planned action that changes
  visibility still emits exactly one notification. An exact `action_batch` fallback whose child
  consumes/relocks its plan also emits exactly one notification before the outer response,
  despite the internal child call. Nested calls that do not change the advertised set emit none.
  Sequential unrelated outer requests each retain independent notification decisions; concurrent
  tasks and separate `RegistryFastMCP` instances do not share depth/revision state. An inner
  exception does not leak nesting state into the next request.
- **Must remain intact:** Preserve the list revision counter, advertised set, notification method
  and payload, response ordering guarantees, single-outstanding-client compatibility, and direct
  call semantics. Do not debounce with time, global mutable state, swallowed notifications, or
  action-specific exclusions.
- **Objective verification:** The spec tester must use a fake session/context that counts
  `send_tool_list_changed()` calls and prove direct transitions notify once, exact nested
  batch-child transitions notify once, no-transition direct/nested requests notify zero, and a
  nested failure is followed by a clean independent request. The regression tester must cover
  concurrent task/context isolation or an equivalent deterministic context-locality assertion
  without sleeps.

### CL-005 — Add independent focused and adjacent regression gates

- **What to change:** Add two new, disjoint tester-owned test modules. The spec suite attacks
  CL-001 through CL-004 at the real registered FastMCP boundary, including schema publication,
  error signaling, ordering, handler/guard counters, notification coalescing, and the exact
  valid/invalid control matrix.
  The regression suite traces the changed registration and dispatch paths across generated plans,
  ordinary direct tools, locked/unlocked tools, `action_batch`, context/result conversion,
  notifications, finalizers/timeouts, and existing successful/failing batch behavior. Each tester
  writes one isolated command and manifests only its own new file.
- **Where:** Spec tester only:
  `tests/test_h01_strict_mcp_boundary.py`. Regression tester only:
  `tests/test_h01_strict_mcp_regressions.py`. The exact filenames may be adjusted by a tester only
  to avoid a verified collision, but they must remain separate and H01-specific.
- **Exact intended behavior:** Tests reproduce request-190 semantics without hardware and fail on
  the pre-repair server. They distinguish registered-boundary behavior from direct handler/model
  surrogates. Error assertions require the injected key, no false success marker, correct
  lock-versus-schema precedence, and zero unintended handler/guard/plan-state work. Positive
  controls prove exact valid direct and batch requests still work and that a child failure is
  structured but marked as a tool error.
- **Must remain intact:** Test roles do not edit production, existing H00/H01 tests, configuration,
  dependencies, lockfiles, manifests owned by another role, fresh experiments, or hardware state.
  No network, board, serial port, probe, sleep-based race, or source-tree mutation is allowed.
- **Objective verification:** The neutral harness must run both tester-recorded commands in the
  same iteration with exit code zero and untampered manifests. The main model then reruns both
  focused suites, the exact host-only registered-boundary reproducer, accepted H00/H01 focused
  suites, `uv lock --check`, build, Ruff, Pyright, test collection, and full pytest.

## Out of scope / must not change

- Fresh H01 specification, amendments, harness, evidence, attestation, test-agent session,
  firmware, fixture, board, serial/provider state, or hardware.
- Plan field definitions, plan text/NULL semantics, plan budgets, permissions, gates, immutable
  bindings, visibility rules, notification rules, finalizers, timeouts, setup, recovery, memory,
  flash, debug, or RF behavior except where CL-001/CL-002 require the general correction from
  silently ignored unknown arguments to schema rejection.
- Existing accepted `README.md`, `pyproject.toml`, `uv.lock`,
  `kernel/processes.py`, `tools/plans.py`, and all four accepted H00/H01 tester files.
- Per-board/tool/OS allowlists, arbitrary caps, dependency changes, SDK forks, global monkeypatches,
  hostile-input defenses, unrelated docs, formatting sweeps, or cleanup.
- Existing contracts not named for change remain unchanged.
- No commits, pushes, deployments, package publication, flash, erase, connection, or generated
  distributable/runtime artifacts during implementation.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, and adjacent behavior touched by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, gate commands, existing accepted files,
  configuration, dependencies, or lock state.
- The doer edits production only in `src/pyocd_debug_mcp/kernel/registry.py` and
  `src/pyocd_debug_mcp/tools/batch.py`; each tester owns only its one new H01 test module.
- Every doer/spec/regression role rereads the complete `../.codex/design_charter.md` before
  analysis, immediately before its first edit, between CL-001/CL-002/CL-003 feature boundaries,
  before verification, and before its final verdict, and records those checkpoints in its final
  message. Missing checkpoints make the role incomplete.
- Pre-repair accepted file hashes listed above remain exact.
- The neutral gate is followed by main-model source/diff inspection, charter rereads at
  pre-verification and post-risky-diff checkpoints, the focused and full locked repository gate,
  and a fresh installed-runtime black-box retest before the H01 test agent resumes.
