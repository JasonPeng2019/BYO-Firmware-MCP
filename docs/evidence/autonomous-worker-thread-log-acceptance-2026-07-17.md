# Autonomous worker-thread LED log acceptance

Date: 2026-07-17

Result: **passed**

This is the stricter repeat of the model-easy/native nRF blinking LED
acceptance. Unlike the earlier ON/status/OFF/status proof, this run required
UART lines emitted from inside the LED worker thread and a bounded proof that
the worker stopped toggling after OFF.

## Isolation and readiness

- Fresh sibling repository: `C:\Users\Jason\Documents\Jason\FirmCLI\za717`
- Initial contents: `.git` only
- No initial source, build, board YAML, or `.firm` state
- Existing `NF Board` profile correctly routed to validation first
- Validation, configuration readiness, live-session readiness, code readiness,
  UART attachment readiness, and UART-work readiness all passed before source
  creation

The acceptance agent received no MCP implementation details and performed all
application/configuration writing in the sibling repository.

## Build, safety, and flash

- Zephyr target: `nrf52840dk/nrf52840`
- Short external build root: `C:\zb717`
- Final ELF SHA-256:
  `f229dc2b43741f2eb419b54b871ba139c84f43505591c449e489baa18c19fa2a`
- Final safety refresh and revalidation: passed
- Flash: guarded `flash_application-plan` plus the exact server-returned
  one-child fallback
- Server result: final ELF flashed within its mapped application partition;
  target left running

## Worker-thread UART proof

One state-preserving UART exchange on the resolved board UART at 115200 baud
matched seven ordered conditions:

1. `ACK blink ON`
2. worker-emitted `[BLINK_WORKER] ON seq=`
3. worker-emitted `[BLINK_WORKER] OFF seq=`
4. `STATUS blink ON`
5. `ACK blink OFF`
6. `STATUS blink OFF`
7. `QUIET_WINDOW_DONE ms=1200 toggles_unchanged=yes`

The raw UART excerpt includes `[BLINK_WORKER] ON seq=1`,
`[BLINK_WORKER] OFF seq=1`, and `[BLINK_WORKER] ON seq=2`. The OFF command
waited for worker quiescence, and the device-side 1200 ms observation verified
that the worker toggle sequence did not change afterward.

The board was left running with blinking OFF and was then disconnected. No
direct pyOCD or serial bypass was used.

## Primary evidence

- Machine-readable result:
  `C:\Users\Jason\Documents\Jason\FirmCLI\za717\acceptance_evidence.json`
- Evidence SHA-256:
  `792b9694271a9c345f62a3c957a0b3f3a16a00f84614f560728e63fc4837a3db`
- Subagent-authored application:
  `C:\Users\Jason\Documents\Jason\FirmCLI\za717\src\main.c`

