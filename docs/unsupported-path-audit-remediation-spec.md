# Unsupported-path audit remediation specification

## Goal

The generic path must remain usable when no reviewed board, pack registration, serial mapping, or
default attach policy applies. Agent research supplies novel facts; the server verifies those facts
against installed backends and live hardware, persists successful choices, and replays them without
turning convenience tables into requirements.

## Required behavior

### Scalar symbols

- Function and unaligned scalar symbols are rejected before guarded-write containment and before a
  plan call is consumed, as well as in the action handler.

### UART association

- A serial endpoint is considered provably board-mapped only when its stable USB identity matches
  the selected probe identity.
- Other endpoints remain usable, but setup asks for the existing friendly external-adapter
  confirmation before caching the association.

### Built-in pyOCD targets without a CMSIS-Pack

- When the agent supplies an exact researched pyOCD target that is actually in the installed
  built-in registry, setup may use it without fabricating a pack.
- The server derives and digests the target class's static memory map, verifies a live connection,
  records a compatible live Arm CPUID proof covering the Arm implementer, architecture, and
  nonzero Cortex-M part field without a closed model allowlist, and persists a closed
  `resolved_builtin_target` authority record.
- The derived map includes the architectural Cortex-M system-control address space. Optional
  peripheral metadata is included only when independently available (for example from the
  agent-supplied pack); built-in target acceptance does not fabricate SVD authority.
- Replay re-derives the installed target geometry and rejects target-class or geometry drift.
- Pack targets whose metadata cannot name the Cortex-M model may use the same server-captured,
  canonical CPUID proof. Profile replay and validation re-check its exact fields, and the resulting
  map exposes the architectural system-control space without trusting agent-supplied ranges.
- Built-in support remains device support only. It does not inherit a reference-board partition,
  wiring, probe, or deployment policy. Flash is available only when the built-in target exposes
  reproducible erase/program geometry and artifact allocation passes the normal containment checks.

### Agent-resolved debug attachment

- Automatic attach attempts are conveniences. If they fail, the still-live setup continuation asks
  the agent to resubmit the exact target or pack together with one researched `swd`/`jtag`
  protocol, supported pyOCD connect mode, and positive debug clock in hertz.
- Target and policy are live-tested during continuation, before the one-shot paired setup action is
  consumed. The server publishes neither candidate nor policy until both validate. Invalid replies
  cannot leave stale policy state for a later candidate. Normal reconnect and validation reuse the
  saved protocol/mode/clock.
- If a previously live-tested attachment disappears before profile commit, setup reports an honest
  connection/environment failure rather than advertising an unreachable continuation retry.
- No raw backend option dictionary or command is accepted.
- A pack whose exact part/target metadata passed but whose default attachment failed is not described
  as a bad package. The same pack may be resubmitted with the required typed attachment tuple, and
  that pair is live-tested before promotion.

### Generic build execution

- Literal agent-supplied build commands remain the universal path; provider detection is an optional
  convenience and never a ceiling.
- Online operation is allowed when local resources are unavailable. Offline mode is explicit, not
  imposed by the server.
- Even in reusable/non-fresh output mode, the helper always refuses a filesystem root as the build
  directory so a mistaken command cannot recursively scan an entire drive.
- Declared ARM executable artifacts are recognized by ELF structure throughout collection,
  containment, symbol lookup, breakpoint binding, and flash whether conventionally named `.elf` or
  `.axf`.

### Replacement setup and generic geometry

- Replacing a setup plan clears the prior continuation, staged candidate and attachment policy,
  and board-scoped research budget. No rejected answer poisons the replacement attempt.
- Multi-bank device packs preserve all physical banks. When no unique default or boot bank exists,
  the lowest-addressed bank is the deterministic compatibility/programming domain rather than a
  reason to reject otherwise usable device support.
- PDSC device-memory regions with explicit readable or writable access are a conservative
  peripheral fallback when no usable SVD register metadata exists. Unknown access never becomes
  write authority, and overlapping aliases receive only the access permitted by every covering
  description. Fallback peripheral windows are segmented around flash, RAM, ROM, and Cortex-M
  system space so they cannot shadow those classifications. An installed built-in target lacking peripheral metadata still completes setup
  with its verified flash/RAM/core capabilities; peripheral tools report their missing authority
  honestly rather than making a pack a universal prerequisite.
- Multi-bank packs preserve every physical bank, but address ordering alone never creates erase or
  programming authority. Driver/sector authority is available only for one uniquely marked
  default/boot bank; otherwise setup remains useful for read/debug while flash stays unavailable.

### Research response teaching

- A research stop returns an `accepted_response` matching that stop's exact requested fields. It
  must not show a pack template for target or attachment research.

## Compatibility

- Existing `resolved_pack` profiles and schema-v3 maps remain readable and replay identically.
- Existing board profiles without a debug protocol retain pyOCD's default protocol selection.
- Caller-supplied memory ranges remain prohibited, and no new hardware action bypasses the plan or
  validation layers.
