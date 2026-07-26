# Main acceptance review — REJECTED after neutral iteration 2

The neutral gate is green, but the repair is not accepted. The current tests use synthetic
authority terms and do not exercise the actual official H04 positive-control pair required by
CL-001/CL-002.

## Independently reproduced failure

- Correct official PDF:
  `../stm32L476rgt.pdf`
- PDF SHA-256:
  `a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65`
- Exact requested part:
  `STM32L476RGT6`
- Exact verified PDSC leaf:
  `STM32L476RGTx`
- Official pack:
  `Keil.STM32L4xx_DFP.3.1.0.pack`
- Pack SHA-256:
  `5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f`

Calling the current production `prove_datasheet_applicability` against the actual correct PDF
with the only terms currently exposed by `DeviceSupportCandidate.datasheet_identity_terms()`,
`("STM32L476RGT6", "STM32L476RGTx")`, produced:

```text
stm32L476rgt.pdf REFUSED DatasheetApplicabilityError datasheet applicability to requested MCU STM32L476RGT6 was not established; provide verifiable official datasheet evidence
```

The same invocation correctly refused the nRF52840 PDF.

Independent read-only inspection of the official PDSC exact-leaf ancestry found:

```text
device Dname="STM32L476RGTx"
subFamily DsubFamily="STM32L476"
family Dfamily="STM32L4 Series" Dvendor="STMicroelectronics:13"
```

Independent PDF text inspection found the official family spelling `STM32L476xx` and concrete
variants including `STM32L476RG`. This inspection used host `pdftotext` only as a main-review
oracle; production must continue to use the maintained in-process parser required by CL-001.

## Why the implementation misses the unchanged plan

CL-001 explicitly requires family/subfamily identity terms derived from the exact verified PDSC
support record so an official family datasheet can establish applicability. The implementation
currently carries only the exact requested ordering code and exact wildcard leaf. It therefore
rejects the correct official H04 family datasheet and would break the required positive setup path.
This is an implementation/test-coverage shortfall, not a plan mistake; no plan amendment is needed.

## Required repair and tests

1. Derive the applicable family/subfamily identity from the exact matched PDSC device ancestry in
   the already digest-verified pack bytes. Do not infer it from caller text, filenames, vendor
   tables, or hardcoded part rules.
2. Bind those server-derived terms into the candidate/support identity and durable replay boundary
   so changed pack authority or changed terms cannot replay as the same support.
3. Support the ordinary official family-placeholder spelling needed by the PDSC subfamily
   (`STM32L476` versus document token `STM32L476xx`) without restoring arbitrary prefix matching.
   A longer concrete SKU such as `ACME-Z9-1440` must still not satisfy `ACME-Z9-144`.
4. Add tester-owned coverage using the actual supplied correct STM32 PDF and verified official-pack
   family/subfamily metadata, plus the actual nRF wrong-family control. Synthetic-only terms are
   insufficient.
5. Preserve every existing exact-pack-leaf, stale-byte, replay, catalog, permission, live-identity,
   cache, and earlier H00–H04 contract.

Doer/tester prose is not acceptance. The same persistent roles must run again sequentially, the
neutral gate must be green, and the main model must rerun the real official-artifact oracle.
