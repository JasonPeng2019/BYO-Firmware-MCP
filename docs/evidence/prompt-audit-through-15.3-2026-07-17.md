# Prompt implementation audit through 15.3 — 2026-07-17

## Result

All software prompts through 15.2 are implemented and validated after this audit.
The audit found and repaired two M7 defects that prior milestone evidence had not
closed:

1. `board_safety_setup` and `board_safety_refresh` existed only as internal engines
   and were absent from the MCP surface. They are now visible, board/run-scoped A-20
   load-gated tools.
2. Production freshness reused stored application/bootloader artifact hashes. It now
   re-hashes only setup-selected pack and build artifact paths on every guarded write;
   build-only public refresh reconstructs regions from those tracked artifacts.

Contract snapshot v13 and the extraction/architecture records intentionally capture
those changes. The complete software validation is 629 passing tests, Ruff clean,
Pyright clean, successful sdist/wheel build, dependency check, import surface, and
bounded stdio EOF boot.

Hardware prompts are recorded honestly rather than simulated. The Nucleo criteria
completed where applicable. The required nRF52833 criteria remain blocked because the
attached Nordic target reports `FICR.INFO.PART=0x00052840`; it is an nRF52840. No
destructive M8 action was attempted on the wrong board.

## Prompt-by-prompt disposition

| Prompt | Disposition | Primary evidence |
| --- | --- | --- |
| 1.1–1.2 | Complete | kernel/registry/run-state/operations/handshake tests and contract |
| 2.1–2.2 | Complete | board-scoped connection/routing/concurrency tests |
| 3.1–3.2 | Complete | FirmStore/profile schema, atomicity, migration-owner tests |
| 4.1–4.2 | Complete | cache/report/migration/ignore tests and full integration |
| 5.1–5.2 | Complete | AC-numbered adversarial plan-engine and registry tests |
| 6.1–6.2 | Complete | permission/dispatch/pilot MCP lifecycle tests |
| 7.1–7.2 | Complete | session/execution/register matrix and contract tests |
| 8.1–8.2 | Complete | revised Layer-2 surface, reminders, bounds, contracts |
| 8.3 | Conditional hardware result retained | Nucleo passed; nRF52833 blocked by positive silicon mismatch in `m5-hardware-smoke-2026-07-17.json` |
| 9.1–9.2 | Complete | preflight routing and resumable workflow matrices |
| 10.1–10.2 | Complete | research/target/pack staging round-trip and failure matrices |
| 11.1–11.2 | Complete | validation/setup tools, Stage 0 reuse, cache/report/status tests |
| 11.3 | Conditional hardware result retained | Nucleo non-destructive acceptance passed; nRF52833 blocked in `m6-hardware-acceptance-2026-07-17.md` |
| 12.1–12.2 | Complete | region/linker/double-verification property and fixture matrices |
| 13.1–13.2 | Complete | canonical fingerprint/map/refresh drift and atomicity tests |
| 14.1–14.2 | Complete after audit repairs | gate/containment tests plus public safety tools and live artifact freshness tests |
| 14.3 | Conditional hardware result retained | Nucleo map/validate/flash/refresh/sector-containment passed; nRF52833 blocked in `m7-hardware-acceptance-2026-07-17.md` |
| 15.1–15.2 | Complete | destructive disclosure/approval binding, typed recovery, report, gate, and contract tests |
| 15.3 | Safely blocked as specified | wrong Nordic silicon; no plan, permission, or recovery call; see `m8-hardware-recovery-2026-07-17.md` |

## Final commands and results

```text
uv run pytest -q
629 passed, 63 expected legacy-profile deprecation warnings

uv run ruff check .
All checks passed!

uv run pyright
0 errors, 0 warnings, 0 informations

uv build
Successfully built source distribution and wheel

uv pip check
All installed packages are compatible

uv run python -c "import ..."
import surface ok

$null | uv run python -m pyocd_debug_mcp.server
clean bounded EOF exit
```

The remaining hardware blockers require the actual designated nRF52833 DK. They
cannot be corrected in software or safely satisfied by substituting the attached
nRF52840 DK.
