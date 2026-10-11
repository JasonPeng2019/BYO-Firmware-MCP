# MCP Issue Monitor — Current Spec-to-Implementation Review

Review status: COMPLETE

Reviewed: 2026-10-10

Scope: section-level release review of the current working tree against
`sentry-evidence/specifications/mcp-issue-monitor-spec-new-f (1).md`

This is not a claim that every individual requirement ID has a dedicated test;
the rows below identify the implementation areas and evidence reviewed. The
named open IDs are checked explicitly to prevent implemented filler rules from
being mislabeled as remote-cutover work.

## Verdict

The specification review itself is complete. The local monitor implementation,
its repository-owned source-handoff client artifacts, and its fixed narrative
contracts are covered by behavioral tests. Client delivery in this preview means
attachment from a clone or extracted raw sdist; the installed server wheel does
not contain or attach the client workspace. Within that explicit local-delivery
contract, real OAuth/OpenID-authenticated remote delivery is the sole open
implementation item. `simulated_remote/`, `dsn=None`, and `FILLER_SIMULATED`
remain local filler and are not evidence of external delivery or an off-box witness.

## Section-by-section review

| Spec area | Status | Implementation and evidence reviewed |
|---|---|---|
| §1 Purpose | Implemented locally; remote pending | Problem reports, routine check-ins, and cumulative health snapshots are distinct in `src/pyocd_debug_mcp/monitor/`; the remote destination remains filler. |
| §2 Boundary assumptions | Implemented within stated boundary | Passive stdio lifecycle and project/store separation are covered by `tests/test_monitor_stdio_lifecycle.py`, `tests/test_monitor_passivity.py`, and `tests/test_monitor_wiring.py`. |
| §3 Detection signals | Implemented | Server signals S-1…S-3 live in classification/thrash paths. The tool-agnostic `byo-issue-report` skill is maintained at `skills/byo-issue-report/` and delivered byte-for-byte in `client-workspace/skills-src/firmware/byo-issue-report/`; it has concrete positive and negative criteria for every S-4…S-14 signal, and `tests/test_monitor_client_skill.py` prevents drift or omission. |
| §4 Required feature areas | Implemented locally; F-62/F-108/F-124 and AC-75 remote cutover pending | Trail, classification, grouping, async delivery, ledger, build profile, workspace binding, staleness block, narrative intake, nonblocking recording (F-27), and filler honesty (F-60) are covered across `tests/test_monitor_behaviour.py`, `tests/test_monitor_delivery.py`, `tests/test_monitor_ledger.py`, `tests/test_monitor_narrative_tools.py`, `tests/test_monitor_wiring.py`, and the CAN-only AC-69 regression in `tests/test_can_server_contract.py`. Real authenticated delivery, identity, cutover, and backlog replay remain open. |
| §5 Contracts | Implemented | `src/pyocd_debug_mcp/monitor/narrative.py` enforces exact issue/check-in shapes. The shipped JSON templates validate directly against those models in `tests/test_monitor_client_skill.py`; complete delivered summary fields are asserted in `tests/test_monitor_wiring.py`. |
| §6 Non-functional requirements | Implemented locally | Passivity, bounded queues/content, redaction, stable constants, and deterministic local behavior have focused tests in `tests/test_monitor_passivity.py`, `tests/test_monitor_redaction.py`, `tests/test_monitor_counters_trail.py`, and `tests/test_monitor_delivery.py`. |
| §7 Implementation risks | Mitigated and tested | Refusal noise, false thrash, sync I/O, stale anchors, ACK deletion, client-form confusion, and source-forgery overclaiming have regression tests. The OAuth/OpenID dependency remains an explicit release risk rather than simulated success. |
| §8 Acceptance criteria | Implemented where locally testable; remote criterion pending | The reconciled `sentry-evidence/plans/snapshot-change-coverage-map.md` names boundary and AC-104 evidence. Broader acceptance evidence resides in `tests/test_monitor_wiring.py`, `tests/test_monitor_delivery.py`, and the preserved result logs. Real remote authentication/delivery cannot pass before cutover. |
| §9 Retired identifiers | Reconciled | Current source, tests, and client artifacts use the active signal/forms/cadence contracts; no retired identifier is used as an implementation requirement. |

## Client artifact review

- `client-workspace/` is versioned in this repository and in the raw sdist, but
  intentionally absent from the installed server wheel. AT09 resolves it from
  an extracted source handoff, not from a developer's parent-directory
  `.agent-workspace`.
- `client-workspace/internal/manual_permissions.py` delegates to the package's
  canonical helper, avoiding a second file-protocol implementation.
- `client-workspace/bin/attach-project` copies assets into a real project-local
  `.agent-workspace` and creates, but never ships, its runtime directory. The
  attached `.gitignore` excludes that project-local runtime state from Git.
- Both destructive skills are human-only and selected by firmware mode.
- The issue-monitor criteria have one canonical definition under `skills/` and
  one byte-identical attached-workspace delivery copy selected by firmware mode,
  with two visibly different JSON templates matching server intake exactly.

## AC-104 honesty review

1. **Tier 1:** `NoOverclaiming.test_tier_one_combines_cumulative_survivor_with_staleness_block`
   checks the cumulative survivor and the exact two-week trip behavior together.
2. **Tier 2:** `ChainIntegrity.test_offline_rewrite_with_recomputed_chain_is_not_detected`
   proves that recomputing the public local chain evades local detection. Detection
   against a published head begins only after real remote cutover.
3. **Tier 3:** `NoOverclaiming.test_the_under_report_ceiling_is_documented`
   locks in the statement that source-level owner forgery is neither prevented nor
   detected and that usage is not unforgeable.

## Open item

Implement and verify real OAuth/OpenID-authenticated remote delivery, including
off-box ACK/head evidence and filler-backlog replay (F-62, F-108, F-124,
AC-75). Until then, all local filler acknowledgements must stay identified as
simulated and must not be used to claim off-box delivery. F-27 (nonblocking
recording) and F-60 (filler honesty) are already local requirements and are not
cutover work.

## Verification evidence

- Maintainer-reported historical focused monitor, client-skill, and AT09
  workspace review run (summary only; its ephemeral raw log was not retained):
  **290 tests run,
  288 passed, 2 skipped** (POSIX run; Windows-only hardening cases skipped),
  95.417 seconds.
- Current opt-in Codex/MCP monitor module: **17 live agent/MCP cases passed**
  against per-test isolated explicit monitor roots with the pinned
  `gpt-5.6-luna` model, plus **5 harness/unit cases** (22 module tests total).
- Ruff check and format check: passed for every changed Python file in this
  slice.
- Pyright: 0 errors for the changed Python files in this slice.
- Current release-candidate command output is retained under
  `sentry-evidence/results/0.1.8rc1-*.log`; those files, rather than an
  ephemeral `/tmp` path, are the durable evidence for current claims.
