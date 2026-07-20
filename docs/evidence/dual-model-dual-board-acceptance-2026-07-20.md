# Dual-model / dual-board acceptance continuation ? 2026-07-20

## Result

The six required GPT Luna medium cells are green. The two Sonnet cells completed before provider
exhaustion are green. The four remaining Sonnet cells are covered by the prompt's explicit Claude
Usage Carve-Out; none is mislabeled as green. The application-only hardware path was used throughout:
no target unlock, mass erase, manual erase, bootloader-region flash, deployment, commit, or push was
performed by this continuation.

| Task | `gpt-5.6-luna` medium | `claude-sonnet-5` medium |
| --- | --- | --- |
| 1 nRF52840 freestanding bootloader | green | green |
| 2a nRF52840 Zephyr threads/console | green | green |
| 2b STM32L476 ThreadX threads/console | green | usage-carve-out skip after partial setup/authoring |
| 3 nRF52840 bare-metal scheduler/console | green | usage-carve-out skip after partial UART diagnosis |
| 4 nRF52840 broken-project repair | green | usage-carve-out skip; not started |
| 5 STM32L476 broken ThreadX repair | green | usage-carve-out skip; not started |

The detailed carve-out, exact session IDs, transcript names, and SHA-256 values are in
[`claude-usage-carve-out-dual-board-matrix-2026-07-20.md`](claude-usage-carve-out-dual-board-matrix-2026-07-20.md).

## Per-run evidence

Detailed journeys are in
[`dual-model-dual-board-acceptance-2026-07-19/`](dual-model-dual-board-acceptance-2026-07-19/):

- `nrf-bootloader-gpt-5.6-luna-medium-journey.md`
- `nrf-bootloader-claude-sonnet-5-medium-journey.md`
- `nrf-zephyr-gpt-5.6-luna-medium-journey.md`
- `nrf-zephyr-claude-sonnet-5-medium-journey.md`
- `nrf-baremetal-gpt-5.6-luna-medium-journey.md`
- `stm-threadx-gpt-5.6-luna-medium-journey.md`
- `nrf-repair-gpt-5.6-luna-medium-journey.md`
- `stm-repair-gpt-5.6-luna-medium-journey.md`

The STM32 final root UART captures are preserved as `stm-threadx-root-uart-15s.json` and
`stm-repair-root-uart-15s.json`. Both ran for 15.02 seconds. The ThreadX capture proves all commands,
`led 200`, `print 9`, ongoing LED/periodic output, and correct changed intervals. The Task 5 capture
proves final command responses and concurrent output. Live debug evidence in the journeys includes
PC/SP and named scheduler/thread/queue/counter reads, a real task-function breakpoint hit, removal,
resume, and final running state.

For RTOS resource selection, transcript review confirmed local NCS `C:\ncs\v3.3.1` and local
X-CUBE-AZRTOS-L4 `2.0.0` were inspected before any acquisition. No RTOS source-tree download,
`west update`, package-manager fetch, or duplicate RTOS initialization occurred.

All builds used `pyocd_debug_mcp.native_build`; none used the legacy Zephyr-specific helper. The
native helper now permits an agent-resolved exact argv, cwd, environment, and declared outputs for
any build system. Zephyr/west and GNU Make detection are only conveniences. Network access is
inherited by default, compatible local dependencies are preferred, and `--offline` is optional.

## Closed product gaps

- **GAP-36:** removed the closed two-provider build ceiling. Exact agent-supplied argv is the generic
  path, with output verification and honest network-policy evidence.
- **GAP-37:** symbol operations now use an explicit current project ELF or a same-run successful-flash
  convenience binding, never unrelated packaged reference firmware.
- **GAP-38:** ordinary target-state/register failures no longer destroy a valid connection or live
  validation gate; typed transport-loss failures still do.
- **GAP-39:** CMSIS-SVD's absent-access `read-write` default is applied with register/peripheral/device
  inheritance. Present malformed access and non-register address blocks remain excluded. Changing
  semantics invalidates stale generic maps. The refreshed exact-pack map exposed STM32 `RCC.CFGR`
  at `0x40021008`; the guarded live read returned `00 00 00 00`. Evidence is
  `stm-threadx-rcc-cfgr-read.json`.

GAP-39 underwent nine fresh Terra-high/fast focused reviews. Each valid criticism was fixed; the
final review returned `CLEAN`.

The final combined diff audit found two valid GAP-37 follow-ups: a normal rebuild could change an
ELF between the first digest check and the handler's second symbol parse, and malformed ELF parsing
could escape containment as an untyped error. Containment now carries its resolved symbol through
request-scoped operation state, the plan engine rechecks bytes after containment and before
consumption, and malformed ELF input becomes a pre-backend plan refusal. The audit's remaining
swap-and-restore attack was dismissed because it contradicts the charter's compliant-agent,
cooperative-user assumptions and explicit no-staging contract. A final Terra re-review returned
`CLEAN`.

## Final software/distribution gate

- Focused GAP-39 tests: **69 passed**.
- `uv run --locked pytest -q`: **1155 passed, 3 skipped, 85 warnings**.
- `uv run --locked ruff check .`: pass.
- `uv run --locked pyright`: 0 errors, 0 warnings.
- `uv build`: pass; wheel SHA-256
  `b2a45f6a3b033052d258a3ecb282f3b97743fff36ac5bbc9b730715a7c536fb4`; sdist SHA-256
  `308c7ee1cc5dc48ac78bddb59ba844b3e94f7bb430a58e2032c3762b60436fa2`.
- Fresh virtual environment installed the wheel with 55 dependencies and imported
  `pyocd_debug_mcp`, `pyocd_debug_mcp.native_build`, and `pyocd_debug_mcp.server`.
- Fresh-root bounded stdio smoke initialized protocol `2025-11-25`, listed 39 tools, called the
  initialization handshake, made zero hardware calls, closed normally, and left no new process.
