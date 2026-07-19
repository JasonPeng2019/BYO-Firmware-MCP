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

### GAP-23 - Native-build cancellation ownership (closed in current working tree)

Hostile review found that the shared owned-process runner removed its recovery marker after
`KeyboardInterrupt` or another abnormal exit without first terminating the child process group. A
cancelled general native build could therefore leave `west` or compiler descendants running with no
marker for restart cleanup. A second hostile pass correctly found that ordering alone was
insufficient when cleanup was not confirmed and Windows escalation killed only the group leader.
`run_owned` now verifies group/tree cleanup, uses native Windows tree termination, retains the marker
on an unconfirmed cleanup, and has cancellation-order, marker-retention, and real-descendant timeout
regressions. Further hostile review found that leader-only restart cleanup and unconditional marker
removal on normal leader exit still admitted detached descendants. Owned Windows children are now
assigned to a kill-on-close Job Object before resume; POSIX groups are verified even after leader
exit; normal completion clears background descendants; recovery is tree/group aware; and marker
roots are stable across working directories. A Windows liveness bug exposed by these tests was also
fixed so exited-but-not-yet-released process handles are not treated as live identities.
The final portability pass found that POSIX marker identity still assumed Linux `/proc`, leaving
macOS without recovery markers. POSIX identities now use a directly declared cross-platform
`psutil` process-birth token, with no-`/proc` and stale-PID regressions.
The next pass found that inaccessible identities were still collapsed into absent processes.
Identity access denial is now an explicit indeterminate state: launch fails with owned cleanup,
hygiene retains the marker, and failed group signaling cannot be reported as successful cleanup.
The following review closed two identity races: POSIX cleanup now uses the stored session-leader PID
as its PGID instead of querying a reaped, reusable PID, and Windows identity uses the native
`GetLastError` result, treating only confirmed nonexistent-process codes as absence and every other
API failure as indeterminate.
Managed-operation cleanup was also deleting its process marker through `close_debug` before group
termination and ignoring an unconfirmed result. Operation resources now bind process and marker,
remove the marker only after confirmed cleanup, and retain it with an explicit cleanup error
otherwise.
The POSIX launcher still had a start-before-marker race when a fast leader became a zombie after
spawning a background child. Zombie leaders now retain their process-birth token until reaped, so a
marker is always created for that deterministic process group; a real POSIX fast-exit recovery test
covers the case.
Review then identified that a leaderless numeric PGID can later be reused and cannot safely authorize
restart signaling. POSIX hygiene now fails closed by retaining/refusing a marker once its leader
identity is gone; it never kills an identity-less group. Ordinary and cancellation cleanup still
verify the live owned group before removing the marker, while Windows crash cleanup remains enforced
by the kernel Job Object.
Because the stable marker root is shared per user, another live server could previously terminate a
first server/helper's owned process. Marker schema v2 now carries the owner PID and birth token;
hygiene refuses live/inaccessible owners and recovers only demonstrably orphaned markers. A bounded
stdio concurrency regression proves a second server leaves the first live helper untouched.
Leaderless POSIX markers were safely retained but initially surfaced only through a generic counter.
Hygiene now distinguishes benign live-owner skips from unresolved or truncated recovery, and MCP
startup aborts with an actionable marker-root error whenever an orphan cannot be cleaned safely.
Managed dispatch still returned a successful handler result while only recording an unconfirmed
subprocess cleanup internally. Successful sync/async dispatch now raises `OperationCleanupError`
for that fatal cleanup condition. A subsequent pass found abnormal handler, timeout, and cancellation
paths still hid it; they now surface the cleanup failure while chaining the original error as cause.
Dispatch, timeout, and cancellation regressions verify both errors and the retained marker.

### GAP-22 - Resolved local build environment in agent guidance (closed in current working tree)

The first Claude Sonnet 5 live attempt received the general helper but repeatedly searched `~/ncs`
and began a recursive home-directory glob because setup status did not expose the local environment
the helper had already resolved. The leg was terminated before any build, download, or flash.
`get_setup_status.build_guidance` now returns the resolved workspace, toolchain metadata, and build
executable (or a fail-closed discovery error) beside the exact general-helper argv, eliminating the
need to guess or scan. Focused and complete software checks plus hostile review passed. A fresh
Claude Sonnet 5 R3 repository then consumed the resolved `C:\ncs\v3.3.1` environment directly,
used the general helper, reported no download/update step, completed no observed download, and
finished the guarded hardware journey.

### GAP-24 - Equivalent JSON-number plan bindings (closed in current working tree)

The second Claude Sonnet 5 live attempt accepted a `serial_exchange` plan whose bounded duration
fields were represented as `3.0` and `0.0`, then twice received `plan/parameter-mismatch` when the
client emitted the server-generated fallback as the equivalent JSON numbers `3` and `0`. The
failure occurred before UART I/O or budget consumption, but made the advertised stable-client
fallback unusable for a normal MCP client.

Diagnosis: the plan engine compared lexical Python JSON encodings even for fields declared as the
schema's single `number` type. Plan: normalize only explicitly numeric action fields when their
floating-point value is integral, while retaining type-preserving binding for integer, JSON, and
other fields. Change: the action-parameter canonicalizer now applies that schema-aware
normalization at both acceptance and execution; a focused regression proves `3.0` and `3` bind
equally while the existing arbitrary-JSON `1` versus `1.0` authority check remains exact. Focused
checks, the complete locked suite, and hostile review passed. Fresh Claude R3 then executed the
server-generated `serial_exchange` fallback successfully, closing the live regression.

### GAP-21 - General native build helper (closed in current working tree)

Live dual-client acceptance exposed that setup status described native builds but did not return an
executable provider-neutral helper command. `pyocd_debug_mcp.native_build` now detects the project
provider, uses only a complete local SDK/toolchain, performs no provisioning, applies offline
guards to the child build, runs one
ordinary native command, and reports machine-readable artifact evidence. The helper grants no MCP
hardware or safety authority. Fresh GPT 5.6 terra and Claude Sonnet 5 runs both consumed the helper,
used local NCS with no observed or successful download, guarded-flashed their applications, and
passed independent UART
acceptance; the evidence is indexed in
`docs/evidence/from-scratch-dual-agent-hardware-acceptance-2026-07-18.md`.

### GAP-20 - Optional live-agent interaction evidence (closed for current nRF52840 scope)

The board-free half is now closed by current real-agent evidence: Claude Sonnet 4.5 and Codex 5.4
each followed the handshake, stopped the hardware path on the literal `no board` answer, and read
one exact all-NULL `board_setup-plan` without submitting a plan or exposing internal values. This
does not permission hard-coded providers in the product. The separately authorized hardware
half is now closed for the current nRF52840 scope by fresh GPT 5.6 terra and Claude Sonnet 5 builds,
guarded application flashes, agent UART conversations, and independent top-level UART transcripts.
Unavailable hardware, credentials, context, or user authorization in any future scope must still be
recorded as blocked rather than converted into a pass.

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

# GAP-25: loaded validation guidance omitted the assigned probe (2026-07-18)

- **Observed:** after a fresh setup, `setup_overview` returned a complete validation route, but
  `load_setup_tool(board_validate)` replaced it with a `next_call` containing only `board_id`.
  `board_validate` correctly rejected that call because the current run's assigned `probe_id` is
  mandatory.
- **Required:** loaded validation guidance must copy the probe identity from the run-scoped
  assignment and return an exact executable call. Without an assignment it must route back to
  `setup_overview`, not advertise an incomplete call.
- **Closure:** tracked by `docs/validation-probe-guidance-spec.md` and
  `docs/validation-probe-guidance-plan.md`.

# GAP-26: fresh flashed ELF was unavailable to symbol tools (2026-07-18)

- **Observed:** a fresh generalized build was collected, safely flashed, and verified over UART,
  but `find_symbol` still searched only the checkout's repository reference path. That path does
  not exist for a fresh logical board, and the uncaught lookup error also closed the debug session.
- **Required:** bind the successfully flashed application ELF for this server run, verify its digest
  before symbol use, retain the reference artifact only as a compatibility fallback, and return a
  bounded refusal rather than disconnecting when symbol metadata is unavailable.
- **Closure:** tracked by `docs/run-scoped-symbol-artifact-spec.md` and
  `docs/run-scoped-symbol-artifact-plan.md`.

# GAP-27: the general native helper supported only Zephyr/west (2026-07-18)

- **Observed:** a fresh bare-metal STM32 repository with a Makefile could not use the exact general
  helper returned by setup status; provider detection recognized only Zephyr CMake projects.
- **Required:** add a local-only, offline, managed GNU Make provider with generic fresh-build
  artifact discovery while leaving the existing Zephyr provider unchanged.
- **Closure:** tracked by `docs/generic-make-native-build-spec.md` and
  `docs/generic-make-native-build-plan.md`.

# GAP-28: STM32L476 catalog entry was not eligible for reviewed fresh setup (2026-07-18)

- **Observed:** the server knew the board and pinned pack but the catalog had no datasheet/runtime
  evidence anchors, so fresh setup could not derive a safety map. Runtime evidence also assumed
  every target was a built-in Python module even though STM32L476 support is CMSIS-Pack-backed.
- **Required:** pin the supplied official PDF and independent region evidence, verify the exact
  local pinned pack bytes as runtime target/SVD support, and preserve the standard full-flash
  application policy without adding normal-workflow steps.
- **Closure:** tracked by `docs/stm32l476-reviewed-setup-spec.md` and
  `docs/stm32l476-reviewed-setup-plan.md`.
## GAP-29 — Reviewed repository pack omitted from fresh setup target preflight

**Observed:** A fresh NUCLEO-L476RG setup accepted the exact reviewed datasheet/catalog record but
reported no exact debug target, because preflight checked pyOCD built-ins and only the new project's
empty pack manifest. GPT 5.6-luna then entered target research and began searching the host despite
the server's pinned STM32 pack being present.

**Required closure:** Include repository and active-project manifest declarations, but authorize an
exact target only through the resolved reviewed catalog plus the existing verified single-pack
selector. Never infer or download. Retry from a fresh subagent repository after software review.
## GAP-30 — Fresh setup discarded reviewed under-reset/clock policy

**Observed:** GPT 5.6-luna fresh STM32 setup reached exact reviewed target support, then both the
initial and paired retry failed before profile commit at `DebugPortStart` with `STLink error (20):
DP wait`. Root diagnosis found `_setup_connection_phase` passed `board=None`, discarding the
catalog's reviewed `under-reset` and 1 MHz debug settings.

**Required closure:** Pass an ephemeral server-owned board configuration containing only reviewed
catalog connection/identity facts and the selected compatible probe family into the shared backend.
Do not add recovery or destructive behavior. Re-run from a fresh subagent repository.

## GAP-31 — Validation ignored repository-pinned CMSIS-Pack support

**Observed:** GPT 5.6-luna fresh STM32 setup resolved, verified, and connected the reviewed
`stm32l476rgtx` pack target, but the immediately following validation reported
`validation/target-unavailable`. Validation checked pyOCD built-ins plus only the fresh project's
empty manifest, losing the repository-pinned authority already used by setup.

**Required closure:** Validation must accept built-in targets or the exact target returned by the
existing verified single-pack selector. Manifest declarations, stale global registration, missing
or changed bytes, and ambiguous providers remain insufficient. Retry from a new subagent repository.
