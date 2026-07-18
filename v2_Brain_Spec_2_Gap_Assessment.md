# Current assessment of `v2_Brain_Spec_2_Gap_Sheet.md`

Assessment date: 2026-07-17  
Baseline: `Jason-MCP-v2` at `ffdcffd`, including the current uncommitted document moves

This is the decision ledger for the follow-on prompt series. It distinguishes missing product
behavior from optional test-harness expansion. The selection rule is the user's stated priority:
fix issues that materially affect a compliant agent and an ordinary firmware developer, without
adding vendor-specific ceremony or unlikely-edge-case hardening.

## Decisions

| Gap | Decision | Current assessment and implementation boundary |
| --- | --- | --- |
| GAP-01 | **Keep** | The literal sentinel `"no board"` is taught by the handshake but is currently routed as a new board name. Handle singleton, normalized variants, and mixed-list clarification before route generation. |
| GAP-02 | **Keep, with a simpler boundary** | Normal `connect` still exposes manual override parameters that belong to guarded `connect_override`. Make the public MCP schema profile-only and make its description point to `connect_override-plan`. Unknown override arguments may be rejected by MCP schema validation; retaining hidden compatibility arguments merely to return a custom redirect would keep the misleading schema alive. Batch validation must use the same schema. |
| GAP-03 | **Keep** | `load_setup_tool` currently unlocks but returns no tool-specific operating knowledge. Add bounded, distinct guidance for setup, validation, safety setup, and safety refresh. |
| GAP-04 | **Keep, coordinated with GAP-18** | Setup routes still force the agent to join several payload sections and infer call shapes. Add exact machine-readable load/next-call templates and accepted response shapes. After GAP-18, a volatile port path must not be a required plan value; it may remain an observation for diagnostics but must not be presented to the user as a fact they must supply. |
| GAP-05 | **Keep** | Requiring the agent to hash a datasheet that the server already reads and verifies is needless setup friction. Permit `null` so the server computes and records the digest; a supplied digest remains only an optional cross-check. No new hash tool is warranted. |
| GAP-08 | **Keep, narrowed to usable public flows** | Bootloader build evidence is a fingerprint source in the design, but refresh accepts application artifacts only. Add symmetric bootloader refresh support. Also remove any continuation ID that no public continuation tool can consume. The current pinned reviewed-evidence authority remains fail-closed; do not reintroduce arbitrary agent-supplied allowed ranges or a general evidence-authority path without a separate ADR. Existing source-drift handling should be reused rather than duplicated. |
| GAP-09 | **Partially keep; audit has one stale factual claim** | `_ALLOWED_KINDS[MEMORY_READ]` already excludes `UNKNOWN` and `PROHIBITED`; do not redo that change. The real gap remains: `SafetyPolicy` has no read-check method and neither raw nor symbol reads invokes containment. Check the exact bytes actually read (scalar width or block length), not an unrelated whole-symbol extent. Keep the policy lean and do not add another permission or plan layer. |
| GAP-10 | **Keep** | Both flash schemas advertise `target_address`, while runtime rejects every non-null value. Remove it and teach that load addresses come from the selected artifact. |
| GAP-11 | **Keep** | Ambiguous validation returns choices without a machine-readable retry recipe. Generate `accepted_response` from the actual error code/choice kind and keep it absent for terminal outcomes. |
| GAP-12 | **Keep the truthfulness goal; modify the document remedy** | Flash documentation is genuinely drifted. However, the current worktree has moved `Plan_Prompt_Contents_Spec.md` into `archive_docs/` while tests/contracts still treat the root file as normative. Do not silently make a historical archive a forever-synchronized source. First choose one clear authority: either restore the plan-prompt spec as a normative root document, or mark the archived copy historical and make `plan_defs.py` plus generated/live contract checks authoritative. In either case, every live plan definition must be schema-tested; avoid brittle prose parsing as the sole check. |
| GAP-18 | **Keep; prefer server-resolved ports** | `serial_port` is volatile and the route does not expose it machine-readably. Remove it from immutable plan/action parameters, keep stable `serial_id`, resolve the current path from inventory/cache at execution, and record the observed path in reports. Update the setup-only runner accordingly. |
| GAP-19 | **Reject as a required product gap; optional test infrastructure only** | The New Brain product contract is MCP-client-neutral. Making the benchmark layer depend on exactly two proprietary CLIs and hard-coded, possibly changing model IDs would reduce flexibility and does not repair server behavior. If a later acceptance task needs another provider, use a small configurable adapter and record the operator-selected provider/model/version; do not hard-code `claude-sonnet-5` or a guessed `Codex 5.6 luna` identifier, and do not make network credentials part of pytest. |
| GAP-20 | **Optional acceptance evidence, not an implementation gap** | Real-client smoke can improve confidence, but dynamic tool exposure is a protocol behavior that should remain covered by SDK/in-process tests and server-side locks. A bounded live Claude or other-client check is worthwhile only when the client, credentials, and authorization are available. It must not be a release-blocking semantic change, must not assume undocumented client behavior, and must not repeat destructive hardware work merely for model parity. Exact model mandates from the sheet are not adopted. |

## Previously closed items

- **GAP-06, GAP-07, GAP-13, GAP-14, GAP-15, and GAP-17 remain closed.** Preserve their
  reviewed-evidence, truthful-advertising, build-guidance, UART stop-on-failure, and strict-evidence
  behavior while making later changes.
- **GAP-16 remains closed as a feature.** Its runner must receive the small downstream CLI/schema
  adjustment required by GAP-18 (remove the volatile UART port argument); that is not a reason to
  redesign or duplicate the runner.

## Follow-on prompt policy

- Implement P4-01 through P4-05 only within the boundaries above.
- P4-06's mandatory dual-provider/model-pinning work is skipped. A provider adapter may be added
  later only when a concrete acceptance need justifies it, and it must be configurable.
- P4-07 remains useful as consolidated software verification, adjusted for whichever normative
  document location is chosen under GAP-12.
- P4-08 and the dual-model portion of P4-10 are optional/manual evidence, not missing product
  functionality. Hardware setup and representative acceptance remain useful when explicitly
  requested, available, and non-duplicative.
- Never turn unavailable hardware, provider credentials, or user authorization into a pass.

## Follow-on execution status

- **P4-01 completed (2026-07-17):** GAP-05, GAP-10, and GAP-18 are implemented. Setup plans now
  bind one stable UART selector while the server resolves and reports the current port; the
  datasheet digest is an optional cross-check computed authoritatively by the server; flash plans
  accept only the artifact whose own load addresses are validated. GAP-12 now uses
  `docs/plan-tool-contract.md`, deterministically generated from every live plan definition, while
  `archive_docs/Plan_Prompt_Contents_Spec.md` remains historical. The requested schema suite passed
  105 tests and affected-file Ruff checks passed.

## Verification performed for this assessment

With workspace-local temporary/cache directories:

```text
136 passed
```

across setup catalog/workflow/tools, handshake, UART, safety verification/regions/enforcement,
fresh-workspace runner, and reviewed-evidence suites. The plan-prompt suite passed `36` tests with
the one root-document synchronization test deselected. That test currently fails before assertions
because the worktree moved `Plan_Prompt_Contents_Spec.md` to `archive_docs/` without updating its
test and contract references. This is the repository-consistency choice called out under GAP-12,
not evidence that the live plan engine failed.
