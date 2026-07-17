> STATUS: COMPLETE AND REVIEW CLEAN UNDER PROMPT 6 DEFECT AUTHORIZATION.

# BYO Server manifest tree-digest bug

## Reproduction

Recompute each directory row in `docs/extraction-manifest.json` using its
declared record format: sorted project-relative POSIX path, NUL, lowercase file
SHA-256, and newline. The recorded `firmware/`, `packs/`, and `tests/cases/`
digests do not match that calculation, even though their individual files and
source/destination copies match.

## Root cause

The S3/S4 tree hashes were recorded with an inconsistent, undocumented path
representation. The manifest described a portable deterministic algorithm but
did not have a regression guard that recomputed destination tree outcomes.

## Scope

- Correct only the affected source/destination tree digest records.
- Make the path representation explicitly project-relative POSIX text.
- Record the supplemental pack document as excluded from the three-file split
  tree because it already has its own manifest row.
- Add a standalone test covering every recorded destination file/tree outcome
  and any future planned row outcome.
- Add an S6D1 defect slice to the extraction ledger and review the fix before
  restarting the complete S6 suite.

No server, board, firmware, benchmark, packaging, or hardware behavior changes
are in scope.

## Acceptance

- The original failing digest reproduction passes for every directory row.
- Every manifest destination path exists and matches its recorded SHA-256.
- Ruff, full Pyright JSON, and full BYO pytest pass with the parent package
  unavailable.
- The complete Prompt 6 integration matrix restarts after the fix.

## Verified

- The portable digest reproduction now passes for all five directory rows.
- The manifest guard validates 80 recorded destination outcomes and accepts no
  incomplete future planned outcome.
- Ruff checked/formatted 57 Python files without changes, full Pyright analyzed
  57 files with zero diagnostics, and all 209 BYO tests passed.
- Every validation subprocess left zero new processes.

## Pending verification

The complete S6 restart, hardware, live provider, cross-host, and extraction
governance/review slices remain pending.
