# Wide acceptance failure loops — 2026-07-18

## Model preflight: requested GPT 4.6-luna unavailable

**Diagnosis.** Codex CLI 0.144.5 rejected the exact `gpt-4.6-luna` slug before a model turn with
HTTP 400: the model is not supported for the authenticated ChatGPT account. The local current-model
cache contains `gpt-5.6-luna` but no 4.6-luna. No API-key authentication is configured. This is an
external model-entitlement/name blocker, not an MCP-server defect.

**Plan.** Preserve the failed preflight honestly and exercise the currently supported Luna client,
`gpt-5.6-luna` at medium effort, rather than fabricate the requested model. The final report must
flag the substitution for user decision; exact 4.6-luna acceptance remains unproven.

**Change.** Configure the Luna leg with model argument `gpt-5.6-luna`, medium effort, and ephemeral
full-access Codex execution. The Codex stream did not independently echo the resolved provider model.
No server source changed.

**Retest.** Pending the fresh full journey below.

## GPT Luna R1: fresh artifact root was not forwarded to the MCP child

**Failure.** R1 reached one non-destructive setup and validation, but its setup report path and
profile appeared under `BYO-Server/.firm`, while the fresh acceptance root had no `.firm`. The run
was terminated before application authoring, build, artifact collection, UART, debug, or flash.

**Diagnosis.** The launcher set `BYO_MCP_ARTIFACT_ROOT` only in the outer Codex process. Codex MCP
children do not inherit that variable unless it is also declared in the invocation-scoped
`mcp_servers.pyocd-debug.env` map. The server therefore correctly used its checkout-root fallback;
this was launcher configuration error, not a server product gap.

**Plan.** Remove only R1's precisely identified task-created checkout `.firm` profile, map, setup
attempt, and three validation reports. Preserve every unrelated `.firm`/P4 evidence file. Create a
new repository and pass `BYO_MCP_ARTIFACT_ROOT`, `PYOCD_MCP_RUNS_ROOT`, `PYTHONPATH`, and
`PYTHONUTF8` explicitly in the MCP server config. Confirm the new profile/report paths are inside
R2 before allowing build or flash.

**Change.** R1 processes were terminated by verified PID tree. Exact task-created paths were removed
after resolving them beneath `BYO-Server/.firm`. R2 uses explicit invocation-scoped MCP environment
keys. No server source changed.

**Retest.** Pending a fresh R2 subagent.

## GPT Luna R2: loaded validation guidance omitted the assigned probe

**Diagnosis.** R2 reached the loaded `board_validate` route, but its generated call omitted the
run-scoped `probe_id` even though setup had already assigned a specific J-Link. The client followed
the server's incomplete loaded call literally. This was a product guidance gap (GAP-25), not model
negligence: with two probes present, a call that relies on rediscovery is ambiguous and must fail
closed.

**Plan.** Specify that loaded validation guidance must include the run-scoped assignment and refuse
to load when no assignment exists; implement the smallest wiring change, cover present/missing
assignment cases, run focused and full suites, and obtain hostile diff review before retrying.

**Change.** Added `docs/validation-probe-guidance-{spec,plan}.md`; wired the setup loader to the
run-scoped assignment lookup; made missing lookup/assignment return `setup_assignment_required`;
updated focused tests, contracts, and GAP-25 documentation.

**Passing retest.** Focused setup tests, Ruff, Pyright, the locked suite, and hostile review passed.
Fresh R3 then received a complete loaded call containing the assigned J-Link probe.
# Server guidance regression-test expectation correction

- **Diagnosis:** the new focused test correctly executed the exact loaded validation call, but the
  fixture's missing profile is represented by the validator's public terminal status
  `validation_incomplete`, not the guessed internal-sounding status `validation_config_missing`.
  The returned call and assignment check both executed; the product behavior was correct.
- **Plan:** correct only the test's terminal-status expectation, then rerun the focused suite.
- **Change:** updated the assertion to the validator's actual public status.
- **Passing retest:** recorded after the focused setup suites complete green.

# Product-contract hash rebaseline

- **Diagnosis:** the first full locked suite reached 1012 passing tests and failed only the
  intentional implementation-owner checksum pin for the three modules changed by GAP-25. The live
  MCP tool descriptions and schemas still matched exactly; no behavior test failed.
- **Plan:** advance the active contract version, update only the three changed implementation
  owner hashes, document the intentional behavior, rerun the contract test, then rerun the complete
  locked suite once.
- **Change:** rebaselined `server.py`, `setup_flow/setup.py`, and `tools/setup.py` in the active
  product contract as version 42.
- **Passing retest:** recorded after the contract test and complete suite finish green.

# Adversarial GAP-25 fail-closed finding

- **Diagnosis:** the first hostile diff review found that omitting the optional assignment lookup
  callback restored the old incomplete validation guidance. Production was wired correctly, but a
  miswired embedding could violate the specification's fail-closed requirement.
- **Plan:** make validation loading treat an absent callback exactly like a missing assignment,
  update the ordinary guidance fixture to model a valid run-scoped assignment, and rerun focused
  tests plus the hostile review.
- **Change:** `load_setup_tool(board_validate)` now returns `setup_assignment_required` whenever the
  callback is absent or has no probe binding; no incomplete call is loaded.
- **Passing retest:** recorded after focused tests and the second adversarial pass complete green.

# GPT 5.6-luna R3 native-target deviation

- **Diagnosis:** setup, complete loaded validation guidance, validation, and local-NCS readiness all
  passed in the fresh R3 root. The agent then guessed the legacy Zephyr target
  `nrf52840dk_nrf52840`; the local NCS 3.3.1 installation rejected it and advertised
  `nrf52840dk`. Its next command also transcribed the fresh project path incorrectly. Both are
  client input errors, not server/product gaps: the general helper correctly selected local NCS,
  stayed offline, invoked no provisioning, rejected the invalid target/path before compilation,
  and reported both errors precisely. No flash plan or flash action was called.
- **Plan:** terminate R3 before any hardware mutation, preserve its evidence, make the launch prompt
  name the exact locally advertised project-native target, and rerun the full journey with a new
  empty repository and fresh server run. No server code change is warranted.
- **Change:** stopped the R3 process tree and added the exact NCS 3.3.1 native target
  `nrf52840dk` to the test prompt. R3's `.firm`, logs, and failed build directories remain isolated
  in its own root.
- **Passing retest:** pending the fresh R4 journey.

# GPT 5.6-luna R4 orchestrator target correction

- **Diagnosis:** R4 correctly followed the strengthened prompt, but the orchestrator had copied the
  board name (`nrf52840dk`) rather than the qualified NCS 3.3.1 board target. The generic helper
  stayed local/offline and returned the authoritative valid targets, including
  `nrf52840dk/nrf52840`. The agent correctly classified the prompt mismatch and stopped before
  collection, planning, or flash. This was an acceptance-prompt defect introduced after R3, not a
  server or application defect.
- **Plan:** terminate R4, correct the prompt to the exact qualified target reported by local NCS,
  and relaunch from another new empty repository. Preserve R4 as failure evidence.
- **Change:** changed only the acceptance prompt target to `nrf52840dk/nrf52840`; no product code or
  hardware state changed.
- **Passing retest:** pending the fresh R5 journey.

# GPT 5.6-luna R5 application Kconfig failure

- **Diagnosis:** the exact qualified target and local-only general helper worked. Configuration then
  failed before compilation because the generated application set undefined NCS 3.3.1 symbol
  `CONFIG_DEBUG_INFO=y`. Inspection also found that the source passed exported
  `volatile uint32_t` counters to Zephyr atomic APIs, which would be a type/correctness defect if
  compilation proceeded. These are application-authoring errors, not server gaps. The attempted
  in-place build-directory deletion was correctly blocked by the client policy; no flash plan or
  flash action occurred.
- **Plan:** terminate R5, preserve it, teach the fresh acceptance prompt the exact supported debug
  setting and state-sharing type rule, and rerun from a new empty repository/server run.
- **Change:** added the two bounded NCS/application compatibility constraints to the prompt; product
  code and hardware authority are unchanged.
- **Passing retest:** pending the fresh R6 journey.

# GPT 5.6-luna R6 Windows path-length build failure

- **Diagnosis:** the general helper resolved local NCS correctly, but the generated project's long
  fresh-root/build paths exceeded a Windows tool invocation/path limit during the build. This was a
  test-root portability issue, not a reason to use a Zephyr-specific helper or download another SDK.
- **Plan:** preserve R6, use a genuinely fresh but short repository root and build directory, and
  rerun the journey with the same local-NCS/general-helper constraints.
- **Change:** R7 used `C:\g56r7` and a short build path. Product code did not change.
- **Passing retest:** R7's generic-helper build completed from local NCS without provisioning or a
  download.

# GPT 5.6-luna R7 shell correction and missing current-ELF debug authority

- **Diagnosis:** the first generated shell layout needed a bounded application-side correction;
  after that correction the single permitted build, guarded flash, and UART application checks were
  green. The subsequent symbol-debug call was nevertheless unavailable because a successful guarded
  application flash did not bind that already-validated ELF as the run-scoped current symbol
  artifact. Requiring a separate build declaration would contradict Safety Layer v2. This was
  product GAP-26.
- **Plan:** specify a run-scoped, successful-flash-only current-ELF binding; compute and verify the
  digest before backend mutation, bind only after backend success, keep binding across ordinary
  flash/reset but clear it with run lifetime, and never persist it. Add success/refusal/TOCTOU tests,
  focused verification, full verification, and hostile review.
- **Change:** added `docs/run-scoped-symbol-artifact-{spec,plan}.md`; added the binding hook in the
  flash path and current-ELF lookup in the memory/debug surface; added fail-closed digest/error and
  backend-order tests; documented GAP-26 and rebaselined the intentional contract hashes.
- **Passing retest:** 41 focused tests (plus a 28-test independent subset), affected Ruff, affected
  Pyright, contract tests, and the full locked suite passed; four hostile-review rounds ended with
  zero valid findings.

# GPT 5.6-luna R8 pre-review abort

- **Diagnosis:** R8 had been launched before the GAP-26 hostile review was complete. Allowing it to
  continue would have tested a change before the required review gate. It had called only
  `setup_overview`; no setup, build, plan, UART, debug, or hardware mutation occurred.
- **Plan:** terminate R8, finish the review/fix loop, then rerun from a new root and new MCP run.
- **Change:** stopped the R8 process tree and retained its prompt/config/PID evidence.
- **Passing retest:** the review reached zero valid findings, then R9 completed the full journey.

# GPT 5.6-luna R9 final green journey

- **Retest:** fresh `C:\g56r9` completed setup and validation, one general-helper build using local
  NCS, artifact collection, guarded application flash, all UART functions, current-ELF symbol read,
  breakpoint set/remove, resume, and disconnect. Root independently reverified UART state changes,
  observable event prints, worker output, and interleaving. No Zephyr-specific helper, download,
  unlock, erase, bootloader flash, STM32 action, or rebuild was used.

# Claude R1: task-local configuration lacked authentication

- **Diagnosis:** the isolated Claude configuration could not authenticate, so the provider exited
  before a model turn and before any MCP/hardware action.
- **Plan:** preserve R1 and retry with an isolated task configuration containing only the existing
  authorized credential material and MCP registration; do not expose unrelated project memory.
- **Change:** created a new fresh root and R2 launcher/config. No product or hardware change.
- **Retest:** R2 authenticated successfully.

# Claude R2: global configuration exposed unrelated memory

- **Diagnosis:** using the global Claude configuration fixed authentication but also loaded global
  memory that was outside the fresh-repository premise; the agent then asked the user rather than
  proceeding. It was stopped before setup or hardware action.
- **Plan:** copy only the minimum credential state into a new task-local configuration, keep global
  memory unavailable, and launch another fresh repository.
- **Change:** R3 used the bounded task-local configuration. No product code changed.
- **Retest:** R3 started cleanly and reached the MCP flow.

# Claude R3: unnecessary board-name question

- **Diagnosis:** the agent stopped to ask for a logical board name even though inventory and the
  acceptance prompt provided enough information to choose a neutral name. This was client behavior,
  not a server defect; no setup/hardware action occurred.
- **Plan:** make the fresh retry prompt explicitly authorize a neutral logical name while preserving
  server-owned routing and identity discovery.
- **Change:** R4 received that bounded clarification in a new empty repository.
- **Passing retest:** R4 completed the full journey without asking for user input.

# Claude R4 passive readiness recovery

- **Diagnosis:** a passive UART readiness wait opened after the one-time boot banner and therefore
  matched nothing while writing zero bytes. The application was running; the readiness predicate was
  inappropriate for a late-open serial port.
- **Plan:** replace the failed plan, not mutate it, and use a harmless carriage-return readiness
  probe that causes the recurring Zephyr shell prompt to be observed.
- **Change:** accepted a replacement serial-exchange plan with the probe; no rebuild or source change.
- **Passing retest:** the replacement exchange matched all eight application steps.

# Claude R4 UART-triggered breakpoint test

- **Diagnosis:** the agent report attributed the miss to a later context compaction/reconnect, but
  the recorded timeline shows the miss preceded that event. Root reproduction showed the breakpoint
  mechanism itself was valid; the unreliable part was assuming that a separate `write_serial` call's
  byte count proved the shell received and executed the command while SWD/J-Link was attached. Later
  buffered fragments confirmed delivery/execution was not established.
- **Plan:** retain the failed diagnostic honestly; independently exercise the same current-ELF
  breakpoint path with a periodic function whose execution does not depend on UART; then request one
  fresh Claude minimum-phase retry. Do not change the server without evidence of a product defect.
- **Change:** no product change. Root set a current-ELF breakpoint at periodic `gpio_pin_set_raw`,
  resumed, observed HALTED at aligned PC `0x000006F4`, removed it, resumed, and disconnected.
- **Passing retest:** `wide-acceptance-claude-breakpoint-periodic-mcp-proof-2026-07-18.json` is green.
  The fresh Claude R5 retry could not start because the provider returned its five-hour usage limit;
  this retry is skipped under the Claude Usage Carve-Out, with the last verifiable state recorded.

# Root Claude UART self-verification parser loops

- **Attempt 1 diagnosis:** ANSI prompt prefixes made a literal `startswith("SAMPLE ")` parser reject
  real sample lines. **Change:** strip ANSI/control prefixes before classifying lines.
- **Attempt 2 diagnosis:** the interval calculation included stale samples buffered before the
  `stream_on` acknowledgement. **Change:** measure only samples timestamped/received after that ACK.
- **Passing retest:** a clean full UART pass verified status, quiet-off behavior, interval change,
  on-state resumption, recurring SAMPLE prints, queued work, selftest, and command/output
  interleaving. Evidence is in the provider UART self-verification JSON.

# Final hostile-review evidence hygiene findings

- **Credential-copy diagnosis:** three preserved Claude launch records copied task-local credential
  files without guaranteed cleanup, and those task-created duplicates remained on disk. **Change:**
  securely bounded deletion removed the three exact task-created config directories; the launch
  records are now sanitized/non-replayable and explicitly forbid copying persistent credentials.
- **Model-claim diagnosis:** Codex launch arguments proved the configured Luna substitute, but its
  stream did not echo provider model metadata. Claude did emit model/version and five-hour-limit
  metadata. **Change:** downgraded the Codex claim to configured/reported and added a sanitized
  provider-metadata record supporting the Claude and usage-carve-out claims.
- **Diagnostic-index diagnosis:** seven failed/exploratory breakpoint records, including two direct
  pyOCD/COM diagnostics, were retained without being indexed; PID files were transient clutter.
  **Change:** indexed and labeled every diagnostic and its safety scope, and removed 14 PID files.
- **Passing retest:** a fresh hostile re-review confirmed the credential copies absent, launch
  records sanitized, model/carve-out claims evidence-bounded, all diagnostics indexed, and no PID
  clutter or secrets; it returned zero valid findings. No product source changed.
