# Pinned-pack setup preflight specification

## Defect

Fresh setup accepts a reviewed STM32L476 catalog entry, but target preflight considers only
pyOCD built-ins and the fresh project's initially empty pack manifest. It ignores the server's
reviewed, pinned repository pack, sends an agent into unnecessary target research, and encourages
host-wide searching even though the exact support is already present.

## Required behavior

- Treat the union of the server repository manifest and active project's promoted manifest as
  locally declared pack targets.
- Select an exact setup target only when it equals the already-resolved reviewed catalog target
  and either is built into pyOCD or resolves through the existing one-target/one-pinned-pack
  verifier.
- Missing, conflicting, or checksum-invalid pack bytes must not become target authority.
- Do not infer targets from MCU strings, broaden catalog matching, download packages, or change
  ordinary built-in-target setup.

## Acceptance

A fresh STM32L476 setup with an empty project manifest routes directly through the exact reviewed
`stm32l476rgtx` target when the repository-pinned pack is present. Focused setup, pack, catalog,
and safety tests plus the complete locked suite remain green.
