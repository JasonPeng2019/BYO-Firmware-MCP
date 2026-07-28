# One-time adversarial plan review

Plan SHA-256: 92403e70ee45a7972a513d772d06c2976f1696125d62d2ebd1cc215615ca96da

- Reviewer session: `019fa2b3-a6d9-7b80-bddb-4086c08c46ef`
- Reviewer model/settings: `gpt-5.6-terra`, medium reasoning,
  `service_tier="priority"`
- Disposition: **APPROVE; no blocking correctness issue.**
- Raw reviewer evidence:
  `.change-loop/fresh-suite/S12-nrf-pack-overlap/plan-reviewer.last.md`

## Numbered risks and adversarial test targets

1. Implement precedence as an explicit trait-derived rank with deterministic metadata
   tie-breaking; input order must not affect accepted geometry or serialized output.
2. Equal-precedence partial and nested overlaps must raise `PackProvisionError` naming the memory
   kind and both half-open ranges, in either input order.
3. Exact duplicates with different names/access/flags must collapse deterministically.
4. Preserve disjoint multi-bank flash and writable-RAM rows independently; never build an
   envelope.
5. A lower-precedence overlapping descriptor is discarded whole. Nested and partial cases must
   prove no uncovered suffix becomes physical, erase, application, write, or deployment
   authority.
6. Preserve scalar default flash/RAM and programmable-flash/FLM selection. Erase sectors remain
   contained in canonical flash; unavailable erase authority stays read/debug-only.
7. Directly test that `GenericMapGeometry` still rejects overlapping flash, overlapping RAM, and
   flash/RAM cross-overlap.

## Charter assessment

No conflict found. The plan fixes the verified adapter boundary, avoids guessed or fabricated
memory, preserves strict persisted authority, uses no vendor/board/environment special case,
fails honestly on ambiguity, and excludes unrelated work.
