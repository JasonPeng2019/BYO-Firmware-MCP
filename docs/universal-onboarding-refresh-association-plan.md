# Plan: refresh completes profile/map association

Specification: `docs/universal-onboarding-refresh-association-spec.md`

1. Extend the server-owned post-refresh commit hook to persist the canonical safety reference before
   updating or clearing any live map stamp.
2. Add a focused test proving the hook fills a missing reference without changing core profile facts.
3. Run focused refresh/profile/setup checks and a Terra-medium fast adversarial audit; vet every
   result in the main agent.
4. Retry the nRF acceptance from a new empty repository with a new Luna-medium agent, preserving the
   successful agent-led pack research behavior.

