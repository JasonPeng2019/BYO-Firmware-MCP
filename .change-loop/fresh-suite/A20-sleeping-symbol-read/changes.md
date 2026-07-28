# Repair request — truthful coherent symbol reads from non-halted targets

## Verified production defect

On server snapshot HEAD `4e1393775167166146c6ee1a0ce310c9747ca3bf`, tracked dirty-diff
fingerprint `6ba9a5e777f0ea38c4af1aeaebfb0df3e72baaca`, production tree
`211f4da4db4a78f8e388ba9fdc646783415ee6eac2bf6f2f6d2e7a5ac8f930d6`,
the public `read_memory_symbol` tool reported a successful
`scheduler_tick_count=0x00000000` while each of two independently routed STM32L476 targets
reported `SLEEPING`. Immediately halting each target and repeating the same symbol/address/ELF
read returned a nonzero live value (`0x000261BA` and `0x000364B7`).

The artifacts were freshly flashed byte-identical canonical images; source/ELF/map hashes,
board/probe/session/server identities, and load ranges were independently reconciled. The same
state-dependent contradiction on both boards excludes one fixture. A successful invented zero is
not an acceptable fallback for an inaccessible non-halted target: the tool must return a truthful
coherent value or an honest error.

## Verified production path

`src/pyocd_debug_mcp/tools/memory.py::read_memory_symbol` resolves and re-verifies the ELF symbol,
then directly calls `MemoryToolServices.read_target_memory` without considering target state.
The process-isolated provider forwards this directly to
`PyOCDSWDInterface.read_memory`. The legacy
`src/pyocd_debug_mcp/services/symbols.py::read_symbol_u32` already demonstrates the repository's
coherent-snapshot pattern: observe state, halt a non-halted target, read, and resume in `finally`.
The public tool does not currently use that pattern and its help text does not disclose any
temporary halt/state-restoration behavior.

## Required observable behavior

1. A public scalar `read_memory_symbol` call must obtain one coherent target snapshot. If the
   target is already halted, read without resuming it. If it is not halted, briefly halt, read,
   and restore execution in a guaranteed cleanup path.
2. The tool must never report success with an unverified fallback value. A halt, read, or
   restoration failure must be returned honestly under the existing server error translation.
   If both the primary operation and state restoration fail, retain both facts rather than
   silently replacing either.
3. Preserve the public argument/return shape, ELF/artifact freshness, symbol width/size checks,
   memory-map checks, event logging, board/session isolation, process deadlines, and all unrelated
   raw-address/write/debug behavior.
4. Update the public tool documentation/help so callers know that a scalar symbol read may briefly
   halt a non-halted core to obtain a coherent value and restores execution before return.
5. Keep the change generic: no STM32, low-power-state name, board, probe, OS, path, toolchain,
   counter, or firmware-specific branch.

## Required automated proof

- Initially halted: one read, no halt/resume, target remains halted.
- Initially running or sleeping: halt then read then resume, with the coherent value returned.
- Read failure after an inserted halt still attempts resume and reports the primary failure.
- Resume/restoration failure is reported; a dual read+resume failure preserves both facts.
- Symbol lookup, artifact re-verification, width/size validation, memory safety checks, and event
  records remain unchanged.
- Adjacent raw-address reads/writes and explicit halt/resume tools do not change behavior unless a
  shared helper is objectively required and directly covered.
- Focused tests, full relevant memory/debug tests, Ruff, Pyright, and `git diff --check` pass.

## Out of scope

- Firmware, fixture, SDK, experiment spec/evidence, or hardware changes.
- A board-specific workaround, arbitrary retry, fabricated zero rejection, or heuristic that
  treats zero itself as invalid.
- Changes to flash, permission, safety-map, plan, or session semantics.
- Commit, push, deploy, or unrelated cleanup.
