# Universal Onboarding Flexibility Follow-up Specification

Status: implementation target

Related documents:

- `docs/universal-device-onboarding-spec.md`
- `decisions/ADR-0003-quarantined-runtime-device-support-onboarding.md`
- `decisions/ADR-0004-artifact-defined-generic-application-authority.md`

## Goal

Make the dynamic path useful for normal firmware work on an arbitrary pyOCD/CMSIS-Pack-supported
MCU without introducing a board catalog as a discovery gate. Starting with a board name, exact part,
datasheet, and selected probe, the agent finds official device support; the server validates and
persists it; ordinary read, debug, register, and application-flash tools then work whenever the
validated support contains the facts those tools require.

## Required behavior

### Discovery remains agent-led

1. The agent performs open-ended online/local research and supplies an official CMSIS-Pack candidate.
2. The server does not search a closed MCU database and does not require a repository manifest entry.
3. The server independently hashes and parses the supplied bytes, proves one exact/wildcard PDSC
   leaf for the user's part, derives the canonical pyOCD target, live-attaches with those bytes, and
   promotes the resulting project-local binding.
4. Agent prose, target strings, checksums, addresses, and ranges are never accepted as authority by
   themselves. The pack bytes and live target are the replayable evidence.

### Evidence-driven generic maps

1. Every flash, writable RAM, read-only memory, and SVD peripheral range described by the exact pack
   leaf is retained as a separate canonical region. Disjoint ranges must not be joined across gaps.
2. One unambiguous boot/default programmable flash is the application-programming domain. Other
   mapped flash remains readable but unallocated until the schema can represent a cross-domain
   deployment safely.
3. All writable RAM ranges are usable for bounded memory operations and vector-stack validation.
4. SVD address blocks are usable for planned register access. If an SVD is absent or malformed,
   register access is unavailable without making setup itself fail.
5. Unknown security, option-byte, recovery, or board-wiring facts are not invented. Generic setup
   creates no unlock, mass-erase, bootloader, or provisioning authority.

### Identity and ordinary application flash

1. Exact and processor-compatible live identity remain distinguishable in the gate stamp and status.
2. Either level may authorize ordinary `flash_application` after the current pack/target/map and
   artifact checks pass. Missing live identity authorizes no hardware action.
3. `flash_bootloader`, `target_unlock`, and destructive recovery continue to require their stronger,
   separately reviewed evidence; compatible identity alone never authorizes them.
4. Status and prompt text must say that compatible identity is sufficient for guarded application
   programming and must not tell the agent to obtain a nonexistent exact-ID record.

### Artifact-defined application allocation

1. A generic application artifact is validated against physical flash, pack erase sectors, target,
   vectors, entry point, and any writable RAM range before permission or backend budget is consumed.
2. The server derives the minimal contiguous erase-sector envelope touched by the artifact. Callers
   cannot supply or widen it.
3. Device blankness is not required. The exact guarded flash plan and user permission authorize
   replacement of bytes in the derived sector envelope; sectors outside it are preserved.
4. The allocation is persisted atomically immediately before programming. Later approved artifacts
   may expand it monotonically to the union envelope, but it never shrinks implicitly.
5. Failed validation performs no mutation and commits no allocation. A programming failure leaves
   the committed allocation in place so retrying the same or a contained artifact is possible.
6. Refresh replays and preserves a valid allocation but cannot create or widen one.

## Failure behavior

- No candidate: return the bounded research continuation.
- Pack/PDSC/target ambiguity or changed bytes: refuse promotion/currentness.
- No live exact or compatible identity: diagnostics only.
- No bounded sector algorithm: allow other supported tools but refuse application flash.
- No SVD: allow setup/read/debug/flash capabilities that do not need it; refuse register addresses
  that lack a mapped range.
- Artifact outside the selected internal-flash domain, invalid vectors, invalid stack, or target
  mismatch: refuse before allocation commit, permission consumption, or backend mutation.

## Acceptance

- Synthetic packs cover unrelated vendors, compatible identity, multiple RAM/ROM ranges, SVD
  peripherals, missing SVD, and tamper replay.
- A nonblank fake target accepts a contained generic application flash, preserves unrelated sectors,
  and records a server-derived allocation before the backend call.
- A later larger artifact expands the allocation; a later smaller artifact does not shrink it.
- Compatible identity permits application flash but still refuses bootloader/recovery authority.
- Focused and complete software verification plus a fresh-repository live-agent run prove that no
  repository board record or prior project manifest is required.
