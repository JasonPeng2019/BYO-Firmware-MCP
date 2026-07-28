# Verified S12 production defect: overlapping CMSIS-Pack flash descriptors block nRF setup

## Independent main-model validation

Fresh S12 run `fresh-experiments/S12_20260726-042610` reproduced the failure on both user-owned
nRF52840 DKs after correcting the Codex MCP launcher to an isolated run-local artifact root.
`fresh_test.py verify` reports `VALID SERVER_FAILURE`; corrected RESULT SHA-256 is
`4dff0d3cd423d9032d628fbf631bd76691b35820b2802c85b3c4c5333b7b0045`.

The exact official Nordic pack resolves the requested `nRF52840-QIAA` to
`nrf52840_xxaa`. Its verified PDSC leaf produces these pyOCD flash records:

- `IROM1`: `0x00000000..0x00100000`, default, boot, testable;
- `nRF52xxx`: `0x00000000..0x00200000`, non-default, non-boot, non-testable.

Production `resolve_registered_pack_geometry()` in
`src/pyocd_debug_mcp/setup_flow/device_support.py` exact-deduplicates ranges but retains both
overlapping records as `flash_regions`. `_derive_generic_safety_map()` passes both to
`GenericMapGeometry`, whose correct persisted-authority invariant rejects overlapping physical
flash with `generic physical flash regions must not overlap`. Public setup then cannot create a
current safety map; `board_safety_refresh` reports `reviewed_evidence_unavailable` and
`board_validate` reports `validation/safety-missing`.

This is a production parser/canonicalization defect, not a board, fixture, datasheet, SDK,
launcher, or stale-state problem.

## Required repair

Canonicalize overlapping verified PDSC physical memory descriptors before constructing generic
map geometry. The behavior must:

1. remain generic—no Nordic, nRF52840, board, OS, path, or toolchain special case;
2. prefer the PDSC's unique default/boot/testable physical region over an overlapping
   non-default/non-boot/non-testable algorithm/container descriptor;
3. preserve disjoint real flash/RAM banks and distinct non-overlapping physical regions;
4. avoid fabricating the non-overlapping remainder of a broader descriptor when authority does
   not establish it as a separate physical bank;
5. fail honestly and actionably when overlapping candidates remain genuinely ambiguous;
6. keep `GenericMapGeometry`'s strict overlap rejection for persisted/untrusted-to-correctness
   documents; fix the upstream verified-pack adapter rather than weakening the authority schema;
7. preserve existing target resolution, erase-driver proof, peripheral/SVD handling, pack replay,
   map validation, and all public tool contracts; and
8. add focused automated tests for the Nordic-shaped overlap, ambiguous overlaps, exact
   duplicates, and disjoint multi-bank geometry, plus adjacent regression coverage.

Follow `.codex/design_charter.md` at every required checkpoint. Choose the simplest deterministic
general algorithm that does not guess or silently fabricate memory.
