# Fresh-workspace nRF52840DK validation

Validated on 2026-07-17 against the `Jason-MCP-v2` checkout and a physically
attached nRF52840 DK. The Zephyr application, client scripts, and evidence were
written and run by the delegated subagent in the sibling repository
`Jason-MCP-v2-nrf-agent`; the coordinating agent did not write application
code.

## Setup-first result

The test began with an empty, isolated artifact root. The setup NULL response
made the subagent stop before hardware access and request all three required
inputs:

- familiar board name and logical identifier: `NF Board` / `nf_board`
- exact board and MCU: `nrf52840dk` / `nRF52840`
- authoritative datasheet: `Nano_BLE_MCU-nRF52840_PS_v1.1.pdf`, SHA-256
  `c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422`

The real stdio MCP setup completed with J-Link probe `683377322`, USB serial
`000683377322` on `COM11` at 115200 baud, committed the profile only after live
checks, and returned `ready_for_code: true`. The exact transcript is
`../Jason-MCP-v2-nrf-agent/isolated-setup-retry-20260717/client-evidence/08-isolated-board-setup.json`.

## Application and hardware result

The subagent-authored application is a Zephyr multithreaded shell application
for `nrf52840dk/nrf52840`. The server refreshed safety from the selected ELF,
performed exactly one application-partition flash, and did not invoke
bootloader flash, unlock, recovery, mass erase, or direct pyOCD programming.

A single guarded UART exchange held one `COM11` handle for readiness and all
commands. It matched the prompt and observed, in order:

1. `blink on` -> `BLINK ON: LED worker enabled`
2. `blink status` -> `BLINK STATUS: ON`
3. `blink off` -> `BLINK OFF: LED worker disabled`
4. `blink status` -> `BLINK STATUS: OFF`

All four checks matched in 1.09 seconds, after which the MCP connection closed
cleanly. The machine-readable proof is
`../Jason-MCP-v2-nrf-agent/isolated-setup-retry-20260717/client-evidence/16-uart-one-open-sequence.json`; its human-readable summary is
`../Jason-MCP-v2-nrf-agent/isolated-setup-retry-20260717/client-evidence/17-final-hardware-result.md`.

The application/evidence repository is clean at commit `576b9c9`.

## Uncoached static-client repeat

After a separate fresh agent exposed that its callable bindings did not refresh
after `tools/list_changed`, the server added an exact accepted-plan transport
fallback and repeated the entire acceptance with another new agent. That agent
received no fallback schema or internal routing help. It started from a sibling
repository containing only `.git`, completed setup/readiness before source
creation, authored and built a different Zephyr multithreaded shell app,
refreshed safety, revalidated, flashed the application partition, and matched
`BLINK ON`, `STATUS ON`, `BLINK OFF`, and `STATUS OFF` in one COM11 session.

The repeat is documented in
`docs/evidence/autonomous-static-client-acceptance-2026-07-17.md`; its primary
machine-readable evidence is the linked sibling `acceptance/evidence.json`.

The final stricter repeat additionally proved actual worker execution through
tagged `[BLINK_WORKER] ON/OFF` UART lines and proved worker quiescence for 1200
ms after OFF. See
`docs/evidence/autonomous-worker-thread-log-acceptance-2026-07-17.md`.
