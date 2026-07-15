# BYO Server verification

Evidence labels are intentionally separate:

- **Verified here:** directly executed during the current extraction slice.
- **Non-hardware verified:** static, mocked, isolated, build, or packaging proof
  that did not use a board or provider.
- **Historical evidence:** useful parent-repo runs that were not rerun here.
- **Pending:** requires fresh hardware, provider, host, clean-room, or human
  authority.

## Current non-hardware evidence

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

The optional Codex-specific benchmark is:

```text
uv run --locked python -m tests.harness.r11_benchmark --case-id <known-good-case-id>
```

It requires a registered Codex CLI, provider/network availability, the live
board, and the checkout's cases/firmware/run roots. Start with one known-good
case per official board before a frozen suite. Benchmark success is not generic
proof for all MCP clients.

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

## Pending verification

The open R1 findings and R3 review, fresh macOS bootstrap, exact
`nrf52833dk`, a working nrf52840 J-Link attach/VCP, authorized reference flash
and Stage 1, green UART/symbol baseline, live Codex/R11, installed-wheel
operational support (not claimed), and licensing/layout/drift decisions remain
pending.
