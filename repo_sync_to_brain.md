# Repository Sync to New Brain Spec

## Purpose

This implementation specification describes the repository changes required to synchronize the
current server with `New_Brain_Spec.md`. It covers only the approved mismatches: removing public
`board_type`, keeping the local datasheet while making its digest server-owned, and correcting stale
post-build guidance. It also locks in the already implemented native-build and optional fallback
behavior through contracts and tests.

## Required outcomes

1. Setup has no public or agent-supplied `board_type`.
2. Reviewed support is resolved internally from the exact MCU part number and the server-computed
   digest of the supplied official datasheet.
3. A custom PCB can reuse reviewed MCU/device support without a user-created board definition.
4. The public setup contract accepts the datasheet path but not a caller-provided datasheet digest.
5. Ordinary builds never direct the agent to run `board_safety_refresh` merely because build artifacts
   changed.
6. Existing safety maps, deployment partitions, flash containment, and authorization behavior do not
   change.
7. Existing native-build-first and optional Zephyr/vendor fallback behavior remains intact.

## 1. Remove `board_type` from the public setup contract

### Plan and tool schemas

- Remove `board_type` from `board_setup-plan` action parameters.
- Remove `board_type` from `board_setup` and `board_fix_setup` public parameters.
- Remove it from generated NULL-envelope guidance, populated examples, action templates, static-client
  fallbacks, and contract snapshots.
- Reject obsolete calls containing `board_type` as unknown-field schema mismatches rather than
  silently ignoring the field.

### Setup state and routing

- Remove caller-owned `board_type` from `SetupUserInput` and related setup request models.
- Remove board-type choices from `setup_overview`, `continue_setup`, accepted-response templates, and
  other public setup routing output.
- Do not ask the user or agent to choose between reviewed catalog identifiers.
- Preserve `display_name` as the user-owned familiar name of one physical board.

### Internal reviewed-support resolution

- Add or adapt an internal resolver that takes the exact MCU part number and the computed datasheet
  digest.
- Match only reviewed support that explicitly accepts that MCU identity and datasheet digest.
- Preserve the existing rule that the user's exact MCU part number is not guessed, rewritten, or
  widened by prefix or wildcard matching.
- Require one unambiguous reviewed result. Return a typed support/evidence error when there are zero or
  multiple valid results.
- Use the resolved internal record everywhere the setup flow currently looks up
  `user_input.board_type`, including target resolution, datasheet validation, probe compatibility,
  profile commit, reviewed evidence loading, safety-map creation, and research-response validation.
- Internal catalog identifiers may remain in repository-owned evidence and diagnostics, but they must
  not become public setup inputs or agent-selected routing values.

### Custom PCBs

- Do not reject a board merely because its familiar name or PCB design is not a reviewed development
  board name.
- Permit it to use an unambiguous reviewed MCU/device record selected by exact MCU and datasheet.
- Continue to refuse writable setup when the MCU/device itself lacks complete reviewed evidence.

## 2. Keep the datasheet input and make hashing entirely server-owned

### Public contract

- Keep `datasheet_path` as an initial setup input.
- Remove `datasheet_sha256` from public setup plan and action parameters.
- Do not ask the user or agent to calculate, copy, or cross-check a digest.

### Server processing

- Resolve the supplied path and apply the existing local-PDF validation.
- Compute SHA-256 from the actual file bytes inside the server.
- Use only that computed digest for reviewed-support matching and reviewed evidence loading.
- Persist the accepted digest in the board profile and applicable setup/evidence records.
- Preserve the existing behavior that a changed or unaccepted document fails with a typed evidence
  error.

## 3. Correct stale post-build guidance

- Replace the `get_setup_status` build-guidance statement that requires `board_safety_refresh` after a
  build.
- State that build guidance is advisory and non-authorizing.
- Route ordinary builds to optional artifact collection and then the applicable flash plan.
- State that the flash plan binds the artifact and the flash action revalidates bytes and containment
  before target mutation.
- Reserve `board_safety_refresh` guidance for missing, invalid, old, or changed stable safety evidence.

## 4. Preserve build and fallback behavior

The current behavior remains normative and must stay covered by tests:

- The project's existing native IDE/CLI build and compatible local SDK/toolchain are primary.
- An appropriate Zephyr or vendor-specific helper may be returned only as an optional fallback when no
  suitable native workflow is available.
- Returned build and dependency commands are advisory and are never executed by the server.
- Guidance carries no plan, permission, gate, safety, or flash authority.
- The server does not silently install, replace, upgrade, or reconfigure the user's toolchain.
- `collect_build_artifacts` continues to collect only explicitly named outputs and grants no authority.

## Affected repository surfaces

At minimum, inspect and update:

- `src/pyocd_debug_mcp/guardrails/plan_defs.py`
- `src/pyocd_debug_mcp/setup_flow/preflight.py`
- `src/pyocd_debug_mcp/setup_flow/board_catalog.py`
- `src/pyocd_debug_mcp/setup_flow/reviewed_evidence.py`
- `src/pyocd_debug_mcp/tools/setup.py`
- `src/pyocd_debug_mcp/server.py`
- `tests/contracts/product-server-tools.json`
- Setup, plan, prompt, static-client, safety, resource-binding, and fresh-workspace tests that currently
  construct or assert setup parameters

Internal repository-owned evidence schemas may retain a catalog identifier where required for
provenance. Removing or renaming those internal evidence fields is out of scope unless necessary to
eliminate a public dependency on them.

## Contract and migration expectations

- The public MCP schema change is intentional: `board_type` and caller-supplied `datasheet_sha256` are
  removed from setup actions and plans.
- Existing persisted profiles and safety maps remain readable without rewriting their safety content.
- If persisted internal provenance includes a reviewed catalog identifier, continue reading it as
  repository-owned evidence.
- Do not migrate a catalog identifier into a new user-visible field.
- Regenerate or deliberately update checked-in contract snapshots after the runtime schema is correct.

## Verification requirements

Add or update tests proving:

1. Setup plan and action schemas do not expose `board_type` or `datasheet_sha256`.
2. Setup succeeds with familiar name, exact MCU, datasheet path, and the existing applicable connection
   and UART parameters.
3. Setup never requests a board-type choice.
4. The server hashes the datasheet bytes and persists the accepted digest.
5. A caller-provided obsolete `board_type` or `datasheet_sha256` is rejected as an unknown field.
6. Zero reviewed MCU/datasheet matches produce a typed support/evidence error.
7. Multiple reviewed MCU/datasheet matches produce a typed ambiguity error.
8. A custom familiar board name can reuse an existing reviewed MCU/device record.
9. Exact target, probe, safety-map, deployment-partition, and flash-containment behavior remains green.
10. Ordinary post-build guidance does not request `board_safety_refresh`.
11. Stable-evidence failures still recommend `board_safety_refresh`.
12. Native build guidance remains primary, while Zephyr/vendor helpers remain optional and
    non-authorizing.

Run the focused setup, plan, contract, safety, and server-resource tests first, followed by the full
verification suite required by the repository workflow.

## Non-goals

This synchronization must not:

- Redesign stable application or bootloader partitions.
- Replace stable deployment policy with artifact-only policy.
- Add persistent custom protection overlays.
- Add resident-bootloader setup questions.
- Change mapped-memory read or write behavior.
- Expand or redesign `serial_exchange`.
- Change internal stable UART identity behavior.
- Add `serial_id` to the user-supplied product contract.
- Change plan, permission, retry, gate, or flash authorization semantics.
- Automatically execute build commands or mutate toolchains.

## Completion criteria

The repository is synchronized when the public schemas, runtime setup flow, guidance, contract
snapshots, tests, and `New_Brain_Spec.md` agree on all required outcomes above and the relevant
verification suite passes without weakening existing safety or flash-containment guarantees.
