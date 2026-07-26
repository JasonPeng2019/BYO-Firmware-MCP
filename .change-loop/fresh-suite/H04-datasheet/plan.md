# Change implementation plan

## Source change list

- Source: `/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/.change-loop/fresh-suite/H04-datasheet/changes.md`
- Goal summary: Prevent generic board setup from promoting a merely well-formed but unrelated PDF
  as durable datasheet authority. Establish and replay a positive, server-verifiable association
  between the PDF bytes and the exact requested MCU/device-support identity before profile commit
  or setup readiness, while preserving the existing exact-byte, exact-pack-leaf, live-identity,
  permission, and returning-board contracts.

## Repository context and assumptions

- Verified architecture and relevant entry points:
  - `src/pyocd_debug_mcp/setup_flow/datasheet_evidence.py` exclusively reads, hashes, captures, and
    replays local PDF bytes, but currently proves only PDF syntax/header, byte stability, digest,
    and canonical content-addressed storage.
  - `src/pyocd_debug_mcp/setup_flow/device_support.py` resolves exact requested parts to verified
    CMSIS-Pack/PDSC leaves or installed built-in target authority. `DeviceSupportCandidate` and
    `BuiltInTargetSupportCandidate` are the server-derived generic support records.
  - `src/pyocd_debug_mcp/server.py::_resolve_setup_support` hashes the caller's PDF and pairs that
    digest with generic support without checking document applicability. The setup profile phase
    later calls `capture_datasheet_evidence`, writes `datasheet_sha256`/`datasheet_ref`, and can
    reach `setup_ready`.
  - `src/pyocd_debug_mcp/firmstore/profiles.py` validates canonical captured references and replays
    exact bytes for persisted profiles; any new durable identity proof must remain server-generated
    and replayable through this boundary.
  - Catalog-backed setup already binds reviewed PDF digests through
    `resolve_reviewed_support_from_datasheet`; the new generic proof must not weaken or replace that
    compatibility path.
- Existing test/build commands relevant to the change:
  - Focused test commands will be authored by the change-loop spec and regression testers and
    executed by the neutral gate.
  - Repository verification surfaces include `pytest`, `ruff check`, and `basedpyright`; run them
    only on the changed modules/tests where the repository-wide baseline is known to contain
    unrelated failures.

## Plan items

### CL-001 — Extract deterministic MCU identity evidence from PDF bytes

- **What to change:** Add one small datasheet-applicability API that uses a maintained, in-process
  PDF parser to extract document metadata/text from the same exact bytes that are hashed. Normalize
  identity text deterministically and require positive agreement with server-derived identity terms
  supplied by the exact requested part and its verified device-support authority. Return a typed,
  replayable proof describing the normalized requested identity, matched server-derived term,
  evidence locus, PDF digest, and parser version. Add the parser as a direct locked dependency if
  no existing maintained dependency provides this capability; do not write a bespoke PDF parser or
  shell out to a host executable.
- **Where:** `src/pyocd_debug_mcp/setup_flow/datasheet_evidence.py`,
  `src/pyocd_debug_mcp/setup_flow/device_support.py` only as needed to expose server-derived
  PDSC family/subfamily/device identity terms, and `pyproject.toml`/`uv.lock` only for the scoped
  parser dependency.
- **Exact intended behavior:** A well-formed PDF is not applicable merely because its filename,
  caller description, manifest row, or digest exists. Applicability succeeds only when extracted
  document evidence positively matches the exact requested part or a model/family identity term
  derived from the exact verified PDSC/built-in support record. A wrong-family PDF and a PDF whose
  text/metadata cannot establish the requested association raise a typed datasheet-evidence error
  identifying the requested part and explaining that verifiable official datasheet evidence is
  required. Malformed, empty, unreadable, or changing PDFs retain their existing finite errors.
  <!-- Assumption: A verified PDSC family/subfamily/device term may establish applicability when an
  official family datasheet intentionally covers multiple package/order-code variants; a generic
  substring guessed only from the caller's part string may not. A built-in target path without
  independent family metadata must obtain an exact normalized part match from the document. -->
- **Must remain intact:** Exact byte hashing, immutable content-addressed capture, canonical
  project-relative references, no arbitrary PDF size/page/count policy, no hostile-input defenses,
  and no board/vendor/hash/filename allowlist. Parser diagnostics must never fabricate a match.
- **Objective verification:** Automated tests create or fixture PDFs whose extracted identity is
  (a) the exact requested part, (b) an authoritative family/subfamily term covering that part,
  (c) a different MCU family, and (d) absent/unextractable. Assert deterministic proof for (a)/(b),
  typed actionable refusal for (c)/(d), identical byte digest/canonical capture behavior, and no
  reliance on the input filename.

### CL-002 — Gate generic profile promotion and readiness on the proof

- **What to change:** Integrate the applicability proof into generic support resolution and the
  final live-tested profile commit. Re-evaluate it from the current PDF bytes after support
  authority exists and immediately before capture/commit, then persist or deterministically replay
  enough server-generated proof data for returning-profile validation. Ensure partial setup can
  request research before pack support exists but cannot promote profile/datasheet authority until
  the proof passes.
- **Where:** `src/pyocd_debug_mcp/server.py::_resolve_setup_support`, the generic setup profile
  phase/capture path, `src/pyocd_debug_mcp/firmstore/profiles.py`, and the narrow setup-flow data
  structures needed to carry the typed proof.
- **Exact intended behavior:** With the H04 wrong-family PDF, public setup stops before durable
  profile promotion and never returns `setup_completed`/`setup_ready`; the response uses the
  existing setup error envelope, names that applicability to the requested MCU was not established,
  and tells the caller to provide verifiable official datasheet evidence. No datasheet reference or
  digest for the rejected document is committed to the board profile. The correct official family
  PDF plus the exact verified pack leaf continues through the existing bounded live check and
  becomes replayable authority. If PDF bytes change between plan/research/support resolution and
  commit, the existing stale-byte refusal remains effective. A returning profile replays from
  captured local bytes with no network and fails closed if the durable proof, captured bytes,
  requested part, or replayed device-support identity no longer agree.
- **Must remain intact:** Exact PDSC leaf/package matching; near-part refusal; pack-manifest
  non-authority; reviewed-catalog hash binding; live probe/silicon/read checks; safety-map gates;
  plan and user-permission gates; no-network replay; non-authoritative attachment cache behavior;
  and all H00/H01/H03 repairs. Do not silently rewrite an existing profile during read-only status.
- **Objective verification:** Public/setup-level automated tests reproduce two fresh wrong-family
  flows and assert refusal, actionable remedy, absence of promoted board profile/datasheet
  authority, and not-ready status. Positive tests assert the correct family PDF/exact leaf reaches
  the same status/profile shape except for any additive server-generated proof. Replay/tamper tests
  assert no-network success from valid captured bytes and fail-closed behavior for changed PDF
  bytes, changed proof, changed part, and changed device-support authority.

### CL-003 — Preserve compatibility and expose actionable failures

- **What to change:** Thread the typed mismatch/unknown-evidence failure through existing setup
  preflight/research/continuation/profile error handling without adding a new privileged operation.
  Keep public tool schemas compatible; improve only the relevant status/code/message/details needed
  for an agent to recover. Support legacy valid profiles by verifying their captured bytes against
  replayed server authority without silently granting missing authority or forcing network access.
- **Where:** Narrow error mapping in `src/pyocd_debug_mcp/server.py`,
  `src/pyocd_debug_mcp/setup_flow/preflight.py` or profile validation only if required, and public
  setup descriptions only if the existing contract omits the recovery step.
- **Exact intended behavior:** The public failure is finite, honest, and distinguishes malformed
  PDF bytes, stale bytes, and an unproven/wrong MCU association. It gives one recovery: supply
  verifiable official datasheet evidence for the exact requested part and repeat setup. Existing
  valid returning profiles do not require network, a new pack download, or mutation merely because
  the proof implementation is newer; they are accepted only when current captured bytes and
  replayed server-owned authority independently establish the association.
  <!-- Assumption: Backward compatibility means replaying and validating legacy evidence, not
  grandfathering a legacy wrong-document association. A legacy profile that cannot establish the
  association remains not ready until the normal setup repair path supplies valid evidence. -->
- **Must remain intact:** Tool names and request schemas; status envelope structure; plan/permission
  semantics; catalog-backed profiles; cache non-authority; and all unrelated setup, debug, build,
  batch, and process behavior.
- **Objective verification:** Regression tests cover legacy valid and invalid profile replay,
  catalog-backed setup, built-in support behavior, setup status without mutation, exact pack leaf,
  near-package mismatch, wrong-family refusal message/code, and unchanged permission/plan
  requirements.

## Out of scope / must not change

- Firmware, fixtures, SDKs, experiment specs, evidence files, or physical hardware state.
- Board/vendor/part/hash/filename allowlists or OS/host-specific paths and commands.
- Network calls in returning-board validation.
- Arbitrary PDF resource caps, sandboxing, path-traversal/ZIP-bomb style hostile-input hardening,
  or any other adversarial-input policy outside the charter threat model.
- Unrelated refactors, cleanup, dependency upgrades, schema redesigns, or changes to earlier
  accepted H00/H01/H03/H04 behavior.
- Tests may be edited only by the spec/regression tester roles; production code only by the doer.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, or generated artifacts.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, and adjacent behavior touched by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, or gate commands.

