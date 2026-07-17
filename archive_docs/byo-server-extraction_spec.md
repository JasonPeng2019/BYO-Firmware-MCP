> STATUS: S1-S6 AUTHORIZED AND IMPLEMENTED - D0, P1-P3, R1-R3, and H1
> remain open; the extraction is not complete.
> TASK: Copy the complete current BYO-agent plus headless-server product into a
> standalone top-level `BYO-Server/` subproject without moving or deleting the
> existing implementation.

# BYO Server Extraction

## Goal in plain English

Create `Firmware-CLI/BYO-Server/` as a self-contained copy of the current
Stages 0-4 product: board data, host/bootstrap flow, adapters, shared services,
the local stdio MCP server, deterministic guardrails, session logging, Stage 1
smoke validation, and the retained R11 external-agent benchmark path.

The copied subproject must run, test, build, and document itself from inside
`BYO-Server/` without importing code or reading required runtime assets from its
parent checkout. The original root implementation remains untouched and
continues to serve the combined BYO plus turnkey product.

Roadmap anchor: implemented product 1, build-plan Stages 0-4 and Feature Master
I01-I03. This is a proposed packaging/repository-layout extraction, not a new
hardware capability.

## Decision gate before implementation

This proposal intentionally creates a second product-code tree, which conflicts
with the build plan and root README rule that product code lives only under
`src/pyocd_debug_mcp/`. It also creates ongoing drift risk between the original
server and the copied server.

On 2026-07-15, the user explicitly invoked Execution prompts 1 through 6. Those
instructions authorize the bounded S1 scaffold/provenance, S2 core-runtime,
S3 data/bootstrap, S4 R11 benchmark, and S5 packaging/documentation slices
plus the S6 integration closure despite the normal ordering gate; they do not
close D0, P1-P3, or the independent review/hardware gates. The following
consequences remain unresolved:

1. `BYO-Server/` is a deliberate standalone subproject and an approved
   exception to the single canonical product-code-path rule.
2. This is a copy, not a move: future fixes will not automatically propagate
   between the root product and `BYO-Server/`; provenance and parity tooling
   will record divergence but cannot prevent it.

Provisional choices used by the prompts below, subject to that sign-off:

- Preserve the internal import package name `pyocd_debug_mcp` and public
  `pyocd-debug-mcp` command inside the isolated subproject to minimize behavior
  drift. Isolation is provided by `BYO-Server/pyproject.toml`, its own lockfile,
  and commands run with `BYO-Server/` as the project root.
- Copy all currently tracked board profiles, including the retained alternate
  `nrf52840dk`, because the current BYO test/benchmark inventory includes it.
- Retain the R11 Codex benchmark as an optional evaluation harness. It is not
  the definition of ordinary BYO use and must stay labeled Codex-specific.
- Remove turnkey-only code and surfaces from the copy, including `brain/`,
  `ux/`, `services/codex_app_server.py`, `services/codex_activity.py`,
  `_brain_sync_timeouts`, turnkey-only timeout/resource definitions, turnkey
  skills/playbooks, R12 harnesses/tests, Rich, and `prompt_toolkit`.
- Do not use symlinks, namespace tricks, parent-relative imports, or runtime
  fallback to files outside `BYO-Server/`.

## Implemented extraction record

S1 created the portable standalone scaffold. S2 copied the complete current
headless runtime import closure as 25 modules. Twenty-two module files remain
source-hash-identical. The approved splits are limited to:

- `server.py`: removes `_brain_sync_timeouts` and its three now-unused imports;
  all 20 ordinary MCP schemas and decorated function ASTs match the source
  snapshot.
- `timeouts.py`: retains only host/server setup, startup, subprocess, and pyOCD
  timeout contracts used by BYO/server paths.
- `runtime_resources.py`: retains checkout/package and R11 benchmark resource
  resolution while removing turnkey skill/playbook resolvers.

`pack_provision.py` is included in S2 because the unchanged SWD adapter imports
it. S3 adds the five tracked board profiles, 102 tracked firmware files, pack
manifest/support material, bootstrap scripts, reference-smoke/pack/Zephyr
helpers, and the focused data/bootstrap test closure. Those sources already
derive roots from their own standalone locations, so behavior and board facts
remain unchanged. S4 adds the exact current R11 Codex runner, all 18 case
manifest/prompt pairs, frozen suites and result schema, the R11 and Stage 1
harness wrappers, and the R10/R11 test closure. Its local root checks preserve
the benchmark's scoring, prompt, timeout, artifact, and canonical-session
contracts while preventing fallback to parent package/data paths. The copy uses the current `InMemorySessionStore`; adding
Redis is explicitly out of scope and would not be extraction-faithful.

S5 finalizes the three retained console scripts, removes the MCP CLI extra and
its Rich/Typer dependency chain, generates an independent 68-package lock, and
adds the BYO-only operator/product documentation. The wheel intentionally
contains package code only: checkout-owned board, firmware, pack, bootstrap,
case, test, and run assets are not package data. Installed-wheel operation is
therefore not claimed; the fresh wheel check proves metadata, imports,
entrypoints, utility help, explicit resource refusal, and stdio EOF shutdown.
The missing authoritative LICENSE/NOTICE decision remains open.

S6 ran the complete non-hardware chain twice from temporary copies outside the
parent tree. The first pass exposed non-reproducible manifest directory hashes;
S6D1 routed that defect through a scoped fix spec/review, corrected provenance
only, and added a destination-outcome regression guard. The mandatory restart
then passed lock/sync, Ruff, full Pyright JSON, all 209 tests, wheel/sdist build
and inspection, fresh wheel install, help/resource checks, prohibited-content
audits, and a real MCP stdio initialize/list-tools/shutdown connection. The live
wire schema for all 20 ordinary tools matches the frozen source snapshot, and
every S6 subprocess left zero new processes. No board or provider was used.

## Scope

### In scope

- A top-level `BYO-Server/` standalone Python/uv project.
- A machine-readable extraction manifest that records source path, destination
  path, inclusion reason, source commit, and SHA-256 at the extraction point.
- The complete headless server dependency closure:
  - `server.py`
  - `adapters/`
  - `guardrails/`
  - shared board-facing `services/`, excluding the two Codex/turnkey services
  - board config, local env, probe and serial discovery
  - reference artifact/smoke helpers
  - pack provisioning and repair helpers
  - typed target errors
  - BYO/server timeout definitions
  - Zephyr build helper
  - R11 benchmark support and its runtime-resource subset
- Root operational assets needed by Stages 0-4:
  - `boards/`
  - `packs/`
  - tracked `firmware/` reference and bug fixtures
  - `host_bootstrap.py`, `stage0_check.py`, `setup_host.ps1`, and
    `setup_host.sh`
  - a BYO-only `.env.example`, `.python-version`, `.gitignore`,
    `pyproject.toml`, and independently generated `uv.lock`
- The R11 case corpus, schema, suites, harness, and relevant tests.
- The non-turnkey test closure for board config, discovery, adapters, services,
  server, guardrails, Stage 0, Stage 1, R11, packs, and Zephyr builds.
- Standalone BYO-only documentation: README, bootstrap/operator guide,
  architecture/boundary note, verification status, and explicit hardware
  handoff.
- Static and executable isolation checks proving that the new subproject does
  not depend on the parent checkout.
- Non-hardware checks, package build/install checks, MCP schema/Inspector smoke
  where possible, and exact hardware/BYO-agent handoff commands where not.

### Out of scope

- Moving, deleting, or rewriting the existing root implementation.
- The Stage-5 turnkey brain, its provider adapters, governed `TurnDecision`
  loop, context/memory system, client actions, skills injection, or UX shell.
- R12 benchmark wrappers or tests.
- `skills/`, `playbooks/turnkey/`, `brain/`, `ux/`, or Codex app-server
  streaming services.
- Remote Streamable HTTP/OAuth transport.
- Redis or a new session-store implementation as part of the copy.
- New boards, new MCP tools, new server behavior, or cleanup-gap fixes unrelated
  to making the copy standalone.
- Claiming fresh hardware, exact-board, provider, or fresh-machine proof without
  running it in the current session.
- Publishing, committing, pushing, or opening a pull request unless separately
  requested.

## Reconciliation summary

### Build plan

- Stages 0-4 define the BYO product and make it a shippable product before the
  Stage-5 turnkey layer.
- Guardrails belong in the server below every client.
- The MCP server is local stdio, headless, blocking-v1, and backed by shared
  adapters/services.
- Board facts are data in `boards/<board>.yaml`; machine and user-project facts
  are runtime inputs.
- The build plan also settles one canonical source tree under
  `src/pyocd_debug_mcp/`, so a top-level copied subproject is an explicit plan
  exception requiring user sign-off.

### Current code

- The server dependency closure is mostly cleanly separated from `brain/` and
  `ux/`.
- Two files under `services/` are turnkey/Codex-only and must not be copied:
  `codex_app_server.py` and `codex_activity.py`.
- `server.py` exposes a turnkey-only `_brain_sync_timeouts` MCP tool that must
  be removed from the BYO copy while preserving all ordinary server tools.
- `timeouts.py` and `runtime_resources.py` mix shared/BYO and turnkey
  definitions and require a careful BYO-only extraction, not a blind copy.
- `benchmark_support.py` is the R11 BYO evaluation runner. It invokes Codex CLI
  directly and requires a live checkout because it copies repo-owned firmware
  workspaces. It is optional benchmark tooling, not ordinary server runtime.
- The root `pyproject.toml` mixes BYO and turnkey dependencies, scripts, and
  bundled data. A new project file and lockfile are required.
- Root README, `init.md`, `.env.example`, and verification prose mix both
  products and must be rewritten for BYO-only truth rather than copied whole.

### Other docs and status

- Feature Master I01-I03 are the current product-definition source for the
  foundation, server, and BYO/R11 path.
- Current code uses `InMemorySessionStore` plus durable run files. The build
  plan/architecture still describe Redis as the settled external-state target.
  This extraction copies current behavior and must surface, not silently fix,
  that conflict.
- Feature Master marks full process-tree/MCP/pyOCD/serial cleanup completeness
  open under W06/P05/P12. The extracted server must carry that limitation.
- Historical R11 proof exists, but fresh exact-board and fresh-machine proof
  remain separate evidence gates.
- The R11 historical spec names Codex CLI as the benchmark client even though
  the older Stage-4 build-plan wording names Claude Code. Ordinary BYO mode is
  model/client agnostic; the retained benchmark remains Codex-specific.
- `pyproject.toml` declares Apache-2.0, but no project-root license/notice file
  was found. A standalone distribution must not invent provenance; legal-file
  handling remains a human decision unless an authoritative source is found.
- The selected `firmcli-write-process` skill refers to
  `.codex/skills/firmcli-workflow-core/scripts/frontier.py`, but that helper is
  missing in the current checkout. The process ledger below is still the
  persistent checklist, but automated frontier commands/gating are unavailable
  until the workflow helper is restored separately.

## Extraction design

### Target shape

```text
BYO-Server/
|-- .env.example
|-- .gitignore
|-- .python-version
|-- pyproject.toml
|-- uv.lock
|-- README.md
|-- init.md
|-- stage0_setup.md
|-- host_bootstrap.py
|-- stage0_check.py
|-- setup_host.ps1
|-- setup_host.sh
|-- boards/
|-- packs/
|-- firmware/
|-- docs/
|   |-- architecture.md
|   |-- extraction-manifest.json
|   `-- verification.md
|-- src/pyocd_debug_mcp/
|   |-- server.py
|   |-- adapters/
|   |-- guardrails/
|   |-- services/
|   |-- board_config.py
|   |-- board_config_cli.py
|   |-- benchmark_support.py
|   |-- local_env.py
|   |-- pack_index_repair.py
|   |-- pack_provision.py
|   |-- probe_inventory.py
|   |-- reference_artifacts.py
|   |-- reference_smoke.py
|   |-- runtime_resources.py
|   |-- serial_resolver.py
|   |-- target_errors.py
|   |-- timeouts.py
|   `-- zephyr_build.py
`-- tests/
    |-- cases/
    |-- harness/r11_benchmark.py
    |-- harness/stage1_smoke.py
    `-- relevant unit/contract tests
```

The exact test list is frozen by the planning manifest, not guessed during
copying. The initial expected set is:

- `test_board_configs.py`
- `test_host_bootstrap.py`
- `test_pack_index_repair.py`
- `test_pack_provision.py`
- `test_probe_inventory.py`
- `test_r10_runtime.py`
- `test_r11_benchmark.py`
- `test_reference_artifacts.py`
- `test_serial_resolver.py`
- `test_server_board_config.py`
- `test_server_import.py`
- `test_server_runtime_tools.py`
- `test_stage0_shared_errors.py`
- `test_symbols.py`
- `test_target_control.py`
- `test_uart_capture.py`
- `test_zephyr_build.py`

### Copy and isolation rules

1. Freeze a source manifest before copying. Record the current commit and hash
   every copied source/data file.
2. Copy files; do not move them and do not edit the original merely to make the
   copy easier.
3. Resolve every import and resource path inside the new subproject.
4. Fail tests if imports, paths, commands, or package data escape
   `BYO-Server/`.
5. Run the subproject with its own environment and lockfile. Do not rely on the
   root `.venv` or root editable install.
6. Preserve server tool names, signatures, return prefixes, refusal/block
   codes, session artifact names, watcher thresholds, and board facts unless a
   difference is explicitly required to remove a turnkey-only surface.
7. Record every intentional difference from the source manifest in a
   destination-side divergence file or manifest field.
8. Do not copy generated caches, `.venv`, `runs/` contents, `dist/`, scratch,
   or parent workflow artifacts.

### Public surface

The copied runtime keeps `pyocd-debug-mcp` as the server entrypoint. The
planning phase must decide whether pack repair, Zephyr build, board-config
inspection, and R11 benchmark receive console entrypoints or remain their
current module/script commands. It must not silently add a public API.

The MCP surface must match the ordinary server surface in the source at the
frozen commit, except that `_brain_sync_timeouts` is absent. The manifest/audit
must compare tool name, description, and input schema, not only function names.

### R11 benchmark boundary

The R11 runner and corpus are included because they are the current concrete
BYO-agent evaluation path. Documentation must say:

- ordinary BYO use is any compatible MCP client driving the headless server;
- the R11 harness is Codex-specific and launches `codex exec` with its current
  permissions contract;
- benchmark success is not universal BYO-client proof;
- provider credentials, registration, and live hardware are external
  prerequisites for live benchmark proof.

## Board facts as data and origin tags

- Copy board YAML without re-deriving target, probe, UART, recover, or silicon
  facts in code.
- Preserve existing `HW-FIXED`, `VENDOR-FIXED`, `PROJECT-DEFINED`, and
  `UNVERIFIED` annotations.
- No serial port, probe UID, target, build path, or host-specific path may be
  baked into the copied code or docs.
- External custom board files must continue to work through the same loader and
  `board_config`/environment inputs.
- The official proof pair remains `nrf52833dk + nucleo_l476rg`.
  `nrf52840dk` remains alternate evidence only.

## Documentation plan

- Write a BYO-only `BYO-Server/README.md` that explains product boundary,
  standalone setup, MCP registration examples, current tool surface source,
  R11 benchmark distinction, and proof limits.
- Write BYO-only `init.md` and `stage0_setup.md`; do not retain turnkey provider,
  brain, UX, skills, or memory instructions.
- Keep MCP operation contracts in copied `server.py` docstrings. Do not create
  per-tool sidecar docs.
- Add `docs/architecture.md` for the product split and
  `docs/verification.md` for evidence levels and exact handoffs.
- Keep the parent repo's build plan, Feature Master, Roadmap, current-progress,
  repo index, and root README synchronized in the implementation unit if the
  user approves this new settled top-level layout. A proposal spec alone does
  not amend those authorities.
- Do not edit or absorb the user's unrelated untracked `markdowns/UX_design/`
  work.

## Portability

- The subproject must be runnable after the same bounded Windows/macOS bootstrap
  as the current product.
- Generate an independent `uv.lock` from the BYO-only dependency set.
- Remove turnkey-only dependencies and environment variables.
- Preserve `pathlib`-based paths and board/probe/serial discovery.
- Test from a copied or temporary location where the parent repo is not on
  `PYTHONPATH` and the parent package is not importable.
- Build and install a wheel into a fresh environment and inspect its contents.
  If board/firmware/pack assets are checkout-only, document that honestly; if
  installed-wheel operation is promised, bundle and resolve those assets
  explicitly.
- Vendor drivers remain bounded external bootstrap prerequisites and are never
  redistributed silently.

## Verification plan

### Static and non-hardware

- Exact manifest inclusion/exclusion audit.
- Search for prohibited references: parent-relative paths, root-only docs,
  `brain`, `ux`, `turnkey`, `_brain_sync_timeouts`, Codex app services, Rich,
  and `prompt_toolkit`, allowing only explanatory negative documentation where
  appropriate.
- Import-closure check for every copied Python module.
- `uv lock --check` or the current uv equivalent from `BYO-Server/`.
- Ruff autofix/format, full Ruff check, full Pyright JSON check, and full pytest
  inside the standalone project.
- Build wheel/sdist as applicable; inspect archive membership.
- Install the built artifact into a fresh environment and run public `--help`
  and import smokes.
- Run the MCP server/list-tools smoke with a bounded timeout and normal shutdown.
- Compare source and destination ordinary MCP schemas and session/refusal
  contracts, accounting for the one explicit brain-tool removal.
- Run Stage 0/Stage 1 and R11 unit tests without touching hardware.
- Audit spawned subprocess cleanup after every server/Inspector/client smoke.

### Hardware and live client

- On each official board, run host bootstrap, Stage 0, Stage 1, MCP Inspector
  tool validation, guardrail/refusal checks, convergence checks, and a stock
  external-agent BYO path from inside `BYO-Server/`.
- Use explicit timeouts and disconnect/close normally after each board test.
- Audit for leftover MCP, Python/uv, pyOCD, serial, provider, or probe sessions.
- Run at least the R11 known-good case per official board before considering a
  full suite, then run the frozen suite only when authorized and the bench is
  stable.
- Keep exact-board, alternate-board, provider, and fresh-machine evidence
  separate.

## Acceptance criteria

- `BYO-Server/` exists as a standalone uv project and the original product tree
  is not moved or deleted.
- Every in-scope source/data/test/doc item maps to an extraction manifest row
  and process-ledger slice.
- No runtime or test import/file dependency escapes `BYO-Server/`.
- The copied ordinary MCP schema and behavior match the frozen source, with
  `_brain_sync_timeouts` as the only planned MCP omission.
- No turnkey brain, UX, skills/playbooks, R12 harness, or turnkey-only
  dependency is shipped in the subproject.
- Board configs, reference artifacts, pack manifest, bootstrap scripts, Stage 1
  smoke, R10 server safety, and R11 benchmark assets are present and resolve
  locally.
- The BYO-only lockfile is reproducible and the full Python-change validation
  gate is green inside the subproject.
- Wheel/install behavior is either proven standalone or explicitly limited to
  checkout operation with no contradictory packaging claim.
- Documentation describes BYO mode, R11 scope, destructive actions, bootstrap,
  cleanup limits, and hardware proof honestly.
- Root governing docs are synchronized only after the user approves the layout
  exception.
- Hardware/live-provider status is reported as verified here, non-hardware
  verified, or pending; historical proof is not relabeled as fresh proof.

## Prompt sequence

Run the prompts below one at a time. S1 was explicitly authorized out of order
as a reversible scaffold. Do not start S2 or a later execution prompt until all
planning prompts report READY and the decision gate above has explicit user
sign-off.

### Planning prompt 1 - reconcile and freeze the product boundary

```text
Use the firmcli-workflow-core and firmcli-specs skills. Read
BYO-Server/byo-server-extraction_spec.md in full, then independently inspect
the current repo authority docs, Feature Master I01-I03, the MCP architecture,
current code, tests, packaging, bootstrap files, and git status.

Do not implement or create BYO-Server yet. Reconcile the proposal against the
current commit. Produce/update a source-to-destination boundary table covering
every source module, root script, data tree, test, harness, dependency,
entrypoint, runtime resource, and document. Classify each item INCLUDE,
EXCLUDE-TURNKEY, SPLIT, REWRITE-DOCS, or DECISION-NEEDED and explain why.

Explicitly resolve or surface: copy-vs-move, package/import naming, public
entrypoints, all-board vs official-pair data, R11 inclusion, checkout-only vs
installed-wheel support, licensing/notice files, the Redis-vs-current-in-memory
conflict, and the accepted drift-maintenance model. Preserve unrelated user
changes. Update only the proposal spec/process planning artifacts. End with
READY FOR MANIFEST or BLOCKED-HUMAN and the exact decisions still needed.
```

### Planning prompt 2 - build the exact extraction manifest and slice plan

```text
Use firmcli-workflow-core, firmcli-specs, and firmcli-write-process. Read the
approved BYO-Server/byo-server-extraction_spec.md and the output of planning
prompt 1. Do not copy product files yet.

Create a machine-readable planned extraction manifest under markdowns/curr/
that records source path, destination path, action (copy/split/rewrite/exclude),
reason, source SHA-256, expected destination role, relevant tests, and any
intentional divergence. Expand BYO-Server/byo-server-extraction_process.md
so every in-scope spec item has at least one slice and every manifest row maps
to a slice. Include explicit dependency, package-data, path-root, console-script,
documentation, isolation-test, and hardware-handoff slices.

The firmcli workflow currently references a missing frontier.py helper. Do not
pretend the automated gate exists: either restore it through a separately
approved workflow fix or record that process status must be checked directly
from the ledger during this extraction. End with exact file counts by category,
the prohibited-content audit rules, and READY FOR PLAN REVIEW or BLOCKED.
```

### Planning prompt 3 - adversarial plan review and freeze

```text
Act as an independent firmcli-review pass over the extraction spec, process
ledger, and planned manifest. Do not implement. Re-read the source dependency
closure rather than trusting the prior summaries.

Look specifically for omitted transitive imports, root-relative paths,
package-data assumptions, root scripts that call excluded entrypoints,
turnkey-only symbols hidden in shared files, test discovery/import collisions,
duplicate package-name hazards, stale docs, generated artifacts accidentally
included, provider-specific R11 behavior mislabeled as generic BYO behavior,
and validation commands that would accidentally use the parent environment.

Write findings into BYO-Server/byo-server-extraction_plan_review.md. Update
the proposal/manifest/ledger only for verified findings. Finish with CLEAN AND
FROZEN or CHANGES REQUIRED. Implementation may begin only after CLEAN AND
FROZEN plus explicit user approval of the decision gate.
```

### Execution prompt 1 - scaffold and provenance

```text
Use firmcli-build plus python-change where Python-facing configuration is
created. Implement only the approved scaffold/provenance slice from
BYO-Server/byo-server-extraction_process.md.

Create BYO-Server/ project structure, ignore rules, Python version, BYO-only
pyproject skeleton, destination extraction manifest/provenance record, and
empty required package/test/data directories. Do not copy behavior code yet.
Do not move/delete root files, create symlinks, or touch unrelated
markdowns/UX_design content. Ensure all paths are portable and no generated
environment/cache/output is added.

Run the slice checks, update the process ledger and spec/review artifacts, and
stop after this slice. Report files changed, non-hardware verification, and the
next open slice.
```

### Execution prompt 2 - copy the headless runtime dependency closure

```text
Use firmcli-build and python-change. Implement only the core-runtime slice from
the approved spec/manifest.

Copy the server, adapters, guardrails, board-facing shared services, and exact
transitive support modules into BYO-Server/src/pyocd_debug_mcp. Exclude brain/,
ux/, services/codex_app_server.py, and services/codex_activity.py. Split mixed
timeouts/runtime-resource code to the minimum BYO/server contract. Remove the
brain-only _brain_sync_timeouts MCP tool from the copy. Preserve every ordinary
MCP tool name/signature/docstring/schema, return/refusal/block prefix, session
artifact name, watcher threshold, and board behavior.

Add focused source-vs-copy schema/contract tests and import-closure tests. Do
not repair unrelated source defects during extraction; record them or route a
real blocking defect through firmcli-fix-bug. Run Ruff, full Pyright JSON, and
the relevant server/service tests inside the BYO project. Update the manifest
with destination hashes and intentional divergences, update the ledger, then
stop and report the next slice.
```

### Execution prompt 3 - copy board data, firmware, packs, and bootstrap

```text
Use firmcli-build, python-change, and the portability rules. Implement only the
data/bootstrap slice.

Copy all approved board profiles/templates, tracked firmware/reference/bug
fixtures, pack manifest/support docs, host_bootstrap.py, stage0_check.py,
setup_host.ps1, setup_host.sh, reference smoke support, pack helpers, and
Zephyr build helper into BYO-Server. Rewrite only path roots and commands needed
for standalone operation. Preserve board facts and origin tags. Do not copy
runs, caches, dist, scratch, parent virtualenvs, or live machine configuration.

Add/run the approved board, discovery, pack, Stage 0, Stage 1, and Zephyr
non-hardware tests from within BYO-Server with the parent package unavailable.
Do not touch real hardware unless the prompt is separately authorized for it.
Update manifest/ledger/docs affected by the slice and stop with the next slice.
```

### Execution prompt 4 - copy R11 BYO benchmark and its tests

```text
Use firmcli-build and python-change. Implement only the R11 benchmark slice.

Copy benchmark_support.py, tests/cases, the R11 result schema/suites, the R11
and Stage 1 harness wrappers, and the approved R10/R11 test closure. Preserve
the benchmark's current Codex-specific semantics, canonical-session
reconciliation, scoring, artifact layout, timeouts, and self-contained prompt
rule. Clearly separate ordinary model-agnostic BYO-server use from the optional
Codex-specific benchmark. Exclude every R12/turnkey/UX/Branch-C/provider-memory
test and fixture not required by R11.

Make all case/firmware/workspace/run roots resolve inside BYO-Server. Add a test
that the benchmark cannot silently consume the parent repo's cases, firmware,
run roots, or package. Run the R11 non-hardware tests and full current BYO test
suite. Update manifest/ledger and stop with the next slice.
```

### Execution prompt 5 - finish packaging, lockfile, and BYO-only docs

```text
Use firmcli-build, python-change, firmcli portability, and the MCP docstring
rules. Implement only the packaging/documentation slice.

Finalize BYO-Server/pyproject.toml, console scripts, package-data rules, and an
independent uv.lock. Remove Rich, prompt_toolkit, turnkey scripts, turnkey
environment variables, skills/playbooks bundles, and other excluded
dependencies. Write the BYO-only README, init.md, stage0_setup.md,
docs/architecture.md, and docs/verification.md. Keep MCP contracts in server.py
docstrings, not sidecars. Document the R11 harness as optional/Codex-specific,
the current in-memory session implementation, cleanup limits, bootstrap
prerequisites, official-vs-alternate board proof, and checkout-vs-wheel support
exactly as verified.

If installed-wheel support is claimed, prove all required board/case/runtime
assets are bundled and resolved. Otherwise state checkout-only limitations
without ambiguity. Do not invent a LICENSE file; surface the missing
authoritative license/notice decision. Run lock/build/install/help/import
checks in a fresh isolated environment, update parent governing docs only if
the user already approved the layout exception, update manifest/ledger, and
stop with the next slice.
```

### Execution prompt 6 - integration closure

```text
Use firmcli-test-suite for the complete BYO-Server non-hardware chain. First
verify every in-scope spec item maps to a completed or explicitly blocked
ledger slice and every planned manifest row has a destination outcome.

From BYO-Server only, with the parent source removed from PYTHONPATH and no
parent editable package available: sync/lock, Ruff autofix+format/check, full
Pyright JSON, full pytest, package build, archive inspection, fresh install,
public --help commands, resource resolution, bounded MCP startup/list-tools/
shutdown, source-vs-copy ordinary tool-schema comparison, and prohibited
reference searches. Audit spawned processes after each subprocess/MCP check.

Any real defect becomes a new ledger slice and goes through firmcli-fix-bug,
then restart this complete integration suite. Do not call the extraction done
while an open slice or unreviewed manifest mismatch remains. Record hardware
and live-provider work as pending with exact handoff commands.
```

### Double-check prompt 1 - independent completeness and isolation audit

```text
Use firmcli-review as an independent reviewer. Review BYO-Server against the
approved extraction spec, process ledger, source/destination manifest, source
commit, and current parent tree. Do not trust prior pass claims.

Recompute file hashes and dependency closure. Prove every include/exclude/split
decision, inspect all imports and path roots, compare ordinary MCP schemas and
stable text contracts, verify no parent-runtime dependency exists, and check
that brain/UX/turnkey/R12/Codex-app-only material is absent. Review docs and
package contents, not only source. Write
BYO-Server/byo-server-extraction_review.md with CLEAN or CHANGES REQUESTED
and precise findings.
```

### Double-check prompt 2 - clean-room build and adversarial test

```text
Use firmcli-test-suite. Copy or mount only BYO-Server into a fresh temporary
location outside the parent repo, create a fresh uv environment, and run the
entire non-hardware validation ladder there. Build/install the artifact into a
second fresh environment. Exercise malformed board configs, missing artifacts,
URL/non-file flash inputs, invalid UART/memory parameters, recover without
confirmation, repeated watcher failures, server disconnect/restart, and R11
session-reconciliation failures using mocks/fakes only.

Verify cleanup after every spawned command. Treat any implicit access to the
original repo, root .venv, root runs, or root package as a failure. Record a
pass/fail matrix and route real failures through firmcli-fix-bug before rerunning
the full clean-room suite.
```

### Double-check prompt 3 - live BYO/server hardware handoff

```text
Use firmcli-test-suite for the exact deployment scenario, but perform destructive
hardware actions only with explicit authorization and the correct attached
board. Run from BYO-Server, not the parent.

For nrf52833dk and nucleo_l476rg separately: run board-scoped host bootstrap,
Stage 0, Stage 1 smoke, MCP Inspector/schema validation, connect/read/state/
UART/symbol checks, deterministic flash-refusal checks, recover-refusal policy,
convergence checks, normal disconnect, and one stock external-agent BYO known-
good case. Run authorized valid flash/recover only where the current test plan
explicitly calls for it. Use timeouts, capture session artifacts, and audit for
leftover provider/MCP/pyOCD/serial/probe processes after each board.

Keep official nrf52833dk proof separate from alternate nrf52840dk evidence.
If hardware/provider access is unavailable, do not simulate a pass: mark the
ledger BLOCKED-HARDWARE with the exact commands and expected artifacts.
```

### Double-check prompt 4 - final consistency and drift audit

```text
Perform the final firmcli-review after all fixes and available hardware checks.
Re-read the approved spec, all review/test matrices, manifest, ledger, root
authority docs, and BYO-Server docs. Confirm the original root product still
passes its non-hardware checks, the new subproject passes independently, parent
docs acknowledge the approved layout exception, proof labels are honest, and
no unrelated user changes were absorbed.

Report verified here, non-hardware verified, pending hardware/provider/fresh-
machine proof, files changed, docs synced, manifest parity/divergence summary,
cleanup status, and open licensing/drift decisions. Return COMPLETE only if all
in-scope items are complete or explicitly handed off and no review finding is
open; otherwise return CHANGES REQUESTED or BLOCKED with the next exact prompt.
```

## Verified

- The current repository authority order, build-plan Stages 0-5, Feature Master
  I01-I05, MCP architecture, Roadmap, current-progress, repo file index, R11
  historical spec, package metadata, server implementation, shared runtime
  services/guardrails, runtime resource roots, test inventory, and bootstrap
  surfaces were inspected for this proposal.
- The current source dependency boundary confirms the BYO/server core can be
  separated without copying `brain/` or `ux/`, but requires deliberate splits
  in `server.py`, `timeouts.py`, `runtime_resources.py`, packaging, environment
  templates, and docs.
- The working tree already contains unrelated untracked
  `markdowns/UX_design/`; this proposal does not modify it.
- The explicitly authorized S1 slice created only standalone configuration,
  provenance, directory markers, and extraction-process documentation. It did
  not copy Python behavior, board data, firmware, packs, tests, or harnesses.
- The explicitly authorized S2 and S3 slices copied the headless runtime and
  data/bootstrap closure. Standalone Ruff and Pyright pass for 50 Python files;
  all 91 focused S3 tests and all 166 current BYO tests pass with the parent
  project disabled. No hardware was accessed.
- The explicitly authorized S4 slice copied 47 byte-identical R11
  module/corpus/harness/test/fixture files and added a standalone parent-root
  isolation test. All 54 focused R10/R11/isolation tests and all 203 current
  BYO tests pass with the parent project disabled. No live Codex or hardware
  benchmark was run.
- The explicitly authorized S5 slice generated a BYO-only lock without Rich,
  prompt_toolkit, Typer, turnkey packages, or turnkey scripts; added the five
  required BYO-only documents and a packaging/docstring contract test; and
  verified a 34-member code-only wheel in fresh isolated environments. Ruff
  and Pyright are clean for 56 Python files and all 207 standalone tests pass.
  No hardware or live provider was accessed.
- The explicitly authorized S6 slice passed the complete corrected clean-room
  integration chain from a copy outside the parent repo. Ruff and Pyright are
  clean for 57 Python files, all 209 tests pass, both distribution archives
  contain 34 files, and a fresh wheel environment contains 55 compatible
  runtime packages. A real MCP stdio client initialized protocol `2025-11-25`,
  listed 20 source-schema-identical ordinary tools, and shut down with no
  surviving process. S6D1 corrected the only manifest mismatch found and its
  independent scoped review is clean.

## Pending verification

- Full D0 approval of the canonical-layout exception, duplicated-maintenance
  model, package identity, asset contract, and licensing disposition.
- P1-P3 exact boundary/planned-manifest freeze and independent plan review.
- R1-R3 independent completeness, adversarial clean-room, and final
  consistency reviews.
- Licensing/notice-file disposition for a standalone distribution.
- Fresh Windows/macOS, exact-board, MCP Inspector, live external-agent, and R11
  hardware proof.
- Restoration or replacement of the missing workflow `frontier.py` helper.
