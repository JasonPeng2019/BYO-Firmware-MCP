# H01 generated-plan textual `null` preservation

## Verified defect

The live H01 MCP request sent a JSON string, `"hypothesis":"null"`, to
`read_serial-plan`. The production server returned
`Invalid plan fields: {'hypothesis': 'must not be NULL'}`. The exact raw request/response are in:

- `../fresh-experiments/H01_20260724-044242/.agent-workspace/evidence/requests.jsonl`
  request id `101`;
- `../fresh-experiments/H01_20260724-044242/.agent-workspace/evidence/responses.jsonl`
  response id `101`;
- `../fresh-experiments/H01_20260724-044242/.agent-workspace/evidence/terminal_failure.json`.

Main-model reproduction against the installed package proved that generated plan-tool direct
model validation preserves `"null"`, while the actual FastMCP `Tool.run()` path converts it to
Python `None` in the SDK's JSON-string pre-parser because the universal all-NULL contract makes
the textual field annotation `str | None`.

## Expected repair

- Preserve all incoming JSON strings verbatim for every generated plan field whose declarative
  type admits text, including the literal `"null"`.
- Keep actual JSON `null` accepted for the universal all-NULL initialization envelope.
- Keep the pinned SDK's useful JSON-string compatibility conversion for genuinely non-text plan
  fields such as object/array inputs.
- Keep current strict schemas, extra-field rejection, direct/dynamic registration, plan
  initialization, reasoning validation, action-parameter validation, gate, permission, budget,
  visibility, notification, and fallback behavior.
- Do not change non-plan tool invocation behavior, the MCP SDK dependency, hardware behavior, or
  fresh-experiment files.
- Add focused automated tests that exercise the real registered `Tool.run()` boundary, not only
  direct plan-engine or handler calls. Prove string `"null"` reaches placeholder reasoning while
  actual JSON `null` still serves initialization. Add adjacent controls for other JSON-looking
  strings and non-text compatibility parsing.

## Scope exclusions

No firmware, board, provider, hardware, documentation-only, dependency, lockfile, packaging,
cleanup, H00 repair, or unrelated server change.
