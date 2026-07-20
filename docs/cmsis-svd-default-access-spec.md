# CMSIS-SVD Default Register Access Specification

## Problem

Fresh generic setup correctly verified an exact CMSIS-Pack for STM32L476RGT6 and
derived its safety map from the pack's PDSC, target, and SVD. The resulting map
nevertheless omitted valid RCC registers whose `<access>` element was absent.
The live acceptance run then refused a read of RCC.CFGR at `0x40021008` as
unknown, although that exact register is present in the verified SVD.

CMSIS-SVD defines register properties through inheritance and specifies
read-write as the default when no access value is defined at the register,
peripheral, or device level. Treating an unresolved parser value as inaccessible
therefore discards valid pack authority and makes generic behavior depend on
whether a vendor redundantly spells out the schema default.

## Required behavior

1. Register access resolution follows CMSIS-SVD semantics:
   - use the register's explicit access when present;
   - otherwise use the peripheral's inherited access when present;
   - otherwise use the device's inherited access when present;
   - otherwise use the CMSIS-SVD default `read-write`.
2. The same default applies to an address-block fallback when a peripheral has
   no register list and neither the peripheral nor device supplies access.
3. Explicit `read-only` and `write-only` declarations remain authoritative.
4. Existing overlap normalization remains unchanged: aliases combine to the
   most restrictive access supported by every covering description, and SVD
   rows overlapping authoritative flash/RAM/ROM remain excluded.
5. Malformed SVD, invalid widths/addresses, and prohibited/system ranges keep
   their existing fail-closed behavior. This change does not turn arbitrary
   peripheral address space into authority; it restores only addresses that the
   exact verified SVD actually describes.
   Address-block fallback applies only when usage is explicitly `registers`;
   `buffer`, `reserved`, missing, and malformed usages remain excluded because
   register access inheritance does not describe their access semantics.
6. The implementation is vendor- and device-neutral. It must not mention or
   special-case STM32, RCC, or any fixed address.
7. The generic map-generator source digest changes so previously generated maps
   are reported stale and normal safety refresh reconstructs them with the
   corrected SVD semantics.

## Acceptance

- A register with no access at any level is emitted as `read-write`.
- Register-, peripheral-, and device-level explicit access still resolve in
  that precedence order.
- A no-register address block with no access is emitted as `read-write`.
- Explicit read-only/write-only behavior and alias intersection remain covered.
- A fixture matching the observed omission includes an access-less register at
  an offset and proves the exact address becomes mapped.
- Focused tests, Ruff, Pyright, and the full locked pytest suite pass.
