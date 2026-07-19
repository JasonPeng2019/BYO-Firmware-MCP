# Run-scoped Current Symbol Artifact Specification

## Problem

Fresh generalized builds are collected and flashed from project-owned paths, but the always-visible
symbol tools still resolve only repository-shipped reference firmware. After a successful guarded
application flash, `find_symbol`, `read_memory_symbol`, and symbol-backed `write_memory` therefore
cannot inspect the firmware that is actually running. The uncaught missing-reference error also
tears down the live debug session instead of returning a bounded refusal.

## Required behavior

- After a successful `flash_application`, associate that board with the exact ELF used by the
  guarded flash (or the required same-stem ELF companion for a HEX) for this server run only.
- Bind the ELF path and SHA-256 digest. Recheck the digest before every symbol lookup so later file
  replacement cannot silently change symbol addresses.
- Never persist this association and never treat it as gate, identity, range, partition, plan, or
  permission authority.
- Do not replace the association on a failed/refused flash or on bootloader flashing.
- Keep the repository reference ELF as the compatibility fallback when no run-scoped application
  ELF has been flashed.
- Convert a missing, unreadable, or changed symbol ELF into a normal
  `memory/symbol-artifact-unavailable` refusal without touching the backend or disconnecting the
  session.

## Safety boundary

Symbol metadata only selects names and addresses. Existing safety-map containment checks still run
before every read/write, and breakpoint plans continue to bind their explicit current ELF.
