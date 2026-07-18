# Contract snapshot history

The active product contract is
`tests/contracts/product-server-tools.json`, introduced at M10 and revised to
version 30 for the visible generic-artifact-collector contract. It names the active milestone, imports
`tests/contracts/plan-prompt-server-tools.json` by SHA-256, and records the
hardening and prompt-contract evidence that now guards the product. The active
file is a cumulative, explicit delta over the frozen M9 baseline. Version 29
adds symmetric application/bootloader safety-refresh inputs, stable drift
classification/remedy payloads, terminal unsupported-board setup behavior,
and the implementation owners changed for that boundary. Version 30 adds the
always-visible `collect_build_artifacts` MCP tool and records its non-authorizing
manifest/safety-handoff implementation together with coherent Zephyr linker-map export.

`tests/contracts/source-server-tools.json` is the final extraction-named
baseline. It is preserved byte-for-byte as historical evidence and is no
longer the active contract entry point. Earlier extraction manifests and
destination hashes remain under `docs/extraction-manifest.json`; their names
describe provenance, not current product ownership.

The active product-contract test verifies the active delta and its frozen
base digest, merges the declared overrides, then compares the resulting tool
schemas and implementation owners to the running composition root. The
separate historical test verifies that the extraction snapshot remains
well-formed and its implementation-owner paths still exist. Future contract
changes must create a deliberate product contract revision; they must not
rewrite or delete extraction evidence.
