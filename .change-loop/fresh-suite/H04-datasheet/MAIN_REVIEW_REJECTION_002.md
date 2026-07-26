# Main acceptance review — REJECTED after neutral resumed iteration 1

The neutral gate and real H04 artifact controls are green, but two adjacent requirements in the
unchanged plan remain violated. Both are independently reproducible host-only production defects.

## 1. Valid PDSC device directly under `family` is rejected

The new `_pdsc_ancestry_terms` implementation only considers a `device` beneath a `subFamily`.
CMSIS-Pack metadata can place a `device` directly beneath `family`; the exact verified support
path worked before this repair and must remain general.

A minimal verified-pack/PDSC reproducer with:

```xml
<family Dfamily="ACME Q Series">
  <device Dname="ACMEQ123x"/>
</family>
```

produced:

```text
PackProvisionError: exact verified PDSC leaf has no family/subfamily ancestry
```

The derivation must locate the exact matched leaf generically, walk its actual PDSC ancestors, and
return the `Dfamily` plus `DsubFamily` when one exists. It must also handle an exact variant leaf
through its actual ancestor chain rather than assuming one fixed XML nesting shape. No
vendor/part-specific exception is permitted.

## 2. Family-placeholder matching is incorrectly available to exact-only authority

The plan explicitly says a built-in target with no independently verified PDSC family metadata
must obtain an exact normalized part match. The current `_contains_identity_term` applies its
trailing-`xx` rule to every term, including the exact requested part.

Independent production invocation:

```text
_contains_identity_term("ACME9xx", "ACME9") -> True
```

Therefore a built-in exact-only candidate can gain applicability from a family-placeholder-like
prefix that was never established as family authority. This is the same provenance distinction the
main acceptance review required; the string tuple still loses it.

Represent or pass identity-term provenance explicitly enough that:

- exact requested/leaf terms require an exact token or exact token sequence;
- only verified PDSC family/subfamily terms may use the conventional trailing-`x` placeholder
  spelling;
- `ACME9xx` does not satisfy an exact-only `ACME9`;
- a verified family term `ACME9` may satisfy `ACME9xx`;
- longer concrete SKUs still do not match.

## Required continuation

This is implementation/test-coverage incompleteness under CL-001/CL-002/CL-003, not a plan mistake.
Do not amend or re-review the plan. Resume the same persistent doer, spec tester, and regression
tester sequentially. Add generic synthetic coverage for direct-family and variant ancestry plus
the exact-only versus verified-family placeholder distinction. Preserve the real H04 positive and
wrong-family controls and every prior contract. Acceptance again requires a green neutral gate and
main-model direct verification.
