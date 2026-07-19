# Universal Onboarding Audit Closure Plan

Specification: `docs/universal-onboarding-audit-closure-spec.md`

1. Gate `continue_setup` research schemas on the workflow's current research-required state and add
   a zero-side-effect regression test.
2. Add an exact manifest-spec pack loader, refactor registry/project binding replay and geometry to
   select by persisted pack identity, and test duplicate-target coexistence.
3. Convert SVD registers into non-overlapping access-aware spans, persist distinct read-write,
   read-only, and write-only region kinds, and test the action matrix.
4. Regenerate contracts; run focused tests, Ruff, Pyright, complete pytest, wheel/import, and bounded
   stdio smoke.
5. Repeat the GPT-5.6-Terra adversarial diff audit until it returns no valid findings, then proceed
   to the whole-codebase self-audit and fresh-repository hardware acceptance.

6. Bind every generic setup/validation/reconnect open to its replayed exact pack path and digest;
   reject project identity-proof edits and duplicate matching PDSC entries.
7. Add focused regressions and repeat the full software and Terra audit gates.

## Terra audit closure amendment plan (2026-07-19)

1. Add exact-authority replay in `setup_flow/device_support.py`; replace part-only replay at profile verification, connection, refresh/map derivation, validation, and allocation staging. Cover the same-part/multiple-pack saved-profile case.
2. Thread `pdsc_device` through the SWD abstraction and every server-owned pack-backed open. Scope pyOCD target registration to exactly that leaf and test normalized-name collision handling.
3. Introduce schema-v3 multi-region geometry while keeping schema-v2 geometry unchanged. Derive physical region unions from PDSC regions, validate regions one-for-one, and make generic containment use union coverage while erase authority remains the explicit proven sector set.
4. Reject generic artifacts whose entry/vector/reset evidence exits the content-derived allocation before staging, permissions, budget consumption, or backend mutation.
5. Run focused tests, Ruff, Pyright, full pytest, package/import, stdio smoke, then repeat exact GPT-5.6-Terra diff review until clean.
