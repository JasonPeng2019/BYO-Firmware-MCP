# Contract snapshot history

The active product contract is
`tests/contracts/product-server-tools.json`, introduced at M10 and revised through
version 60 for universal device onboarding flexibility and hardening. It names the active milestone, imports
`tests/contracts/plan-prompt-server-tools.json` by SHA-256, and records the
hardening and prompt-contract evidence that now guards the product. The active
file is a cumulative, explicit delta over the frozen M9 baseline. Version 29
adds symmetric application/bootloader safety-refresh inputs, stable drift
classification/remedy payloads, terminal unsupported-board setup behavior,
and the implementation owners changed for that boundary. Version 30 adds the
always-visible `collect_build_artifacts` MCP tool and records its non-authorizing
manifest/safety-handoff implementation together with coherent Zephyr linker-map export.

Version 53 records universal device onboarding without
changing the public tool schema: exact-part CMSIS-Pack quarantine/promotion,
project-scoped replay, immutable datasheet capture, schema-v3 generic maps,
compatible identity gating, adaptive attach facts, and artifact-defined generic
application allocation. These implementation-owner changes deliberately keep
caller-supplied ranges, targets, permissions, and persisted live authority out
of the MCP contract.

Version 54 binds target enumeration and live attach to the validated bounded
pack digest, avoids retaining rejected pack bytes globally, requires exact live
silicon identity for flash, and persists the minimal generic allocation before
programming so a partial failure cannot leave ownerless modified flash.

Version 55 retains agent-led pack/target discovery while the server validates exact
pack/PDSC/target bytes, maps disjoint pack memory and SVD peripheral evidence, permits
processor-compatible live identity for ordinary application programming, and replaces the
blank-only first-flash rule with a server-derived monotonic artifact allocation. Bootloader,
unlock, mass-erase, and recovery authority remain separate.

Version 60 corrects bounded UART observation semantics: a capture without an expected-text
sentinel now remains open for the requested window, unmatched sentinels are not truncated by the
reopen window, and blocking reads are narrowed to the remaining deadline. The one-open default and
public MCP schema are unchanged.

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
