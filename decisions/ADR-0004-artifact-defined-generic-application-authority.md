# ADR-0004: Artifact-Defined Generic Application Authority

Status: Accepted; refines ADR-0003 for ordinary application programming

## Context

Runtime device-support onboarding deliberately supports parts that have never appeared in the
repository. For most such parts, a CMSIS-Pack can prove the exact device leaf, target, physical
memory, flash algorithm, and processor class, but it cannot provide a vendor-independent exact
silicon-ID register. Requiring an exact silicon-ID proof therefore makes the dynamic path unable to
perform the ordinary application flash that onboarding exists to enable. Requiring a completely
blank device has the same effect for normal development boards and firmware upgrades.

## Decision

An agent still performs open-ended support research. The server accepts no target or address claim
as authority: it derives the exact PDSC leaf and canonical target from the supplied pack, replays the
pack bytes, non-destructively attaches, and validates a live processor-compatible identity.

For ordinary application programming, that compatible live identity is sufficient when all of the
following also hold:

- the exact pack/PDSC/target/datasheet binding is current;
- the artifact's target, vectors, entry point, load ranges, RAM stack, and erase sectors are valid;
- every affected sector is covered by the selected pack's bounded sector-programming algorithm;
- the user approves the exact guarded flash plan; and
- the server, rather than the caller, derives and persists the application-sector allocation before
  backend mutation.

The allocation is based on the artifact's required erase-sector envelope, not on device blankness.
It may expand monotonically for a later approved artifact but never shrinks implicitly. Programming
preserves sectors outside that envelope. This authority never implies bootloader, mass-erase,
unlock, option-byte, provisioning, or recovery authority; those remain separately gated.

Pack-described RAM, ROM, flash, and SVD peripheral address blocks are mapped as independent regions
so generic tools degrade by evidence availability rather than by a single-board memory model.

## Consequences

- Fresh and previously programmed boards follow the same general application workflow.
- Parts without a vendor-specific exact ID register remain usable for application development.
- The user-approved artifact can replace unknown prior bytes in its affected erase sectors. This is
  an intentional usability tradeoff; the plan describes the artifact and programming consequence.
- A selected target with only processor-compatible live proof has weaker anti-misbinding assurance
  than an exact silicon-ID proof, so bootloader and destructive recovery stay unavailable.
- Missing SVD, flash algorithm, vector evidence, or physical containment disables only the dependent
  capability and returns a specific remedy.
