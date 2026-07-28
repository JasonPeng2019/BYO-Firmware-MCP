Strengthened the manifested regression suite to use the real `resolve_registered_pack_geometry` path with an offline registered-pack fixture.

It now covers:

- Candidate/manifest/archive replay through the production resolver.
- Stable scalar default flash/RAM selection.
- Deterministic replay with reversed parsed-region order and equal geometry digest.
- Conservative overlap discard plus preservation of disjoint flash/RAM banks.
- Generic-map consumption with no fabricated gap, erase, application, or deployment authority.

Recorded portable command remains:

```text
.venv/Scripts/python.exe -m pytest -q tests/test_regression_s12_nrf_pack_overlap.py
```

Verification passed: pytest in PowerShell and Bash, Ruff, and diff check.

Charter checkpoints logged: pre-analysis, pre-edit, pre-verification, and pre-verdict.
