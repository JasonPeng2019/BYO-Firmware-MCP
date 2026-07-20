# Plan: Universal Onboarding Memory/SVD Overlay Normalization

Specification: `docs/universal-onboarding-memory-overlay-spec.md`

1. Add a small address-overlap predicate in `setup_flow/device_support.py` and normalize SVD
   peripheral rows against the already-derived canonical flash, RAM, and ROM rows.
2. Return the canonical memory rows and only non-overlapping SVD rows in `PackMemoryGeometry`; keep
   all existing exact-pack and geometry derivation unchanged.
3. Add focused unit coverage using a synthetic verified device whose SVD contains one register in
   flash and one ordinary peripheral register. Assert that only the ordinary peripheral survives
   and retains its access.
4. Add an integration-level generic-map assertion that the normalized geometry produces no region
   conflict while retaining physical flash authority.
5. Run the focused tests, Ruff, and Pyright through a Terra-medium fast test agent. Main reviews the
   result before any follow-up change.
6. Run a Terra adversarial diff audit. Main reviews every criticism, fixes valid findings, and
   repeats until clean.
7. Retry the nRF52840 journey from a brand-new repository with Luna-medium and prove ready-state
   reuse without importing prior project authority.

