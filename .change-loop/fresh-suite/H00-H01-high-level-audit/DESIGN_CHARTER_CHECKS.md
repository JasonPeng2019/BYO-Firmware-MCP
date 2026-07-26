# Design-charter checkpoints

Governing file: `../.codex/design_charter.md` from the MCP-Trial-3 root.

## Current high-level main model

1. **Before high-level validation — PASS.** Reread the complete charter before
   classifying any H00/H01 plan or diff. Applied correctness, simplicity,
   generalizability, neatness, and the prohibition on unrelated edits.
2. **Between verified request and plan — PASS.** Reread the complete charter
   before authoring the repair plan. Restored the existing fail-closed identity
   abstraction instead of preserving a mistaken lower-level plan, rejected
   unlocked tool resolution, and converted a 60-second arbitrary test retry
   into a short truthful teardown bound.
3. **Independent high-level plan review — PASS.** The `gpt-5.6-sol`
   read-only reviewer reread the complete charter and passed exact plan SHA-256
   `8bd4a8516ead395712df8f4db2287efecfdc73eed8dd66a249e1095b0634f1ad`
   with seven recorded execution risks/test targets.
4. **Immediately before implementation deployment — PASS.** Reread the
   complete charter before starting the persistent Terra roles. Confirmed the
   exact plan/review hash, sequential role order, isolated runtime, no
   concurrent change-loop process, no hardware authorization, and the narrow
   production/test ownership boundary.

## Required role checks

The doer, spec tester, and regression tester must each reread the complete
charter:

- before first analysis;
- immediately before editing;
- between distinct source/test features;
- before verification; and
- before final verdict.

Each role must include its checkpoint attestations in its final message. The
main model will add its pre-implementation-review, between-feature,
pre-verification, and final-acceptance checks here while supervising the loop.
