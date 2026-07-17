> STATUS: REVIEW CLEAN.

# BYO Server manifest tree-digest bug review

## Verdict

Clean after one bounded fix iteration. The change corrects provenance records
and adds a destination-only regression guard; it does not alter server, board,
firmware, benchmark, packaging, or hardware behavior.

## Findings

| Severity | Gate | Location | Finding | Resolution |
| --- | --- | --- | --- | --- |
| Must fix | Reproducible provenance | `docs/extraction-manifest.json` | Three tree rows used hashes that could not be reproduced by the declared algorithm. | Corrected source/destination hashes and specified project-relative POSIX paths. |
| Must fix | Regression coverage | `tests/test_extraction_manifest.py` | No test recomputed recorded destination outcomes. | Added coverage for every file/tree row and future planned outcomes. |

No open finding remains in this defect slice.

## Verification

- Focused manifest tests: 2 passed.
- Ruff autofix/format/check: 57 files clean; formatter made no changes.
- Full Pyright JSON: 57 files, zero errors, warnings, or information diagnostics.
- Full BYO pytest: 209 passed.
- Process audit: zero surviving processes after every command.

The root `run_check_ladder.py` is not an appropriate substitute for this
standalone slice because it targets the parent project and its required
`frontier.py` helper is absent. The equivalent Ruff/Pyright/pytest gates ran
directly from `BYO-Server` with an external copy-mode environment and an empty
`PYTHONPATH`.

## Hardware hand-off

Not applicable to this provenance-only defect. No probe, serial port, pack
installation, provider, flash, recover, UART, or board discovery was used.

## Preserved behavior

All copied file contents, the ordinary 20-tool MCP contract, firmware fixtures,
board facts, benchmark semantics, and checkout-only package boundary remain
unchanged.

Review clean. Return to the Prompt 6 caller and restart the complete integration
suite from a new clean-room copy.
