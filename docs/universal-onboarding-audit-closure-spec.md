# Universal Onboarding Audit Closure Specification

Status: implementation target

## Goal

Close the remaining correctness and generality gaps found by the independent diff audit without
changing the agent-led discovery model. The agent still finds and supplies official support; the
server validates and replays the exact evidence.

## Required behavior

1. A setup continuation accepts a friendly `choice_id` only while the workflow is waiting for a
   friendly choice. Target or pack research payloads are accepted only while the same continuation
   is in `setup_research_required`. An out-of-sequence payload performs no pack staging, hardware
   attach, project write, or run-scoped override.
2. Persisted part-to-PDSC-to-target bindings replay their exact pack identity (pack id, filename,
   digest and matching binding) rather than selecting a provider globally by target name. Multiple
   installed packs may legitimately expose the same normalized target without preventing an exact
   saved board from replaying its own pack.
3. Optional SVD data grants register-write capability only for register spans whose resolved SVD
   access explicitly permits ordinary writes. Read-only or unknown-access spans remain mapped for
   diagnostics but cannot satisfy register writes; write-only spans do not satisfy reads. Missing or
   malformed SVD grants no register capability.
4. These changes remain project-independent, OS-independent, and free of board-specific target,
   path, or address assumptions.

## Verification

Focused tests must prove out-of-sequence continuation rejection before side effects, exact pack
selection in a duplicate-target manifest, and read/write SVD access enforcement. Ruff, Pyright, the
complete locked pytest suite, package/import, stdio smoke, and a renewed GPT-5.6-Terra diff audit
must all pass.

## Second audit additions

5. Every generic live open (setup, validation, reconnect, and ordinary operations) uses the exact
   pack identity replayed from that profile rather than asking the adapter to select globally by
   target name.
6. Project-promoted manifest identity proof must equal the proof derived from the exact pack leaf;
   only the versioned server registry may add separately reviewed identity evidence.
7. A candidate pack containing more than one matching PDSC device entry is ambiguous even when the
   entries share the same case-folded part name, and must be rejected.

## Terra audit closure amendment (2026-07-19)

The final implementation must additionally satisfy these behavior requirements:

1. A saved generic profile is replayed by its complete persisted device-support authority tuple, not re-resolved by part number. Adding another valid pack for the same part must not make an already configured board ambiguous or redirect it.
2. Before a generic application artifact can create or use an allocation, every required execution address (entry, vector table, reset handler) must lie inside the exact erase-sector allocation derived from its loadable content. Physical-device containment alone is insufficient.
3. Schema-v3 geometry represents every independently described physical flash and writable RAM region without joining gaps. Flash mutation remains available only for regions covered by exact verified erase-sector/driver proof; secondary RAM remains usable even when another bank is primary.
4. Every pack-backed session open carries the persisted PDSC leaf as well as pack bytes/digest and canonical target. Temporary pyOCD target registration selects that exact leaf, so unrelated normalized-name collisions neither redirect nor strand the saved board.
