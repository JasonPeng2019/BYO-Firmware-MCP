# Generic firmware artifact collector specification

Status: implemented and focused-smoke verified (2026-07-17)

## Purpose

Provide one small, build-system-neutral way to turn outputs from any native firmware build into a
portable, hash-addressed bundle for later safety inspection. The collector is convenience and
provenance only. It does not build, execute commands, discover allowed memory, open a hardware
gate, or authorize flashing.

The existing Zephyr helper remains optional and must use the same collection rules. Its current
linker-map loss is part of this change: when a Zephyr application image has an application-specific
map, the helper must select it with the matching ELF and preserve it as `firmware.map`; a missing
expected map must fail clearly rather than silently producing an incomplete bundle.

## User contract

The library and CLI accept explicit typed paths for at most one artifact of each role:

- `elf`: an ELF-family debug/executable image, regardless of vendor suffix;
- `hex`: an Intel HEX-family image;
- `bin`: a raw binary image; and
- `map`: a linker map, treated as opaque bytes so vendor encodings remain portable.

At least one of `elf`, `hex`, or `bin` is required. The generic collector never guesses artifact
roles from filenames and never searches a build tree. Producers may declare expected roles; every
expected role must have an explicit, existing source before any output is promoted.

The MCP server exposes the same primitive as the always-visible `collect_build_artifacts` tool.
Its indexed description teaches when to use it, and its response returns canonical paths and a
bounded non-authorizing safety handoff. It never requires a board connection or a plan tool.

Example:

```text
python -m pyocd_debug_mcp.artifact_collector \
  --output-dir build/collected \
  --producer native-project-build \
  --elf out/application.out \
  --hex out/application.hex \
  --map out/application.map \
  --expect elf --expect hex --expect map
```

## Output contract

The output is a new or empty directory containing only the roles supplied plus a manifest:

```text
firmware.elf
firmware.hex
firmware.bin
firmware.map
build-manifest.json
```

Each artifact is copied byte-for-byte. The manifest is deterministic for identical inputs and
producer text and contains:

- schema version and collector owner;
- producer label;
- one record per present role with its canonical relative path, original basename, byte count,
  and lowercase SHA-256; and
- sorted present and expected role lists.

The portable manifest contains no absolute host paths, commands, environment variables, secrets,
gate state, permissions, plans, allowed ranges, or other authority. Native build tools may record
their separate command/version evidence elsewhere.

## Safety and filesystem behavior

1. The collector launches no subprocess, performs no network operation, and accesses no hardware.
2. Inputs must resolve to nonempty regular files. One source cannot be assigned to multiple roles.
3. An output directory must be absent or empty and must not be a symlink/junction. The collector
   never adopts, cleans, or overwrites a nonempty user directory.
4. Sources cannot live inside the destination. Collection is staged in a sibling directory; all
   validation and hashing finish before the staged directory is promoted.
5. Failure removes only collector-owned staging data and leaves sources and any prior output
   untouched.
6. The library returns typed records. On success the CLI prints one bounded JSON summary to
   stdout. On failure it prints a concise diagnostic to stderr and exits nonzero; it does not mix
   partial or error JSON with the success stream.
7. File suffixes and contents are not treated as safety authority. Downstream ELF/HEX/map parsers,
   target checks, safety reconciliation, containment, and gate freshness remain mandatory.

## Zephyr integration

The Zephyr helper remains a build convenience, not the generic collector itself.

- A plain build uses `<build>/zephyr` only when no sysbuild domain metadata is present.
- A sysbuild uses the exact default domain and build directory declared by generated
  `domains.yaml`. It never guesses from the source-directory basename or chooses the shallowest
  ELF. Missing, inconsistent, escaping, or ambiguous metadata fails with the discovered ELF
  candidates listed for diagnosis. ELF, HEX, BIN, and MAP are taken only from that one domain.
- Zephyr collection expects ELF and MAP, preserves HEX and BIN when generated, and writes the same
  `build-manifest.json` as a manual/native build.
- The generic collector still writes a new/empty directory. The Zephyr adapter first creates a
  complete generic bundle in a sibling staging location, then replaces only its managed canonical
  `firmware.*` exports in the helper-owned build directory and installs the manifest last. It does
  not clean a native incremental build tree. Missing expected output leaves prior exports and the
  prior manifest untouched.

Collection proves provenance, not deployment readiness. The current safety-refresh flow consumes
explicit artifact paths rather than this manifest and requires a coherent ELF for linker-derived
containment. A HEX-only or BIN-only bundle may be useful to another consumer, but collection alone
cannot authorize it for guarded flashing; in particular, a raw BIN carries no trusted address.

## Acceptance criteria

- **AC-1:** ELF/HEX/BIN/MAP inputs are copied without byte changes and recorded with correct hashes.
- **AC-2:** Any subset containing ELF, HEX, or BIN works; map-only and empty requests fail.
- **AC-3:** Missing expected, missing, empty, duplicate, or directory inputs fail before promotion.
- **AC-4:** Nonempty, linked, root, source-containing, or source-overlapping destinations fail
  without modifying user files.
- **AC-5:** The manifest is deterministic, portable, sorted, and contains no authority or absolute
  source paths.
- **AC-6:** A handled copy, hashing, or staging failure leaves no final generic bundle and removes
  owned staging data.
- **AC-7:** A sysbuild tree with aggregate and child images selects the coherent default-domain
  ELF/HEX/MAP set from `domains.yaml`, even when its name differs from the source basename.
- **AC-8:** Zephyr preserves `firmware.map` and fails clearly when its expected application map is
  absent.
- **AC-9:** Collection and CLI tests run on temporary files only; no SDK, network, or hardware is
  required.

## Non-goals

- Running arbitrary native build commands.
- Detecting or installing every vendor SDK.
- Deep-validating ELF, HEX, BIN, or linker-map semantics.
- Persisting build artifacts under `.firm`.
- Replacing `board_safety_refresh`, artifact fingerprinting, flash containment, or permissions.
- Adding a provider/plugin framework in this slice.
