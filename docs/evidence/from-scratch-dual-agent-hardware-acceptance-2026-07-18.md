# Dual-client from-scratch hardware acceptance ? 2026-07-18

## Acceptance result

**Passed.** GPT 5.6 terra at medium effort and Claude Sonnet 5 at medium effort each
started in a distinct empty Git repository containing only the nRF52840 product-specification PDF,
used the MCP server's generalized native-build guidance, built a two-thread Zephyr LED/UART
application with the installed local NCS, flashed only the application through the guarded plan
path, and passed both agent-driven and independent top-level UART verification.

The tested board was the recoverable nRF52840 DK attached as J-Link UID `683377322` with stable
UART identity `000683377322` (`COM11`, 115200 baud). The STM32 companion attachment was never
selected or mutated. No run invoked target unlock, mass erase, manual erase, bootloader flash, or
deployment outside the application partition.

## Client matrix

| Provider client | Exact model | Effort | CLI | Fresh-root result | Independent UART result |
| --- | --- | --- | --- | --- | --- |
| Codex | `gpt-5.6-terra` | medium | Codex CLI `0.144.5` | pass | pass |
| Claude Code | `claude-sonnet-5` | medium | Claude Code `2.1.76` | pass | pass |

Both launchers used their built-in full-access/non-interactive permission modes. Each prompt
explicitly prohibited the legacy `pyocd_debug_mcp.zephyr_build` helper and every Zephyr-specific
backup mechanism.

## Generalized build and local-NCS proof

Both agents used the exact form returned by `get_setup_status.build_guidance`:

```text
<server-python> -m pyocd_debug_mcp.native_build --project-dir <project> --build-dir <build> --target nrf52840dk/nrf52840
```

The guidance and helper resolved `C:\ncs\v3.3.1`, toolchain environment
`C:\ncs\toolchains\936afb6332\environment.json`, and west at
`C:\ncs\toolchains\936afb6332\opt\bin\Scripts\west.exe`. Build output reported local Zephyr
`4.3.99` / `ncs-v3.3.1`, local Zephyr SDK `0.17.0`, `No download step`, and `No update step`.
Machine-readable helper output set `helper_provisioning=false` and `offline_guards=true`. Neither
agent invoked the legacy Zephyr-specific helper. Orchestrator monitoring and complete agent tool/log
review observed no download command, network provisioning action, or successful download. These are
offline build guards and observed-run evidence, not a claim that the helper is an OS network sandbox
for arbitrary project scripts.

## GPT 5.6 terra leg

- Fresh project root: `acceptance-gpt-5.6-terra-20260718-r2`.
- Hardware-server run: `run-20260718T222207Z-5d4517f3`.
- Setup plan: `plan-0f577f5f0bb840c0`.
- Initial flash/UART plans: `plan-51840279596ed970`, `plan-f3d06923a4bc5377`.
- Corrected flash/UART plans: `plan-0e9f51543fc4ed1c`, `plan-1b77be18128ee161`.
- Final ELF SHA-256: `b252f3b94a333e32dd96423bfab77b905e2ab2c2c3a4dc12d64f71efa7ebb7c7`.
- Final map SHA-256: `bed176a9d7dc26b05533467151a15adb4f709e5acfb220b33d01054b4ff999bf`.
- Agent raw log: `codex-run.jsonl`, 552474 bytes, SHA-256
  `d42f08ecfbed89f41eb6fff64735ceb7d89350a2cc329c2860d7332f6548626a`.
- Agent report source SHA-256:
  `f96544d05257db987687d5c82c9b460615ee85d43cea8decfbcd04999e7f555f`.

The first live UART test exposed burst loss in polling input. The failure loop diagnosed the input
path, planned a Zephyr line-console fix, changed only the application, rebuilt through the same
general helper, reflashed through a replacement application plan, and passed the agent retest.
Top-level verification then exercised status, off, on, and 120/300 ms rate changes through one UART
open. It observed all responses, no ordinary toggles while disabled, resumed toggles after on, a
fast median interval of 0.14 s, a slow median interval of 0.3045 s, and command responses interleaved
with toggle prints. Full transcript: `from-scratch-gpt-5.6-terra-medium-2026-07-18-uart.json`.

## Claude Sonnet 5 leg

### Recovered launch/server failures

1. **R1 ? missing resolved NCS guidance.** Claude received the general helper name but not the
   already-resolved local environment, searched `~/ncs`, and began a recursive home scan. The leg
   was terminated before build, download, or flash. GAP-22 was specified, planned, fixed, reviewed,
   and software-verified before a fresh restart. R1 raw-log SHA-256:
   `010fc07c82e60209e89ac3ebeda79df184e5cfd6d6983bf34cbc17c072c4aad9`.
2. **R2 ? semantic JSON-number plan binding.** R2 successfully used local NCS, built, and guarded-
   flashed, but the server-generated stable fallback changed numeric `3.0`/`0.0` to equivalent JSON
   `3`/`0`; exact plan comparison rejected it before UART I/O or budget consumption. The leg was
   terminated. GAP-24 was specified, planned, fixed with schema-aware number canonicalization,
   hostile-reviewed, and verified with focused tests plus the complete suite before a fresh
   restart. R2 raw-log SHA-256:
   `d0082d9ad0a6ef096f2ca1276af824ecd85168927c1eed078f9a1a73e5c1313f`.

### Successful R3

- Fresh project root: `acceptance-claude-sonnet-5-20260718-r3`.
- Setup plan: `plan-894cd7df94e1d36d`.
- Initial flash plan: `plan-cb9ab5daf88b70a9`.
- Diagnostic flash/capture plans: `plan-697d7e09acd9b289`,
  `plan-f258a8861eea5fc5`, `plan-c9713257eaf318b2`, `plan-ae5eafc1849202da`.
- Final flash plan: `plan-299957683fc114ae`.
- Final successful UART plan: `plan-3c7271e5bb72bf8b`.
- Final ELF SHA-256: `04eec8b96e44aaf5d44bca4ef39295cc7bfb2b8b2dc74818bf9ab5e10fdf41e3`.
- Final map SHA-256: `e51816be9ed7bc5eb564a44d3dd32d9493bcf3d00eee79515287250a3e3e6b52`.
- Agent raw log: `claude-run.jsonl`, 2759302 bytes, SHA-256
  `1ae476866178af51d74220778d502cd79fe607f106dd25044ea72aee55249fd5`.
- Agent report source SHA-256:
  `3661322301e02647cf96c14f39d50239b0e985d8179a3c9596668e3c07d156c0`.

R3's first application UART test failed after receiving only the first seven bytes of a 13-byte
command. The agent performed the required diagnose/plan/change/retest loop: temporary byte-level
traces proved the nRF UARTE polling path lost the remainder permanently; the application changed to
interrupt-driven FIFO draining into a Zephyr message queue; the traces were removed; a clean local-
NCS general-helper rebuild and guarded application reflash passed. The agent's six-step exchange
matched status, rate 150, status, off, status, and on, with toggle prints interleaved and an explicit
disabled transition.

Top-level verification independently retained one physical COM open across status, rate 120,
status, off, status, on, rate 300, and status. Every response matched; the off window contained zero
ordinary toggles; on resumed them; measured medians were 122 ms and 302 ms; and toggle prints
continued around command traffic. Full transcript:
`from-scratch-claude-sonnet-5-medium-2026-07-18-uart.json`.

A first final-UART plan (`plan-700ae363e305d37b`) waited for a one-shot boot banner that had already
scrolled away and therefore sent no bytes. The retry plan used the recurring LED event as readiness
and passed without a code change.

After the result/report was complete, Claude Code's own project-memory hook wrote one environment
note outside the acceptance repository. The orchestrator removed that exact task-created file and
confirmed it absent. It did not execute MCP, build, UART, or hardware work and did not affect the
fresh-root evidence.

## Software gate for server fixes

After GAP-22/GAP-24 and the generalized helper/process-lifecycle changes:

- focused affected suites: pass;
- Ruff: pass;
- Pyright: 0 errors;
- complete locked pytest: **1010 passed, 3 skipped, 79 warnings**;
- final hostile diff review: zero valid major or critical findings.

Package/import and bounded stdio checks are recorded in `docs/verification.md` after their final
execution. The application-only failures did not change server source and therefore did not require
another server full-suite run.

## Evidence index

- GPT prompt: `from-scratch-gpt-5.6-terra-medium-2026-07-18-prompt.md`.
- GPT journey: `from-scratch-gpt-5.6-terra-medium-2026-07-18-journey.md`.
- GPT independent UART transcript: `from-scratch-gpt-5.6-terra-medium-2026-07-18-uart.json`.
- Claude successful-run prompt: `from-scratch-claude-sonnet-5-medium-2026-07-18-prompt-r3.md`.
- Claude journey: `from-scratch-claude-sonnet-5-medium-2026-07-18-journey.md`.
- Claude independent UART transcript: `from-scratch-claude-sonnet-5-medium-2026-07-18-uart.json`.
- Claude strict MCP configurations and launch scripts preserve the R1/R2/R3 launch settings.
