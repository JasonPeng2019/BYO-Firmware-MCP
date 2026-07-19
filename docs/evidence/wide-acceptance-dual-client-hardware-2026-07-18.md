# Wide dual-client generalized-build hardware acceptance — 2026-07-18

## Scope and honest model disposition

The requested exact `gpt-4.6-luna` identifier was rejected by Codex CLI 0.144.5 with HTTP 400
before a model turn and is **not proven**. The substitute launch was configured for
`gpt-5.6-luna` at medium effort; Codex's stream did not echo independent provider model metadata, so
the evidence proves the launch configuration and agent report rather than a provider-resolved model
identifier. Claude's provider stream identified exact `claude-sonnet-5`, through Claude Code 2.1.76.

Both successful applications started in separate new repositories containing the nRF52840 PDF,
used the server-returned general helper `python -m pyocd_debug_mcp.native_build`, resolved the
pre-existing `C:\ncs\v3.3.1` workspace and toolchain `936afb6332`, and performed no helper
provisioning or download. Neither used `pyocd_debug_mcp.zephyr_build` or another Zephyr-specific
build backup.

Only the Nordic nRF52840/J-Link target (`683377322`, COM11) was mutated. The attached STM32/ST-Link
target (`066FFF514988525067233337`, COM12) was inventory-only and untouched. No unlock, mass erase,
manual erase, bootloader/security flash, or caller-supplied memory range was used.

## Result matrix

| Provider | Model evidence | Fresh setup | One general build | Guarded app flash | Root UART verification | Current-ELF debug |
| --- | --- | --- | --- | --- | --- | --- |
| Codex | configured/reported `gpt-5.6-luna`, medium (substitute) | pass | pass | pass | pass | pass |
| Claude Code | `claude-sonnet-5`, medium | pass | pass | pass | pass | pass via root periodic-breakpoint proof |

The two distinct codebases exercised different concurrent systems:

- Luna: LED/event scheduler, queued Fibonacci worker, Zephyr shell, mutex/atomic shared state,
  event prints, run/pause/period/job/stats/selftest/reset commands.
- Claude: LED telemetry producer, queued CRC32 worker, Zephyr shell, mutex-protected shared state,
  SAMPLE/WORK_DONE prints, stream/interval/enqueue/status/selftest/clear commands.

The root UART checks confirmed that commands changed behavior rather than merely echoing text,
output continued concurrently, quiet/off states suppressed recurring samples, rate changes were
measurable, and worker/event prints were observable.

## Artifact and plan evidence

### GPT 5.6-luna

- MCP run: `run-20260719T012333Z-0a54a1fb`
- setup plan: `plan-72a1200ccdf0d797`
- flash plan: `plan-bf5f013592bba46f`
- serial plan: `plan-144b5578f8eb3e22`
- breakpoint plan: `plan-f324a6d3da39a430`
- ELF SHA-256: `04cb0e314eb3fe74fee0396406fe1b75e2496ba39a92a211a33125729bb3f15f`
- map SHA-256: `ba717f6c05f140d16c5ef15d400ec395b0b5aa0ba6acca27b4a093b5c27cfca1`

The detailed agent timeline is
`wide-acceptance-gpt-5.6-luna-medium-2026-07-18-journey.md`; root UART evidence is
`wide-acceptance-gpt-5.6-luna-medium-2026-07-18-uart-self-verification.json`.

### Claude Sonnet 5

- MCP run: `run-20260719T013751Z-2c3174cb`
- setup plan: `plan-d2e3586dff966679`
- flash plan: `plan-9d0cb9cad5a39d1e`
- successful serial replacement plan: `plan-c5404e30593bdca0`
- ELF SHA-256: `4457cc7b2f644cdbc49413fc55cf2c7953d0d00f48ad08034a22f254a7bf90d6`
- map SHA-256: `ea87625cd122783ede4087a95cf9f5d31ae16527bc9a0cb868eaa7835912d3bb`

The provider's raw journey report is preserved as
`wide-acceptance-claude-sonnet-5-medium-2026-07-18-agent-raw-journey.md`. Its section 8 conclusion
that reconnect cleared the tested breakpoint is superseded: the recorded miss occurred before
compaction/reconnect, and a byte-count-only serial write did not prove the shell received/executed
the command while debug was attached. Root independently proved the generalized current-ELF
breakpoint path using the periodic `gpio_pin_set_raw` call, observing HALTED at `0x000006F4`, then
removing the breakpoint, resuming, and disconnecting. See
`wide-acceptance-claude-breakpoint-periodic-mcp-proof-2026-07-18.json` and
`wide-acceptance-claude-sonnet-5-medium-2026-07-18-uart-self-verification.json`.

A requested fresh Claude minimum debug retry could not begin because the provider returned its
five-hour usage limit before any task or MCP action. The Claude Usage Carve-Out applies only to that retry;
the full R4 setup/build/flash/UART journey itself completed. No retry success is fabricated.

Sanitized provider/launcher claim support is retained in
`wide-acceptance-provider-metadata-2026-07-18.json`.

## Failure-loop and server-fix evidence

All acceptance failures, diagnoses, plans, changes, and green retests are recorded in
`wide-acceptance-failure-loops-2026-07-18.md`. Exploratory breakpoint diagnostics are separately
indexed below. Genuine product gaps GAP-25 and GAP-26 are also in
`../../v2_Brain_Spec_2_Gap_Sheet.md` with their specifications, plans, focused tests, full-suite
results, and hostile-review disposition.

### Exploratory breakpoint diagnostics (not acceptance passes)

The following retained files are failed or exploratory diagnostics, not substitutes for the green
MCP proof. `direct-diagnostic` and `write-helper-diagnostic` used direct pyOCD/COM access under the
same user-authorized non-destructive Nordic application/debug scope to isolate transport behavior;
they did not unlock, erase, flash, or touch STM32. Their records show the Nordic target returned to
its normal sleeping/running state; the subsequent final MCP proof removed its breakpoint, resumed,
and disconnected. The five `mcp-*` files record failed or
inconclusive MCP reproductions and are preserved to avoid hiding the investigation:

- `wide-acceptance-breakpoint-direct-diagnostic-2026-07-18.json`
- `wide-acceptance-breakpoint-write-helper-diagnostic-2026-07-18.json`
- `wide-acceptance-breakpoint-mcp-reproduction-2026-07-18.json`
- `wide-acceptance-breakpoint-mcp-internals-2026-07-18.json`
- `wide-acceptance-breakpoint-mcp-serial-exchange-2026-07-18.json`
- `wide-acceptance-breakpoint-mcp-ready-probe-2026-07-18.json`
- `wide-acceptance-breakpoint-mcp-handle-direct-write-2026-07-18.json`

Final board state: Nordic application running with debug disconnected; STM32 untouched.
