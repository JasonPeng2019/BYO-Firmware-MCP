# HIL Setup Relock Regression — PASS

- Server run: `run-20260724T005330Z-a124f8a1`
- Board / MCU: `NUCLEO-L476RG` / `STM32L476RGT6`
- Scope: P1/P2 same-server setup-relock regression; no server restart.
- Destructive operations: none. No flash, erase, unlock, security change, or target write was requested or performed.

## Exact sequence

1. Routed the attached ST-Link board and submitted non-destructive P1 setup with the supplied datasheet.
2. P1 stopped at its real CMSIS-Pack target-resolution continuation (as required); it was not continued.
3. Submitted and started a separate same-board P2 setup in the same server run, retiring P1.
4. P2 reached its own CMSIS-Pack continuation. Submitted the already-present official archive facts exactly: `Keil.STM32L4xx_DFP`, version `3.1.0`, URL `https://www.keil.com/pack/Keil.STM32L4xx_DFP.3.1.0.pack`, SHA-256 `5672383C07FBDCEE0E471A33F4F8BEB2E1F3200BC999244DCD6858E0E8E8203F`, local path `C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\testing_folder\Keil.STM32L4xx_DFP.3.1.0.pack`.
5. The live continuation accepted the pack and resolved `stm32l476rgtx`; followed the live paired `board_fix_setup` redirect.
6. Paired setup fix completed and automatic validation passed. Live setup status reported `setup_ready` and normal guarded-work readiness.
7. Live setup-plan initialization reported the underlying `board_setup` action hidden and locked pending a new exact plan, proving completed P2 closed the paired/primary setup allowance while normal profile readiness remained available.
8. Normal profile connection reported already connected; then disconnected cleanly.

## Evidence

- P1 setup report: `testing_folder/artifacts/.firm/setup/setup-attempt-20bd62a3bd715a6c/report.json`
- P1 event log: `testing_folder/artifacts/.firm/setup/setup-attempt-20bd62a3bd715a6c/events.jsonl`
- P2 pre-continuation report: `testing_folder/artifacts/.firm/setup/setup-attempt-d963b1bc82820b91/report.json`
- Completed paired-fix report: `testing_folder/artifacts/.firm/setup/setup-attempt-12531771a9bf0d00/report.json`
- Completed paired-fix event log: `testing_folder/artifacts/.firm/setup/setup-attempt-12531771a9bf0d00/events.jsonl`
- Validation evidence: `testing_folder/artifacts/.firm/validation/validation-a5ef699a5ddc28ce/report.json`

Outcome: **PASS**. P1 was stopped at its genuine continuation; P2 superseded it, accepted the verified official 3.1.0 archive, completed paired setup/validation, relocked setup authorization, and was disconnected.
