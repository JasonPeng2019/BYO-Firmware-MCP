# Change implementation plan

## Source change list

- Source: `.change-loop/fresh-suite/H03/changes.md`
- Goal summary: Make `build-manifest.json` byte-for-byte canonical on every host by preventing
  Windows text newline translation, while preserving the collector's public schema, provenance-only
  semantics, atomic staging, and refusal behavior.

## Repository context and assumptions

- Verified architecture and relevant entry points:
  `src/pyocd_debug_mcp/artifact_collector.py::collect_artifacts` owns source normalization,
  destination validation, canonical artifact copies, manifest construction, staging, and the final
  `os.replace`; its current `Path.write_text()` call serializes sorted compact JSON plus `"\n"` but
  translates that newline to CRLF on Windows. The MCP `collect_build_artifacts` surface delegates
  to this collector, so repairing this single serializer fixes both direct and MCP callers without
  changing their interfaces.
- Existing test/build commands relevant to the change: focused collector coverage exists in
  `tests/test_server_trust_model_round_3.py` and
  `tests/test_server_trust_model_round_4.py`; the neutral tester roles must record isolated pytest
  commands that exercise their new assertions.
- <!-- Assumption: Canonical means `json.dumps(manifest, ensure_ascii=False,
  separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"` exactly; no UTF-8 BOM, CR,
  indentation, or additional trailing bytes are permitted. This is the already sealed H03
  contract and requires no public schema or API change. -->

## Plan items

### CL-001 — Emit one platform-independent canonical manifest byte sequence

- **What to change:** Replace the text-mode manifest write in `collect_artifacts` with the simplest
  exact-byte write of the existing sorted compact JSON serialization encoded as UTF-8 followed by
  one LF byte. Keep serialization construction local to the collector and add no OS branches,
  settings, helper abstraction, or dependency.
- **Where:** `src/pyocd_debug_mcp/artifact_collector.py`, specifically the
  `build-manifest.json` write inside `collect_artifacts`.
- **Exact intended behavior:** On Windows and POSIX, every successful collection writes a manifest
  equal byte-for-byte to
  `json.dumps(parsed_manifest, ensure_ascii=False, separators=(",", ":"),
  sort_keys=True).encode("utf-8") + b"\n"`. The file ends in `0a`, never `0d0a`; Unicode producer
  and source names remain literal UTF-8; all manifest keys and values remain unchanged.
- **Must remain intact:** Preserve `schema_version`, `owner`, `producer`, `present_roles`,
  `expected_roles`, per-role artifact metadata, canonical file names, lexical role ordering,
  copied bytes/hashes/sizes, returned `CollectionResult`, symlink/junction resolution, absent-or-empty
  destination requirement, staging cleanup, atomic destination replacement, and all existing
  validation/refusal behavior. Do not touch MCP schemas, hardware flows, native-build behavior,
  documentation, dependencies, or unrelated working-tree changes.
- **Objective verification:** A spec tester creates a collector result with multiple roles and
  non-ASCII metadata, reads `build-manifest.json` as bytes, parses it, independently builds the
  canonical byte sequence above, and asserts complete equality, exactly one final LF, no CRLF, no
  BOM, and agreement of artifact hashes/sizes. A regression tester exercises a successful
  collection plus at least one missing-role and one nonempty-destination refusal, proving the
  success payload/schema is unchanged and refusals leave no destination or staging residue.

## Out of scope / must not change

- Fresh-experiment H03 harness, evidence, specification, or result files.
- Any board, serial, debug, flash, reset, RF, setup, plan, permission, or hardware behavior.
- Existing uncommitted H00/H01 repairs and their tests except for executing compatible regression
  checks.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, or generated artifacts.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, and adjacent behavior touched by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, or gate commands.
