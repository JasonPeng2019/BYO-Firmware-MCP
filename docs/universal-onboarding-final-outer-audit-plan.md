# Plan: Universal Onboarding Final Outer-Audit Closure

Specification: `docs/universal-onboarding-final-outer-audit-spec.md`

1. Correct the stale `resolve_registered_pack_geometry` documentation while leaving its tested
   behavior unchanged.
2. Recompute the affected source contract hash and keep the contract change note truthful.
3. Make overview use the same semantic-currentness verifier as runtime safety enforcement, and add
   a fresh generic integration assertion that a structurally valid stale map routes to refresh.
4. Add concise current-tree evidence for the two fresh Luna-medium journeys and their closed
   failure loops; link it from `docs/verification.md`.
5. Have a Terra-medium fast test agent run the project-defined complete verification sequence,
   including an isolated built-wheel import and bounded real stdio initialize/list/shutdown.
6. Main reviews every result. Fix only validated defects, rerun the failed check, then repeat the
   complete suite as required.
7. Have a fresh Terra-medium fast adversarial agent audit the final diff. Main reviews every
   criticism and repeats until no valid criticism remains.
8. Main repeats the whole-codebase oversight audit, `git diff --check`, and status review.
