# Validation pack-support plan

Specification: `docs/validation-pack-support-spec.md`

1. Add focused server tests for built-in, verified-pack, missing-pack, and invalid-pack validation
   support decisions.
2. Make the validation support hook use pyOCD's built-in registry only for built-ins and the
   existing `verified_pack_for_target` selector for every non-built-in target.
3. Run focused tests, Ruff, Pyright, the locked full suite, and hostile diff review.
4. Retry the blocked hardware phase from a new empty repository and new server run.
