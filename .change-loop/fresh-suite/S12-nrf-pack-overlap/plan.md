# Change implementation plan

## Source change list

- Source:
  `.change-loop/fresh-suite/S12-nrf-pack-overlap/changes.md`
- Goal summary: Repair verified CMSIS-Pack geometry canonicalization so overlapping descriptive
  flash/RAM records cannot block an otherwise valid generic safety map, while preserving strict
  persisted-map invariants, disjoint physical banks, and honest failure for unresolved ambiguity.

## Repository context and assumptions

- Verified architecture and relevant entry points:
  - `src/pyocd_debug_mcp/setup_flow/device_support.py::resolve_registered_pack_geometry`
    parses the exact replayed PDSC leaf, selects scalar default flash/RAM and programming geometry,
    and currently copies every exact-deduplicated flash/RAM candidate into
    `PackMemoryGeometry.flash_regions` / `ram_regions`.
  - `src/pyocd_debug_mcp/server.py::_derive_generic_safety_map` converts those collections into
    `GenericMapGeometry` and persisted physical `SafetyRegion` rows.
  - `src/pyocd_debug_mcp/safety/map_build.py::GenericMapGeometry.__post_init__` correctly rejects
    overlapping physical regions as an authority-document invariant and must remain strict.
  - The verified Nordic `nrf52840_xxaa` PDSC leaf exposes default/boot/testable `IROM1`
    `[0x0,0x100000)` plus non-default/non-boot/non-testable `nRF52xxx`
    `[0x0,0x200000)`; exact-range deduplication retains both and triggers the strict overlap
    rejection on two independent boards.
- Existing test/build commands relevant to the change:
  - focused tests run through the repository virtual environment with
    `.venv/Scripts/python.exe -m pytest` with the tester-owned paths and `-q` on Windows, or the equivalent
    `.venv/bin/python -m pytest ... -q` in POSIX;
  - adjacent regression surfaces include pack replay/geometry, generic safety-map construction,
    setup/validation, and the existing trust-model tests;
  - repository checks include Ruff, Pyright, and `git diff --check`.
- Design-charter application:
  - correctness: derive only physical ranges actually supported by the verified PDSC metadata;
  - simplicity: one deterministic canonicalizer at the pack-adapter boundary;
  - generalizability/dynamism: use standard region traits, never a Nordic/board special case;
  - neatness: keep overlap reconciliation in the module that owns PDSC-to-domain adaptation;
  - no paternalistic guarding: retain only the correctness guard against ambiguous physical
    authority.

## Plan items

### CL-001 — Canonicalize verified PDSC physical regions deterministically

- **What to change:** Replace exact-range-only flash/RAM canonicalization with one small shared
  physical-region canonicalizer operating on the parsed PDSC region objects before they become
  `PackAddressRegion` values. Exact duplicate ranges collapse. Distinct candidates are evaluated
  using the existing PDSC traits in this precedence order: unique/default region, boot region,
  testable region, then an unmarked region. Process higher-precedence records first. Preserve every
  candidate disjoint from already accepted physical ranges. When a lower-precedence candidate
  overlaps an accepted higher-precedence range, discard that entire lower-precedence descriptor;
  never subtract the overlap and invent its remaining tail as a new bank. When two distinct,
  non-exact ranges with equal highest precedence overlap, raise `PackProvisionError` with the
  memory kind and conflicting ranges instead of guessing. Return accepted ranges in deterministic
  address/name order.
- **Where:** `src/pyocd_debug_mcp/setup_flow/device_support.py`, adjacent to the existing
  `PackAddressRegion` and `resolve_registered_pack_geometry` adapters.
- **Exact intended behavior:** The Nordic-shaped `[0,1 MiB)` default+boot+testable record plus
  `[0,2 MiB)` non-default/non-boot/non-testable record canonicalizes to exactly `[0,1 MiB)`.
  `[1 MiB,2 MiB)` is not fabricated. Exact duplicates yield one range. Disjoint physical banks
  all remain. Equal-precedence partial/nested overlaps fail before a safety document is built,
  naming an ambiguous verified PDSC physical flash or RAM description and the conflicting
  half-open ranges.
- **Must remain intact:** Scalar `flash_start/end` and `ram_start/end` selection, programmable
  flash/FLM choice, erase-sector proof/digest, erased-byte evidence, ROM/peripheral/SVD parsing,
  CPU-system regions, pack byte/binding replay, deterministic geometry digesting, and public
  exception types remain compatible. No part number, board, OS, path, or vendor special case is
  allowed.
- **Objective verification:** Automated tests assert the Nordic-shaped overlap result, whole-row
  discard without a fabricated tail, exact duplicate collapse, deterministic disjoint multi-bank
  preservation, equal-precedence ambiguous flash failure, and the same rules for writable RAM.

<!-- Assumption: PDSC default, then boot, then testable flags are the existing deterministic
physical-authority precedence. A lower-precedence overlapping descriptor is treated as a broader
algorithm/container description and discarded whole; no uncovered suffix is promoted to physical
memory without its own disjoint descriptor. Equal-precedence overlap is genuinely ambiguous and
must fail honestly. -->

### CL-002 — Feed only canonical physical geometry into generic safety authority

- **What to change:** Use the CL-001 canonical collections for
  `PackMemoryGeometry.flash_regions` and `ram_regions`, and therefore for
  `_derive_generic_safety_map`'s `GenericMapGeometry` and physical safety regions. Keep the strict
  overlap checks in `GenericMapGeometry` unchanged so malformed persisted documents or future
  adapter regressions still fail closed. Ensure a canonicalized read/debug-only map remains valid
  when erase authority is unavailable and gains no application/erase authority merely because an
  overlap was resolved.
- **Where:** `src/pyocd_debug_mcp/setup_flow/device_support.py` integration at
  `resolve_registered_pack_geometry`; verify unchanged downstream behavior in
  `src/pyocd_debug_mcp/server.py::_derive_generic_safety_map` and
  `src/pyocd_debug_mcp/safety/map_build.py::GenericMapGeometry`.
- **Exact intended behavior:** Replaying the verified nRF52840 pack produces a non-overlapping
  physical flash geometry ending at `0x00100000`; generic map construction no longer fails with
  `generic physical flash regions must not overlap`. Setup/refresh/validate can proceed using that
  reviewed physical authority. A truly ambiguous overlap returns an actionable pack-geometry
  error and never persists a map. No overlap reconciliation widens flash, RAM, erase, write, or
  deployment authority.
- **Must remain intact:** `GenericMapGeometry` continues rejecting overlapping input and
  flash/RAM cross-overlap; persisted source digests and replay remain deterministic; disjoint
  multi-bank read/debug capability, artifact-derived allocation monotonicity, target identity,
  validation gates, and all public MCP schemas/messages outside the more actionable ambiguity
  detail remain unchanged.
- **Objective verification:** An integration-level test reaches generic map construction with the
  Nordic-shaped verified geometry and asserts the single 1 MiB physical flash region, no
  fabricated tail/application region, and no overlap error. Separate regression assertions feed
  overlapping rows directly to `GenericMapGeometry` and confirm its rejection is unchanged.

### CL-003 — Protect pack replay and adjacent generic-map behavior

- **What to change:** Add adversarial spec and regression coverage owned by the change-loop
  testers. The spec surface proves every CL-001/CL-002 rule. The regression surface traces pack
  registration/replay through generic safety derivation and checks existing disjoint-bank,
  erase-proof, peripheral/SVD, malformed-authority, and strict-map behavior touched by the diff.
- **Where:** Tester-owned files under `tests/`, with isolated commands/manifests under
  `.change-loop/fresh-suite/S12-nrf-pack-overlap/state/`.
- **Exact intended behavior:** Both tester suites independently fail on the pre-repair
  exact-dedup implementation and pass only when canonicalization is generic, conservative, and
  deterministic. Tests use synthetic/in-memory verified pack/PDSC fixtures or the existing test
  fixture mechanisms; they do not require live boards, external network, a vendor-installed SDK,
  or the S12 run directory.
- **Must remain intact:** Existing tests are not modified, disabled, renamed, or weakened. The
  doer never edits tests or gate controls. Test fixtures do not encode a production nRF special
  case as the implementation.
- **Objective verification:** The neutral harness executes the spec-tester and regression-tester
  commands in the same iteration, both exit zero, manifested test hashes remain untampered, and a
  main-model replay runs the focused new tests plus relevant existing pack/safety/setup tests,
  Ruff, Pyright, and `git diff --check`.

## Out of scope / must not change

- Do not weaken or remove `GenericMapGeometry` overlap, flash/RAM separation, erase-sector, source
  digest, allocation, or persisted-authority validation.
- Do not add a Nordic/nRF52840/board allowlist, hardcoded address, pack filename, OS branch, or
  environment-specific path.
- Do not change MCU/target resolution, datasheet applicability, pack download/cache semantics,
  public setup schemas, plan/permission gates, hardware actions, or firmware.
- Do not edit the S12 experiment, its evidence, fixture mapping, or test specification as part of
  the production repair.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, or generated artifacts.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, and adjacent behavior touched by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, or gate commands.
