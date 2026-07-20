# Plan: Universal Onboarding Project Authority Reuse

Specification: `docs/universal-onboarding-project-authority-reuse-spec.md`

1. Centralize server-side map-authority verification so generic maps use
   `resolve_persisted_pack_support` with the active project store and the map's exact authority
   document, while reviewed maps keep their current verifier.
2. Use that helper in setup-overview routing and the existing validation/currentness paths without
   changing map derivation or deployment authority.
3. Extend the fresh-root generic onboarding integration test: start with a profile lacking
   `safety_ref`, invoke a real `SafetyRefresher` wired to the production derive and post-commit
   callbacks, assert the canonical reference, and prove real `setup_overview` routes to validation.
4. Run focused pytest, Ruff, Pyright, and a Terra-medium fast adversarial diff audit. Main reviews
   all output before any further change.
5. Retry both fresh-device acceptance paths as required, with particular attention to no-research
   reuse routing.

