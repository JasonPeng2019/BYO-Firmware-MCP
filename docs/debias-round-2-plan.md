# De-bias round 2 implementation plan

Status: implemented and focused-verification green

## Approach

Use the one identity source already authoritative on the live automatic setup path: the exact
reviewed catalog mapping resolved internally from exact MCU plus server-hashed datasheet. A staged pack may make that target executable,
but cannot redefine the mapping. Old profiles and pyOCD's spelling normalizer are never mapping
authority. This is smaller and safer than an alias registry or more naming heuristics.

Rejected alternatives:

- More prefix/wildcard cases preserve the defect.
- Agent-supplied evidence prose is not authority.
- A general alias-plugin framework adds ceremony without another provider.

## Steps

1. Remove normalized target auto-detection and all prefix/wildcard consistency logic.
2. Thread the exact reviewed target into detection, override, target-research, and pack-research
   checks; do not trust a legacy profile mapping.
3. Update refusal text, focused tests, contracts, and de-bias report.

## Smoke test

In one in-process MCP setup/research test, prove a reviewed opaque target is accepted, a broad
prefix is rejected, and a staged pack can provide only the catalog's exact target. Exercise the
continued setup through preflight/live-connect/profile commit, and confirm no old normalized,
prefix, wildcard, or legacy-profile authority remains. Run the complete suite, Ruff, Pyright,
build, and stdio smoke.
