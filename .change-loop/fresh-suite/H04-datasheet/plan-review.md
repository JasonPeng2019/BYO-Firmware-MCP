# One-time adversarial plan review

- Reviewer identity: `/root/h04_datasheet_plan_adversarial`
- Reviewer model: `gpt-5.6-terra`, medium reasoning
- Reviewed plan SHA-256:
  `e3d2bad59a92dda5bd41a0cab23081e01b0397d6e74ed410ffe0f37edab5aae6`
Plan SHA-256: e3d2bad59a92dda5bd41a0cab23081e01b0397d6e74ed410ffe0f37edab5aae6
- Verdict: `PROCEED`
- Review count: one; this file does not replan or alter the reviewed plan.

## Numbered execution risks and test targets

1. Bind parser-extracted applicability evidence and persisted/captured PDF digest to the same byte
   snapshot. Reject any change between proof, capture, and commit; explicitly test the TOCTOU
   boundary.
2. Derive family/subfamily terms solely from exact verified PDSC support metadata. Built-in targets
   without independent family metadata require exact normalized-part evidence; do not introduce
   vendor/part prefix heuristics.
3. Cover parser-unavailable, malformed, encrypted/unreadable, and textless PDFs with typed,
   actionable errors and no fabricated match. Any parser dependency must be direct and
   lockfile-pinned.
4. Validate profile replay against captured bytes, requested part, and replayed support authority
   entirely offline. Do not grandfather a legacy profile that cannot prove the association.
5. Prove wrong-family refusal leaves no promoted profile/datasheet authority. Preserve
   correct-family/exact-leaf success, catalog-backed behavior, stale-byte rejection, near-part
   refusal, and accepted H00/H01/H03/H04 behavior.

## Mandatory design-charter checkpoints for every change-loop role

The doer, spec tester, and regression tester must each read `../.codex/design_charter.md` and
`.change-loop/fresh-suite/H04-datasheet/DESIGN_CHARTER_CHECKS.md`:

1. at the start of every role turn;
2. before the role's first edit;
3. between distinct CL items or test-feature groups;
4. before running its final verification; and
5. immediately before returning.

The doer must additionally reread them after any risky production diff. Each role appends a dated
acknowledgement to `DESIGN_CHARTER_CHECKS.md` naming the contemplated/current diff or test surface,
how it preserves correctness, simplicity, generalizability, neatness/usability/dynamism, the
trusted-but-fallible boundary, rejected environment/board/vendor-specific alternatives, and scope
exclusions. A vague or missing acknowledgement is not acceptance, even if its model prose says the
work passed.
