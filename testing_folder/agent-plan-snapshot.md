# Setup-plan replacement HIL plan

## Scope

Verify only the setup-authorization changes from the latest prompt:

1. Start from fresh artifact/run roots with no attachment or setup cache.
2. Use one real attached board and its exact MCU identity.
3. In one MCP server process, accept setup plan P1 and start it until it leaves
   an incomplete continuation/allowance.
4. Accept replacement setup plan P2 for the same logical board without
   restarting the server.
5. Start P2, forcing SetupWorkflow to retire P1.
6. Complete P2's real continuation and invoke the paired `board_fix_setup`.
7. Prove P2 remains active/callable through the paired action and setup reaches
   its truthful terminal result.
8. Confirm the matching P2 allowance relocks after completion.

Prefer a real external-UART confirmation if the live inventory truthfully
identifies such an adapter for the chosen board. Otherwise use a naturally
required real setup continuation (for example exact target/CMSIS-Pack research)
to reproduce the same allowance-replacement state without falsely claiming a
physical UART association.

## Safety and storage

- Setup/identity validation only; no flash, erase, recovery, unlock, or security
  changes are required.
- All generated profiles, packs, evidence, logs, prompts, and reports stay under
  `testing_folder`.
- Hardware actions use only the temporary MCP registration.
- Disconnect deliberately and remove every temporary `byo-hil-*` registration.
