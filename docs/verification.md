# BYO Server verification

Evidence labels are intentionally separate:

- **Verified here:** directly executed during the current extraction slice.
- **Non-hardware verified:** static, mocked, isolated, build, or packaging proof
  that did not use a board or provider.
- **Historical evidence:** useful parent-repo runs that were not rerun here.
- **Pending:** requires fresh hardware, provider, host, clean-room, or human
  authority.

## Current non-hardware evidence

### P4-07 consolidated software checkpoint (2026-07-17)

The post-gap-fix repository checkpoint is green on Windows, branch `Jason-v3-BYO`, baseline
commit `5a98858ca0213cb318b96a835d95f8bee863ba4d`. The final complete run of
`uv run --locked pytest` passed **949 tests** with one explicit environment-dependent skip and 66
legacy-profile warnings. `uv run --locked ruff check .` passed; `uv run --locked pyright` reported
zero errors and warnings; `uv build` produced both the wheel and sdist; and a real MCP SDK stdio
client initialized the server, listed tools, and found `initialization_handshake`.

Tool context was uv 0.11.19, Python 3.12.13, pytest 9.1.1, Ruff 0.15.21, Pyright 1.1.411,
MCP 1.28.1, pyOCD 0.45.0, and pyocd-debug-mcp 0.1.0. Exact commands, intermediate corrective
failures, durations, and final results are machine-readable in
[`evidence/p4-07-software-verification-2026-07-17.json`](evidence/p4-07-software-verification-2026-07-17.json).
No hardware or live provider model was used in this software checkpoint.

S1-S4 established the standalone scaffold, copied runtime/data/bootstrap/R11
closure, ordinary 20-tool schema parity, exact-copy hashes, checkout-owned
resource roots, parent-root isolation, and full current BYO test suite. At S4,
Ruff and Pyright were clean and all 203 standalone tests passed with the parent
package unavailable.

S5 finalized packaging and documentation. Its closeout evidence records:

- an independent `uv.lock` checked against the BYO-only dependency set;
- no Rich, prompt_toolkit, turnkey scripts/environment variables, skills,
  playbooks, brain, UX, R12, or Codex app-server service in the shipped wheel;
- wheel/archive inspection proving that board, firmware, pack, case, test,
  bootstrap, and run assets are not bundled;
- a fresh wheel install with package imports and utility `--help` checks;
- stdio-server entrypoint/import behavior without claiming a conventional
  `pyocd-debug-mcp --help` interface; and
- Ruff, full Pyright JSON, and standalone pytest results.

S6 copied the project outside the parent repo with an empty `PYTHONPATH` and no
parent editable package. The first complete pass found inconsistent manifest
tree digests. S6D1 corrected only those records, documented project-relative
POSIX hashing, and added a regression guard over all 80 recorded destination
outcomes. The scoped fix review is clean, and the complete suite was restarted
from a new copy.

On the corrected restart, the independent lock resolved 68 packages; the
copy-mode development environment installed 64 packages. Ruff made no changes
and is clean for 57 Python files. Full Pyright JSON analyzed those 57 files with
zero diagnostics, and all 209 standalone tests pass. Wheel and sdist each
contain 34 files; the fresh wheel environment contains 55 compatible runtime
packages. Seven supported help surfaces, checkout resource resolution, two
expected wheel resource refusals, prohibited product/import/package searches,
and 11 focused manifest/schema/import/packaging tests pass.

A real MCP SDK client connected over stdio, initialized protocol `2025-11-25`,
listed 20 ordinary tools, matched every live description/input schema to the
frozen source snapshot, confirmed `_brain_sync_timeouts` absent, and closed the
server normally. The `connect` tool was advertised but never invoked. Every
audited S6 command left zero new processes.

R2 later repeated the full clean-room/adversarial matrix on a tree with 212
tests and completed two real board-free MCP initialize/list-tools/shutdown
cycles. The Prompt 9/H1 live tree then fixed a cross-probe-family serial
selection defect and passes Ruff, full Pyright, and all 214 tests.

## Wheel support statement

Installed-wheel board operation is **not supported**. The build proves Python
package metadata, entrypoints, selected imports, and utility help only. It does
not prove connect, flash, UART, pack provisioning, bootstrap, Stage 1, R11, or
run-root behavior from site-packages. All such work must use the complete
`BYO-Server/` checkout.

## Hardware and client proof

The official scoped pair is:

- `nrf52833dk` — exact official Nordic target;
- `nucleo_l476rg` — exact official ST target.

The retained `nrf52840dk` profile/suite is alternate Nordic evidence. It cannot
close exact `nrf52833dk` or official-pair acceptance. Board YAML contains
historical bench annotations, but S5 did not attach a board and does not relabel
those annotations as fresh standalone proof.

For each official board, a separately authorized live handoff must run, in
order:

```text
uv run --locked python host_bootstrap.py --board-id <board-id> --install-packs
uv run --locked python stage0_check.py --board-id <board-id> [authorized artifact/options]
uv run --locked python -m tests.harness.stage1_smoke --board-id <board-id>
```

Then validate MCP initialize/list-tools and board-scoped connect/read/state/UART
and symbol paths, deterministic refusal behavior, convergence blocking, normal
disconnect, and process/probe cleanup. Flash or recover only with explicit
authorization and the reviewed board/artifact.

The optional agent-command benchmark is:

```text
uv run --locked python -m tests.harness.r11_benchmark --case-id <known-good-case-id>
```

Without extra arguments it retains the registered Codex compatibility adapter.
Pass `--agent-config <absolute-json-path>` to launch any operator-selected CLI
or wrapper that satisfies the documented prompt/result/MCP-manifest contract;
see [agent-command-adapter.md](agent-command-adapter.md). It requires provider/
network availability, the live board, and the checkout's cases/firmware/run
roots. Start with one known-good case per official board before a frozen suite.
Benchmark success is evidence for the recorded adapter, not generic proof for
all MCP clients. Unit tests use only local fake executables and never invoke a
real model.

## Prompt 9 / H1 live evidence (Windows)

H1 is **BLOCKED-HARDWARE**, with partial live evidence kept separate by board.

Official `nucleo_l476rg`:

- exact ST-Link UID `066FFF514988525067233337` and metadata-linked `COM12`;
- board-scoped bootstrap and non-destructive Stage 0 passed target availability
  plus a real 32-bit read at `0x08000000` (`0x20001180`);
- standalone Inspector CLI `0.22.0` listed all 20 MCP tools;
- real BYO MCP connected, reported `SLEEPING`, halted, reported `HALTED`, read
  `0x20001180`, resumed, and returned to `SLEEPING`;
- the tracked ELF resolved `stage1_known_value` at `0x08003EC8`, but the live
  value was `0x0800314B`, not the known-good `0x1234ABCD`; no claim is made that
  the tracked reference image is currently flashed;
- bounded UART did not observe `boot ok` on the final pass;
- recover without confirmation refused, two identical URL flash inputs refused,
  the third was convergence-blocked, and normal disconnect closed the session;
- final session artifacts are under
  `runs/20260715T220038Z-8a7323c0`; cleanup found zero BYO-owned survivors.

Alternate `nrf52840dk` (not official Nordic proof):

The failure record below is historical. It is superseded for alternate-board
coverage by the current-tree autonomous acceptance in
`docs/evidence/autonomous-current-tree-acceptance-2026-07-17.md`: setup-first
readiness, local NCS build, safety refresh, validation, guarded application
flash, one-handle UART worker ON/OFF/status/quiet proof, disconnect, and the
strict evidence validator all passed on J-Link UID `683377322` / `COM11`.

- exact J-Link UID `683377322` is visible, but no J-Link VCP is exposed;
- live bootstrap originally misassigned the STM32 VCP. The H1 fix now refuses
  that cross-family match and retains correct STM32 selection; its scoped
  review is clean and 214 tests pass;
- both Stage 0 and real MCP `connect` stalled in the J-Link backend. The final
  MCP call timed out at 60 seconds and shut down with zero surviving BYO
  processes; `nrfjprog --com` also reported J-Link backend errors;
- no Nordic memory, UART, symbol, flash, recover, or session-artifact success is
  claimed.

No successful flash, recover, write-memory/register, breakpoint, Stage 1, or
provider-backed R11 action was run. The operator guide explicitly requires
separate reference-flash authorization, and destructive recover requires its
own authorization.

## Cleanup audit

After every server, Inspector, Stage 1, or R11 subprocess:

1. call MCP `disconnect` when a session exists;
2. allow the owning client/server to shut down normally;
3. check for stale MCP, Python/uv, Codex, pyOCD, serial, vendor-tool, or probe-
   owner processes; and
4. record cleanup as confirmed, partial, failed, or unknown.

Current timeouts and direct handle closes do not prove descendant process-tree
cleanup. Any timeout/interruption leaves cleanup pending until independently
audited.

During H1 the first Nordic Stage 0 outer timeout left two precisely identified
BYO Python processes; they were terminated by PID after provenance validation.
All subsequent board, MCP, and Inspector checks—including both final-tree MCP
sessions—left zero BYO-owned survivors. The pre-existing parent-repo MCP process
was recorded and left untouched.

## Licensing and publication

No authoritative project-root LICENSE or NOTICE file was found for the
standalone copy. `pyproject.toml` therefore makes no license claim. Build/test
of a local artifact is verified separately from permission to publish it;
distribution remains blocked pending the authoritative human decision.

## Verified

S1-S5 evidence is retained. S6 clean-room integration, S6D1 manifest repair,
the mandatory full restart, and the board-free real MCP connection are verified
as described above.

## P4-08 board-free dual-agent contract smoke (2026-07-17)

No hardware was attached or used for this smoke. Both real-agent runs passed
the same bounded scenario and made exactly these advertised MCP calls:

1. `initialization_handshake`
2. `setup_overview` with the literal `no board` answer, returning
   `setup_no_board` and no routes
3. one `board_setup-plan` call with every live plan field null

Neither run submitted a populated plan or called setup, validation, safety,
connection, or hardware actions. Both final answers stayed conversational and
contained no JSON, continuation tokens, or internal board/connection/plan IDs.

- Claude: Claude Code `2.1.76`, exact model
  `claude-sonnet-4-5-20250929`, medium effort, 180-second bound, isolated
  configuration, checkout-scoped strict MCP config, and exact bounded tool
  allowlist. The passing session had no interactive permission block and used
  at most 17,952 of 180,000 logged context tokens. Auto permission was tried at
  medium effort first, but the provider's service-side circuit breaker disabled
  it; that blocked attempt is preserved rather than called a pass.
- Codex: Codex CLI `0.142.2`, exact model `gpt-5.4`, medium effort,
  danger-full-access/approval-never test profile, invocation-scoped MCP
  registration, and 180-second bound. The provider exited zero; recorded usage
  was 108,442 input tokens (89,728 cached), 1,014 output, and 429 reasoning.

Evidence is under
`docs/evidence/agent-contract-smoke-claude-2026-07-17/`,
`docs/evidence/agent-contract-smoke-codex-2026-07-17/`, and the combined index
`docs/evidence/p4-08-agent-contract-smoke-2026-07-17.json`. No Fable, Opus, or
5.6-Sol model was used. P4-08 closes only GAP-20's board-free half; hardware
acceptance remains separately authorized.

Post-run focused verification passed 112 tests; affected Ruff and Pyright
checks were clean, and `git diff --check` reported no whitespace errors.

## P4-09 non-destructive clean-root hardware smoke (2026-07-17)

The setup-only runner passed on the attached reviewed nRF52840 DK using:

- board/profile: `p4_09_nrf52840dk`, display name `P4-09 nRF52840 DK`, reviewed
  type `nrf52840dk`, exact MCU `nRF52840-QIAA`;
- probe: J-Link UID `683377322`;
- UART: stable USB identity `000683377322`, resolved by the server to COM11 at
  115200 baud without a caller-supplied port path; and
- local authoritative `Nano_BLE_MCU-nRF52840_PS_v1.1.pdf`.

The isolated artifact root began without the named profile. Live setup returned
`setup_completed`, recorded the `setup/core-profile-committed-after-connect`
phase, and produced exactly one schema-v2 profile. Same-run `board_validate`
returned `validation_passed_uart_not_configured`; the readiness payload still
proved the stable UART attachment and reported `ready_for_code=true` and
`ready_for_uart_work=true`. The runner's fixed surface started no code phase and
exposed no flash, erase, bootloader-write, unlock, callback, or arbitrary-command
path.

The first run ID was `run-20260718T054738Z-4fb648ba`. A new stdio server started
as `run-20260718T054905Z-751b555f`; before repeated validation it reported the
persisted configuration current but `live_session_ready=false` and
`ready_for_code=false`, with the remedy to connect and run `board_validate`.
Repeating validation restored both to true. Thus disk artifacts did not reopen
the per-run hardware gate.

The exact commands, full MCP timelines, readiness payloads, immutable report
IDs and SHA-256 hashes, profile hash, and source-evidence hashes are in
`docs/evidence/fresh-setup-hardware-2026-07-17.json`.

### Generic artifact collector MCP integration — 2026-07-17

The build-system-neutral collector is now an always-visible MCP tool as well as
a library and CLI. A focused software pass covered discovery, exact schema,
initialization guidance, success/refusal responses, staged byte/hash fidelity,
the `.firm` persistence boundary, metadata-driven Zephyr sysbuild ELF/map
selection, safety handoff compatibility, the active tool contract, and package
entry points: 129 passed and 2 host-link tests skipped. A separate stdio client
listed `collect_build_artifacts`, read its handshake guidance, collected fake
vendor-named ELF/map files, and verified canonical paths and provenance-only
status. Ruff, Pyright, and `uv build` passed. No SDK, network, or hardware was
used.

## Pending verification

The open R1 findings and R3 review, fresh macOS bootstrap, exact
`nrf52833dk`, official-board reference flash and Stage 1, live Codex/R11, installed-wheel
operational support (not claimed), and licensing/layout/drift decisions remain
pending.
