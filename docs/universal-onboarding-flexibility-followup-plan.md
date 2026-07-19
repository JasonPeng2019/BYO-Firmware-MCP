# Universal Onboarding Flexibility Follow-up Plan

Specification: `docs/universal-onboarding-flexibility-followup-spec.md`

Authority decision: `decisions/ADR-0004-artifact-defined-generic-application-authority.md`

## Slice 1 - General pack map evidence

1. Extend the resolved pack geometry record with separate flash, RAM, ROM, and SVD peripheral
   regions while retaining one selected application-programming flash and its exact erase sectors.
2. Parse SVD bytes from the already bounded verified pack. Reject XML entities and malformed ranges;
   treat missing/malformed SVD as capability absence, not setup failure.
3. Emit all evidence-backed regions in schema-v3 maps without joining disjoint ranges. Keep the
   primary geometry fields for application-flash compatibility and permit additional physical/read
   regions in strict schema validation.
4. Add synthetic multi-region and SVD tests.

## Slice 2 - Usable generic application authority

1. Replace blank-only `fresh_device_allocation` with a closed `artifact_application_allocation`
   policy containing exact sectors, bounded-driver proof, creation map/artifact digests, optional
   parent-allocation digest, and its canonical allocation digest.
2. Derive a first allocation or monotonic expansion from the artifact's required sectors. Never read
   the whole device and never accept caller ranges.
3. Stage all checks during guarded plan validation, compare-and-swap the map immediately before the
   backend transaction, and retain the allocation if programming fails.
4. Permit exact or compatible live identity for `flash_application`; retain exact/separate policy
   requirements for bootloader and recovery paths.
5. Update status, public prompts, docs, and tests to describe the actual capability.

## Slice 3 - Verification and audits

1. Run affected setup, map, safety, flash, gate, contract, and end-to-end suites.
2. Regenerate contract hashes and plan-tool documentation, then run Ruff, Pyright, complete locked
   pytest, package/import, and bounded stdio smoke.
3. Self-audit the whole codebase against the universal objective. If a remaining general-case gap is
   found, write another focused spec/plan before implementing it.
4. Run GPT-5.6-Terra adversarial diff reviews and fix every valid finding until a full pass is clean.
5. Run fresh-repository GPT-5.6-Luna acceptance for both attached boards using only board name, exact
   part, datasheet, and live inventory; verify every tool capability-appropriately and perform no
   destructive action without a valid guarded plan and permission.
