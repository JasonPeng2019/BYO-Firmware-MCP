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
| GAP-19 | **Reject the mandatory dual-provider/model-pinned form; optional generic adapter implemented** | The New Brain product contract is MCP-client-neutral. Making the benchmark layer depend on exactly two proprietary CLIs and hard-coded, possibly changing model IDs would reduce flexibility and does not repair server behavior. A later concrete request justified a small configurable agent-command adapter: it records the operator-selected adapter/model/version, accepts validated argv for any compatible CLI or thin wrapper, and leaves vendor-specific MCP registration translation to that trusted operator configuration. It does not hard-code provider model IDs or make network credentials part of pytest. |
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
- P4-06's mandatory dual-provider/model-pinning work remains rejected. The subsequent concrete
  request for provider-neutral acceptance justified a configurable agent-command adapter; it does
  not install providers, pin models, or claim a universal vendor CLI protocol.
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
- **P4-02 completed (2026-07-17):** GAP-01, GAP-03, GAP-04, and GAP-11 are implemented. The
  normalized literal `no board` sentinel cannot create a route; setup routes now carry exact
  loader/action composition and pre-filled server-known fields; each loadable setup tool returns
  its own bounded operating guide; and validation choice retries preserve selectors across a
  probe-then-UART conversation. The requested 61-test setup/validation/handshake suite, real stdio
  MCP flow smoke, affected-file Ruff, Pyright, and contract checks passed.
- **P4-03 completed (2026-07-17):** GAP-02 is implemented. Visible `connect` now accepts only
  `board_id`, uses the named project profile, and suppresses legacy launch-environment probe,
  target, and external-config overrides. Manual values remain available only through planned,
  hidden `connect_override`. Direct and real-composition `action_batch` regressions prove unknown
  override fields fail before backend dispatch. The requested 46-test suite, stdio schema smoke,
  affected integration tests, Ruff, Pyright, and active product-contract checks passed.
- **P4-04 completed (2026-07-17):** GAP-09's remaining behavior is implemented without changing
  the already-correct memory-read kind table. Raw scalar, raw block, and symbol reads now check the
  exact bytes their backend will access. Mapped RAM, flash, and peripheral reads pass; UNKNOWN and
  PROHIBITED spans fail before backend I/O with distinct remedies. Raw planned reads also perform
  containment as a pre-execution guard before budget decrement. The requested 53-test suite plus
  active product-contract checks, affected-file Ruff, and Pyright passed.
- **P4-05 completed (2026-07-17):** GAP-08 is implemented within its reviewed fail-closed boundary.
  Safety refresh accepts symmetric application and bootloader artifacts, but a bootloader rebuild
  can replace only regions inside an independent reconciled PACK/EVIDENCE-owned bootloader
  envelope; an old build-derived region cannot authorize its own replacement, and missing authority
  is an honest terminal maintainer blocker. Pack/official drift reloads current pinned sources,
  reruns two-source reconciliation, and reproduces retained build regions before atomic promotion;
  failures write a blocked report and preserve the closed old map. Drift payloads expose stable classifications, changed
  sources, and exact remedies; geometry and schema changes route through full safety setup plus
  validation. Boards without complete pinned reviewed evidence now return terminal
  `safety_setup_unsupported_board`, list the reviewed automatic board types, and expose no dead
  continuation. Safety setup/refresh responses no longer advertise correlation IDs that no public
  safety tool can consume; immutable reports retain them. Caller-supplied ranges and general
  evidence continuations remain deliberately absent.
- **Configurable agent-command adapter completed (2026-07-17):** the MCP server remains directly
  usable by any standard stdio MCP client. The optional R11 harness now also accepts an explicit
  trusted argv configuration for any agent CLI or thin wrapper that can consume a neutral MCP
  launch manifest (or a preconfigured MCP registration) and produce the benchmark result JSON.
  Provider CLI flags are not falsely treated as standardized; version/registration checks are
  optional, secret values are excluded from metadata, all subprocesses use finite timeouts, and
  local fake-provider integration tests exercise both file and stdout result transports. The
  legacy Codex adapter remains the default for backward compatibility; no provider or model is
  installed or pinned automatically.
- **P4-07 completed (2026-07-17):** the consolidated checkpoint is green. The final complete
  software run passed 949 tests with one explicit environment-dependent skip; Ruff, Pyright,
  wheel/sdist build, and a real MCP stdio initialize/list-tools smoke all passed. Minimal repairs
  aligned historical acceptance tests with archived documents, preserved extraction hashes as
  provenance rather than a live contract, updated stale test doubles/evidence line references,
  and narrowed test payload types for Pyright. No new genuine product gap was found. Exact
  versions, commands, intermediate failures, and final outcomes are in
  `docs/evidence/p4-07-software-verification-2026-07-17.json`.
- **P4-08 completed (2026-07-17):** the bounded board-free real-agent contract smoke passed for
  Claude Code 2.1.76 using exact Sonnet 4.5 (`claude-sonnet-4-5-20250929`) at medium effort and
  Codex CLI 0.142.2 using exact `gpt-5.4` at medium effort. Each run made only the expected
  handshake, literal `no board` overview, and all-NULL `board_setup-plan` calls; no populated plan,
  setup, validation, connection, safety, or hardware action ran, and final user prose contained no
  internal identifiers or JSON. Claude's provider-side auto circuit breaker remained disabled at
  medium effort, so the passing isolated run used a checkout-scoped strict MCP config and an exact
  bounded allowlist without an interactive permission prompt. Both passing bundles and the prior
  blocked attempts are retained under `docs/evidence/agent-contract-smoke-*-2026-07-17/`.
- **P4-09 completed (2026-07-17):** the non-destructive clean-root hardware smoke passed on the
  positively identified nRF52840 DK: J-Link `683377322`, stable UART identity `000683377322`
  resolved by the server to COM11, exact `nRF52840-QIAA`, and the local authoritative nRF52840
  PDF. The root began without the named profile; setup committed exactly one schema-v2 profile
  after live connection, same-run validation made `ready_for_code` true, and no code or
  destructive action ran. A different restarted Server Run proved disk artifacts did not restore
  the live gate (`live_session_ready=false`, `ready_for_code=false`) until `board_validate` passed
  again. Exact commands, MCP timelines, report IDs/hashes, and both run IDs are in
  `docs/evidence/fresh-setup-hardware-2026-07-17.json`.

## Verification performed for this assessment

P4-07 supersedes the earlier focused-only note: the complete repository suite passed **949 tests**
with one explicit skip, and whole-repository Ruff and Pyright are clean. Packaging and real stdio
discovery also passed. See `docs/evidence/p4-07-software-verification-2026-07-17.json`.
