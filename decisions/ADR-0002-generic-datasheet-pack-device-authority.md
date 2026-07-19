# ADR-0002: Generic Datasheet-and-Pack Device Authority

- **Status:** Proposed
- **Date:** 2026-07-19
- **Deciders:** BYO Server maintainers

## Context

Automatic setup currently requires a matching reviewed-board catalog record. That blocks a
new supported MCU even when the user supplies its local official datasheet and the host has a
verified local CMSIS-Pack/pyOCD target. The catalog also carries facts that must not be
silently inherited by another board sharing the same MCU: deployment partitions, wiring,
and transport preferences.

The product goal is user input limited to an exact part and local datasheet identity. The agent
may resolve a pack/target from server-provided local inventory, but it cannot become an
authority source or submit raw ranges, reset parameters, or partitions.

## Decision

1. Introduce one common DeviceSupportAuthority interface.
   - A reviewed catalog record is available only for a migrated profile or separately proven
     board identity; an MCU/PDF match cannot select it.
   - A generic resolved authority is the default for every new profile with an exact part, server-read PDF, and
     verified locally manifest-pinned pack/pyOCD target.
2. A server-owned verified-pack registry, populated only by separate non-runtime/admin provisioning,
   establishes pack provenance and digest before setup. The server issues opaque run-scoped support candidates. The agent can select one and supply
   citations; it cannot send a pack path/digest, target string, or memory facts.
3. The server verifies pack bytes, PDSC exact-device metadata, canonical pyOCD target,
   isolated target-binding proof, pyOCD runtime identity, datasheet evidence, and live identity
   before it creates a live gate. Exact and compatible-geometry identity stamps have distinct
   semantics; a family ID never claims exact-part proof.
4. A generic map begins with physical/prohibited regions only. It has no flashable application
   partition until a server-verified whole-device fresh policy or an existing server-recorded
   deployment allocation proves ownership. An artifact sector envelope alone is not ownership.
5. Existing catalog profiles migrate through the common representation. Catalog data remains
   available as an explicit reviewed override but cannot supply build, wiring, partition, or
   transport facts to a different logical board.
6. Runtime remains local-only. No target unlock, mass erase, recovery, bootloader flash,
   option/security write, caller-supplied ranges, or pack download is added.

## Consequences

- New supported devices no longer require a source edit to the catalog.
- Generic setup may offer read/debug but correctly withhold flash when identity evidence,
  physical geometry, bounded flash-driver behavior, or deployment ownership is not proved.
- Fresh setup has an explicit server-mediated candidate-selection stage.
- The single-file map-authority invariant remains intact. The map schema advances to v3; legacy
  v2 catalog maps are compatibility-read and rederived by refresh only. No new persistent
  authority artifact is introduced.
- Implementations must update setup, validation, map refresh, and flash policy together;
  partially bypassing the catalog in only one layer is unsafe.

## Rejected alternatives

- **Let callers name a target, pack, or range.** This would move safety authority to an
  untrusted client and recreate the current range/target-inference hazards.
- **Treat a matching MCU family as enough.** Package variants and device aliases are not
  partition, target, or board authority.
- **Auto-enable full flash from physical geometry.** A datasheet cannot prove resident
  bootloader ownership.
- **Grow the reviewed-board catalog indefinitely.** That cannot satisfy generic support and
  encourages accidental cross-board policy inheritance.
