# Contract snapshot history

The active product contract is
`tests/contracts/product-server-tools.json`, introduced at M10 and revised to
version 19 for the plan-prompt contract. It names the active milestone, imports
`tests/contracts/plan-prompt-server-tools.json` by SHA-256, and records the
hardening and prompt-contract evidence that now guards the product. The version
19 file is a narrow delta over the frozen M9 baseline: it overrides only the 15
plan-tool contracts and the six implementation owners changed for the exact
nested plan envelope and expanded all-NULL guidance.

`tests/contracts/source-server-tools.json` is the final extraction-named
baseline. It is preserved byte-for-byte as historical evidence and is no
longer the active contract entry point. Earlier extraction manifests and
destination hashes remain under `docs/extraction-manifest.json`; their names
describe provenance, not current product ownership.

The active product-contract test verifies the version-19 delta and its frozen
base digest, merges the declared overrides, then compares the resulting tool
schemas and implementation owners to the running composition root. The
separate historical test verifies that the extraction snapshot remains
well-formed and its implementation-owner paths still exist. Future contract
changes must create a deliberate product contract revision; they must not
rewrite or delete extraction evidence.
