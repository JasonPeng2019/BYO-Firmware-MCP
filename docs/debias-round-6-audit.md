# De-bias round 6 audit and triage

This was the sixth fresh audit and therefore the configured safety-cap round. It accepted no new
small, truthful runtime fix. The auditor's detailed citations were grouped and triaged as follows.

1. **DEFER [TOOLCHAIN]/[MCU] — pyOCD, SWD, and Cortex-style debug architecture.** The limitation is
   real. A renamed interface with only one implementation would be a fake abstraction; an honest
   fix needs a second working backend/transport/ISA plus capability negotiation. This also covers
   register classes and widths, breakpoint instruction size, executable RAM policy, and nonuniform
   peripheral access.
2. **DISCARD [MCU] — reviewed board catalog, memory maps, and evidence.** Exact device facts belong
   in reviewed data, not executable heuristics. Unknown boards fail closed rather than receiving
   invented authority. Broader zero-touch support requires separately reviewed evidence, not a
   generic code shortcut. The exact dependency hashes are intentional safety evidence.
3. **DEFER [MCU] — contiguous geometry model.** Multi-bank, discontiguous, and variable-sector
   devices need a real typed geometry/evidence schema and fixtures. Pretending one scalar erase size
   generalizes would weaken containment.
4. **DISCARD [TOOLCHAIN] — configured probe and serial CLI aliases.** The primary pyOCD API and USB
   inventory paths use runtime provider/stable identity. The named CLI parsers are labeled,
   parameterized compatibility fallbacks. The provider API accepts registered providers not named
   by those fallback aliases.
5. **DEFER [TOOLCHAIN] — J-Link session and UID compatibility behavior.** These are hardware-proven
   fallback workarounds. Round 5 removed their misleading generic error semantics; replacing the
   workarounds themselves needs a provider capability contract and bench validation.
6. **DISCARD [MCU] — reset-and-halt flash preparation.** The executable behavior is generic; only a
   comment cites the board on which it was proven. Erase/write containment still runs before the
   backend is called.
7. **DEFER [MCU] — UART/text-only telemetry.** RTT, SWO, semihosting, CAN, binary framing, and other
   channels are valid product-surface expansions, but require at least one real implementation and
   an explicit transport/framing contract. Renaming the existing pyserial/UTF-8 tools would not add
   support. UART identity, baud rate, and external-adapter confirmation remain runtime inputs.
8. **DISCARD [TOOLCHAIN] — BIN collection versus ELF/HEX flashing.** The collector truthfully treats
   BIN as provenance-only because it has no trusted load address. MCP guidance and docs explicitly
   require coherent ELF/map evidence for guarded firmware and say collection never authorizes
   flashing.
9. **DEFER [TOOLCHAIN] — ELF/GNU/Zephyr linker evidence grammar and image formats.** Supporting IAR,
   ArmClang, LLD variants, S-record, UF2, vendor containers, or raw address-bound images honestly
   requires real fixtures and parser/provider adapters. Caller-supplied ranges remain forbidden.
10. **DISCARD [TOOLCHAIN] — Zephyr/GCC managed build code.** It is an explicitly labeled,
    parameterized fallback behind the live provider-neutral native-build guidance and generic MCP
    artifact collector. Target-specific firmware is test/reference evidence, not the generic build
    route.
11. **DEFER [TOOLCHAIN] — host bootstrap and less-common OS/process support.** Bootstrap scripts are
    outside the live MCP path. The previously identified macOS process-identity gap needs a tested
    OS identity primitive; weakening stale-PID checks is not acceptable.
12. **DEFER [TOOLCHAIN] — CMSIS-Pack-only target packages.** This follows the sole real pyOCD
    backend. A generic package facade without another backend/package implementation would add
    complexity without capability.
13. **DISCARD [MCU]/[TOOLCHAIN] — tracked profiles, immutable evidence, migration, benchmarks, and
    hardware acceptance scripts.** These record or exercise intentionally exact boards, hosts, and
    cases. They do not define generic runtime authority or the MCP schemas.
14. **DISCARD [TOOLCHAIN] — legacy Stage 0 vendor fallbacks.** They are outside the live MCP route,
    so changing them cannot satisfy this loop's runtime requirement. Retire them when Stage 0 is
    removed or routed through a future backend service.

## Termination

No new finding survived triage as an honest small runtime fix. The loop terminates at its configured
six-round safety cap. The deferred items above are real capability boundaries, not claimed passes;
each requires a real second implementation/evidence model rather than a cosmetic abstraction.
