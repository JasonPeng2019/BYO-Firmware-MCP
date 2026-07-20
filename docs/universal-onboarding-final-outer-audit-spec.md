# Universal Onboarding: Final Outer-Audit Closure

## Goal

Close the final documentation and verification gaps found by the main-agent whole-codebase audit
without changing the accepted generic onboarding authority model or narrowing device support.

## Required behavior

1. The pack-geometry API documentation must describe what the implementation actually exposes:
   all independently described flash, writable RAM, and ROM ranges are retained, while legacy
   scalar fields identify one deterministic programming/default flash and RAM choice.
2. The documentation must not claim that disjoint RAM banks are discarded or joined across gaps.
3. Current evidence must record both fresh-project Luna-medium hardware journeys, including the
   dynamic Nordic official-pack research path, the locally reusable STM32 support path, direct
   validation routing on reuse, capability-limited tool results, and the absence of hardware
   mutation.
4. Encountered live-acceptance failures and their general fixes must remain explicit rather than
   being overwritten by the final green result.
5. The complete locked test, Ruff, Pyright, package/import, and bounded stdio checks plus one final
   adversarial diff audit must pass before completion.
6. Overview may route a saved board directly to validation only when the map is semantically
   current for the active profile and exact support binding. A valid but stale map must route to
   refresh before any validation connection is attempted.

## Non-goals

- No new board, vendor, address, target, or build-system special case.
- No relaxation of exact pack-byte/PDSC/target replay or caller-supplied range prohibition.
- No claim that a pyOCD/CMSIS-Pack server can operate unsupported non-pyOCD architectures or make
  unavailable board wiring and peripheral facts appear.
