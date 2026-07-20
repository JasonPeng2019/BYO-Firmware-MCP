# Generic symbol artifact selection — specification

## Problem

After a server restart, `find_symbol` and `read_memory_symbol` silently fall back to a packaged
reference-board ELF. A fresh project may have just validated and connected the correct physical
board while the symbol tools search an unrelated repository firmware image. Generic boards with no
packaged reference ELF cannot use the symbol tools at all. `write_memory` has the same hidden
dependency for symbol-backed writes.

## Required behavior

1. `find_symbol` and `read_memory_symbol` accept an optional explicit local `elf_artifact`. The
   agent normally supplies the ELF produced by the current project, especially after server restart.
2. `write_memory` and its plan accept a nullable `elf_artifact`. A symbol-backed write plan requires
   that explicit ELF so its meaning is digest-bound; a raw-address write requires NULL and does not
   carry an irrelevant ELF.
3. Explicit ELF paths work for any board, MCU, build system, repository, and host path. Packaged
   reference-board firmware is never silently substituted as the current project's symbols.
4. For the read-only symbol tools, a successful application flash in the same Server Run remains a
   convenience binding when no explicit ELF is supplied. If neither source exists, refusal teaches the agent to pass
   `elf_artifact`; it never reports a misleading symbol-not-found from another image.
5. Every selected ELF is resolved, restricted to a regular `.elf` file, hashed, and rechecked before
   target access. Planned symbol writes bind a non-null explicit ELF digest through the plan engine;
   nullable raw-address plans carry no artifact binding.
6. Symbol address resolution never bypasses memory-map containment, validation, plan, permission,
   or live-session checks. The ELF identifies an address; the server still verifies the actual
   target access range.
7. Tool descriptions document when to pass the parameter, what is returned, and recovery for a
   missing, changed, malformed, or symbol-mismatched ELF.
8. A planned symbol write resolves its address from the same bytes that passed the accepted-plan
   digest check. If the ELF changes during containment, execution refuses before budget,
   permission, or backend use. Once that final check passes, execution uses the already-resolved
   symbol rather than parsing mutable path contents again.
9. A malformed ELF produces a normal typed pre-backend refusal from containment; it never escapes
   as an unclassified handler error.

## Acceptance

- A restarted server with no current flashed-ELF binding can find and read exported symbols from an
  explicitly supplied project ELF.
- No explicit/current ELF refuses before backend access and does not open a packaged reference ELF.
- A changed explicit ELF is refused before target access.
- Symbol-backed `write_memory` plans bind explicit ELF bytes; raw-address plans accept null.
- Replacing the ELF during containment refuses before backend access, while replacing it after the
  final digest check cannot redirect the already-resolved symbol write.
- Malformed ELF input is reported as a plan/containment refusal.
- Existing same-run flash convenience still works.
- Focused tests, contracts, Ruff, Pyright, full pytest, and a fresh Terra diff audit are green.
