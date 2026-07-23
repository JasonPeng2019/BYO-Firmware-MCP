# HIL Results — 2026-07-23

Live MCP registration: `byo-hil-temp-20260723-163714`  
Server run: `run-20260723T233834Z-77642c33`

Cleanup: the temporary MCP registration was removed after the final disconnect.

All board operations below used only that registration's live MCP tools. Exact
tool-call transcripts are retained in `testing_folder/agent/nested-agent.jsonl`.

| Contract | Nordic nRF52840 DK / nRF52840-QIAA | ST NUCLEO-L476RG / STM32L476RGT6 |
|---|---|---|
| 1. Baseline + `connect_under_reset` with conflicting ambient routing overrides | **BLOCKED** — the live inventory exposed two indistinguishable J-Link debug probes (identifiers ending `710208` and `854191`) and required an explicit friendly-probe assignment. No ordering-based association was made. | **BLOCKED** — baseline setup and `board_validate` passed, but `connect_under_reset-plan` required a recorded normal-attach failure; ordinary setup attach succeeded. The registered live surface also supplied no controlled second-registration/session mechanism with which to prove deliberately conflicting ambient `PYOCD_*` values. No under-reset action was invoked. |
| 2. Multi-output artifact selection | **BLOCKED** — no authoritative Nordic probe assignment/setup. | **PASS** — candidate A/B real Cortex-M4 ELF+MAP builds succeeded. With both present, `native_build` returned `artifact_selection: explicit_declaration_required` and selected no artifact. Explicit selection of each candidate succeeded. Evidence: `testing_folder/work/stm32_hil/build/`; transcript. |
| 3. Known test firmware flash; final state only observed | **BLOCKED** — no authoritative Nordic probe assignment/setup. | **PASS** — collected candidate D was flashed with `flash_application`; server reported target left running and subsequent `get_state` observed `RUNNING`. Artifact SHA-256: `b7d6857bed423329709b1ad7faa64d50f826791dae696a0d7d63e116c7e168ab`. Evidence: `testing_folder/work/stm32_hil/collected_d/`, transcript. Natural unobservable-reset behavior: **NOT EXERCISED**. |
| 4. One typed recovery; old session invalidation; reconnect/reflash/validate | **BLOCKED** — no authoritative Nordic probe assignment/setup. | **NOT EXERCISED** — live `get_board_info` reported `recover_mode: (none)` and `requires_recover_validation: False`; no typed recovery contract was available, so no erase/recovery action was attempted. |

## ST identity and setup evidence

- Probe: ST-Link, stable identifier ending `233337`; UART attachment resolved to COM12 but UART was not used.
- Profile target: `stm32l476rgtx`; validation passed with expected `STM32L476RGT6` and Cortex-M4 CPUID compatibility identity `0x410FC241`.
- Initial setup required official CMSIS-Pack support. The downloaded official Keil `STM32L4xx_DFP` 3.1.0 pack is at `testing_folder/work/packs/Keil.STM32L4xx_DFP.3.1.0.pack` (SHA-256 `5672383C07FBDCEE0E471A33F4F8BEB2E1F3200BC999244DCD6858E0E8E8203F`). The server setup report and validation reports are under `testing_folder/artifacts/.firm/setup/` and `testing_folder/artifacts/.firm/validation/`.

## Test-image notes

- Temporary source/linker files only: `testing_folder/work/stm32_hil/main.c` and `testing_folder/work/stm32_hil/stm32l476rg.ld`.
- Candidate A and C were rejected before any flash write because their vector initial-stack values were outside the server-verified writable primary SRAM range. Candidate D corrected this and flashed successfully.
- The board was disconnected only after final state observation; no production source was edited and no recovery/mass erase was performed.
