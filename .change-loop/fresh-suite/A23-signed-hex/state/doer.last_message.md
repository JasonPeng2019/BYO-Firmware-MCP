No production source changed in iteration 3.

Verified:

- Neutral spec command: 13 passed, 8 subtests passed.
- Intended Windows-shell regression equivalent: 13 passed, 8 subtests passed.
- Ruff, Pyright, and `git diff --check`: pass.

Remaining neutral-gate evidence is harness-side:

- Its Bash runner misparses protected Windows command `.\.venv\Scripts\python.exe`.
- It flags the untracked tester-owned `tests/test_a23_signed_hex_spec.py` as doer-modified. I did not alter it.

I preserved all protected tests and gate controls. Charter checkpoints recorded: pre-analysis, pre-verification, final verdict.
