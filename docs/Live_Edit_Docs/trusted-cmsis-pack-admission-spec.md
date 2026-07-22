# Trusted CMSIS-Pack Admission

Status: proposed

## Summary

The server must accept structurally valid CMSIS-Packs returned by the compliant setup agent
without applying arbitrary archive-size, expanded-size, member-count, or compression-ratio
security ceilings. Pack integrity, exact-device resolution, flash-authority derivation, and live
attachment remain mandatory.

This change fixes current STM32U5 onboarding. The official
`Keil.STM32U5xx_DFP.3.2.0.pack` is only 8,821,594 bytes on disk, but its members total
174,140,971 uncompressed bytes. The current 128 MiB expanded-size ceiling therefore rejects a
normal official pack before the server examines its PDSC device or flash algorithm.

Known regression artifact:

- pack ID: `Keil.STM32U5xx_DFP`
- version: `3.2.0`
- source: `https://www.keil.com/pack/Keil.STM32U5xx_DFP.3.2.0.pack`
- SHA-256: `e320687fe534f2fe6902e9bcdee981315abea26c4ca547142af9b9439e958be6`
- archive members: 401
- total declared uncompressed size: 174,140,971 bytes
- maximum individual-member compression ratio: approximately 43.96:1

## Trust model

The setup caller is a compliant agent. It researches an official vendor pack, acquires it, and
submits the exact source record and local bytes requested by the server. The pack is not an
anonymous hostile upload endpoint.

The server remains responsible for proving that the returned bytes can establish deterministic
device authority. Trusting the agent to return the researched artifact does not allow the agent to
choose memory ranges, flash sectors, a PDSC leaf, or the final pyOCD target.

Consequently, archive resource ceilings are not part of the authorization boundary. They must not
turn a valid official pack into an unsupported device. Structural parsing and authority checks
remain part of the boundary.

## Required behavior

### Pack byte loading

Replace the current size-bounded byte loader with a pack byte loader that:

1. requires a regular, non-empty file;
2. reads the selected file exactly;
3. detects a file that changes while it is being read;
4. computes and persists the SHA-256 of the exact bytes; and
5. applies no fixed maximum archive-byte size.

The existing `MAX_CMSIS_PACK_ARCHIVE_BYTES` rejection must be removed. If the
`read_bounded_pack_bytes` name is retained temporarily for compatibility, its docstring must no
longer claim a fixed byte bound. Prefer renaming it to `read_pack_bytes` and updating all internal
call sites in one change.

### Archive validation

Remove these admission policies from `setup_flow/device_support.py`:

- `_MAX_PACK_MEMBERS`;
- `_MAX_PACK_UNCOMPRESSED_BYTES`;
- `_MAX_PACK_COMPRESSION_RATIO`;
- rejection based only on member count;
- rejection based only on total declared expanded size; and
- rejection based only on a member's compression ratio.

Continue to require:

- a readable ZIP archive;
- exactly one PDSC document;
- no absolute or parent-traversing archive member paths;
- no XML `DOCTYPE` or entity declarations in the PDSC; and
- successful parsing by pyOCD's `CmsisPack` implementation.

Those checks establish a usable, deterministic format. They are not resource limits and must not
be relaxed by a caller flag.

Archive member count, declared expanded bytes, and maximum compression ratio may be recorded as
diagnostics. Diagnostic values must never grant authority and must not reject the candidate.

### Authority checks retained unchanged

The change must not bypass or weaken any of the following:

- exact MCU ordering-code normalization;
- exactly one matching PDSC device leaf, including the existing lowercase `x` placeholder rules;
- server-derived canonical pyOCD target name;
- exact pack-byte SHA-256 persistence and replay;
- manifest conflict and multiple-provider rejection;
- physical flash and writable RAM discovery;
- unambiguous programmable/default/boot flash selection;
- complete sector coverage;
- usable `EraseSector` and `ProgramPage` FLM symbols, or the existing equivalent pyOCD flash
  information route;
- valid erased-byte value;
- live target attachment before profile commit; and
- later replay of the persisted pack, device, target, geometry, and identity authority.

The server must continue to fail closed for missing or ambiguous device authority even though it
no longer rejects a pack merely for being large.

## Code changes

### `src/pyocd_debug_mcp/pack_provision.py`

- Remove `MAX_CMSIS_PACK_ARCHIVE_BYTES`.
- Rename `read_bounded_pack_bytes` to `read_pack_bytes`, or change its contract and migrate the
  misleading name in the same release.
- Preserve the non-empty, regular-file, stable-read, and exact-byte behavior.
- Update callers in pack provisioning, target enumeration, setup promotion, device support, and
  the pyOCD adapter.

### `src/pyocd_debug_mcp/setup_flow/device_support.py`

- Remove the three resource-ceiling constants and their rejection branches.
- Retain structural ZIP and PDSC validation.
- Optionally return an immutable archive-inventory record for setup diagnostics; do not persist it
  as authority.

### Setup reporting and documentation

- Replace wording that describes agent-returned packs as untrusted or archive-bounded.
- Describe pack admission as structurally validated, digest-bound, exact-device resolved, and
  live-tested.
- If archive metrics are reported, label them informational.
- Do not introduce a U5 family whitelist or a U5-specific exception.

## Tests

Add focused tests in a new pack-admission test module.

1. A structurally valid pack whose declared expanded total exceeds 128 MiB is not rejected for
   size.
2. A structurally valid pack with more than 4,096 members is not rejected for member count.
3. A structurally valid pack containing a highly compressible non-PDSC resource is not rejected
   for compression ratio.
4. Empty files, malformed ZIP files, multiple PDSCs, unsafe member paths, and PDSC entity
   declarations remain rejected.
5. Candidate bytes that change between selection and promotion remain rejected.
6. A digest mismatch remains rejected and the candidate is not promoted.
7. Exact-device ambiguity and target mismatch remain rejected.
8. A pack without complete programmable flash authority remains debug-only.

Refactor archive inventory validation so the first three cases can use synthetic member metadata;
the unit suite must not allocate hundreds of MiB merely to cross the former thresholds.

Add an opt-in integration test that accepts a local path to the pinned U5 3.2.0 regression
artifact. It must verify at least:

- archive admission;
- exact binding for `STM32U575ZIT6Q` to PDSC leaf `STM32U575ZITxQ`;
- canonical target `stm32u575zitxq`;
- physical flash and writable RAM geometry;
- complete sector geometry and a non-null driver proof; and
- no requirement for a U5-specific server branch.

The integration test must be network-free. Artifact acquisition is a separate developer step, and
the test must verify the pinned SHA-256 before using the local file.

## Acceptance criteria

The implementation is complete when:

1. `Keil.STM32U5xx_DFP.3.2.0.pack` passes pack admission with the pinned digest above.
2. `STM32U575ZIT6Q` reaches the existing live-attachment step instead of failing with
   `CMSIS-Pack exceeds the supported unpacked size`.
3. With suitable physical hardware attached, successful live attachment permits normal generic
   profile creation, safety-map generation, application allocation, and statically contained
   ELF/AXF/HEX flashing.
4. Without hardware, the static integration test proves the same target and flash authority up to
   (but not including) live attachment.
5. Existing malformed-pack, digest, exact-device, target, geometry, and promotion failures still
   fail closed.
6. No board, vendor, family, pack version, or target-specific size exemption is added.

## Compatibility and migration

Existing pinned manifests and board profiles remain valid. Their pack digests and derived support
IDs do not change solely because archive resource ceilings were removed.

No schema migration is required unless optional archive diagnostics are persisted. If diagnostics
are persisted, they must go only into setup reports and must not participate in support IDs,
profile authority, safety-map digests, or runtime permissions.

The implementation must accept future official packs based on their deterministic contents rather
than requiring another ceiling increase when vendor packs grow.
