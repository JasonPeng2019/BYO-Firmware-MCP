# Contract snapshot history

The active product contract is
`tests/contracts/product-server-tools.json`, introduced at M10 and revised through
version 65 for universal device onboarding flexibility and hardening. It names the active milestone, imports
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

Version 61 makes the native-build helper provider-independent: named Zephyr and Make detection are
only conveniences, while an agent may pass any exact argv/cwd/environment, use network access by
default, and declare verified outputs for arbitrary build systems.

Version 62 makes symbol lookup project-independent: find/read tools accept an explicit project ELF,
symbol-backed write plans digest-bind an optional ELF, raw-address writes use NULL, and a restarted
server no longer substitutes packaged reference-board firmware for current project symbols. The
final audit also makes containment's resolved symbol request-scoped and rechecks the accepted ELF
after containment, so an ordinary concurrent rebuild refuses before consumption and the handler
does not reinterpret changed path contents.

Version 63 preserves healthy debug sessions after ordinary handler errors. Cancellation and timeout
still close an uncertain session, while a recoverable backend refusal no longer forces reconnect and
revalidation.

Version 64 applies the CMSIS-SVD read-write schema default after register, peripheral, and device
access inheritance. Exact verified SVD registers are no longer discarded merely because a vendor
relies on the standard default; explicit access modes and all existing range controls are unchanged.

Version 65 removes the remaining build-provider ladder from the general helper and setup guidance.
Every build system now uses one exact agent-supplied argv/cwd/environment path with inherited network
access by default; the server no longer selects Zephyr, Make, an SDK root, compiler, board target, or
provider-specific output convention. The legacy Zephyr benchmark command remains available only to
existing repository fixtures and is absent from host setup, MCP guidance, and the normal workflow.

Version 66 refuses executable function symbols and unaligned scalar symbol accesses before target
I/O. A normal symbol-kind or width mistake now returns actionable guidance without leaking a backend
assertion or disrupting the live connection; variable reads/writes and function breakpoints remain
unchanged.

Version 67 closes the unsupported setup path without making packs or canned attach attempts a
ceiling. An agent-researched installed built-in pyOCD target can now establish replayable geometry
and compatible live identity without a CMSIS-Pack; agent-researched protocol/mode/clock settings are
live-tested and persisted; unrelated UART adapters are no longer fabricated as probe-mapped. Guarded
function/unaligned symbol writes are also refused before containment and plan consumption.

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

- Contract 68 (post-M10-unsupported-path-remediation): live-tests agent-resolved target/attachment pairs before consuming setup, validates architectural Arm CPUID fields, adds Cortex-M system-control geometry, preserves root-path build protection, and teaches exact research response schemas.
