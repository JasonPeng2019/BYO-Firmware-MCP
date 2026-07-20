# Universal onboarding live acceptance — 2026-07-19

## Scope and isolation

Two independent `gpt-5.6-luna` medium-effort agent runs exercised the real MCP server from new
temporary project roots. Each root began with only the requested board name, exact MCU part, and
copied datasheet. No board profile, safety map, project pack manifest, or prior `.firm` state was
copied in. Both attached probes were visible, and each run assigned only the intended probe.

The acceptance was deliberately non-mutating. It did not flash, erase, unlock, deploy, reset,
halt, resume, or write target memory/registers/UART. Mutation and destructive plan surfaces were
exercised only through their all-NULL teaching calls.

## nRF52840 dynamic-support journey

- Fresh root:
  `C:/Users/Jason/AppData/Local/Temp/byo-luna-nrf52840-8aca7427384f479d91939317ad863162`
- Board / exact part: `Fresh nRF52840 Board` / `nRF52840-QIAA`
- Selected probe: J-Link `683377322`; ST-Link `066FFF514988525067233337` remained unassigned.
- Agent research source: Nordic Semiconductor's official nRF Device Family Pack directory.
- Accepted pack: `NordicSemiconductor.nRF_DeviceFamilyPack.8.59.0.pack`
- Exact pack SHA-256:
  `9a05ad8445527af57a517297832e05229e9e96a5db35ff3dcb6155ed5a352f69`
- Verified PDSC leaf / server-derived pyOCD target: `nRF52840_xxAA` / `nrf52840_xxaa`
- Result: setup completed, schema-v3 map and canonical profile association were persisted, live
  validation passed, and `get_setup_status` reached `setup_ready`.
- Reuse: after disconnect, the same familiar name routed directly to validation. No setup,
  research, or refresh recurred; revalidation returned to `setup_ready`.
- Safe surface coverage: handshake, overview/assignment, setup plan and continuation, validation,
  status, connection/state/info, bounded reads/symbol prerequisites, every visible all-NULL plan,
  disconnect, and reuse routing. Prerequisite-dependent tools returned truthful bounded refusals.

### Closed nRF failure loops

1. **Map/profile association:** the first fresh run built the map but left the profile without its
   canonical `safety_ref`, so reusable setup never became complete. The general fix associates the
   canonical map after every successful refresh and reports a retryable refresh block if the
   post-commit profile association fails. A focused Terra audit passed 77 tests plus Ruff/Pyright.
2. **Pack memory/SVD overlay:** the next fresh run found 63 conflicts because Nordic's UICR was
   described both as physical flash and as SVD registers. The vendor-neutral fix gives verified
   physical memory classification precedence and omits whole overlapping SVD rows before alias
   segmentation. Non-overlapping registers remain available. The final fresh run above passed.
3. **Project-local reuse:** main audit found that overview used repository-only authority while
   validation used project-local authority. One shared exact replay path now resolves the active
   project's immutable pack bytes and binding. The real refresh-to-overview test and a Terra audit
   passed 96 tests plus Ruff/Pyright.

## STM32L476 fresh-project journey

- Fresh root:
  `C:/Users/Jason/AppData/Local/Temp/byo-luna-stm32-94a0bc6ee15e4a999cb82a7063849a0c`
- Board / exact part: `Fresh NUCLEO-L476RG` / `STM32L476RGT6`
- Datasheet SHA-256:
  `a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65`
- Selected probe: ST-Link `066FFF514988525067233337`; J-Link remained unassigned.
- Support: the server replayed already-available exact local support for target
  `stm32l476rgtx`; correct reuse avoided unnecessary online research or download.
- Safety-map digest:
  `cc9113865bc1eac130268fe04a299b0df5f1412e120974acfd044468f1fe3ea6`
- Result: setup and validation passed and status reached `setup_ready`.
- Reuse: after disconnect, overview routed directly to validation; revalidation passed and status
  returned to `setup_ready` without setup or refresh.
- Safe surface coverage matched the nRF journey. No hardware mutation occurred.

## Interpretation

The runs prove both intended modes: agent-led official support discovery for an unknown fresh
project and deterministic reuse of exact local support when it already exists. Persisted authority
is project-local exact pack/datasheet/profile/map evidence; research continuations and live gates
remain run-scoped. Capabilities degrade according to available pack, artifact, UART, and halted-core
facts rather than making setup fail globally.

This is evidence for Arm MCUs that pyOCD can attach to and that a CMSIS-Pack/built-in provider can
describe. It is not a claim that missing board wiring, unavailable peripherals, unsupported debug
architectures, or absent build artifacts can be inferred from a datasheet alone.

