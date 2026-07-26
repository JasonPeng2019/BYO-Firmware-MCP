# H03 production repair request — deterministic manifest line ending

Authorized local firmware validation. Scope is the local `BYO-Firmware-MCP` repository and the
host-only artifact-collector behavior; no board or hardware operation is involved.

## Verified defect

The H03 fresh-suite run at
`fresh-experiments/H03_20260725-071507` exercised the sealed
`collect_build_artifacts` MCP tool twice with the corrected standard newline-delimited MCP stdio
transport. Both successful collections wrote `build-manifest.json` with a final CRLF (`0d0a`) on
Windows. The required cross-host canonical representation is sorted, compact UTF-8 JSON followed
by exactly one LF byte (`0a`) and no CR.

Independent main-model byte checks:

- attempt-005 manifest: 486 bytes,
  SHA-256 `3586ae291c91dea11f22b6988e8a692995f5bf4ae01d37d72f554bdf17ad280d`,
  final bytes `0d0a`;
- attempt-006 manifest: the same 486-byte SHA-256 and final bytes `0d0a`;
- independently reserialized canonical bytes: 485 bytes,
  SHA-256 `c0b5766fe30b4e1a6d31df143f5b80bac1a08ebd433240c637fa22e167509bba`,
  final byte `0a`.

The production cause is the text-mode `Path.write_text()` call in
`src/pyocd_debug_mcp/artifact_collector.py`, which translates `\n` to the host newline on Windows.

## Expected repair

Write the manifest as exact UTF-8 bytes so the serialization is identical on Windows and POSIX:
sorted keys, compact separators, `ensure_ascii=False`, then exactly one LF byte. Preserve the
existing manifest schema, values, artifact copies, atomic staging/replace behavior, refusal
atomicity, canonical names, public result payload, and all unrelated server behavior.

Add focused automated coverage that compares the complete manifest bytes to the canonical
serialization and explicitly rejects CRLF. Add adjacent regression coverage for the existing
collector success/refusal behavior. Do not modify fresh-experiment files, do not operate hardware,
and do not commit.

## Charter alignment

This is a narrow correctness and generalizability repair: the same public artifact provenance must
have the same canonical bytes on every supported host. Use the simplest implementation in the
single module that owns manifest serialization; add no platform checks, configuration knobs, or
new abstraction.
