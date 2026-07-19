# ADR-0003: Quarantined Runtime Device-Support Onboarding

Status: Accepted; supersedes ADR-0002 only for discovery/provisioning

## Decision

An agent may locate and stage an official CMSIS-Pack during setup. Its claims are never authority.
The server quarantines the bytes, hashes them, applies bounded archive/XML parsing, proves the exact
PDSC leaf for the user-supplied ordering code, derives the target identifier, loads only that pack,
and performs a non-destructive live attach. Only the resulting canonical evidence record is persisted.

On every later profile/map use, the server re-hashes the bytes and replays the PDSC/target proof.
The project manifest is an index, not trust: editing it cannot bypass replay. Dynamically onboarded
support may authorize physical read/debug facts after live identity proof, but never deployment
ownership, mass erase, unlock, or a destructive recovery policy.

This permits automatic support discovery without requiring a repository release for every MCU while
keeping agent prose, URLs, filenames, and self-reported checksums outside the safety boundary.

## Consequences

- Runtime network/package discovery is allowed only in the explicit setup research phase.
- No background download occurs during normal connect, validation, refresh, or flash.
- Official checksums/signatures strengthen provenance but do not replace deterministic byte/PDSC/live
  verification.
- Flash requires independent datasheet reconciliation and a separately generated deployment policy.

