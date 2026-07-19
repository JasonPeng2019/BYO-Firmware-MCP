# Reviewed setup connection policy specification

## Defect

Fresh setup resolves reviewed connect policy (`debug_connect_mode`, debug clock, safe identity
addresses), but its pre-commit live probe opens with `board=None`. The shared backend therefore
drops the reviewed under-reset/clock settings. On the attached NUCLEO-L476RG this reproducibly
fails twice at `DebugPortStart` with ST-Link DP wait before a profile can be committed.

## Required behavior

- Construct an ephemeral, server-owned `BoardConfig` from the resolved reviewed catalog plus the
  selected live probe and pass it to the ordinary shared backend for pre-commit connection.
- Preserve the actually selected compatible probe family; do not force a reference-board probe.
- Verify the opened handle's stable probe identity still matches the immutable setup selection
  (allowing only the existing decimal zero-padding equivalence) before any read or commit.
- The J-Link serial-normalization retry must compare the retry probe identity before opening it, so
  a different sole-visible same-family probe receives no backend initialization or halt.
- Apply only reviewed connection/identity facts. Do not persist new authority before live checks,
  infer recovery, unlock, erase, or add another connection mechanism.
- Keep module-backed and pack-backed boards on the same generic connection path.

## Acceptance

Focused tests prove reviewed mode/clock/identity reach the backend before profile commit. The
attached STM32 completes fresh setup without recovery or destructive action, and all software
checks remain green.
