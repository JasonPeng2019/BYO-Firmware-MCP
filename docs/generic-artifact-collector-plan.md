# Generic firmware artifact collector implementation plan

Status: implemented and focused-smoke verified (2026-07-17)

## Scope discipline

Implement one portable collection primitive and reuse it from the existing Zephyr helper. Do not
add provider discovery, vendor-specific builders, arbitrary command execution, MCP tools, or new
authority paths.

## Steps

1. **Collector library and schema**
   - Add `pyocd_debug_mcp.artifact_collector` with a four-value role enum, typed input/result
     records, SHA-256 helpers, deterministic manifest rendering, staging, and safe promotion into a
     new/empty directory.
   - Keep canonical names fixed and manifest paths relative.
   - Validate all inputs and expected roles before creating the final output.

2. **CLI and MCP surfaces**
   - Expose module execution and a `pyocd-collect-artifacts` console script.
   - Accept only explicit typed paths, repeated validated `--expect` roles, an output directory,
     and a short producer label.
   - Print a bounded JSON result to stdout on success. Use concise stderr diagnostics and nonzero
     exit on invalid requests; do not define a second JSON error protocol.
   - Register an always-visible `collect_build_artifacts` MCP wrapper with explicit path fields,
     refusal remedies, canonical returned paths, and a non-authorizing safety handoff. Teach the
     workflow in both its indexed description and the initialization handshake.

3. **Zephyr vertical integration**
   - For sysbuild, resolve the declared `domains.yaml` default domain and its build directory;
     reject inconsistent, escaping, missing, or ambiguous metadata and list candidate ELFs only as
     diagnostics. For plain builds, use only `<build>/zephyr`. Never select by source basename,
     traversal order, or path depth.
   - Include optional `zephyr.bin` discovery.
   - Build a complete sibling generic bundle expecting ELF and MAP. Only after it is complete,
     atomically replace each managed canonical export and install the manifest last. On collection
     failure, leave prior exports and manifest untouched.
   - Preserve the native incremental build tree, existing build ownership marker, and unrelated
     `.gitkeep`/`.gitignore` behavior. Clean only scratch export directories already owned under
     the helper's existing rules, never a same-directory native build tree.

4. **Focused tests**
   - Add collector tests for role combinations, byte/hash fidelity, manifest stability, expected
     roles, missing/empty/duplicate inputs, destination refusal, portable paths, and simulated
     staging failure.
   - Extend Zephyr tests for aggregate-vs-application sysbuild selection, map/bin preservation,
     a default-domain name different from the source basename, a bootloader child, missing-map
     refusal, incremental-tree preservation, and old-output preservation on failed collection.
   - Add CLI subprocess smokes for success and a missing expected map.

5. **Documentation and packaging**
   - Document the artifact-first native-build workflow and explicitly describe the collector as
     non-authoritative.
   - Update README/agent contract and package-script tests without making Zephyr the universal
     recommendation.
   - State the safety handoff precisely: callers pass canonical ELF/HEX/MAP paths explicitly to
     safety refresh; the manifest is not consumed automatically, and raw BIN has no address
     authority.

6. **Verification**
   - Run collector, Zephyr-helper, packaging, safety-linker, safety-refresh, and documentation
     focused tests.
   - Run affected Ruff and Pyright checks plus module/console CLI smokes with temporary fake
     artifacts. No hardware or real SDK build is part of this slice.

## One-way doors avoided

The manifest is deliberately schema-versioned and narrow. It does not define a build-provider API,
does not become safety authority, and does not require native projects to adopt canonical build
commands. Future optional providers can emit this same bundle without changing the safety model.

## Focused verification

- `uv run --locked pytest tests/test_artifact_collector.py tests/test_zephyr_build.py
  tests/test_safety_linker.py tests/test_safety_refresh.py
  tests/test_packaging_contract.py::test_public_scripts_and_dependencies_are_byo_only -q`
  — 106 passed, 2 skipped.
- Collector/Zephyr rerun after the final ambiguity test — 68 passed, 2 skipped.
- MCP integration suite covering discovery, schema, handshake guidance, collection, contracts,
  security, Zephyr, linker, refresh, and packaging — 129 passed, 2 skipped.
- Affected Ruff and Pyright checks — green.
- `uv run --locked pyocd-collect-artifacts --help`, `uv build`, and a real stdio MCP
  list/handshake/collection smoke — green.

All tests use temporary fake artifacts. No SDK, network, hardware, flash, or UART action was used.
