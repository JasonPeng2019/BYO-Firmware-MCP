# De-bias round 2 specification

Status: accepted for implementation

## Audit and triage

1. **[TOOLCHAIN] Python part/target naming heuristic ? ACCEPT.** The current prefix/wildcard
   rule is not a pyOCD or CMSIS-Pack identity contract. It can reject provider aliases and accept
   broad collisions. Remove it rather than adding more string cases.

## Desired behavior

- The current automatic setup path accepts a part-to-target relationship only from the
  server-owned reviewed catalog entry selected by `board_type`. This matches the existing
  connection/safety gate and avoids pretending arbitrary target research is authoritative.
- A staged CMSIS-Pack may supply the catalog's exact missing target, but it cannot change the
  reviewed part-to-target mapping. Its enumerated device record proves target presence only.
- A supported target name alone is not evidence that it belongs to the requested part.
- No prefix, suffix, wildcard, vendor, or MCU-family Python branch decides consistency.
- Candidate support and live connection remain required; this change grants no authority.

## Scope and interface

- Replace `_part_matches_target` and every normalized auto-detection/profile/override branch in
  `server.py` with the exact selected catalog entry.
- Simplify `TargetResolver.validate_candidate` to require that exact reviewed target.
- MCP tool names and schemas do not change. Research failures explain that reviewed/provider
  mapping evidence is missing instead of claiming a spelling mismatch.

## Non-goals

- Accepting caller prose as identity authority.
- Guessing aliases, broadening the reviewed catalog, or changing the pyOCD backend in this round.
- Treating an old profile or pyOCD's spelling normalizer as part-to-target authority.
