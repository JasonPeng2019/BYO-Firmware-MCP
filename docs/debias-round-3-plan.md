# De-bias round 3 implementation plan

## Approach

Carry a provider family with each `ProbeInfo`. Obtain it from pyOCD's registered provider-class map
on the generic Python-API path. For the CLI compatibility path, load a validated executable/argv
specification, accept a provider-qualified UID, and then consult one small packaged alias registry
shared with board matching. Never infer CMSIS-DAP by exclusion. Selection filters on the observed
provider family before using hints only to disambiguate multiple same-family probes.

The simpler alternative of changing the current default to `unknown` was rejected because it would
discard authoritative metadata already supplied by pyOCD and make normal setup less convenient. A
new plugin abstraction was also rejected: pyOCD already owns the provider registry, so mirroring it
in code would add complexity and drift.

## Steps

1. Add strict packaged probe-family alias and CLI-fallback data with a small loader. Resolve the
   executable from an explicit environment override or `PATH`, validate argv as separate strings,
   and never invoke a shell.
2. Extend `ProbeInfo` with `family` and `family_source`; populate it from pyOCD's registered class
   map on the primary path.
3. On parsed CLI output, prefer a provider-qualified UID, otherwise use configured aliases, and
   retain `unknown` when no single match exists.
4. Make board-aware selection require an exact observed family before hint scoring. Remove
   `_infer_probe_family` from the server, consume inventory directly, and derive an active session's
   family from its live probe class rather than the board profile. Improve the fail-closed catalog
   mismatch remedy for an unknown family.
5. Add focused tests and rebaseline only contract/evidence hashes affected by the live code change.

## Smoke test

In one in-process MCP server run, exercise setup inventory twice: once with a fake pyOCD API probe
whose class is registered under a new provider key (generic path), and once with API discovery empty
plus CLI output matched by the packaged compatibility aliases (fallback path). Assert that the
configured executable/argv was used and that both provider families appear correctly in
`setup_overview` probe choices. A third call in that same MCP run uses unrecognized CLI output and
asserts that the live payload reports `probe_family: unknown`. In a separate focused setup gate
test, carry that unknown family to the reviewed-family gate and prove it is refused before profile
commit. Run the focused probe inventory and setup hardware-inventory tests, Ruff, and Pyright.
