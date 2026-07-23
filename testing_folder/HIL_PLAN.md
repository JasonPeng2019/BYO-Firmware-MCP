# Hardware verification plan

## Scope

Verify only the four changed contracts against the attached nRF52840 DK and
NUCLEO-L476RG:

1. `connect_under_reset` ignores conflicting ambient pyOCD routing overrides.
2. A real multi-output build is reported as explicit-selection-required without
   selecting an artifact.
3. Real flash success reports only an observed final state.
4. Destructive recovery invalidates the old connection and requires a clean
   reconnect plus `board_validate`.

## Board facts supplied to the firmware MCP setup flow

- Nordic nRF52840 DK / PCA10056; target MCU package family:
  `nRF52840-QIAA`. Local product specification:
  `../Nano_BLE_MCU-nRF52840_PS_v1.1.pdf`.
- ST NUCLEO-L476RG; target MCU: `STM32L476RGT6`. Local datasheet:
  `../stm32L476rgt.pdf`.

The live probe/USB identities remain authoritative. Do not infer which attached
probe belongs to a board from list order.

## Storage and isolation

- Server artifact root: `testing_folder/artifacts`
- Server owned-process root: `testing_folder/runs`
- Test firmware/builds/logs: `testing_folder/work`
- Nested Codex logs: `testing_folder/agent`

The temporary MCP registration must be removed after testing. Do not commit,
push, or write outside this folder except for a narrowly required server-source
fix discovered by hardware evidence.

## Safety and authorization

The user explicitly authorized board setup and the hardware actions needed for
these tests. Follow the live MCP server’s current plan/permission contract.
Recovery/mass erase is allowed only as the CL-004 test, after recording board
identity and preparing a known-good reflash/revalidation path.

## Acceptance evidence

- Preserve exact MCP responses and nested-agent transcript/logs.
- Record each board’s live identity, selected probe, UART (if used), firmware
  artifact identity, flash result, observed state, and reconnect/validation
  result.
- Distinguish PASS, FAIL, BLOCKED, and NOT EXERCISED for each changed contract.
