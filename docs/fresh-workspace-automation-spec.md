# Fresh-workspace board automation specification

Status: implementation target for the `Jason-MCP-v2` repair.

## 1. Outcome

A user can start in a repository with no `boards/` directory and no `.firm/`
state, name an attached board, supply its board/MCU identity when known, and
have the MCP server complete setup before any coding agent is allowed to write
application code. For the current acceptance bench the supplied identity is:

- board type: `nrf52840dk`;
- live MCU family/product: `nRF52840` (proved by FICR);
- populated package: `nRF52840-QIAA` (proved by reviewed board documentation, not FICR alone);
- pyOCD target: `nrf52840`;
- probe unique ID: `683377322`;
- UART: `COM11`, 115200 baud; and
- live identity check: `FICR.INFO.PART` at `0x10000100` equals `0x00052840`.

Setup is non-destructive. It may enumerate, connect, read identity and memory
geometry, inspect server-owned evidence, write project-local configuration and
reports, and validate UART availability. It must not flash, erase, recover,
change security state, or write target memory.

## 2. User experience

1. The server asks in ordinary language for the familiar name, board type, and
   MCU only when they were not already supplied by the user.
2. The agent calls `load_setup_tool`, initializes `board_setup-plan` with the
   universal all-NULL request, and submits the exact plan JSON.
3. For a fresh profile, setup explicitly requests the board/MCU identity and an
   authoritative datasheet before accepting a populated setup plan. The current
   acceptance uses `nrf52840dk` and `Nano_BLE_MCU-nrf52840_PS_v1.1.pdf`. Skipping
   either request is a test failure.
4. `board_setup` performs the complete deterministic setup pipeline. It may
   return friendly choices only for real ambiguity. It never asks for an
   internal board ID, connection ID, target token, continuation token, or
   permission enum.
5. A successful response has `status: setup_completed` and reports that the
   profile, attachment cache, safety baseline, and live validation all passed.
6. The calling automation must treat any other status as a stop. It must not
   start a coding agent, build, flash, or UART action.
7. Only after live `board_validate` succeeds in the current Server Run may the
   orchestrator begin the code-writing phase.

## 3. Required features

### F1. Explicit board identity input and verification

- Setup accepts the user-supplied board type separately from the familiar
  logical name.
- A server-owned board catalog supplies the exact target, exact MCU/package,
  probe family, silicon identity register, safe test-read address, UART default,
  reference build, and reviewed safety provenance for supported boards.
- User-supplied identity and catalog identity must agree exactly.
- Live silicon must be read and matched before a profile is committed.
- Family/product identity, package identity, board revision, and target support
  remain separate evidence anchors; setup must not claim FICR proves a package.
- The current `nrf52840dk`, `nrf52833dk`, and `nucleo_l476rg` fixtures are
  supported. Unknown boards route to bounded research without guessing.

### F2. Encoding-independent discovery

- Built-in pyOCD targets are obtained through a Python API, not by parsing a
  locale-sensitive `pyocd list --targets` table.
- Every owned text subprocess uses UTF-8 explicitly and finite timeouts.
- Probe and UART enumeration preserve stable USB identity and never select a
  mutable display label as identity.

### F3. Setup-specific lifecycle bounds

- Planned-action runtime bounds come from the same immutable plan definition
  that renders the NULL guidance. `board_setup` and `board_fix_setup` therefore
  enforce the documented 300-second finite bound; helper steps remain smaller.
- `board_safety_refresh` and `board_validate` receive explicit bounded classes
  rather than falling through to the generic 30-second timeout. Public
  `board_safety_setup` is retired; refresh creates the first map and repairs an
  invalid one.
- Timeout/cancellation leaves no profile half-commit, no open probe/UART, no
  assignment, and no authority state. Reports remain immutable.
- Setup checks cancellation between every phase and before every commit. It does
  not return cancellation while an interruptible worker can still mutate state.

### F4. Profile-free bootstrap connection

- Setup can open a temporary, read-only connection from the planned probe,
  catalog target, and supplied board identity without first resolving a profile.
- `connect_override` also honors its documented run-scoped probe/target
  parameters when a profile is absent; it may construct an in-memory board
  definition but may not persist it.
- Normal `connect` continues to require a committed profile for a named logical
  board.
- Normal `connect` resolves schema-v2 profiles from the selected `.firm` root
  first. Legacy checkout YAML is read-only compatibility fallback, never the
  basis of a clean-root pass.
- Temporary setup attaches reserve the stable probe identity atomically. A lease
  cannot overlap another lease or assignment and promotes atomically to the
  persistent connection used by final validation.

### F5. Transactional staged setup

- Inventory, identity verification, core profile staging, live connection,
  optional profile enrichment, safety construction, final validation, and
  attachment-cache confirmation execute in a deterministic order.
- The core profile is committed only after live target/identity verification.
- Optional fields and the safety reference are committed only after their
  evidence passes.
- Failed candidates remain only in immutable reports.
- Retry resumes the first unverified phase and never repeats a verified
  destructive operation; setup itself has no destructive phases.

### F6. Automatic authoritative safety baseline

- Supported board catalog entries provide reviewed, repository-owned device and
  official-document facts. Caller-supplied allowed ranges are never accepted.
- Live pyOCD flash/RAM geometry must agree with the catalog before promotion.
- A repository-owned deployment policy defines the maximum application envelope,
  bootloader/storage reservations, prohibited ranges, and erase geometry. A
  reference ELF is only a test vector and never grants or widens authority.
- Prohibited regions override broader physical/peripheral regions.
- A missing user build does not block initial setup. It leaves user-application
  flashing unavailable until a later build is inspected and proven contained by
  the unchanged deployment policy. User build symbols may narrow but never widen
  the trusted envelope.
- The sole safety authority is atomically committed `memory_map.yaml`, containing
  reviewed identity, semantic source digests, geometry, partitions, and regions,
  but no gate, permission, plan, or assignment state.

### F7. Setup-completion barrier

- Add an always-readable `get_setup_status` result containing a stable status
  vocabulary and plain-language remedy. It exposes separate
  `configuration_ready` and `live_session_ready` facts.
- `configuration_ready` requires the schema-v2 profile and current safety map.
- `live_session_ready` requires an assigned persistent connection, exact probe,
  current-run live identity proof, and current map digest. Pseudo-connection gate
  stamps are forbidden.
- `ready_for_code` is true only when both facts are true.
- The readiness result is evidence, not persisted authority. Restart resets the
  live part and requires validation again.
- The setup-only runner emits evidence and exits; it never launches arbitrary
  commands. An external orchestrator must assert `ready_for_code` before
  launching a coding agent. A write spy proves no source file was created first.

### F8. Fully automated runner

- Provide a bounded runner for a fresh artifact root that drives real MCP stdio:
  handshake, setup-tool load, NULL plan, populated plan, setup, readiness check,
  and evidence capture.
- The runner takes board identity, friendly name, probe UID, UART, and artifact
  root as explicit command-line inputs. It never silently selects among multiple
  devices.
- The trusted runner accepts no arbitrary command, callback, shell string, or
  executable argv.
- Machine-readable transcripts include versions, exact identities, timings,
  artifact paths, and terminal status without secrets.

### F9. Post-code continuation

- After the agent builds code, safety refresh inspects the new ELF/HEX/map and
  either proves the application partition unchanged or requires full safety
  setup.
- Flash remains guarded by the normal plan, validation, gate, freshness, segment,
  entry/vector, and erase-sector checks.
- UART ON/OFF validation runs only after an application flash succeeds and must
  capture the expected terminal strings.
- Add one plan-guarded bounded serial exchange that opens the resolved UART once,
  writes one exact command, and captures its immediate response without clearing
  it between calls. It uses the normal board lock, byte cap, cleanup, and plan.

### F10. Observability and cleanup

- Every phase records start, success/failure, duration, and typed remedy.
- Setup reports distinguish inventory, catalog match, live identity, connection,
  profile commit, safety commit, validation, cache, and readiness.
- All temporary sessions are closed in one idempotent cleanup path.
- A subsequent call can immediately reuse the probe and UART without host
  intervention.

### F11. Datasheet request and trusted evidence ingestion

- A fresh-profile setup NULL prompt and continuation explicitly ask the agent for
  board type, MCU, and an authoritative datasheet path.
- The plan binds the datasheet path and SHA-256. Setup accepts only a local PDF,
  never a URL fetched during privileged execution.
- The current test must use `Nano_BLE_MCU-nrf52840_PS_v1.1.pdf`; absence, wrong
  type, hash change, or device mismatch fails before attach/commit.
- Catalog packages include strict device-support evidence, official-document
  evidence, deployment policy, document revision/sections, and hashes. `verify2`
  validates them; user prose and live geometry cannot fabricate official facts.

### F12. Permission and exact setup parameters

- Setup parameters bind `board_type`, logical ID/display name, exact MCU evidence,
  stable `probe_uid`, stable UART USB identity plus current port path, baud, mode,
  datasheet path, and datasheet hash.
- A choice that changes any bound parameter requires a replacement plan. The
  paired repair retries only transient work with unchanged parameters.
- Automation begins only after one explicit setup authorization. It never
  synthesizes, persists, replays, or logs structured permission authority.

## 4. Acceptance criteria

- **FA-1** A clean artifact root contains no profile before setup and contains one
  valid `.firm/boards/<logical_id>.yaml` only after live identity succeeds.
- **FA-2** `nrf52840dk` setup completes over MCP stdio with the supplied bench
  identity and never touches the STM32 probe/UART.
- **FA-3** Setup takes longer than 30 seconds in a fake slow-validation case but
  completes below its 300-second bound.
- **FA-4** Locale/charmap settings cannot make the target inventory empty.
- **FA-5** Profile-free temporary attach works; normal named `connect` still
  fails without a profile.
- **FA-6** Wrong board type, wrong MCU, wrong probe, or silicon mismatch creates
  no profile and calls no mutating backend method.
- **FA-7** Safety baseline creation is automatic for each supported fixture and
  fails closed on geometry/evidence conflict.
- **FA-8** `ready_for_code` is false before validation, true after successful
  setup, false after disconnect/restart, and isolated per board.
- **FA-9** The automated runner never invokes its code phase when any setup phase
  is blocked, incomplete, failed, cancelled, or timed out.
- **FA-10** A fake end-to-end run covers clean root through readiness, build
  artifact refresh, guarded application flash, UART ON/OFF confirmation, and
  cleanup.
- **FA-11** Real hardware acceptance performs setup first in a brand-new root,
  records the readiness proof, and only then uses the separately authored app.
- **FA-12** Full pytest, ruff, pyright, package/import, contract snapshots, and
  bounded stdio startup/shutdown are green.
- **FA-13** Fresh setup explicitly requests board/MCU and datasheet; the supplied
  nRF52840 PDF is hashed, validated, and recorded as evidence before setup runs.
- **FA-14** Clean-root setup uses logical ID `nf_board`, ignores a poisoned legacy
  YAML, then normal profile-backed connect succeeds from `.firm` after restart.
- **FA-15** A malicious linker script cannot widen the deployment envelope, and
  an immediate UART response is captured by one serial exchange operation.

## 5. Non-goals and fail-closed limits

- Setup never promises automatic support for an unreviewed arbitrary MCU.
- The runner never invokes `target_unlock`, bootloader flash, mass erase, or a
  caller-supplied safety range.
- Existing profiles are validated or repaired; they are not silently replaced.
- Disk reports never restore a gate, permission, plan, or connection.
