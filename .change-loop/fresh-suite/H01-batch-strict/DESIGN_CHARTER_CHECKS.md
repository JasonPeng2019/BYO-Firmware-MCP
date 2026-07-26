# H01 batch strictness — design-charter checkpoints

## Post-spec/request — 2026-07-25T03:10Z — root main model

Reread `../.codex/design_charter.md` in full after preserving the failing H01 evidence and before
planning this server repair.

- **Correctness:** silently dropping an unknown MCP field lets a compliant but fallible agent
  execute a request different from the one it supplied. A `batch_failed` result marked
  `isError: false` also misreports a failed operation. Both are correctness defects, not hostile
  input concerns.
- **Simplicity / one home:** strictness belongs at the registered MCP argument-model boundary.
  Batch child arguments should reuse the selected child's ordinary registered schema rather than
  grow a parallel validator.
- **Generalizability:** the repair must apply by tool registration/dispatch contract, with no
  board, action-name, host, or H01 constants.
- **Neatness / usability:** the published JSON Schema must match runtime rejection behavior.
- **Trust model:** rejecting malformed fields catches an honest agent mistake and prevents silent
  semantic drift. It adds no caps, sandboxing, path restrictions, or attacker-oriented defenses.
- **Scope:** preserve every accepted H00/H01 production change and all plan, permission, gate,
  budget, notification, timeout, finalizer, and hardware behavior. Do not clean up unrelated code.

## Post-plan — 2026-07-25T03:24Z — root main model

Reread the complete charter between the request and final plan text.

- The plan repairs the general registration/dispatch boundary rather than hardcoding the three
  H01 controls. This is the simplest one-home solution that does not leave identical silent-drop
  defects elsewhere.
- Physical lock precedence remains first. Schema validation moves ahead only of unlocked
  plan/handler execution, so the repair catches malformed caller intent without weakening
  authorization or adding paternalistic gates.
- `batch_failed` becomes an MCP error while retaining its structured machine details; this follows
  the charter's no-silent-failure rule without inventing retry, rollback, or limits.
- Production scope is limited to `kernel/registry.py` and `tools/batch.py`; accepted H00/H01 files
  and every unrelated hardware/tool contract are explicitly frozen by hash and acceptance gate.
- Each implementation/test role is required to reread the charter at every requested feature and
  verification boundary and record the checkpoints.

## Post-plan nested-dispatch audit — 2026-07-25T03:34Z — root main model

Reread the charter after tracing `RegistryFastMCP.call_tool()` through the exact server-returned
batch fallback. Both the child and outer call frames compare `list_revision` and can send the same
discovery-change notification.

- Added one plan item for context-local outer-request notification ownership. This is required
  correctness for the existing H01 one-request/one-transition contract, not speculative cleanup.
- The plan rejects timing debounce, global mutable state, and batch/action-name branches; the
  general nested-dispatch rule is simpler, concurrency-safe, and hardware-independent.
- The change remains in the already-authorized `kernel/registry.py` production scope and must
  preserve every direct-call notification behavior.

## Post-review-block amendment — 2026-07-25T03:45Z — root main model

Reread the complete charter after the independent plan reviewer found that CL-002 would validate
valid inputs twice.

- Double-running model validators is an avoidable correctness and compatibility risk. The A1
  amendment requires one pre-parse/model-validation pass and reuse of its validated result.
- The replacement path must preserve context injection, sync/async managed execution, result
  conversion, exception wrapping, guard ordering, and raw argument semantics rather than
  approximating the pinned SDK.
- A side-effecting validator counter and wire-level MCP controls turn the subtle lifecycle
  requirement into falsifiable evidence.
- The amendment adds no new production file, dependency, board/OS condition, cap, or unrelated
  refactor and keeps the original charter-aligned strictness/notification/failure goals intact.
## 2026-07-25 — sandbox-recovery checkpoint before resumed implementation

- The main/orchestrating model reread `.codex/design_charter.md` in full after the first
  doer/spec-tester turns were rejected as read-only and before resuming implementation.
- The repair remains limited to the verified H01 correctness defect: reject mistaken unknown
  arguments at the registered MCP boundary, report a failed batch honestly as an MCP error, and
  coalesce nested registry notifications without changing hardware scope, board knowledge, or
  legitimate intended operations.
- The Windows `workspace-write` child sandbox proved effectively read-only. Per the current
  change-loop contract, the same persistent role threads will resume with only that sandbox layer
  removed (`danger-full-access`); no combined approval/sandbox bypass flag is used.
- This recovery introduces no server feature, platform constant, hardware action, or new guard.
  It preserves the charter's correctness, simplicity, generality, and narrow-scope requirements.

- 2026-07-25T03:47:42.0343885Z � Main-model pre-verification checkpoint: reread the complete .codex/design_charter.md after neutral-gate recovery and before independent focused/full verification. The reviewed diff remains limited to one general registered-boundary strictness/dispatch policy plus honest batch error signaling; no hostile-input hardening, hardware-specific branch, dependency change, or unrelated cleanup was accepted.

- 2026-07-25T04:20:03.9003126Z � Main-model post-risky-diff/pre-acceptance checkpoint: reread all 20197 characters of .codex/design_charter.md (SHA-256 03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb) after the final tester-owned proof additions and 226-pass full repository gate. Independent source/diff audit accepts the two production files as the simplest general correctness fix: strict Pydantic registration, lock-before-once-only-validation, context-local outer notification ownership, and honest structured batch failure. No hardware-specific constants, adversarial-input defenses, arbitrary caps, dependencies, or unrelated production edits were introduced.

- 2026-07-25T04:20:51.6171538Z � Main-model pre-packaging/runtime checkpoint: reread the complete 20197-character charter before producing the isolated installed H01 runtime. Packaging must preserve the accepted general source byte-for-byte and must not become deployment, publication, hardware action, or an opportunity for unrelated edits.
