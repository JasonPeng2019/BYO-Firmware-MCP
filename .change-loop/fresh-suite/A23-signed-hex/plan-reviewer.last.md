SHA independently reviewed: `112f77f7d2e524bd540f5a9cd1779fa429bda1ecb7ca7fc2c1958f1470a1b42c`

Session/thread: `/root`

**APPROVE**

The plan is narrowly scoped and preserves the essential mistake guards and runtime authority. It defines direct connection deterministically as overlap or exact half-open boundary contact—no arbitrary tolerance—and supports the observed prefix/suffix case. It retains meaningful-byte completeness/conflict checks, allows only the exact unordered `0x00`/`0xFF` mismatch pair, and sends all exact parsed HEX ranges through both reviewed-map and generic-geometry/erase enforcement paths.

1. Implementation risk: connectedness must operate on sorted maximal contiguous HEX data ranges, not Intel-HEX record boundaries. Test prefix-only, suffix-only, bridging data, and a one-byte gap rejection.
2. Implementation risk: preserve the exact `hex_ranges` in evidence; do not merge, clip, or substitute the ELF partition. Test reviewed-partition and generic-physical-flash refusals for connected wrapper bytes.
3. Implementation risk: fill equivalence must be symmetric and exclusive: `00↔FF` passes; `00↔01` and `FF↔01` conflict. Meaningful ELF-byte omission must remain `build/hex-incomplete`.
4. Implementation risk: disconnected supplemental content must fail before mutation with a stable error identifying its first address and retaining the existing artifact-selection remedy.
5. Existing guards remain correctly placed: target identity, vector/entry/stack validation, reviewed/static partition checks, physical flash checks, erase-sector checks, stale-plan handling, and readback verification are unaffected.

No charter conflict found. The plan introduces no vendor/bootloader specialization, no guessed memory authority, and no bypass of runtime safety-map or generic physical-geometry enforcement.