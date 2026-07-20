# Dual-model / dual-board firmware acceptance plan — 2026-07-19

## Objective

Prove the current MCP server can autonomously onboard fresh datasheet-only projects and support
real build, guarded flash, UART verification, and live debugging for eight independent runs:

| Task | Hardware | GPT 5.6 Luna medium | Claude Sonnet 5 medium |
|---|---|---:|---:|
| Freestanding custom bootloader | nRF52840-DK | pending | pending |
| Zephyr multithreaded console | nRF52840-DK | pending | pending |
| ThreadX multithreaded console | NUCLEO-L476RG | pending | pending |
| True bare-metal scheduler/console | nRF52840-DK | pending | pending |

## Isolation and launch discipline

1. Before each run, independently enumerate probes and serial ports and reconcile the intended
   physical board by stable UID. Do not silently reuse stale COM assumptions.
2. Create a new temporary git repository containing exactly one file, `datasheet.pdf`, copied from
   the matching repository-root datasheet. Give it a unique project-local `.firm` artifact root.
3. Register the checkout MCP only for that invocation. Do not change global client configuration.
4. Use exact models and effort: `gpt-5.6-luna` medium without fast mode, and
   `claude-sonnet-5` medium without fast mode.
5. Capture raw JSONL/tool transcripts, CLI/model version, session ID, prompt sequence, and all
   outputs. Preserve every first failure rather than replacing it with a final summary.

## Per-run conversation

1. Send only the task-specific framing prompt and hard constraints.
2. Resume the same session after build and guarded flash; require at least 15 seconds of UART.
3. For console applications, resume again and exercise every command individually, including
   before/after interval evidence and concurrent background output.
4. Resume again for live debug: halt, PC/SP, exported symbols, task breakpoint hit/removal, resume,
   and a running final state.
5. Resume once more to write `journey.md` with plan IDs, artifact hashes, steps, failures, and
   recoveries.

## Independent verification

- Read raw tool transcripts, not just model prose.
- Confirm the RTOS runs use the supplied local NCS or X-CUBE-AZRTOS source before any download,
  package-manager action, or network fetch of an RTOS tree.
- Confirm repeated exact bootloader UART output.
- Confirm every console command and both interval changes from timestamps while console responses
  interleave with background activity.
- Confirm the breakpoint really hits a task function and is removed before resume.
- Confirm the board is left running and no unapproved destructive operation occurred.

## Failure loop

Classify from transcript and live evidence:

1. **Server defect:** write a focused spec and plan, implement the general fix, run a fresh
   `gpt-5.6-terra` high/fast read-only diff audit, vet every finding, run affected tests and the
   complete pytest/Ruff/Pyright suite, then resume the blocked model session.
2. **Agent defect:** do not change server or author firmware. Resume the same session with one
   evidence-specific corrective prompt, then independently repeat the failed acceptance check.
3. **Environment/hardware:** re-enumerate probe/COM identity, provide corrected facts to the same
   session, and repeat only the affected phase.

Use a generous but bounded number of corrective rounds. A genuinely wedged/lost session may be
replaced only with a newly isolated repo and a recorded explanation.

## Exit gate

Do not claim completion until all eight rows are green, every `journey.md` and raw transcript has
been audited, all server defects have a clean Terra audit, and the final complete software suite is
green.

