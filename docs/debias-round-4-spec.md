# De-bias round 4 specification

## Accepted finding

### [MCU] Fresh-profile commit gives every silicon identity a Nordic register name

**Problem.** The live setup connection phase takes a generic catalog-provided silicon-ID address,
mask, and expected value, but writes the fixed label `FICR.INFO.PART` into every committed profile.
That would attach Nordic semantics to a future reviewed target from another family.

**Desired behavior.** Fresh setup stores the neutral label `silicon_id`. Exact addresses, expected
values, masks, and the reviewed source remain authoritative; the label is descriptive only and must
not contain an MCU-specific register assumption in code.

**Scope.** The setup profile commit in `server.py`, focused setup commit tests, and this report.

**Interface impact.** No MCP schema or call sequence changes. A model sees the same setup statuses;
new profiles use a portable descriptive label.

**Non-goals.** This does not rewrite historical profile/report evidence, change identity matching,
or let callers provide silicon evidence.

## Round-4 audit triage

1. **DEFER [TOOLCHAIN] pyOCD-only debug stack.** Valid architectural limitation, but no honest
   simple fix exists without a real debug-provider capability/session contract and at least one
   second backend. A renamed interface with one implementation would be a fake abstraction.
2. **DEFER [MCU] Cortex-M/SWD register contract.** Valid; supporting another ISA requires typed
   runtime register capability metadata and a real non-Cortex backend. Do not weaken current safety
   by pretending unknown register classes are portable.
3. **DEFER [MCU] 32-bit transfer/register model.** Valid; wider/nonuniform buses need backend
   capability negotiation and safety semantics, not just widening integer validators.
4. **DEFER [MCU] Cortex-M image assumptions.** Valid; a general executable-image contract needs a
   second image model and target-provided entry/vector semantics.
5. **DEFER [TOOLCHAIN] GNU/Zephyr linker-map grammar.** Valid; ELF is already preferred, but honest
   IAR/ArmClang/LLD support needs real fixtures and parser adapters.
6. **DISCARD [TOOLCHAIN] collector calls BIN deployable.** User-facing MCP guidance already marks
   BIN provenance-only with no trusted address; guarded flash truthfully accepts only ELF/HEX.
7. **DISCARD [MCU] sector flash and mass erase.** These are typed backend capabilities with
   fail-closed containment, not caller-assumed ranges; unsupported recovery remains manual-only.
8. **DISCARD [MCU] three reviewed catalog entries.** Target-specific reviewed evidence belongs in
   data, and unknown boards fail honestly. Adding unreviewed automatic authority would be unsafe.
9. **DISCARD [TOOLCHAIN] exact pyOCD evidence pin.** Safety evidence must fail on dependency-byte
   drift until reviewed; the wider runtime dependency range does not grant safety authority.
10. **DISCARD [MCU] reviewed address maps.** Exact target memory facts are correctly isolated in
    reviewed data rather than executable control flow.
11. **DISCARD [MCU] tracked board profiles.** Profiles are exact target contracts by design and do
    not constrain the schema to those targets.
12. **ACCEPT [MCU] fixed `FICR.INFO.PART` label.** Replace the code literal with a neutral label.
13. **DISCARD [TOOLCHAIN] three CLI alias families.** The primary API path accepts any registered
    pyOCD provider dynamically; these entries are configured legacy CLI compatibility aliases.
14. **DEFER [TOOLCHAIN] J-Link DLL workaround.** Valid compatibility debt, but changing a
    hardware-proven attach workaround without a second live validation path risks breaking normal
    probes. It should become an explicit backend option when that provider contract exists.
15. **DISCARD [TOOLCHAIN] two vendor serial parsers.** Generic USB inventory is primary; both are
    labeled, configured, parameterized fallbacks.
16. **DISCARD [MCU] common USB-UART hints.** All serial devices remain visible and selectable by
    stable identity; the list only improves optional scoring for otherwise unrelated adapters.
17. **DEFER [MCU] UART-only transport.** Valid product-surface limitation requiring real RTT/SWO or
    binary transport capability contracts, not a cosmetic rename.
18. **DEFER [MCU] Stage-0 nRF fallback.** Valid legacy CLI debt but outside the live MCP path; a fix
    here alone would not satisfy this loop. Remove it when Stage 0 is retired or routed through the
    live generic flash service.
19-20. **DISCARD [TOOLCHAIN/MCU] target-specific firmware fixtures.** They are test evidence, not
    general server interfaces.
21-22. **DISCARD [TOOLCHAIN] Zephyr bootstrap defaults/platform matrix.** Zephyr is an explicitly
    labeled fallback behind native build plus generic collection, and repo/ref/toolchain/paths are
    parameterized. Unsupported managed-download hosts can still use local native builds.
23. **DEFER [TOOLCHAIN] host bootstrap coverage.** Valid convenience-script limitation outside the
    live MCP path; the server itself is not restricted to those bootstrap scripts.
24. **DEFER [TOOLCHAIN] macOS process start token.** Valid runtime bug. A safe cross-platform token
    requires a tested OS API or process library; `/proc` cannot simply be approximated without
    weakening stale-PID identity checks.
25-27. **DISCARD [TOOLCHAIN/MCU] benchmark and hardware acceptance fixtures.** Their explicit board
    and shell matrix defines test cases, not the live product surface.
28. **DISCARD [TOOLCHAIN] absolute paths in tracked hardware evidence.** Immutable evidence records
    the host paths actually observed; it is historical, not portable configuration or authority.

