No source change this iteration.

The neutral failure was environmental: default `.venv` lacked `pytest`. Its stale `lib64` reparse point prevents `uv sync --locked` from rebuilding that environment safely.

Using the existing locked `.h01-venv` refreshed to the workspace source:

- `test_h01_plan_text_preservation.py` — 3 passed, 36 subtests passed
- `test_h01_plan_text_regressions.py` — 3 passed
- `git diff --check` — passed
- H00 files retain their reviewed hashes

Unresolved: the neutral default `.venv` remains unusable until its inaccessible `lib64` reparse point is repaired externally; I did not delete it because its target could not be safely resolved.

Design-charter checkpoints completed: before analysis, before verification, and before final verdict.
