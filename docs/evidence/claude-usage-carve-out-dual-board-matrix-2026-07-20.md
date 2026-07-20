# Claude Sonnet 5 usage carve-out ? 2026-07-20

The dual-model/dual-board acceptance matrix invoked its explicit Claude Usage Carve-Out after the
provider returned: `You've hit your limit ? resets 7:10am (America/Los_Angeles)` from the exact
`claude-sonnet-5`, effort-medium session. No Claude completion is fabricated.

## nRF52840 bare-metal console leg

- Run root: `C:\firmcli-acceptance-20260720\nrf-baremetal-sonnet-r1`
- Session: `becb2a16-cddc-4a00-85c5-3b33efb2c05d`
- Last transcript: `claude-run-corrective2.jsonl`
- Last transcript SHA-256: `2273d4c928de3d79a238160102cbd19ef2f3987f927efa85cb00f6449ebb29d6`
- Verified before exhaustion: fresh setup from the datasheet; agent-resolved official Nordic pack;
  validation; provider-neutral exact-argv build; artifact collection; guarded application flash;
  live diagnosis of an LR-clobbering PendSV bug; minimal fix; rebuilt/recollected/reflashed image;
  live timestamped LED and periodic-print UART output.
- Still red at exhaustion: the console echoed `status` but lost its terminating byte/response while
  background output continued. The corrective turn was diagnosing RX polling loss and shared-UART
  serialization. It did not finish a fix or the required all-command interval proof. This leg is not
  claimed green.

## STM32L476 ThreadX leg

- Run root: `C:\firmcli-acceptance-20260720\stm-threadx-sonnet-r1`
- Session: `495d2e77-fa80-4d8d-9123-1c1839a6aeb3`
- Partial transcript: `claude-run-p1.jsonl`
- Partial transcript SHA-256: `01604fd3ba25e43a4b4b7d3954e91fc9e099790288c9994cfcc1aed286f736f9`
- Verified before exhaustion: fresh setup/validation and local-first inspection of
  `C:\Users\Jason\STM32Cube\Repository\Packs\STMicroelectronics\X-CUBE-AZRTOS-L4\2.0.0`.
- Last state: firmware authoring was in progress. No build, collection, flash, UART acceptance, or
  debug acceptance is claimed. The orchestrator terminated the still-running provider process after
  the same account had returned the five-hour limit in the nRF session.

## Remaining Sonnet cells

The nRF repair and STM32 repair Sonnet cells were not started after the same provider exhaustion.
They are skipped under the same explicit carve-out and are not claimed green.

Acceptance continues on the required GPT Luna cells, as permitted by the carve-out.
