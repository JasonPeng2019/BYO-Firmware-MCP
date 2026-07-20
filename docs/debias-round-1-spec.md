# De-bias round 1 specification

> Historical: build-guidance portions are superseded by
> `docs/universal-native-build-command-spec.md`. Current guidance has no named provider detection
> or `toolchain_fallback` field.

Status: accepted for implementation

## Audit triage

1. **[TOOLCHAIN] Single pyOCD target backend — valid but deferred this round.** A real generic
   backend selector needs at least one independently useful provider and capability contract;
   wrapping the same singleton in another abstraction would only pretend to generalize it.
2. **[MCU] Arm/Cortex-M register semantics — valid but deferred this round.** The safe replacement
   is backend-reported register roles and widths. Guessing roles for other architectures would
   weaken the execution-state permission boundary.
3. **[MCU] Hardcoded reviewed board catalog — accepted.** Reviewed identities are legitimate data,
   but they must be loaded through one validated data schema rather than compiled into code.
4. **[MCU] Family-derived board defaults — accepted.** Remove nRF/STM32 address and recovery
   inference. Missing optional evidence remains missing and validation names the remedy.
5. **[MCU] Board-ID-specific attach behavior — accepted in part.** Move attach mode and debug clock
   into validated board/profile facts. J-Link retry behavior is a parameterized pyOCD-adapter
   fallback triggered only by a typed backend failure, so it is retained as [TOOLCHAIN] fallback.
6. **[MCU] Nordic-only recovery vocabulary — accepted.** Replace it with typed backend mass erase
   plus `manual_only`; the exact erased ranges and one-time approval remain server-derived.
7. **[MCU] UART-only communication surface — discarded.** UART is an intentionally scoped,
   board-neutral transport feature behind an adapter, not an MCU selection rule. Adding every
   peripheral would make the server harder to use without fixing target bias.
8. **[TOOLCHAIN] Vendor-first serial association — accepted.** Generic USB identity/scoring must run
   first. Vendor CLIs remain optional, parameterized fallbacks only for unresolved ambiguity; their
   executable comes from PATH or an explicit environment path, never frozen install paths.
9. **[TOOLCHAIN] pyOCD/CMSIS-Pack target vocabulary — valid but deferred with item 1.** Generalizing
   the vocabulary without another target backend would create fields no runtime can execute.
10. **[TOOLCHAIN] ELF/HEX and GNU-style safety evidence — valid but deferred this round.** Adding a
    format name without a parser and containment proof would be unsafe. The new explicit artifact
    collector is generic provenance, while guarded flash remains honestly narrower.
11. **[TOOLCHAIN] Zephyr-only generated build guidance — accepted.** The visible generic artifact
    collector becomes the primary server guidance; Zephyr is clearly labeled an optional,
    parameterized terminal fallback. Remove board-prefix dependency updates from that fallback.
12. **[MCU] Reference smoke and benchmark fixtures — discarded.** These are explicitly scoped test
    evidence, not runtime target selection or MCP interface truth. Multiple fake and real target
    fixtures already exercise the general mechanisms.

Deferred findings remain open for later fresh rounds; they are not claimed fixed.

## Desired behavior

### Catalog data [MCU]

- Production code contains no board name, part number, memory address, or board-specific geometry.
- A packaged, server-owned data document supplies reviewed entries through one strict loader.
- Adding reviewed support means adding validated data and evidence, not editing Python branches.

### Explicit board behavior [MCU]

- No MCU-family prefix chooses a read address, recovery requirement, recovery mechanism, attach
  mode, or clock.
- Optional profiles may be incomplete; validation refuses a missing safe-read address before live
  backend access and gives a setup/repair remedy.
- Attach mode and frequency are typed board/profile facts used uniformly by the backend.

### Recovery [MCU]

- The public mechanism is `backend_mass_erase`, meaning the connected typed backend's documented
  whole-device erase primitive, or `manual_only`.
- No target-name substring or vendor name authorizes recovery. Existing plan binding, complete map
  disclosure, fresh one-time permission, and post-recovery closed gate remain unchanged.
- The connected typed backend must report the mass-erase capability before disclosure or approval.
- Legacy `nrf_pyocd_unlock` profile values are read-only aliases normalized with a warning; new
  writes and all public plan/action values use only `backend_mass_erase`.

### Serial association [TOOLCHAIN]

- Stable USB identity and generic scoring are the default.
- Vendor helpers run only when generic evidence remains ambiguous. Executables resolve from PATH or
  explicit environment variables and receive runtime probe facts; no literal port is embedded.
- A validated server-owned fallback registry, not MCU-family branches, selects the helper command,
  parser, and preference policy from typed probe facts.

### Build guidance [TOOLCHAIN]

- Every ready profile receives primary native-build/artifact-collector guidance through MCP.
- When reviewed data supplies a Zephyr target, a separate `toolchain_fallback` record supplies the
  parameterized helper command. It never replaces the generic primary route.

## Interface impact

- Existing MCP tool names remain unchanged.
- `get_setup_status.build_guidance` becomes provider-neutral at the top level and may contain a
  labeled Zephyr fallback.
- Recovery plan/action parameters use `backend_mass_erase`; the NULL guide teaches that exact term.
- Board/profile documents gain optional `debug_connect_mode` and `debug_clock_hz` facts.

## Non-goals

- Claiming OpenOCD, GDB-server, non-Arm register, UF2, S-record, or raw-BIN guarded support before a
  real provider/parser exists.
- Weakening setup evidence, map reconciliation, containment, permissions, or the default-closed
  gate.
- Adding arbitrary commands, caller-supplied ranges, or persisted authority.
