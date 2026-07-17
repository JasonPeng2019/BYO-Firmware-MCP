# M10 final validation — 2026-07-17

## Outcome

**Blocked: the repository is not complete under Task 20's definition.** The software
implementation and quality boundary are green, and the Nucleo hardware boundary passes, but
the designated nRF52833 DK is absent. Recovery and simultaneous-board acceptance therefore
remain blocked, and publication also remains blocked by the missing licensing decision.

## Software and performance

- Complete suite: **717 passed** with 63 expected legacy-profile deprecation warnings.
- Ruff, Pyright, package build, dependency check, imports, and 35-tool stdio boot/shutdown pass.
- Gate/freshness maximum: `0.004677` s
  against 0.250 s.
- Eight-device enumeration maximum:
  `0.000023` s against 10 s.
- NULL-plan/handshake maximum:
  `0.000018` s against 2 s.

## Hardware

- Nucleo L476RG: **pass**, probe `066FFF514988525067233337`, UART
  `COM12`. The focused halted recheck returned the accepted v2
  vector `0x20001180`; validation, guarded actions, cleanup/reconnect, and final gate closure pass.
- nRF52833 DK: **blocked**. The observed Nordic probe is
  `683377322` and reads
  `0x00052840` (nRF52840), not `0x00052833`.
- Recovery: **blocked before permission or execution**. No correct board, backup, or live map.
- Simultaneous two-board isolation: **blocked** because only one official board is present.
- New destructive operations during Task 20: zero flashes, erases, unlocks, or bootloader writes.

## Traceability

- Pass: 63
- Partial: 6
- Blocked: 72
- Blocked manual/procedural: 3
- Fail: 0

Every AC-1.x through AC-19.x and CC-1 through CC-22 row, including exact automated test nodes,
inspected assertion counts, procedures, status, and evidence, is present in the companion JSON.

## Open questions and risks

All Q-1 through Q-10 and R-1 through R-11 have explicit treatments in the companion JSON.
Material open items are the client cancellation census, soft human-approval legitimacy,
product decisions Q-3/Q-4/Q-5/Q-9/Q-10, pending Q-2/Q-8 confirmation, real POSIX lifecycle
coverage, the nRF/two-board bench, and licensing.

## Evidence

- Machine-readable final report: `docs\evidence\m10-final-validation-2026-07-17.json`
- Task 20 execution report: `C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\docs\evidence\m10-task20-execution-2026-07-17.json`
- External result root: `C:\Users\Jason\Documents\Jason\FirmCLI\M10-Final-Acceptance\2026-07-17_run1`
- Final Nucleo result: `C:\Users\Jason\Documents\Jason\FirmCLI\M10-Final-Acceptance\2026-07-17_run1\boards\nucleo_l476rg_attempt8\acceptance.json`
- Performance: `C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\docs\evidence\m10-performance-2026-07-17.json`
- Cancellation evidence: `C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\docs\evidence\m9-hardware-lifecycle-2026-07-17.json`
