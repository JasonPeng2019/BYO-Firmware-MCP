# Final clean-run summary — PASS

Server run `run-20260724T005330Z-a124f8a1` completed the requested same-run P1/P2 setup-relock regression for `NUCLEO-L476RG` (`STM32L476RGT6`).

P1 was intentionally left at its actual CMSIS-Pack continuation. A new P2 setup was accepted and started without restarting the server, which retired P1. P2's continuation accepted the supplied, already-local official `Keil.STM32L4xx_DFP` 3.1.0 archive at `testing_folder/Keil.STM32L4xx_DFP.3.1.0.pack`, official SHA-256 `5672383C07FBDCEE0E471A33F4F8BEB2E1F3200BC999244DCD6858E0E8E8203F`, and official URL `https://www.keil.com/pack/Keil.STM32L4xx_DFP.3.1.0.pack`.

The live paired setup fix then completed and automatic validation passed. Live status was `setup_ready`; a live setup-plan initialization showed that normal `board_setup` authorization was again hidden/locked until a fresh exact plan, while normal validated profile readiness remained available. The already-connected profile was then disconnected.

Evidence:

- `testing_folder/artifacts/.firm/setup/setup-attempt-20bd62a3bd715a6c/report.json` (P1 continuation)
- `testing_folder/artifacts/.firm/setup/setup-attempt-d963b1bc82820b91/report.json` (P2 continuation)
- `testing_folder/artifacts/.firm/setup/setup-attempt-12531771a9bf0d00/report.json` (completed paired fix)
- `testing_folder/artifacts/.firm/validation/validation-a5ef699a5ddc28ce/report.json` (passing validation)

No destructive operation occurred: no flash, erase, unlock, security change, or target write.
