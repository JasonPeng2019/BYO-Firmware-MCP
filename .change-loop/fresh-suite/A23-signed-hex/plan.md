# Change implementation plan

## Source change list

- Source: `/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/.change-loop/fresh-suite/A23-signed-hex/changes.md`
- Goal summary: Permit a same-build deployment HEX to wrap its companion application's ELF
  image with connected signing/header/trailer data and conventional fill-byte representation
  changes, while retaining content-correlation checks and leaving final address/erase authority
  with the existing reviewed safety-map enforcement.

## Repository context and assumptions

- Verified architecture and relevant entry points:
  `src/pyocd_debug_mcp/safety/enforce.py::_extract_runtime_evidence` resolves an exact
  same-stem ELF/AXF companion for a selected HEX and passes both to
  `src/pyocd_debug_mcp/safety/linker.py::extract_build_evidence`. The latter parses the two
  images, currently requires the HEX address set to be a subset of the ELF image, compares
  overlapping bytes, requires all non-fill ELF bytes in the HEX, and independently requires
  HEX ranges to fit the ELF-symbol-derived partition. Both the reviewed-partition path
  (`SafetyPolicy.check_flash`) and generic path
  (`SafetyPolicy.check_generic_application_candidate`) already include every
  `BuildEvidence.hex_ranges` item in their content checks; they enforce the reviewed/static
  partition or verified physical geometry/content-derived erase allocation respectively.
- Existing test/build commands relevant to the change:
  `uv run --locked --no-sync pytest`, `uv run --locked --no-sync ruff check .`,
  `uv run --locked --no-sync pyright`, and `git diff --check`. Focused tester commands must
  use isolated tester-owned files under `tests/`.

## Plan items

### CL-001 — Correlate connected deployment HEX content without requiring an ELF subset

- **What to change:** Replace the unconditional HEX-address-subset and
  ELF-partition-containment requirements with one small, content-based companion relationship:
  parse the HEX into its existing maximal contiguous data ranges; require every HEX range that
  contains supplemental addresses absent from the ELF image to overlap or directly touch
  file-backed ELF load content; compare every overlapping meaningful byte; and retain the
  existing requirement that every meaningful ELF byte is present in the HEX. Treat only a
  `0x00` versus `0xFF` overlap difference as equivalent fill representation. Return a stable
  build-evidence error at the first supplemental address in any disconnected HEX range.
- **Where:** `src/pyocd_debug_mcp/safety/linker.py`, in narrowly named private relationship
  helpers if they make `extract_build_evidence` clearer, and the HEX-validation block of
  `extract_build_evidence`.
- **Exact intended behavior:** A valid companion HEX may have a prefix, suffix, or gap-filling
  data that is contiguous with (or part of a contiguous HEX range overlapping) ELF-backed load
  content, even when that wrapper lies outside an ELF-symbol-derived partition. Overlapping
  bytes must be identical except for the unordered fill pair `{0x00, 0xFF}`. A differing
  overlap where either value is not a fill byte remains `build/hex-content-conflict`; omission
  of any ELF byte whose value is neither `0x00` nor `0xFF` remains
  `build/hex-incomplete`. A supplemental HEX component separated from all ELF-backed load
  content is refused with a specific `build/hex-*` code/message that identifies its first
  address and describes it as disconnected/unrelated companion content.
- **Must remain intact:** Strict Intel HEX syntax, checksum, EOF, repeated-address, address-range,
  ELF parsing, vector/entry/stack validation, ELF-segment partition validation, artifact
  provenance, same-stem companion selection, and all non-HEX behavior. The extractor must not
  recognize MCUboot, Zephyr, Nordic, slot addresses, fixed wrapper sizes, or any toolchain.
- **Objective verification:** Automated unit tests synthesize/patch a valid Cortex-M ELF
  companion and HEX records to prove: connected prefix-only, suffix-only, and prefix+suffix
  wrappers pass; a connected `0x00`/`0xFF` fill difference passes; a meaningful overlapping
  conflict reports `build/hex-content-conflict`; a meaningful omission reports
  `build/hex-incomplete`; and a supplemental disconnected HEX component reports the new stable
  relationship error with its first address. The A23 canonical artifact relationship is also
  reproduced without embedding its board, addresses, or external experiment files in production
  code.

<!-- Assumption: “Directly connected” means a maximal contiguous HEX data range overlaps a
file-backed ELF load range, or its half-open boundary exactly touches one. No arbitrary byte,
sector, header-size, target, or toolchain tolerance is introduced. -->

### CL-002 — Preserve policy-level authority over every programmed and erased address

- **What to change:** Keep `BuildEvidence.hex_ranges` as the exact parsed HEX data ranges and
  ensure no extractor-level relaxation bypasses either runtime policy path. Only remove the
  redundant ELF-symbol-partition rejection for a connected wrapper; do not expand, synthesize,
  or relabel the ELF-derived `flash_partition`. Preserve the current public
  `SafetyPolicyError` translation and `select_valid_build_artifact` recovery for linker evidence
  failures.
- **Where:** `src/pyocd_debug_mcp/safety/linker.py::extract_build_evidence` and regression
  coverage through `src/pyocd_debug_mcp/safety/enforce.py::SafetyPolicy.check_flash`,
  `check_generic_application_candidate`, and `_extract_runtime_evidence`. Production changes
  outside `linker.py` are permitted only if an objectively necessary error-translation detail
  cannot be achieved by the existing generic translation at `enforce.py:367-376`.
- **Exact intended behavior:** Connected wrapper bytes outside the companion ELF's symbol-derived
  partition reach the existing policy layer. The reviewed/static path still refuses any actual
  HEX byte or required erase sector outside its authorized deployment partition. The generic
  path still refuses any actual HEX byte, execution evidence, or required erase sector outside
  verified physical flash, and still derives one bounded contiguous application allocation from
  all ELF and HEX content. A relationship failure remains pre-execution and nonmutating with the
  existing artifact-selection remedy.
- **Must remain intact:** Target identity, safety-map freshness, reviewed partition authority,
  verified physical geometry, vector/entry/reset/stack checks, contiguous erase allocation,
  stale-plan checks, board/session isolation, symbol binding, actual flash execution, readback
  verification, and operator disclosure/permission behavior. Do not weaken checks by dropping HEX
  ranges from `content_ranges` or by substituting the ELF partition for actual programmed ranges.
- **Objective verification:** Policy-level regression tests use controlled loaded safety maps and
  connected wrapper fixtures to show an in-policy wrapper passes evidence validation, the same
  wrapper outside a reviewed application partition fails `safety/flash-outside-partition` (or
  the existing erase-sector equivalent), and a wrapper outside generic physical flash fails
  `safety/flash-outside-physical-device`; assertions also confirm the returned evidence preserves
  the exact HEX ranges. Existing server tests, Ruff, Pyright, and `git diff --check` remain green.

## Out of scope / must not change

- Fresh-experiment firmware, fixtures, SDK/toolchain output, experiment documents, evidence, and
  hardware state.
- Artifact-collector naming/manifest format, same-stem companion discovery, plan/permission
  semantics, flash adapters, and post-program verification.
- Recognition of any named bootloader, signing format, board, MCU, address, wrapper size, build
  system, or host environment.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, or generated artifacts.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, and adjacent behavior touched by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, or gate commands.
