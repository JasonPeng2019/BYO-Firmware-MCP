> STATUS: S1-S6 COMPLETE UNDER EXPLICIT SLICE AUTHORIZATION. D0, P1-P3,
> R1-R3, and H1 remain open; the extraction is not complete.
> The referenced workflow `frontier.py` helper is missing in this checkout, so
> automated status/gate commands are unavailable until restored. The STATUS
> tokens below remain the authoritative manual frontier for this proposal.

# BYO Server Extraction Process

Task spec: `BYO-Server/byo-server-extraction_spec.md`

## Slice frontier

- [ ] D0 | TODO | Obtain explicit user approval for the canonical-layout exception, standalone-copy drift model, package naming, wheel-vs-checkout contract, and licensing disposition.
- [ ] P1 | TODO | Reconcile and freeze the complete source-to-destination boundary.
- [ ] P2 | TODO | Create the planned extraction manifest with hashes, actions, tests, and slice mapping.
- [ ] P3 | TODO | Complete an independent adversarial plan review and freeze the plan.
- [x] S1 | DONE | Scaffold `BYO-Server/` and extraction provenance without copying behavior code.
- [x] S2 | DONE | Copy and isolate the headless MCP server runtime dependency closure.
- [x] S3 | DONE | Copy and isolate boards, firmware, packs, bootstrap, Stage 0/1, and Zephyr support.
- [x] S4 | DONE | Copy and isolate the R11 BYO benchmark corpus, harness, and relevant tests.
- [x] S5 | DONE | Finalize BYO-only packaging, lockfile, public commands, and documentation.
- [x] S6 | DONE | Run the complete standalone non-hardware integration and isolation suite.
- [x] S6D1 | DONE | Repair non-reproducible manifest tree digests and add a standalone destination-outcome regression guard.
- [ ] R1 | CHANGES REQUESTED | Independently audit completeness, exclusions, hashes, contracts, and parent isolation; the first review recorded three open findings.
- [x] R2 | DONE | Run a clean-room build/install and adversarial non-hardware test matrix.
- [ ] H1 | BLOCKED-HARDWARE | STM32 read-only Stage 0/MCP proof is partial-green, but exact official-pair closure lacks nrf52833dk; alternate nrf52840dk has no visible UART and its J-Link backend attach times out. Stage 1/R11 reference flashing was not authorized.
- [ ] R3 | TODO | Complete final parent/new-subproject consistency, proof, drift, and documentation review.

## Current state

- Done: repo/authority/dependency reconnaissance, proposal prompt pack, and the
  explicitly authorized S1 scaffold, S2 core runtime, S3 data/bootstrap, and
  S4 R11 benchmark, S5 packaging/documentation, S6D1 manifest repair, and S6
  standalone integration slices.
- In progress: none.
- Done: R2 clean-room/adversarial validation in addition to S1-S6/S6D1.
- Changes requested: R1 provenance/planned-manifest/documentation findings.
- Blocked hardware: H1 exact official-pair proof. The next manual frontier
  remains D0; R3 can close only after the open R1 findings and H1 boundary are
  resolved or explicitly accepted.

## Limitations

- The core runtime, board/firmware/pack data, bootstrap flow, Stage 1 wrapper,
  R11 corpus/harness/test closure, packaging, independent lockfile, and
  standalone operator/product documentation are present, and S6 completed the
  clean-room integration ladder.
- The plan intentionally proposes a second product-code tree and therefore
  still requires full D0 approval under the current authority rules.
- The workflow's documented deterministic frontier helper is missing.
- Full cleanup completeness is already an open product gap under W06/P05/P12.

## Open decisions

- Approve `BYO-Server/` as an exception to the canonical single source tree.
- Accept independent-copy drift risk or choose a generated/vendor-sync model.
- Preserve `pyocd_debug_mcp`/`pyocd-debug-mcp` names or rename the standalone
  package/command.
- Promise checkout-only operation or installed-wheel operation with bundled
  board/firmware/pack assets.
- Determine the authoritative Apache-2.0 license/notice files for standalone
  distribution.

## Slice evidence

S1 created portable ignore/environment/Python/project skeleton files, empty
directory markers, and `docs/extraction-manifest.json`. The manifest records
source commit `d73444f3b7288c286c1814ec1e63fdbd862616ec`, source and destination
hashes for each split/rewrite input, the absence of behavior code, and the open
D0/P1-P3 decisions. See `byo-server-extraction_s1_review.md` for the bounded
non-hardware review.

S2 copied 25 runtime modules: 22 are source-hash-identical and three are
intentional splits (`server.py`, `timeouts.py`, and `runtime_resources.py`). It
also copied six focused source tests and added contract/import-closure checks.
The ordinary 20-tool FastMCP schema and decorated function AST hashes match the
frozen source; only `_brain_sync_timeouts` is omitted. `pack_provision.py` is in
S2 because `adapters/swd_pyocd.py` imports it; packs and pack-data tests were
then completed in S3. See `byo-server-extraction_s2_review.md`.

S3 copied all five tracked board profiles and all 102 tracked firmware files,
including the three retained reference ELF/HEX pairs and tracked reference/bug
fixtures. It copied the tracked pack manifest/README/ignore rules plus the
source pack-repair support note, four root bootstrap scripts, four data/support
modules, and ten focused source tests. Paths already resolved from the
standalone project root, so no Python or script behavior rewrite was needed.
The only data-tree divergence makes the ignored source support note trackable
in the standalone pack directory. See `byo-server-extraction_s3_review.md`.

S4 copied `benchmark_support.py`, all 40 tracked R11 case/schema/suite files,
the R11 and Stage 1 thin harness wrappers, the R11 contract test, and its three
structured-result fixtures. The R10 runtime test was already retained from S2.
All 47 copied S4 files are byte-identical. A standalone isolation test proves
the imported package plus case, schema, firmware, workspace, and run roots are
owned by `BYO-Server/`, not the parent canonical paths. No R12/turnkey harness,
test, fixture, brain, UX, or provider-memory content was copied. See
`byo-server-extraction_s4_review.md`.

S5 finalizes the three retained console scripts, code-only wheel/sdist rules,
and independent 68-package lock. Removing the unused `mcp[cli]` extra removes
Rich, Typer, markdown-it, and shellingham from the closure. The five BYO-only
documents state checkout-only operation, optional Codex-specific R11 scope,
the current in-memory session store, cleanup limits, official/alternate proof,
vendor bootstrap requirements, and the unresolved legal decision. A fresh
34-member wheel install passed imports, resource refusal, entrypoint metadata,
utility help, and bounded stdio EOF shutdown. See
`byo-server-extraction_s5_review.md`.

S6D1 repaired three non-reproducible directory-digest records without changing
copied content, made the manifest algorithm explicitly project-relative and
POSIX, and added a standalone guard over every destination outcome. The scoped
fix loop passed Ruff, full Pyright JSON, and all 209 BYO tests. See
`byo-server-manifest-tree-digest_bug_review.md`.

S6 copied the corrected project to a fresh location outside the parent repo and
restarted the complete suite. Lock/sync, Ruff autofix/format/check, full
Pyright JSON, all 209 tests, 34-file wheel/sdist inspection, a fresh 55-package
wheel environment, seven help surfaces, checkout/wheel resource contracts,
prohibited-content searches, and 11 focused contract tests passed. A real MCP
client initialized protocol `2025-11-25`, listed all 20 ordinary tools with
schemas matching the frozen source snapshot, and shut down normally. No board
tool was called and every audited command left zero new processes. See
`byo-server-extraction_s6_review.md`.

R2 repeated the complete validation ladder from a fresh external copy, added
focused URL/non-file flash refusal coverage, exercised the adversarial matrix,
built and installed both archives in a second environment, and completed two
real MCP lifecycle/tool-view cycles. All 212 tests on that R2 tree passed. See
`../markdowns/curr/byo-server-extraction_r2_test_matrix.md`.

H1 attached alternate `nrf52840dk` and official `nucleo_l476rg` hardware on a
Windows host. Live validation found and fixed a cross-probe-family serial
resolver bug; the post-fix suite is Ruff/Pyright clean with 214 tests passing.
The STM32 path passed exact probe/COM discovery, read-only Stage 0, Inspector
tool listing, live MCP connect/state/halt/read/resume/refusal/convergence/
disconnect, and cleanup. Its retained session is
`runs/20260715T220038Z-8a7323c0`. The Nordic alternate's J-Link backend attach
timed out and no J-Link VCP was visible. Exact `nrf52833dk`, authorized Stage 1
flashing, and provider-backed R11 remain unavailable, so H1 is
`BLOCKED-HARDWARE`. See
`../markdowns/curr/byo-server-extraction_h1_test_matrix.md`.

## Hardware handoff

Exact board-scoped commands are frozen in `stage0_setup.md` and
`docs/verification.md`. No hardware result is claimed by this ledger.

## Verified

- Proposal inputs and current repo boundary were inspected non-destructively.
- S1 scaffold structure and provenance were created without hardware access.
- S2 Ruff, full Pyright JSON, 75 focused tests, source/destination hashes,
  ordinary MCP contracts, and standalone import closure passed without hardware.
- S3 Ruff and full Pyright JSON passed for 50 Python files; 91 focused S3 tests
  and the full 166-test standalone suite passed with the parent project disabled.
  Board, firmware, pack, bootstrap, Stage 0 error, discovery, and Zephyr roots
  all resolve inside `BYO-Server/`.
- S4 Ruff and full Pyright JSON passed for 55 Python files; 54 focused R10/R11
  and isolation tests plus the full 203-test standalone suite passed with the
  parent project disabled. The R11 corpus and result fixtures match their
  source tree digests.
- S5 lock check, Ruff, Pyright, all 207 standalone tests, code-only wheel audit,
  fresh locked sync, fresh wheel install, import/resource/entrypoint checks,
  public utility help, and bounded stdio EOF shutdown passed without hardware.
- S6's post-fix clean-room restart passed Ruff/Pyright for 57 files, all 209
  tests, build/install/archive/resource/help/prohibited-reference checks, the
  80-row manifest guard, and real MCP initialize/list-tools/shutdown with zero
  surviving spawned processes.
- R2's later clean-room matrix passed all 212 tests on its final tree and two
  real board-free MCP lifecycle cycles.
- H1's live fix tree passes Ruff/Pyright for 57 files and all 214 tests. The
  official STM32 board passed the read-only Stage 0/MCP surface described in
  its H1 matrix; this is not flash/UART/reference-baseline proof.

## Pending verification

- D0/P1-P3 planning/governance, the open R1 findings, R3, live-provider,
  exact `nrf52833dk`, authorized Stage 1/reference flashing, Nordic J-Link/UART
  repair, cross-host, and fresh-machine work.
