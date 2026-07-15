> STATUS: S6 NON-HARDWARE INTEGRATION PASS. EXTRACTION NOT COMPLETE.

# BYO Server S6 integration review

## Verdict and frontier

The complete non-hardware integration chain passes after one provenance defect
was repaired through `firmcli-fix-bug` and the entire chain restarted from a
new clean-room copy. S1-S6 and S6D1 are done. This is not a final extraction
verdict: D0, P1-P3, R1-R3, and H1 remain open, and R1 is the next prompt-order
slice.

All 80 recorded manifest destination rows now have reproducible outcomes. The
planned list contains zero rows because P2 was never frozen; therefore no
planned row is mismatched, but global boundary completeness cannot be claimed
until P1-P3 and R1 close.

## Corrected clean-room matrix

| Gate | Result |
| --- | --- |
| Parent isolation | PASS - copied outside the parent; empty `PYTHONPATH`; package and resources resolve only under the copy |
| Lock/sync | PASS - 68-package lock; 64-package copy-mode dev environment |
| Ruff | PASS - autofix/format made no changes; 57 files clean |
| Full Pyright JSON | PASS - 57 files, zero diagnostics |
| Full pytest | PASS - 209 tests |
| Manifest/contracts | PASS - 80 destination outcomes; 11 focused manifest/schema/import/packaging tests |
| Build/archive | PASS - wheel and sdist each contain 34 files; prohibited assets absent |
| Fresh wheel install | PASS - 55 compatible runtime packages |
| Public help | PASS - pack repair, Zephyr, bootstrap, Stage 0, Stage 1, R11, and board-config surfaces |
| Resource boundary | PASS - checkout roots stay local; wheel refuses both unbundled benchmark resources |
| Prohibited references | PASS - no prohibited product paths/imports, parent absolute paths, or Rich/Typer/prompt-toolkit lock packages |
| MCP connection | PASS - real stdio initialize/list-tools/shutdown; protocol `2025-11-25`; 20 source-schema-identical tools |
| Process cleanup | PASS - zero new surviving processes after every audited command |

The MCP client did not invoke `connect` or any other tool. This verifies the
actual server transport, initialization, capability enumeration, schema
delivery, and normal shutdown without enumerating or opening hardware.

## Defect loop

The first run proved that `firmware/`, `packs/`, and `tests/cases/` directory
digests could not be reproduced by the manifest's declared algorithm. S6D1
corrected the provenance hashes, specified project-relative POSIX records,
recorded the pack supplement exclusion, and added
`tests/test_extraction_manifest.py`. The scoped bug spec and clean review are
`byo-server-manifest-tree-digest_bug_spec.md` and
`byo-server-manifest-tree-digest_bug_review.md`. No runtime content changed.

## Cleanup and exclusions

Validation environments, caches, archives, and run output lived only under the
temporary S6 roots. The repository copy contains no generated environment,
cache, archive, run, or scratch directory after cleanup. Existing parent MCP/
Codex processes were outside the S6 provenance filter and were not terminated.

No probe discovery, serial enumeration, pack installation, vendor tool, board
connection, memory access, flash, recover, UART, provider call, or R11 case was
run. The root validation helper was not used because it targets the parent
project and its documented `frontier.py` dependency is absent; the full BYO
matrix ran directly from the isolated subproject instead.

## Exact pending handoff

Run only after attaching and identifying the named board, and separately
authorizing mutation:

```text
uv run --locked python host_bootstrap.py --board-id nrf52833dk --install-packs
uv run --locked python stage0_check.py --board-id nrf52833dk --install-packs
uv run --locked python -m tests.harness.stage1_smoke --board-id nrf52833dk

uv run --locked python host_bootstrap.py --board-id nucleo_l476rg --install-packs
uv run --locked python stage0_check.py --board-id nucleo_l476rg --install-packs
uv run --locked python -m tests.harness.stage1_smoke --board-id nucleo_l476rg
```

For live MCP board proof, start `uv run --locked pyocd-debug-mcp` through a
compatible client, initialize/list tools, then call `connect`, read-only memory/
state/symbol tools, authorized UART operations, and `disconnect` for the one
selected board. Flash/recover require separate explicit authorization and a
reviewed artifact/confirmation.

After hardware MCP proof, the optional provider-specific known-good handoff is:

```text
uv run --locked python -m tests.harness.r11_benchmark --case-id nrf52833dk__k001_reference_green
uv run --locked python -m tests.harness.r11_benchmark --case-id nucleo_l476rg__k001_reference_green
```

Audit MCP, Python/uv, Codex, pyOCD, serial, vendor-tool, and probe ownership
after each live command. No result for these pending commands is claimed here.

## Next slice

R1: independently audit completeness, exclusions, hashes, contracts, and parent
isolation. D0/P1-P3 still prevent a final extraction-complete claim.
