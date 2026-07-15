# `R11` Benchmark Cases

`tests/cases/` is the canonical home for tracked `R11` benchmark definitions.

Each case lives under `tests/cases/<case_id>/` and contains:

- `case.yaml`: machine-readable benchmark contract
- `prompt.md`: the exact Codex prompt template for the case
- optional `notes.md`: human-only notes

Tracked suite metadata also lives here:

- `suites.yaml`: named benchmark suites and execution order
- `r11_result_schema.json`: structured Codex result contract

Case kinds in the first pilot:

- `known_good`
- `injected_bug`
- `observability_fault`

Bug source trees stay under `firmware/<board>/bugs/`. The runner copies those
tracked fixtures into gitignored workspaces under `runs/` before Codex edits
them.

The frozen `pilot_v1*` suites remain the scoped acceptance gate for the primary
STM32 + Nordic pair. Additional board-specific suites can live beside them when
the repo carries alternate board profiles that reuse the same R11 case family.
