# Reviewed peripheral read coverage — specification

## Observed defect

A fresh, validated nRF52840 project could debug RAM and Cortex-M state but could not read the
running UARTE registers at `0x40002000`. The reviewed safety map described only the small writable
GPIO register window. `read_memory_address` therefore classified every other volatile peripheral
as unknown, recommended `board_safety_refresh`, and refresh deterministically recreated the same
incomplete map.

This blocks ordinary firmware diagnosis and is inconsistent with generic pack-backed onboarding,
which already imports access-aware SVD peripheral rows.

## Required behavior

1. A reviewed target's map must describe the volatile peripheral address spaces documented by both
   its pinned device-support evidence and pinned official datasheet evidence.
2. Broad peripheral-space coverage is read-only. Existing narrowly reviewed writable windows remain
   writable; broad coverage must not widen register-write authority.
3. Nonvolatile, access-control, option/configuration, and other prohibited windows remain prohibited
   and must split otherwise contiguous peripheral space rather than overlap it.
4. The nRF52840 reviewed map must therefore permit a read of UARTE0 registers, continue to permit the
   existing GPIO write window, and continue to prohibit NVMC/ACL writes and reads.
5. Refresh must deterministically rebuild the expanded map so its advertised refresh remedy becomes
   effective after the server is updated.
6. The design remains vendor-neutral: unknown devices continue through the existing exact
   pack/PDSC/SVD path; this change removes a legacy reviewed-profile capability gap rather than
   adding a new board-selection branch.

## Assumption

The product specification's Arm system map and peripheral instantiation table are authoritative for
read-only peripheral-space classification. Read-only inspection is the least-authority capability
needed for normal live diagnosis; writes retain the narrower existing policy.

## Acceptance

- Reviewed evidence reconciliation includes split read-only APB/AHB peripheral spans without
  conflicts.
- `0x40002200` (UARTE0 SHORTS) is readable.
- `0x4001E504` (NVMC/ACL) remains prohibited.
- `0x50000504` remains writable as reviewed GPIO.
- Focused tests, full pytest, Ruff, and Pyright are green.

