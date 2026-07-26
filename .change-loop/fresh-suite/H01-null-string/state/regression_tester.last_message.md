Regression environment restored and both focused suites pass:

- Spec: 3 passed, 36 subtests
- Regression: 3 passed

Recorded command remains executable:

`uv run --locked --no-sync pytest -q tests/test_h01_plan_text_regressions.py`

Covered edges: generated-plan object decoding, nested literal text preservation, strict schemas/unknown fields, and non-plan SDK pre-parser isolation.

Residual risk: full repository gate was not run. Removed only the broken generated `.venv/lib64` reparse entry to restore the locked environment.

Design-charter checkpoints: before analysis, before verification, and before final verdict. No test edits were needed this iteration.
