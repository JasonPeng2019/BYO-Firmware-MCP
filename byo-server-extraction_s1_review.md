> STATUS: S1 REVIEW - PASS WITH REPOSITORY BASELINE LIMITATIONS.

# BYO Server S1 Scaffold Review

## Scope and result

This review covers only the explicitly authorized S1 scaffold/provenance slice.
The project skeleton is present, portable, and contains no copied Python
behavior, board profiles, firmware, packs, tests, or harnesses. S1 is complete.
D0 and P1-P3 remain mandatory before S2.

## Evidence

- `pyproject.toml` and `docs/extraction-manifest.json` parse successfully.
- Every S1 destination hash recorded in the manifest matches the file on disk.
- The tree contains no `.py` files, symlinks, virtual environment, lockfile,
  runtime output, distribution output, or cache after cleanup.
- The project metadata has no direct Rich or `prompt_toolkit` dependency and no
  turnkey entrypoint. The provisional `pyocd-debug-mcp` identity and server
  entrypoint follow the proposal but remain subject to D0/P1.
- `uv lock --dry-run` resolved 74 packages under Python 3.12.13 without writing
  `uv.lock`. The MCP CLI extra can still bring Rich transitively for development;
  S5 must prove the final shipped dependency contract rather than treating this
  skeleton check as packaging closure.
- Ruff check/fix and format passed. Pyright analyzed 126 files with zero errors,
  warnings, or information diagnostics. The standard ladder's mypy check passed
  for 72 source files.
- Repo-wide pytest is not green. Normal collection stops because the pre-existing
  `.codex/skills/firmcli-workflow-core/scripts/frontier.py` is missing. With that
  one test ignored, 455 tests passed and 10 unrelated turnkey UX tests failed
  because this Windows execution host has no console screen buffer for
  `prompt_toolkit`.
- The validation-created `BYO-Server/.ruff_cache/` was identified and removed;
  no validation-spawned processes remained. Pre-existing Codex, Claude, and MCP
  server processes were left untouched.

## Reconciliation findings

The build plan still defines one canonical product-code path. The user's direct
S1 instruction authorized this reversible scaffold out of sequence, but it did
not approve the complete duplicate-tree/drift model or freeze the extraction
boundary. The manifest therefore contains only S1 split/rewrite inputs and an
empty `planned_extraction_entries` list. No claim is made that P2 exists.

The source project declares Apache-2.0 metadata but no authoritative root
license/notice file was found. This scaffold deliberately omits license metadata
until D0 resolves that distribution decision.

## Verified

- S1 structure, ignore rules, Python pin, BYO-only project skeleton, provenance,
  hashes, dependency dry-run, static checks, and cleanup were verified without
  hardware.
- Unrelated `markdowns/UX_design/` content was not modified or absorbed.

## Pending verification

- D0 approval, P1 boundary freeze, P2 complete planned manifest, and P3 clean
  adversarial plan review.
- Runtime/data/test extraction, standalone lock/build/install, clean-room
  isolation, MCP behavior, hardware, provider, and fresh-machine proof.
- Resolution of the missing frontier helper and unrelated Windows no-console UX
  test failures if a fully green parent-repo gate is required.
