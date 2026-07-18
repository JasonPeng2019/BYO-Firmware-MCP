# De-bias round 5 specification

## Accepted finding

### [MCU] / [TOOLCHAIN] Generic AP-discovery failure text names nRF52 and J-Link

**Problem.** The live pyOCD error mapper treats the backend's `KeyError(1)` as a generic AP#1
initialization failure, but its returned guidance claims an nRF52 MCU interpretation and a J-Link
toolchain/provider interpretation. The same backend exception on another target or through another
probe provider would mislead a model toward the wrong recovery path.

**Desired behavior.** Return target-neutral guidance: one expected access port was unavailable;
possible causes include target lock, reset/attach state, probe connectivity, or incompatible target
selection. The remedy must tell the model to follow setup/validation evidence and only use the typed
recovery route when the server identifies it. Error classification and authority remain unchanged.

**Scope.** `adapters/swd_pyocd.py`, focused adapter tests, and a paired live MCP `connect` smoke.

**Interface impact.** No schema change. A failed `connect` response becomes accurate for every
pyOCD-supported target and no longer steers an agent toward Nordic/J-Link assumptions.

**Non-goals.** This does not generalize the pyOCD backend, change typed recovery, or alter the
hardware-proven J-Link UID retry/session option.

## Round-5 audit triage

- **DEFER:** the repeated pyOCD-only, SWD/Cortex-M, image-format, UART-only, GNU-linker, alternative
  ISA/transport, and macOS process-token findings remain the real architectural limits documented in
  Round 4. None has a truthful small fix without a second implementation/capability contract.
- **ACCEPT [MCU]:** remove the nRF52 interpretation from generic AP-discovery failure text.
- **ACCEPT [TOOLCHAIN]:** remove J-Link-specific interpretation from the provider-neutral mapper and
  prove identical neutral behavior through both the ordinary provider path and the existing
  parameterized J-Link UID-retry fallback.
- **DISCARD:** reset-and-halt programming is applied generically; only its comment cites the board on
  which it was first proven. Probe aliases and vendor serial parsers are configured fallbacks behind
  generic API/USB discovery. Reviewed boards, evidence, firmware, benchmarks, and acceptance scripts
  are target-specific data/test fixtures by design. Zephyr remains an explicitly labeled,
  parameterized fallback behind native build plus the live generic collector.
- **DEFER:** J-Link UID/session workarounds and serial-family conflict hints are compatibility debt,
  but changing hardware-proven behavior without a provider capability/config migration and live
  bench evidence is not a safe small change.
