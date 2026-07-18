# De-bias round 3 specification

## Accepted finding

### [TOOLCHAIN] Unknown debug probes are guessed to be CMSIS-DAP

**Problem.** Setup currently derives a probe family from a short list of description substrings and
uses CMSIS-DAP as the default. An unfamiliar pyOCD probe is therefore mislabeled and may be routed
as a reviewed probe family. Supporting a new pyOCD backend also requires editing setup code.

**Desired behavior.** The generic default is pyOCD's authoritative runtime probe-provider identity.
When pyOCD's Python API supplies a probe object, inventory records the provider key registered for
that probe class. Probe selection and active-session inventory retain that observed identity; an
expected board family is never substituted for it. When only the parameterized `pyocd list`
compatibility fallback is available, the executable and inventory argv come from validated
configuration/runtime resolution, an explicit provider-qualified UID is used first, and configured
text aliases are used only as a clearly labeled legacy fallback. If none is conclusive, the family
is `unknown`; it is never guessed. Setup remains fail-closed and explains that the probe could not
be matched to the reviewed board type.

**Scope.** `probe_inventory.py`, packaged fallback configuration, the shared probe-family data used
by board configuration and legacy CLI parsing, setup inventory in `server.py`, focused
inventory/setup tests, and MCP-facing setup guidance where the mismatch is reported.

**Interface impact.** No MCP tool or input schema changes. Probe choices may now truthfully report
`unknown` instead of the false `cmsis-dap` default. A model continues to select a stable probe choice
ID; it is told to use a probe whose provider matches the reviewed board or repair the reviewed board
support rather than inventing a family.

**Non-goals.** This does not add a new debug backend, permit callers to assert a probe family, or
weaken the exact reviewed-board/probe-family check. It does not replace pyOCD as the current debug
transport provider.

## Round-3 audit triage

1. **ACCEPT [TOOLCHAIN]:** unknown probe descriptions default to CMSIS-DAP. The smallest honest fix
   is runtime provider metadata first, a configured CLI compatibility fallback second, and
   `unknown` otherwise.
