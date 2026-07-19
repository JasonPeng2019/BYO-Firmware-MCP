# PLAN: Generic Datasheet-and-Pack Device Authority

Specification: docs/datasheet-pack-universal-support-spec.md  
Decision: decisions/ADR-0002-generic-datasheet-pack-device-authority.md

## Scope discipline

- Preserve Safety Layer v2: caller ranges stay prohibited; memory_map.yaml remains the sole
  persisted map authority; gates, plans, permissions, candidate selection, and attach attempts
  remain run-scoped.
- Runtime is local-only: no pack download/index update/package install, unlock, mass erase,
  recovery, bootloader flash, or option/security writes.
- Existing reviewed catalog boards remain compatible but become optional authority overrides.
- Work in fresh temporary roots; never use old .firm evidence as authority.

## Slice 1: Generic local pack inspection

1. Add a server-owned verified-pack registry and separate non-runtime/admin provisioning path;
   runtime never trusts an arbitrary installed pack or self-computed digest.
2. Add safe verified-pack metadata inspection for registry-pinned local CMSIS packs.
3. Emit immutable exact part/device/target candidate records; reject archive, metadata, alias,
   or provider ambiguity.
4. Define immutable registry target bindings: PDSC leaf/alias/core/geometry/SVD/algorithm tuple,
   canonical pyOCD target, and isolated verified-pack proof. Define a separate immutable built-in
   target registry; reject targets lacking proof metadata.
5. Add server-owned candidate inventory and run-scoped opaque candidate selection.
6. Test synthetic packs, changed bytes, malformed archives, aliases, ambiguity, and no target/path
   caller input.

Verification: focused pack/inspector/resolver tests, Ruff, Pyright.

## Slice 2: Common device-support authority and setup routing

1. Introduce the common authority representation and catalog adapter.
2. Route every new profile through generic part/PDF/local-pack resolution. Catalog authority is
   available only for migrated profiles or separately proven board identity; it never follows from
   an MCU/PDF match.
3. Remove generic dependence on catalog expected-target equality; retain exact candidate proof.
4. Add bounded server-issued attachment policy candidates; preserve catalog policy as an override.
5. Keep profiles uncommitted until pack/target proof and live attach succeed.

Verification: setup inventory/workflow/target tests and fresh in-process MCP tests.

## Slice 3: Generic evidence, identity, and map derivation

1. Add server-owned typed PDF evidence extraction/indexing with no agent numeric fact inputs;
   define source spans, value normalization, parser versions, and closed conflict rules.
2. Define one normative identity lattice: exact proof, compatible-geometry proof, and
   connection-only proof, each with exact gate/flash capabilities.
3. Evolve the single map atomically to schema-v3: exact identity/authority-source/source-digest
   fields, nullable partitions, strict parsing, and legacy-v2 catalog read/refresh migration.
4. Derive and cross-check physical geometry, prohibited regions, and identity proof from pack,
   pyOCD, and PDF evidence.
5. Make validation, refresh, and map derivation consume common authority.
6. Keep unknown identity/geometry useful for diagnostics but ineligible for gate/flash.

Verification: evidence, validation, map-build, refresh, gate, and drift tests.

## Slice 4: Deployment policy and containment

1. Start generic maps with no flashable partition.
2. Derive artifact sector envelopes without treating them as ownership.
3. Permit first flash only under a whole-device fresh-policy proof and immutable bounded driver
   operation plan; reject chip-erase/unbounded flash drivers.
4. Add schema-v3 deployment allocation lifecycle: run-scoped blank proof, commit only after
   backend-confirmed success, failure/cancellation leaves nonblank-unknown, and refresh revalidates
   rather than reboots allocation. Store only non-circular allocation evidence; creation artifact
   digest is audit provenance and never stales ordinary subsequent builds.
5. Limit reflash to server-recorded allocations and refuse unknown nonblank ownership before
   backend mutation.
6. Preserve catalog/verified bootloader policies and existing artifact-digest enforcement.

Verification: flash plan/enforcement/backend-call tests plus fresh fake-backend end-to-end tests.

## Slice 5: Surface, build guidance, migration, and documents

1. Expose bounded generic support state and opaque continuation candidates in setup guidance.
2. Remove MCU-only development-kit build fallback; retain generic native-build guidance.
3. Migrate catalog profiles through common authority without widening partitions.
4. Update architecture, prompts, contract snapshots, safety docs, and gap records.

Verification: contract/prompt/docs tests, package/import, bounded stdio MCP smoke.

## Slice 6: Hardware acceptance and review

1. Run a fresh-root noncatalog path using a local datasheet and pack, generic build, guarded
   application flash, observable output, and bounded debugging.
2. Run focused suites, Ruff, Pyright, complete locked pytest suite, package/import, and stdio smoke.
3. Review the diff adversarially before and after risky authority milestones.
4. Run final GPT 5.6 Terra adversarial review; audit every finding, fix valid defects, rerun
   affected checks and the full suite, and repeat review until no valid finding remains.

Done only when generic setup does not require a reviewed-board record or user pack/target input,
flash remains fail-closed without deployment evidence, and all software/hardware acceptance evidence
is green.
