# v2 Brain Spec 2 gap sheet for BYO-Server

Audit date: 2026-07-17 (revision 4, reconciled by P4-07)

This sheet records the current implementation state after P4-01 through P4-07. The superseded
pre-implementation findings are retained in Git history and in
`v2_Brain_Spec_2_Gap_Assessment.md`; they are not left labeled open after their fixes pass the
complete software gate.

## Selection policy

Only issues that materially affect a compliant agent or ordinary firmware developer belong here.
The product remains MCP-client-neutral, authority remains fail-closed, and provider-specific model
pinning is not product behavior. Live model or hardware evidence is recorded separately from
software correctness and is never fabricated when unavailable.

## Remaining evidence gap

### GAP-20 - Optional live-agent interaction evidence (hardware half only)

The board-free half is now closed by current real-agent evidence: Claude Sonnet 4.5 and Codex 5.4
each followed the handshake, stopped the hardware path on the literal `no board` answer, and read
one exact all-NULL `board_setup-plan` without submitting a plan or exposing internal values. This
does not permission hard-coded providers in the product. The remaining hardware half stays
separately authorized and non-duplicative; unavailable hardware, credentials, context, or user
authorization must still be recorded as blocked rather than converted into a pass.

## Already implemented - execution verified

The P4-07 complete run verifies all rows below. Citations identify the current implementation or
contract owner, not merely a similarly named test.

| Gap | Current implementation evidence |
| --- | --- |
| GAP-01 | Literal normalized `no board` and mixed-name clarification are handled before route creation (`src/pyocd_debug_mcp/server.py:4013`, `server.py:4125-4141`); the handshake teaches the sentinel (`tools/handshake.py:73-97`). |
| GAP-02 | Public `connect(board_id)` is profile-only (`tools/session.py:23-31`, `server.py:1004-1011`); guarded manual values remain in `connect_override` (`tools/session.py:47-63`). Batch schema regressions are covered in `tests/test_batch.py`. |
| GAP-03 | `load_setup_tool` returns bounded per-tool guidance from `_load_guidance` (`tools/setup.py:35-181`, `tools/setup.py:203-216`). |
| GAP-04 | Setup routes compose server-owned call templates, stable attachment choices, and ambiguity replies (`server.py:3980-4097`); agents need not invent internal IDs. |
| GAP-05 | `datasheet_sha256` is nullable in the setup plan (`guardrails/plan_defs.py:199`) and execution accepts null while retaining server-side byte review (`tools/setup.py:354-404`). |
| GAP-06 | Two-source reviewed-evidence reconciliation remains covered by `tests/test_reviewed_setup_evidence.py`. |
| GAP-07 | Reviewed automatic-support advertising remains covered by the setup catalog/workflow suites. |
| GAP-08 | Refresh accepts symmetric bootloader artifacts (`server.py:2984-3053`), returns stable drift classification (`safety/refresh.py:58-70`, `safety/refresh.py:348`), and unreviewed boards terminate honestly without a dead continuation (`server.py:2463`). |
| GAP-09 | Exact raw and symbol byte spans pass through `SafetyPolicy.check_memory_read` (`safety/enforce.py:137`, `tools/memory.py:161`, `tools/memory.py:235`); UNKNOWN/PROHIBITED spans fail before backend I/O. |
| GAP-10 | Flash plan schemas contain artifact-derived addressing and no caller `target_address` (`guardrails/plan_defs.py:333-353`); NULL guidance explains artifact-owned load addresses. |
| GAP-11 | Validation choice outcomes generate exact retry arguments while terminal outcomes retain null (`setup_flow/validate.py:169-188`, `setup_flow/validate.py:279-319`). |
| GAP-12 | Live definitions and generated `docs/plan-tool-contract.md` are checked for every `PLAN_DEFINITIONS` entry (`tests/test_plan_prompt_contents.py:35-37`, `tests/test_plan_prompt_contents.py:209-219`); the old prose spec is historical under `archive_docs/`. |
| GAP-13 | Advisory local-first build guidance remains implemented and tested through setup status. |
| GAP-14 | Reviewed catalog digest-mismatch behavior remains execution verified. |
| GAP-15 | `serial_exchange` stop-on-failure behavior remains covered by `tests/test_uart_capture.py`. |
| GAP-16 | The fresh-workspace runner is implemented; it accepts stable UART identity and does not accept a volatile UART port (`scripts/run_fresh_workspace_e2e.py:234`, `scripts/run_fresh_workspace_e2e.py:336-337`). |
| GAP-17 | Strict acceptance-evidence validation remains execution verified. |
| GAP-18 | Setup plans bind stable `serial_id`, not `serial_port` (`guardrails/plan_defs.py:188`); current port paths are resolved from inventory at execution (`server.py:3314-3339`) and recorded diagnostically. |
| GAP-19 | The mandatory dual-vendor/model-pinned proposal was rejected as contrary to provider neutrality. Its valid flexibility need is implemented by the optional trusted argv adapter (`agent_command_adapter.py:266-409`) and `--agent-config` benchmark option (`benchmark_support.py:1432`), with no provider allowlist or automatic model pin. |

## P4 prompt status

| Prompt | Status | Result |
| --- | --- | --- |
| P4-01 | Complete | GAP-05/10/12/18 schema truth and generated plan contract. |
| P4-02 | Complete | GAP-01/03/04/11 self-guiding setup and validation replies. |
| P4-03 | Complete | GAP-02 normal/override connection separation. |
| P4-04 | Complete | GAP-09 memory-read containment. |
| P4-05 | Complete | GAP-08 bootloader refresh and honest safety terminal states. |
| P4-06 flexible variant | Complete | Provider-neutral configurable agent-command benchmark adapter; the mandatory hard-pinned dual-provider form remains rejected. |
| P4-07 | Complete | Full software, static, build, and real stdio checkpoint passed. |
| P4-08 | Complete | Claude Sonnet 4.5 medium and Codex 5.4 medium passed the bounded board-free real-agent contract smoke; prior blocked launch attempts remain preserved as evidence. |
| P4-09 | Complete | A clean isolated nRF52840 DK root passed non-destructive setup, same-run validation/readiness, restart gate closure, and repeated validation on the positively identified bench hardware. |

## P4-07 consolidated verification

Evidence: `docs/evidence/p4-07-software-verification-2026-07-17.json`.

Final results on Windows, branch `Jason-v3-BYO`, commit baseline
`5a98858ca0213cb318b96a835d95f8bee863ba4d`:

- `uv run --locked pytest`: **949 passed, 1 skipped, 66 warnings**.
- `uv run --locked ruff check .`: **pass**.
- `uv run --locked pyright`: **0 errors, 0 warnings**.
- `uv build`: **wheel and sdist built**.
- real MCP stdio initialize/list-tools smoke: **pass**, with `initialization_handshake` advertised.
- the large `tests/test_zephyr_build.py` execution gate passed; its one environment-dependent case
  remained an explicit skip.

The first consolidated run exposed stale historical-document paths, a historical-manifest test
that still asserted live hashes, two stale test doubles, stale assertion line evidence, and test
mapping types that Pyright required to be narrowed. These were corrected without changing product
authority. The complete suite then passed again. No new genuine product gap was discovered.

## P4-08 board-free real-agent verification

Evidence bundles:

- `docs/evidence/agent-contract-smoke-claude-2026-07-17/`
- `docs/evidence/agent-contract-smoke-codex-2026-07-17/`
- `docs/evidence/p4-08-agent-contract-smoke-2026-07-17.json`

Both successful runs made exactly three advertised MCP calls, in order:
`initialization_handshake`, `setup_overview({"board_names":["no board"]})`, and one
`board_setup-plan` call with all 11 live plan fields null. Both observed `setup_no_board` with no
routes, submitted no populated plan, ran no setup/validation/connection/safety/hardware action,
and kept internal IDs and JSON out of user-facing prose.

Claude Code 2.1.76 used exact model `claude-sonnet-4-5-20250929` at medium effort. Its isolated
auto-permission attempts encountered the provider's service-side auto circuit breaker; a
checkout-scoped strict MCP configuration plus a bounded exact tool allowlist succeeded without an
interactive permission block and used at most 17,952 of 180,000 logged context tokens. Codex CLI
0.142.2 used exact model `gpt-5.4` at medium effort with invocation-scoped MCP registration and
exited successfully. Neither run used Fable, Opus, or a 5.6-Sol model. Failed preliminary attempts
remain inside the provider evidence directories and are not relabeled as passes.

## P4-09 clean-root hardware setup verification

Evidence: `docs/evidence/fresh-setup-hardware-2026-07-17.json`.

The scripted setup-only runner started with no profile in the isolated artifact root and used the
reviewed `nrf52840dk`, exact `nRF52840-QIAA`, J-Link UID `683377322`, stable UART identity
`000683377322`, 115200 baud, and the local authoritative nRF52840 PDF. Live inventory resolved the
UART identity to COM11; the caller never supplied a volatile port path. Setup returned
`setup_completed`, committed exactly one schema-v2 profile only in the recorded
`setup/core-profile-committed-after-connect` phase, and same-run validation returned the accepted
non-destructive `validation_passed_uart_not_configured` result. `get_setup_status` nevertheless
proved the stable UART attachment resolved and reported `ready_for_code=true` and
`ready_for_uart_work=true`.

A distinct restarted Server Run loaded the disk artifacts but initially reported
`live_session_ready=false` and `ready_for_code=false`, with the exact remedy to connect and run
`board_validate`. Repeating validation in that new run restored both values to true. No code,
flash, erase, bootloader write, or target unlock action ran. The evidence contains both MCP
timelines, exact commands, profile/report identifiers, SHA-256 hashes, readiness payloads, and the
two distinct run IDs.
# Live acceptance gap: exact general native-build helper (2026-07-18)

- **Observed:** `get_setup_status.build_guidance` described native builds but returned no exact
  general helper command, while the only executable helper was Zephyr-specific.
- **Required:** a local-only, provider-neutral native-build helper and exact returned command that
  discovers an installed NCS environment without download or toolchain mutation.
- **Closure:** tracked by `docs/general-native-build-helper-spec.md` and
  `docs/general-native-build-helper-plan.md`.
