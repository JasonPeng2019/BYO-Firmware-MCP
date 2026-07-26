# Main acceptance — H04 datasheet-to-part binding repair

Accepted after independent main-model review. No commit, push, deployment, flash, or hardware
action was performed by this repair loop.

## Identity

- Base server commit: `6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876`
- Main-authored plan SHA-256:
  `e3d2bad59a92dda5bd41a0cab23081e01b0397d6e74ed410ffe0f37edab5aae6`
- Persistent doer: `019f9b63-a959-7240-92ad-6d3a7ad88be5`
- Persistent spec tester: `019f9b67-6dd1-7a40-b222-90c5fb80d542`
- Persistent regression tester: `019f9b6b-eb18-7ee2-9d15-462b3b3b7464`

## Accepted behavior

- Generic setup no longer treats an arbitrary well-formed PDF as authority for a requested MCU.
- The exact PDF bytes must positively match exact server-derived part/device identity or a verified
  PDSC family/subfamily term.
- Placeholder matching is provenance-aware: exact-only terms cannot use it, while verified family
  terms may use a complete trailing `x+` token convention without digit- or vendor-specific rules.
- Correct official STM32L476 family evidence is accepted; the supplied nRF52840 document is refused
  for the same STM32 support authority.
- Verified PDSC ancestry works for direct family devices and nested variants.
- The proof is re-evaluated before capture/commit and replayed from immutable local bytes.
- Legacy support IDs remain valid only when every prior immutable authority field still agrees.

## Evidence

- Final neutral report:
  `.change-loop/fresh-suite/H04-datasheet/state/test_report.md`
  - SHA-256:
    `962ca5d92c723d74acf19acf45e373d456583578ab4d8243eca8ccabc8c7338b`
  - Spec: `16 passed, 4 subtests passed`
  - Regression: `6 passed`
- Independent real-artifact oracle:
  `.change-loop/fresh-suite/H04-datasheet/state/main_acceptance_oracle.after-rejection-004.log`
  - SHA-256:
    `abaa62172e8185190dd344d22eecef0aac46fcdf7ebe6b2e6769b98f9afdc03e`
  - Result: `MAIN_ORACLE_PASS`
- `git diff --check`: pass.
- `python -m compileall -q src`: pass.
- Focused Ruff, restricted to repair-added lines: zero diagnostics.
- Dependency-complete focused Basedpyright:
  - all H04 tests and the new datasheet module are clean;
  - the sole remaining production error is on an unchanged pre-H04 line in
    `device_support.py` and is outside this repair.

## Main rejection history resolved

1. Correct official STM32 PDF was initially refused.
2. Direct-family PDSC ancestry and exact-only placeholder provenance were incomplete.
3. Verified letter-ending families were incorrectly refused.
4. Repair-introduced focused static findings, stale prose, and duplicate ancestry parsing remained.

All four rejection artifacts remain in this runtime, and the same persistent roles resolved each
one without changing the accepted plan.

## Design-charter conclusion

The main model reread the complete charter before final acceptance. The repair is a correctness
guard against a fallible agent selecting the wrong document, not hostile-input hardening or a
paternalistic restriction. It remains generic across parts/vendors, uses verified runtime
authority rather than allowlists, introduces no arbitrary PDF resource cap, and preserves the
existing plan/permission, exact-byte, pack-leaf, live-identity, and returning-board contracts.

