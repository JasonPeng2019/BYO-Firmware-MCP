# Safety Layer v2 Specification

Status: implementation specification  
Decision record: `decisions/ADR-0001-single-file-safety-authority.md`

## 1. Purpose

Safety Layer v2 makes normal firmware iteration simple without weakening the write boundary. Board
identity is established once, the persistent safety authority is one stable memory map, and every
firmware image is checked against that map immediately before it is flashed. Host-side safety
damage bottoms out at `board_safety_refresh`; it never sends an established board through initial
setup again.

## 2. Invariants

1. `.firm/safety/<board_id>/memory_map.yaml` is the only persisted safety-authority file.
2. Gates, live validation, plans, permissions, and mismatch allowances remain run-scoped and are
   never restored from disk.
3. Callers never supply allowed memory ranges, erase geometry, or authority-bearing evidence.
4. A new application or bootloader binary does not change persistent safety authority.
5. Every flash is checked from its actual ELF/HEX contents before the backend can erase or write.
6. Every safety-only failure names `board_safety_refresh` as the greatest recovery action.
7. Setup is not a safety recovery mechanism for an established profile.
8. Prohibited and unknown regions remain denied, and prohibited containment overrides all other
   classifications.

## 3. Persistent memory-map schema

`memory_map.yaml` is a self-contained, deterministic document with these top-level fields:

- `schema_version`: integer `2`.
- `board_id`: exact logical board identifier.
- `identity`: exact MCU part number, pyOCD target, and reviewed board type.
- `source_digests`: semantic profile, reviewed device-support evidence, reviewed official evidence,
  and map-generator schema digests.
- `geometry`: physical flash/RAM bounds and uniform or explicit erase geometry.
- `partitions`: stable application and optional bootloader half-open ranges.
- `regions`: typed half-open ranges with executable flag and usable provenance.

The semantic profile digest covers only safety-relevant identity fields. It excludes display names,
timestamps, UART settings, probe labels, `safety_ref`, report identifiers, and paths. Firmware
artifact paths and hashes are not persisted in the map. Canonical parsed map content is hashed in
memory when a gate needs a map stamp; no persisted aggregate fingerprint exists.

`source_manifest.json` and `safety_report.json` are removed completely. They are neither read nor
written, and old copies under `.firm/safety/**` are deleted. Old map schemas are unsupported and
route to a fresh `board_safety_refresh`; there is no legacy authority migration.

## 4. Stable authority and partitions

Stable physical ranges, prohibited ranges, erase geometry, and deployment partitions come only
from server-owned reviewed catalog/pack/target/SVD/datasheet evidence. Every automatically
flash-capable catalog entry explicitly declares whether its application partition is authoritative
and declares any bootloader partition separately. A full-physical-flash application partition is
valid only when that reviewed policy explicitly asserts that no protected resident bootloader
partition exists. Missing partition authority keeps flashing unavailable. A build may prove that it
fits a partition but may never widen one. An intentional reviewed partition-policy change requires
`board_safety_refresh`.

Initial setup must finish with sufficient reviewed inputs to reproduce the map. If an independent
source later disappears, refresh fails closed and requests that exact evidence; it does not route
through setup.

## 5. `board_safety_refresh`

`board_safety_refresh` is the only safety maintenance and recovery tool. Its public schema accepts
only `board_id`; build artifacts never become refresh inputs or map authority.

Refresh performs one internal mode:

- **Deterministic rebuild:** rederive the complete candidate map from current server-owned reviewed
  sources every time. Report which semantic groups changed, but do not maintain a second scoped
  mutation algorithm after the source manifest has been removed.

Refresh validates the staged document, checks conflicts/overlaps and prohibited precedence, and
atomically replaces only `memory_map.yaml`. A failed rebuild never promotes a partial map. Its
payload contains `validation_required` and an exact next action. Refresh preserves an existing
valid live-identity stamp, but it never creates one.

Refresh is required only when stable safety authority may change: MCU/target evidence, flash/RAM
geometry, reviewed application/bootloader partition policy, map schema, reviewed pack/SVD/target or
datasheet evidence, or a missing/corrupt map. It is not required for an ordinary rebuild, path
change, build timestamp, application size change within the partition, reset, flash, or UART use.

## 6. Lean `board_validate`

Validation performs only the live proof required to open a gate:

1. load the existing schema-v2 profile;
2. resolve the intended stable probe identity;
3. open or reuse a bounded non-destructive connection;
4. read the reviewed silicon/part identity and compare it with the profile;
5. load the single map and prove its identity matches the profile;
6. create a run-scoped live-identity stamp and bind the current canonical map digest.

Validation does not capture UART, test firmware behavior, collect artifacts, install/research
packages, rebuild the map, reset/halt, flash/erase, or rewrite the profile. UART readiness remains
in `get_setup_status` and serial actions.

The tool must be called only in these three trigger categories:

1. **No current live proof:** server restart, initial setup before the first gate opening, or a run
   in which validation has not passed.
2. **Connection identity changed:** physical/logical disconnect or reconnect, different probe, or a
   connection override that changes probe/target identity.
3. **Hardware identity may have changed:** explicit MCU/profile identity repair or destructive
   target recovery.

It must not be called merely because of a build, flash, reset/halt, UART operation, safety refresh,
full memory-map reconstruction, or bookkeeping change. After refresh, validation is required only
when a valid live identity proof is absent for one of the reasons above.

## 7. Run-scoped identity and map stamps

The gate keeps separate concepts even if represented by one cohesive API:

- `LiveIdentityStamp`: board, connection, probe, observed MCU identity, and validation run.
- `SafetyMapStamp`: canonical digest of the current parsed map.

Disconnect, connection change, restart, identity repair, and destructive recovery clear the live
proof. Successful refresh may update the map stamp only when the live proof still applies to the
same board and connection. Neither stamp is serialized.

## 8. Flash-time artifact containment

Every `flash_application` and `flash_bootloader` call independently inspects the selected ELF/HEX
at execution time and verifies, before any backend mutation:

- target identity exactly matches the map/profile;
- all loadable and HEX ranges are fully inside the appropriate stable partition;
- entry point and vector table are present/valid when required and inside that partition;
- every required erase sector is wholly inside that partition;
- no range touches the other partition, prohibited space, ROM bootloader, or unknown space.

At populated plan acceptance, the server hashes the selected artifact and binds that digest to the
run-scoped plan. At execution start it rehashes the artifact. A mismatch is a pre-execution refusal,
does not burn budget or permission, invokes no backend mutation, and requires a replacement plan.
This promise covers plan acceptance through execution start; compliant workflows must not rebuild
the selected output concurrently with an active flash operation. No immutable staging copy is
required for that normal firmware workflow.

Normal workflow:

```
build firmware -> collect_build_artifacts -> flash plan -> flash-time containment -> flash
```

## 9. Setup eligibility and MCU mismatch

For an existing profile, populated `board_setup-plan`, `board_setup`, and `board_fix_setup` remain
physically locked. A mismatch allowance produced by `board_validate` for that exact board, probe,
expected MCU, and observed MCU changes the refusal into a neutral adoption route: tell the user and
ask what they want. If they keep the different hardware, obtain a new familiar name through
`setup_overview` and run setup under its new logical `board_id`; never unlock an in-place identity
rewrite. The allowance is cleared by restart, disconnect, probe change, or successful validation
of the expected MCU.

A genuinely unknown board with no profile remains the sole non-mismatch initial-setup path, but
automatic safety authority is available only for a board type with complete packaged reviewed
evidence. Unreviewed/custom board types fail closed until maintainers add that evidence; agents do
not invent it or supply allowed ranges.
Ordinary safety failures never expose setup.

On silicon mismatch, validation must not instruct the agent to rerun setup. It reports expected and
observed identity in conversational language and tells the agent to inform the user and ask what to
do. The server may expose the setup plan, but no repair occurs until the user explicitly chooses.
A different MCU is never adopted by rewriting the established profile in place. If the user wants
to keep the newly attached hardware, setup creates a new logical board/profile; the old profile and
its authority remain intact. `board_fix_setup` remains limited to an incomplete same-identity setup.

## 9.1 Exact live identity support

Every automatically supported board must have a reviewed live identity proof. This may be an exact
part register or a masked device-family identifier when package information is not electronically
observable. The proof and its limitation are cataloged. Missing identity evidence makes validation
stamp-ineligible rather than falling back silently to a generic readable address.

## 9.2 Breakpoint executable evidence

Stable deployment partitions are not treated as entirely executable. `set_breakpoint` accepts and
plan-binds the current ELF artifact, parses executable loadable sections at execution time, and
permits the breakpoint only inside one of those sections. The artifact digest is checked with the
same plan-acceptance/execution-start rule as flash. `remove_breakpoint` remains always available.

## 9.3 ELF and HEX contract

ELF is the authoritative source for entry point, vector table, executable segments, and target/build
metadata. A selected HEX must have a matching ELF companion collected from the same build; HEX-only
flashing remains unsupported. Target checking means the live connection target must match the
reviewed map/profile target; the server does not claim an arbitrary HEX independently proves its MCU.

## 10. Routing and guidance

All public descriptions, initialization guidance, NULL-plan prompts, loader guidance, accepted-plan
responses, status payloads, and docs use this remedy model:

- missing/malformed/inconsistent/stale map -> `board_safety_refresh`;
- reviewed evidence/geometry/partition-policy change -> `board_safety_refresh`;
- artifact outside the stable partition -> fix/select the build;
- no live proof -> `board_validate`;
- live MCU mismatch -> tell the user expected versus observed and ask for guidance;
- user elects to adopt mismatched hardware -> now-eligible `board_setup-plan`;
- unavailable independent reviewed evidence -> fail closed and name it.

The artifact collector tells agents that ordinary collected artifacts proceed to their flash plan;
it recommends refresh only for an actual stable-map problem.

## 11. Acceptance criteria

- **AC-S2.1:** only `memory_map.yaml` is read/written below each board safety directory.
- **AC-S2.2:** timestamp, display-name, UART, path, and safety-reference changes do not stale a map.
- **AC-S2.3:** missing, malformed, old-schema, or unclassifiable maps rebuild through refresh without
  setup.
- **AC-S2.4:** refresh preserves a same-connection live proof and reports whether validation is
  required.
- **AC-S2.5:** validation has only the three documented trigger categories and performs no UART or
  firmware-behavior check.
- **AC-S2.6:** setup remains locked for an established matching board and ordinary safety failures.
- **AC-S2.7:** silicon mismatch exposes a scoped new-profile adoption route; user-facing guidance
  first asks the user what to do and the established profile cannot be overwritten.
- **AC-S2.8:** ordinary new ELF/HEX bytes do not stale the gate or require refresh.
- **AC-S2.9:** artifact bytes changed after plan acceptance are rejected before budget/backend start.
- **AC-S2.10:** valid application and bootloader images are checked against stable partitions on
  every call; boundary/erase violations produce zero backend erase/write calls.
- **AC-S2.11:** no caller-supplied allowed range, persisted authority, or automatic setup repair is
  introduced.
- **AC-S2.12:** MCP contracts, focused suites, static checks, full pytest, package build, and stdio
  smoke are green; an independent agent can follow the revised MCP flow from returned guidance.
- **AC-S2.13:** every automatically supported catalog entry has a reviewed live identity proof and
  authoritative deployment-partition policy, otherwise validation/flash remain unavailable.
- **AC-S2.14:** `set_breakpoint` uses per-call ELF executable sections rather than treating the whole
  stable application partition as executable.
