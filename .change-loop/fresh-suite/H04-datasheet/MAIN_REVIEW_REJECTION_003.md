# Main acceptance review — REJECTED after second resumed neutral gate

The neutral gate is green, and the two prior defects are fixed. One leftover context-free
restriction still violates the plan and design charter's arbitrary-hardware requirement.

## Verified family placeholders incorrectly require a digit-ending family term

`_contains_identity_term` now correctly receives explicit provenance, but it still permits the
trailing-`x` document convention only when `collapsed[-1:].isdigit()`. That condition was a
temporary safeguard before provenance existed; it is not a CMSIS-Pack, PDF, protocol, or hardware
constraint.

Independent production results:

```text
verified family term "LPC55S" against "LPC55Sxx" -> False
verified family term "ACMEQ" against "ACMEQxx" -> False
exact-only term "ACMEQ" against "ACMEQxx" -> False
```

The first two are false refusals. Family names and subfamily names can legitimately end in a
letter. Once a term is explicitly marked as verified PDSC family/subfamily authority, a trailing
one-or-more-`x` placeholder is the complete narrowly scoped rule. Exact-only terms must continue to
refuse it, and arbitrary concrete suffixes must continue to fail.

Remove the digit-ending assumption and add generic tests proving:

- verified family `LPC55S` (or another letter-ending synthetic family) matches `LPC55Sxx`;
- exact-only `LPC55S` does not;
- `LPC55S0` or another concrete longer token does not;
- existing digit-ending H04 and real official controls remain green.

This is implementation/test-coverage incompleteness under the existing CL-001 plan and charter,
not a plan mistake. Do not amend or re-review the plan. Resume the same persistent roles
sequentially and rerun the neutral gate.
