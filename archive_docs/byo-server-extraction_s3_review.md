> STATUS: S3 DATA/BOOTSTRAP REVIEW - PASS WITH DEFERRED PACKAGING AND
> CROSS-HOST LIMITATIONS.

# BYO Server S3 Data and Bootstrap Review

## Scope and result

This review covers only the explicitly invoked S3 data/bootstrap slice. The
standalone tree now contains all approved board data, tracked firmware and
fixture data, pack metadata/support material, host/bootstrap scripts,
reference-smoke and build helpers, and the focused non-hardware test closure.
S3 is complete. The R11 corpus and Stage 1 harness wrapper remain S4;
packaging, lockfile, and standalone operator documentation remain S5.

## Copied closure

- All five tracked board YAML files are byte-identical to the source. Their
  board facts and `HW-FIXED`, `VENDOR-FIXED`, `PROJECT-DEFINED`, and
  `UNVERIFIED` origin tags were not rewritten.
- All 102 Git-tracked firmware files are byte-identical, including the three
  retained reference ELF/HEX pairs and reference/bug fixture sources. Ignored
  build output was not copied.
- The pack manifest and README were copied. No downloaded `.pack` or live host
  pack state was copied.
- `host_bootstrap.py`, `stage0_check.py`, `setup_host.ps1`, and `setup_host.sh`
  are exact copies, as are `board_config_cli.py`, `pack_index_repair.py`,
  `reference_smoke.py`, and `zephyr_build.py`.
- Ten focused source tests are hash-identical. A generated S3 closure test
  additionally proves local data roots, the exact board inventory, reference
  artifact signatures, the bounded firmware build-file inventory, absence of
  downloaded packs, and utility command registration.

The copied implementations already derive their roots from the script or
package location. No path-root or command rewrite was needed for standalone
execution, and no source behavior defect was repaired during extraction.

## Intentional divergences

- The source `packs/live_index_repair.md` is an ignored, untracked support note
  referenced by the tracked pack README. S3 explicitly requested pack support
  docs, so the note is copied byte-for-byte and the destination
  `packs/.gitignore` unignores only that file. This exception and both tree
  digests are recorded in the manifest.
- The S3 project skeleton registers the source's existing
  `pyocd-pack-repair` and `pyocd-zephyr-build` utility commands. S5 remains
  responsible for finalizing the public script and packaging contract.

## Verification

- Standalone Ruff check/fix and format passed for 50 Python files.
- Standalone full Pyright JSON analyzed 50 files with zero errors, warnings, or
  information diagnostics.
- With the parent project disabled through `uv run --no-project
  --with-editable .`, all 91 focused S3 tests passed and the complete current
  BYO suite passed all 166 tests.
- The PowerShell setup script parses. Pack repair, Zephyr build, bootstrap, and
  Stage 0 help commands run from the standalone project; board-config
  inspection reports the YAML path under `BYO-Server/boards`.
- Tree digests match for the five board files and 102 tracked firmware files.
  Script/module/test file hashes match their recorded sources, and the pack
  tree's one intentional divergence is explicit.
- No hardware, probe, serial device, live pack installation, Zephyr build,
  provider, or fresh-machine check was run.

## Limitations and next frontier

- `setup_host.ps1` and `setup_host.sh` correctly retain `uv sync --locked`, but
  the independent lockfile is an S5 deliverable. Full setup execution is
  therefore deferred rather than weakened for this slice.
- The Stage 1 smoke implementation is present as `reference_smoke.py`; its
  command-line harness wrapper is explicitly assigned to S4. Firmware notes
  that name that harness retain their source wording until then.
- BYO-only `README.md`, `init.md`, `stage0_setup.md`, architecture, and
  verification documentation are S5 deliverables.
- Bash syntax checking could not run on this Windows host because neither a
  WSL distribution nor Git Bash is installed. The source macOS bootstrap also
  retains its pre-existing manual STM32CubeProgrammer prerequisite. Both need
  cross-host verification; neither justifies an unrelated source fix in S3.
- The parent repository's pytest collection remains blocked by the
  pre-existing missing workflow `frontier.py` helper. D0/P1-P3 also remain
  open despite direct S3 authorization.

The next implementation slice is S4: copy and isolate the R11 BYO benchmark,
its case corpus, the R11 and Stage 1 harness wrappers, and their approved tests.

