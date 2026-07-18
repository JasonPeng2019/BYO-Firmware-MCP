# Autonomous static-client hardware acceptance

Date: 2026-07-17

Result: **passed**

The acceptance agent received only the product goal and started from a new git
repository containing only `.git`. It did not inspect the server implementation
or receive the `action_batch` fallback schema from the supervising agent.

## Native setup behavior

The agent independently:

1. called the initialization/setup entry flow;
2. asked for a familiar board name;
3. detected that no matching YAML existed;
4. asked for the exact board type, MCU part number, authoritative datasheet,
   friendly probe/UART choices, baud rate, and setup permission;
5. submitted the populated setup plan;
6. used the exact accepted-plan fallback despite its static MCP callable list;
7. completed profile/safety setup and validation; and
8. required `setup_ready`, `configuration_ready`, `live_session_ready`,
   `ready_for_code`, `uart_attachment_ready`, and `ready_for_uart_work` before
   creating application source.

The resulting schema-v2 profile was committed by FirmStore as `nf_board` with
exact MCU `nRF52840`, pyOCD target `nrf52840`, J-Link attachment hints, COM11
UART attachment, and a safety reference. The reviewed datasheet bytes matched
SHA-256 `c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422`.

## Application and hardware proof

The agent alone wrote a Zephyr application with an application-created LED
worker thread and Zephyr UART shell. It built for `nrf52840dk/nrf52840` using
nRF Connect SDK 3.3.1. The final ELF SHA-256 was
`b2c96a7a8977139759a2e7014397bdf8e441c01698d20e9ded8b11a4e3d19879`.

The first build location exceeded Windows generated dependency-path limits.
The agent diagnosed this without help, removed that build output, selected a
short external build directory, and completed a clean build. This did not touch
hardware and is retained as honest usability evidence.

After build-aware safety refresh and revalidation, the agent used the guarded
`flash_application` plan and exact one-child fallback. The server reported that
the ELF was flashed only inside its mapped application partition and left the
target running.

One state-preserving `serial_exchange` opened COM11 at 115200 baud and matched
all four ordered observations:

- `blink on` → `BLINK ON`
- `blink status` → `STATUS ON`
- `blink off` → `BLINK OFF`
- `blink status` → `STATUS OFF`

The final board state was running with blinking OFF and the debug session was
disconnected. No direct pyOCD or serial bypass was used.

## Primary evidence

- Workspace: `C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server-e2e-acceptance-v2-20260717-134002-735`
- Machine-readable transcript: `C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server-e2e-acceptance-v2-20260717-134002-735\acceptance\evidence.json`
- Build artifacts: `C:\temp\nfv2-20260717-134002\zephyr`

