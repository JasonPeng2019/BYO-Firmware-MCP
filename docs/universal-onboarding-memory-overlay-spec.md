# Universal Onboarding: Memory/SVD Overlay Normalization

## Problem

A verified CMSIS-Pack can describe one address twice for two different purposes. The PDSC/pyOCD
memory map describes the physical storage type and erase geometry, while the SVD names fields or
registers located in that storage. Nordic's UICR is one real example: it is a separately erasable
flash region and is also represented as an SVD register block. Treating both descriptions as
independent safety-region kinds makes a valid pack conflict with itself and leaves a freshly set up
board permanently not ready.

This is a general data-model issue, not a Nordic exception. Configuration flash, option bytes,
OTP, EEPROM windows, aliases, and vendor-specific nonvolatile control blocks can have the same
shape on other devices.

## Required behavior

1. Exact verified pack bytes and the selected PDSC device remain the sole source of generic memory
   geometry. No board name, vendor name, address, or peripheral name may be hardcoded.
2. PDSC/pyOCD memory-map classification has precedence over SVD register classification when their
   half-open address ranges overlap. A location declared as flash, writable RAM, or ROM remains that
   memory kind even if the SVD also gives it a register name.
3. SVD rows that overlap any verified flash, RAM, or ROM region are omitted from the generic
   peripheral-region set. Omission is whole-row: a register must never be split into partial
   addresses.
4. Non-overlapping SVD peripheral rows retain their resolved read/write access and remain available
   as peripheral authority.
5. The normalization must happen while deriving `PackMemoryGeometry`, before the safety map is
   built. Conflict detection itself remains strict so genuinely incompatible independent sources
   are still reported rather than silently accepted.
6. Refresh of an already persisted generic profile deterministically derives the normalized map;
   repeated refreshes are idempotent and the profile's canonical safety reference remains valid.
7. The fix must not widen application or bootloader deployment authority. Artifact-defined flash
   allocation, exact-sector containment, pack digest replay, and live identity requirements remain
   unchanged.
8. Tests must prove both halves of the rule: an overlapping SVD register is excluded, while an
   ordinary non-overlapping peripheral register is preserved with its access mode.

## Assumptions

- The memory map describes physical storage semantics; the SVD describes programmer-facing register
  metadata. When they overlap, preserving one physical classification is the least ambiguous and
  most portable representation.
- Raw reads of such locations may still use mapped-memory tools. Register-write plans must not gain
  authority merely because an SVD labels storage as a register.

## Non-goals

- No vendor-specific prohibited-address catalog is introduced.
- No pack content is rewritten.
- No security-sensitive write is automatically authorized.
- No caller-supplied range becomes authority.

