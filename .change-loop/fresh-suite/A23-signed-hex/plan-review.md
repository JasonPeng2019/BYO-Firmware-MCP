# One-time adversarial plan review

Plan SHA-256: 112f77f7d2e524bd540f5a9cd1779fa429bda1ecb7ca7fc2c1958f1470a1b42c

- Reviewer session: `019fa631-007c-7cb3-a930-6ea12d7201a5`
- Reviewer model/settings: `gpt-5.6-terra`, medium reasoning,
  `service_tier="priority"`
- Prompt-policy SHA-256:
  `fcb25396d58af7ee6e7ffc931142b830e8a1b28ea3e5c197a1ca1e3d6248aa68`
- Disposition: **APPROVE; no blocking correctness issue.**
- Raw reviewer evidence:
  `.change-loop/fresh-suite/A23-signed-hex/plan-reviewer.last.md`

## Numbered risks and adversarial test targets

1. Connectedness must operate on sorted maximal contiguous HEX data ranges, not Intel HEX record
   boundaries. Test prefix-only, suffix-only, bridging data, and a one-byte-gap rejection.
2. Preserve exact `hex_ranges` in evidence; never merge, clip, or substitute the ELF partition.
   Test reviewed-partition and generic-physical-flash refusals for connected wrapper bytes.
3. Fill equivalence must be symmetric and exclusive: `0x00` versus `0xFF` passes, while
   `0x00`/`0x01` and `0xFF`/`0x01` conflict. Meaningful ELF-byte omission remains
   `build/hex-incomplete`.
4. Disconnected supplemental content must fail before mutation with a stable error identifying
   its first address and retaining the existing artifact-selection remedy.
5. Preserve target identity, vector/entry/stack validation, reviewed/static partition checks,
   physical-flash checks, erase-sector checks, stale-plan handling, and readback verification.

## Charter assessment

No conflict found. The plan introduces no vendor/bootloader specialization, no guessed memory
authority, and no bypass of runtime safety-map or generic physical-geometry enforcement. Its
overlap-or-exact-boundary rule has no arbitrary tolerance and preserves the fallible-agent
mistake guards while unblocking the verified ordinary signed-image case.
