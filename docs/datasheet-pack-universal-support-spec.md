# Datasheet-and-Pack Universal Device Support Specification

**Status:** Superseded by [`universal-device-onboarding-spec.md`](universal-device-onboarding-spec.md)
**Decision:** [ADR-0003](../decisions/ADR-0003-quarantined-runtime-device-support-onboarding.md)
**Direction:** reviewed board records become optional overrides, not a prerequisite for a new device.

This file preserves the pre-ADR design survey. Its admin-only/local-registry and semantic-PDF
proposals are historical, not current product behavior. ADR-0003 permits bounded quarantined pack
onboarding during setup; the active specification deliberately treats captured PDF bytes as a
research-source binding rather than claiming a universal semantic PDF parser.

## Goal

The server must set up, validate, debug, and, when safety evidence is sufficient, guardedly flash a board from:

- an exact MCU part number;
- a local official datasheet, whose SHA-256 is computed by the server; and
- locally available CMSIS-Pack or pyOCD support.

The user does not name a pack, target, reset mode, debug clock, register, memory range, erase size, or partition. The agent resolves a candidate from server-provided local inventory. The server independently proves that selection before it creates persistent profile, map, gate, or flash authority.

A user-supplied datasheet hash is an expected identity only. The server must still read the local PDF bytes: a hash alone cannot supply hardware facts.

Any board means any board with an eligible local device-support pack or pyOCD target and an attachable supported probe. Datasheets do not reveal board wiring, external clocks, existing bootloaders, security state, or locked silicon. Missing facts must cause a bounded refusal, never an invented configuration.

## Current-state survey

The generic build helper is project-neutral, but fresh hardware setup is catalog-required:

1. board_catalog.resolve_reviewed_support_from_datasheet requires an exact packaged CatalogBoard before setup inventory may select a target.
2. Setup obtains its target from catalog.pyocd_target, while TargetResolver rejects any candidate that differs from the catalog expected target.
3. verified_pack_for_target safely verifies a manifest provider for an already-known target, but cannot discover exact part-to-pack-to-target relationships.
4. reviewed_evidence and server map derivation require catalog evidence, geometry, partitions, and identity data. A generic successful connection cannot create a map, validate, or flash.
5. The STM32 local-pack, attachment-policy, and validation fixes correctly repair the catalog route. They must become generic mechanisms instead of requiring a new catalog record for each device.
6. catalog_board_for_mcu can offer development-kit build guidance from MCU identity alone. A custom board must never inherit another board's target, pins, partitions, or transport policy.

The existing catalog remains a compatibility and reviewed-override provider. It must not be the automatic-support gate.

## Deep implementation audit and resolved assumptions

The second code survey found four additional seams that the implementation must close rather than
paper over with a catalog lookup:

1. Profile creation currently commits a catalog target, reference-board probe family, identity
   register, connection speed, and test address together.  A generic path must persist only the
   selected probe and the independently verified device-support record.  It must never borrow a
   reference-board probe policy or a test/identity register merely because the MCU matches.
2. Validation presently requires a catalog silicon-ID register and exact expected value.  Generic
   validation therefore needs a typed identity-proof provider with the capability lattice below;
   it may be diagnostics-only when the verified support record has no safe exact/compatible proof.
   It must not invent an ID register from a target name.
3. Map currentness, refresh, and partition-region provenance currently call the catalog directly.
   They must depend on the common authority object.  In particular, a generic map may contain
   physical regions and no deployment partition; that is a usable read/debug map, not a reason to
   borrow a reference-board full-flash policy.
4. The current active-project pack manifest is deliberately useful for project-local *inventory*,
   but it is writable by the project.  It cannot be a generic runtime authority registry.  Only
   immutable server-owned registry records may contain part-to-device-to-target bindings.  A
   project manifest can at most mirror an already registered record and supply the exact pinned
   bytes.

**Assumption resolved for correctness:** “any board” means any reasonable target for which the
host already has a server-provisioned, verified CMSIS-Pack or built-in-target record and a
non-destructive pyOCD/probe path.  It does not mean that a PDF alone authorizes a target name,
flash layout, package download, board wiring, or a destructive recovery operation.  This is the
smallest general contract that keeps the agent input to part plus PDF while preserving the
existing safety boundary.

### Four-principle audit

- **Correctness:** Device facts, board facts, identity proof, and deployment ownership are
  separate authorities.  A missing higher authority fails only the capability that needs it.
- **Simplicity:** One common authority record and one registry are preferable to adding a
  per-board exception or a second manifest format; agent selection uses an opaque candidate only.
- **Generalizability:** No board name, OS path, probe family, clock rate, or development-kit
  build target follows from an MCU match.  Capability discovery and bounded fallback are used
  instead.
- **Neatness:** The resolver owns registry parsing and target binding; setup, validation, map
  refresh, and flash consume its typed result rather than each reinterpreting catalog YAML.

## Adversarial authority closure

The following rules resolve the one-way-door risks in this change:

1. **Trusted local-pack onboarding:** generic resolution scans only a server-owned verified-pack
   registry. A separate host/admin provisioning operation establishes a pack's provenance and
   exact digest before runtime; it is not part of setup, needs no agent decision, and never trusts
   a self-computed digest from an arbitrary installed file. Repository and active-project manifests
   are registry views, not permission to trust manifestless packs.
2. **New profiles are always generic:** an MCU part/PDF match never automatically selects a
   board catalog entry. Catalog authority is available only to an existing migrated profile or
   after separately proven board identity; absent that proof, its partition, wiring, and transport
   facts are unavailable. The generic path is therefore also the default for an L476 custom PCB.
3. **Blank sectors are not ownership proof:** generic first flash is disabled unless the server
   establishes a documented fresh-device policy by proving all programmable internal-flash sectors
   are blank, all protected/non-programmable regions are excluded, and the selected driver exposes
   bounded sector operations. A later reflash is limited to an already server-recorded allocation.
   An image cannot claim ownership merely because its own sectors happen to be blank.
4. **Identity has honest levels:** exact ordering-code proof permits the normal live gate and,
   subject to deployment policy, flash. A compatibility-class proof explicitly records that it is
   not an exact-part claim. It permits the live gate, read/debug, and generic flash only when
   target/core plus every writable flash/RAM/erase fact match the selected support record and the
   stricter fresh-device or recorded-deployment rules pass. Connection-only proof permits only
   diagnostics. A family ID is never represented as exact-part proof.
5. **Flash drivers are constrained:** generic flash accepts only an immutable server-generated
   operation plan whose erase/program sectors are all within the policy. Drivers that can choose
   chip erase or cannot prove bounded sector operations are refused in generic flash mode.
6. **PDF evidence is typed:** every extracted fact has a normalized value, unit/address width,
   source-page/span digest, parser version, confidence, and a closed conflict rule. If deterministic
   extraction cannot produce a critical field, pack-derived geometry plus a PDF citation is
   diagnostics-only, not flash authority.
7. **Map migration is explicit:** schema-v3 represents an authority kind and canonical generic
   sources while retaining legacy schema-v2 catalog maps for compatibility. Old maps are rederived
   and atomically rewritten only by refresh; generic maps permit null partitions. A mismatch refuses
   currentness rather than silently upgrading or widening a map.

## Authority model

Introduce a common DeviceSupportAuthority abstraction, consumed by setup, connection, validation, safety-map derivation, refresh, and flash containment.

It has two implementations:

- Reviewed catalog authority: current packaged records and evidence, retained only for migrated
  profiles or separately proven board identity.
- Resolved datasheet-pack authority: the default for every new profile, including a board whose
  MCU happens to appear in the catalog.

Persist only server-generated canonical facts needed to rederive support: exact part, datasheet digest, canonical target, pack identity, pyOCD identity, and evidence-source digests. Keep memory_map.yaml as the only persisted safety-map authority. Do not persist agent prose, plan state, permissions, or a second safety authority.

### Single-map schema evolution

The final on-disk schema is memory-map schema version 3, still stored only as
memory_map.yaml. Its exact additions/replacements are:

| Location | Schema-v3 representation |
| --- | --- |
| identity | exact mcu_part_number, canonical pyocd_target, authority_kind (catalog or resolved_pack), and immutable support_id; reviewed_board_type is removed |
| authority_source | exact canonical object: registry record/proof IDs and digests for resolved_pack, or catalog record digest for catalog |
| source_digests | semantic_profile, device_support, datasheet_evidence, deployment_policy, and map_generator_schema; no semantically false reviewed placeholders |
| partitions | application and bootloader are independently nullable; null application is valid but never flash-authorizing |
| deployment_policy | exact canonical object: kind (none, fresh_device_allocation, or reviewed_override), immutable allocated erase sectors, blank-proof digest, creation_pre_execution_map_digest, creation_artifact_digest for audit only, and allocation_digest over only the policy fields; absent allocation is kind none |

All fields use strict exact-key parsing and canonical domain-separated digests. The semantic
currentness digest covers identity, authority_source, source_digests, geometry, nullable partitions,
deployment_policy, and regions. A schema-v2 map is read only as a legacy catalog map: it never authorizes generic
support, is not silently rewritten on load, and is rederived then atomically replaced as schema-v3
only by board_safety_refresh. Existing catalog profiles retain their old map until that refresh;
generic profiles always require schema-v3. Failed rederivation leaves the old file untouched and
refuses currentness. Tests must prove atomic replacement, legacy catalog compatibility, strict
v3 rejection, generic null partitions, and no authority widening during migration.

| Fact | Owner and rule |
| --- | --- |
| Exact part and datasheet | Server normalizes the part and reads/hashes the local PDF. |
| Pack bytes and digest | Server-owned verified-pack registry. Repository/active-project manifests are read-only projections keyed to registry records and cannot add or rebind an authority. Runtime never downloads or queries a live index. |
| Pack metadata, SVD, flash algorithm, target support | Server parser and pyOCD from verified local support only. |
| Candidate selection | Agent chooses only a server-issued opaque candidate ID and may provide citations. |
| Physical memory/erase facts | Server-derived from verified pack/pyOCD support and server-read PDF; caller ranges remain prohibited. |
| Board wiring/external hardware | Optional project/runtime facts, never inferred from MCU identity. |
| Deployment partitions | Server-derived policy, never inherited from another board or supplied as ranges. |

## Generic local resolution

### Pack inspection

Add a read-only PackInspector and DeviceSupportResolver that scan only packs in the server-owned
verified-pack registry, exposed through repository and active-project manifests. For each candidate it must:

1. verify exact bytes before opening the archive;
2. safely parse CMSIS-Pack/PDSC data with archive-size, member-count, path traversal, XML entity, and malformed-document limits;
3. enumerate exact device and variant aliases, cores, SVDs, memory declarations, flash algorithms, and pyOCD-resolvable target candidates;
4. return immutable candidate records with pack digest, PDSC device identity, canonical target, pyOCD version, and evidence references;
5. index pyOCD built-in targets through equivalent exact metadata where available.

Support must never be inferred from an MCU-family prefix, filename, substring, global pyOCD registration, or unmanifested pack. A pack lacking an exact part-to-device-to-target relationship is inventory-only and cannot authorize setup.

### Deterministic target-binding contract

PDSC does not normatively define a pyOCD target-name string. The verified-pack registry must
therefore contain server-provisioned immutable target bindings, rather than allowing runtime name
inference. Each binding is the tuple:

- registry record ID and verified pack SHA-256;
- one exact PDSC leaf part number plus normalized aliases declared by that leaf, never aliases
  guessed from a family name;
- canonical processor/core tuple, normalized flash/RAM/erase geometry, SVD digest, and flash
  algorithm digest from that leaf;
- one canonical pyOCD target identifier and the pyOCD version used to verify it; and
- an isolated pack-load proof record: pyOCD is invoked with only the verified pack payload and
  target identifier, and its resolved target metadata must equal the recorded core, geometry,
  SVD, and algorithm tuple.

Provisioning rejects a binding if pyOCD does not expose enough resolved metadata for these
equalities. Runtime replays the isolated proof against the current verified bytes/version before
the binding is a candidate. Built-in targets use a separate immutable server-owned built-in target
registry with the same part-alias/core/geometry/SVD proof tuple and pyOCD-version digest. Neither
registry permits runtime target-name guessing.

### Agent selection

- One exact verified candidate: server selects it.
- Multiple exact verified candidates: server returns bounded summaries and opaque run-scoped IDs. The agent uses the local datasheet to select an ID through setup continuation, with concise page citations.
- No candidate: return setup/device-support-unavailable with local-only remediation. Do not download, search a live pack index, or ask the user for a target name.
- Continued ambiguity: return a bounded refusal naming conflicting local identities. Do not create a partial profile.

Continuation accepts neither pack path/digest nor raw target string. The populated setup plan displays the selected canonical pack and target as immutable server-filled facts, not caller parameters.

### Target proof before profile commit

Before commit, prove without flash, erase, unlock, option/security writes, or other backend mutation that:

1. the pack still matches its manifest bytes;
2. the PDSC device matches the exact requested part;
3. the target is supplied by that pack, or is an exact built-in target;
4. pyOCD replays the registry's isolated PDSC-leaf target binding using only that pack and proves
   equality of the recorded core, geometry, SVD, and algorithm tuple;
5. target core/device support is compatible with the selected PDSC device; and
6. a bounded non-destructive live attach succeeds.

Later mismatch invalidates the live proof and map currentness. It must never silently fall back to another target or pack.

## Datasheet evidence and capability states

Add a server-owned DatasheetEvidenceExtractor. It reads the PDF itself and records digest plus stable page/text-span references. It derives:

- exact part/package aliases and document revision;
- physical nonvolatile and RAM regions;
- erase geometry where documented;
- system, OTP, option, and security regions;
- documented device-ID evidence when available.

The agent may nominate server-issued page or text-span IDs to disambiguate a document. It may not submit numeric ranges, erase sizes, masks, expected IDs, or partitions. Each parsed fact stores a normalized value, unit/address width, source-page/span digest, parser version, confidence, and closed cross-source conflict result. The server parses the nominated source itself and cross-checks safety-critical facts against verified pack/pyOCD support.

Conflicts, ambiguous tables, unsupported layouts, or missing critical facts fail closed. Canonical source records, references, parser version, and fact digests belong in the map semantic source record so refresh detects PDF, pack, or parser drift.

Use explicit states:

| State | Permitted operations |
| --- | --- |
| target_resolved | inventory and setup planning only |
| connected_unidentified | bounded connection diagnostics only |
| identity_verified | gated read/debug |
| physical_map_verified | runtime artifact containment evaluation |
| deployment_policy_verified | guarded application flash |

Missing a higher state must not make lower-state capabilities unusable.

## Generic attachment and identity

Replace catalog-required debug_connect_mode and debug_clock_hz with a server-owned bounded policy:

1. try provider/target normal non-destructive attach defaults;
2. on a documented failure, offer only server-issued candidates derived from probe capability and pack/target metadata, such as lower supported SWD speed or attach-under-reset;
3. agent selects an opaque candidate, not a raw clock or reset value;
4. attempts have a small run-scoped budget and preserve observations; attach-under-reset is disclosed as reset-causing but never becomes unlock, mass erase, recovery, or bootloader flash;
5. a catalog policy may rank a candidate for a known board but generic support must not require it.

Persist a successful policy only as a revalidated reproducibility hint.

Add IdentityProbeResolver using verified PDSC/SVD/pyOCD metadata plus datasheet evidence. Core
identity may corroborate but cannot prove an exact part. All reads must be inside server-derived
safe-readable regions and match documented proof. The capability lattice is normative:

| Proof | Gate/read/debug | Generic flash |
| --- | --- | --- |
| Exact ordering-code proof | Allowed after normal validation | Allowed only with deployment and bounded-driver proof |
| Compatibility-class proof with exact target/core and identical writable flash/RAM/erase geometry | Allowed; stamp says compatible, not exact | Allowed only with deployment and bounded-driver proof |
| Connection/core-only proof | Diagnostics only | Never |

No code or report may label a compatibility-class stamp as exact-part validation.

## Deployment and safety map

Physical flash does not establish flash ownership. For resolved datasheet-pack authority:

1. initial map contains verified physical/prohibited regions, null partitions, and deployment_policy
   kind none;
2. artifact collection derives a candidate erase-sector envelope but does not establish ownership;
3. first application policy is possible only under a server-verified fresh-device policy: every programmable internal-flash sector is blank, protected/non-programmable regions are excluded, and the bounded driver operation plan is eligible;
4. the first-flash plan holds its whole-device blank proof and candidate allocation only in
   run-scoped plan state. The server writes the schema-v3 fresh_device_allocation to
   memory_map.yaml atomically only after backend-confirmed successful execution; it binds the
   allocated erase sectors, the pre-execution map digest, audit-only creation artifact digest, and
   a non-circular allocation digest;
5. failed, cancelled, timed-out, or partially confirmed first flash creates no allocation. The
   observed target is nonblank-unknown and future generic flash refuses until a separately safe
   recovery/remedy establishes authority;
6. refresh revalidates an existing allocation against its recorded sectors, allocation digest, and
   driver/geometry evidence. It never requires the creation artifact bytes or final map digest and
   never reapplies the all-blank bootstrap rule to a successful allocation; each reflash retains
   its own run-scoped artifact binding and runtime containment checks;
7. reflash is limited to a previously server-recorded allocation. Nonblank sectors with unknown ownership refuse flash; never erase to discover ownership;
8. bootloader/application splits arise only from a server-validated deployment record plus artifact evidence, or an optional catalog override; preserve bootloader bytes and erase-sector boundaries;
9. generic execution verifies the immutable operation plan and sector contents immediately before every backend mutation; chip-erase or unbounded drivers are refused;
10. current Safety Layer v2 artifact binding, segment/vector/entry containment, gates, and zero-backend-call refusal behavior remain mandatory.

Refresh rederives generic device/deployment inputs and refuses semantic drift. Caller-supplied ranges remain prohibited.

## Build and board features

- Native build remains the generic default. An unknown MCU must not receive a development-kit Zephyr target because it shares an MCU with one.
- Expose resolved core/target/memory facts as read-only build guidance. Agents may create Make, CMake, vendor, bare-metal, or RTOS projects; the generic helper builds the project they create.
- Do not promise LED, UART, pin mux, oscillator, or board peripherals from an MCU datasheet. Those are optional project/runtime facts. Missing UART must not block setup.
- Project board facts can guide application generation but cannot broaden memory or flash authority.

## Tool, profile, and migration changes

1. Setup overview/status gains a bounded device_support section: datasheet digest, resolution state, candidate count, opaque IDs, and next allowed action.
2. Existing setup continuation selects server-issued support and attachment-policy candidates. Remove catalog expected-target equality from generic resolution while retaining exact candidate proof.
3. Profile/map source records hold server-generated canonical target, pack/PDSC identity, pyOCD identity, and source digests. The plan engine binds their canonical digest.
4. Refactor reviewed_evidence, map derivation, validation support checks, and connection construction to consume DeviceSupportAuthority rather than CatalogBoard directly.
5. Retain reviewed_boards.json only as an optional provider for migrated/proven-board profiles. Remove it from new-profile setup, generic map derivation, and MCU-only build guidance.
6. Existing profiles migrate by resolving catalog authority through the common representation. Never silently widen a legacy partition.

## Security invariants

- Runtime is local-only: no pack update, package installation, source download, or live-index lookup.
- Unmanifested packs, changed bytes, duplicate providers, unsafe archives, ambiguous aliases/targets, stale pyOCD identity, and conflicting PDF/pack facts fail closed.
- No backend mutation occurs before target/pack proof, map/artifact containment, plan digest, permission, and deployment-policy checks.
- This feature introduces no target unlock, mass erase, recovery, bootloader flash, option/security writes, or caller-provided ranges.
- Candidate selection, attachment attempts, gates, plans, and permissions are run-scoped. Restart, disconnect, or identity mismatch clears live proof.

## Required tests

Add nonredundant tests for:

1. fresh temporary root with no catalog entry and one verified pack/PDSC exact part/target: generic setup commits;
2. exact aliases versus prefix/family/filename guesses;
3. multiple candidates with opaque selection and no raw target/path acceptance;
4. changed digest, duplicate provider, stale registration, malformed/oversize pack, and PDSC/SVD/target disagreement: zero backend calls;
5. built-in targets through the same common authority model;
6. PDF extraction replay, changed PDF, citation/text-span validation, and absent/conflicting geometry or identity evidence;
7. generic attachment ordering/budget/disclosure and no destructive fallback;
8. missing identity proof: no live stamp or flash;
9. no initial partition; whole-programmable-device blank first-flash proof; refusal when an
   application sector is blank but another programmable/reserved sector is nonblank or unreadable;
   atomic non-circular allocation commit only after success; failed/cancelled/partial first-flash
   lifecycle; refresh after success; two distinct valid artifacts reflashing inside one allocation
   without staling the map/gate; zero erase/program calls for refusal; and bootloader preservation;
10. generic refresh/currentness across pack/PDF/pyOCD drift;
11. unchanged Safety Layer v2 containment, artifact binding, breakpoint evidence, caller-range prohibition, gate clearing, and action-batch behavior;
12. catalog compatibility plus an L476 fresh-root case with the catalog entry disabled that succeeds through generic resolution;
13. no development-kit Zephyr target from MCU-only resolution; and
14. fresh-root MCP generic setup to native build to collect to flash-plan to validation to debug using fake backends for rejected-boundary proof.

## Live acceptance and completion

Use a board selected through the generic path, not a catalog override. In a fresh project with local datasheet support, an agent must resolve support without user pack/target fields, use generic build guidance, run guarded application flash, and demonstrate board-appropriate observable output plus bounded debug. Repeat with another probe family or target/provider class when hardware is available. Preserve model, prompt, candidate timeline, resolution digests, plan IDs, artifact hashes, backend-call proof, and transcript.

This changes the safety-authority model. Before implementation, write an ADR defining generic resolved authority, the catalog's optional role, and deployment-policy rules; then write and adversarially review a plan.

Completion requires a noncatalog part/PDF/local-pack setup with no user pack/target/clock/reset/range fields; server proof of all agent-selected candidates; generic validation/refresh/debug under current gate semantics; no flash before physical, identity, artifact, and deployment evidence; catalog compatibility without policy inheritance; provider-neutral build guidance; full software verification; fresh-root MCP evidence; noncatalog live hardware acceptance; and final adversarial review.
