# H04 production repair request: bind datasheet authority to the requested part

Authorized local firmware validation. The affected target is the local BYO-Firmware-MCP server
and a user-owned development board used only for bounded non-destructive identity reads. No remote
or third-party target is in scope.

## Verified production defect

At server commit `6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876` plus the preserved, accepted
uncommitted H00/H01/H03/H04 repair tree, the public generic setup workflow accepts any syntactically
valid PDF as datasheet authority for the requested MCU. It hashes and captures exact bytes, but
does not establish that the document describes the requested part before profile promotion.

H04 reproduced this twice in fresh isolated roots:

- requested exact part: `STM32L476RGT6`;
- supplied wrong-family document: `Nano_BLE_MCU-nRF52840_PS_v1.1.pdf`;
- independently verified wrong-document SHA-256:
  `c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422`;
- both runs returned `setup_completed`, then `setup_ready`;
- both durable profiles promoted that wrong hash as `datasheet_sha256`.

Evidence:

- `../fresh-experiments/H04_20260725-100800/.agent-workspace/MAIN_SERVER_FAILURE_REVIEW_004.md`
- `../fresh-experiments/H04_20260725-100800/.agent-workspace/evidence/req004/state.json`
- `../fresh-experiments/H04_20260725-100800/.agent-workspace/evidence/req004/repeat_state.json`
- `../fresh-experiments/H04_20260725-100800/cases/req004_wrong_pdf/.firm/boards/h04_stm32_evidence_board.yaml`
- `../fresh-experiments/H04_20260725-100800/cases/req004_wrong_pdf_repeat/.firm/boards/h04_stm32_evidence_board.yaml`
- `../fresh-experiments/H04_20260725-100800/.agent-workspace/evidence/req002/pdf_oracle.json`

## Required behavior

1. Before generic setup can capture/promote a PDF as durable datasheet authority or report setup
   ready, the server must have positive, server-verifiable evidence that the document is applicable
   to the exact requested MCU/part.
2. A valid but wrong-family PDF must fail closed before durable profile promotion. The public
   outcome must name the document/part mismatch or inability to establish the association and give
   an actionable remedy to supply verifiable official datasheet evidence for the requested part.
3. The design must be generic. Do not hardcode the observed boards, vendors, part numbers,
   filenames, document hashes, host paths, or operating system.
4. The correct official STM32 PDF plus exact official pack leaf must continue to pass. Exact
   package/suffix mismatches and a pack without an exact leaf must continue to fail.
5. Preserve exact-byte hashing, immutable content-addressed capture/replay, stale-byte detection,
   existing profile/device-support authority, live identity checks, plan/permission gates,
   returning-board no-network replay, and the accepted attachment-cache behavior.
6. Do not silently infer document authority from a caller string, filename, pack manifest/index,
   or a merely well-formed PDF. If the server cannot prove applicability, report that uncertainty
   and stop before authority promotion.
7. Add focused automated spec tests for wrong-family refusal, correct-document success, and no
   durable promotion on refusal. Add regression tests for replay, stale bytes, exact pack leaf,
   near-part mismatch, and existing catalog-backed behavior.

## Exclusions

- No firmware, fixture, SDK, experiment-spec, or evidence rewrite.
- No board/vendor/hash/filename allowlist.
- No arbitrary PDF size/page/count limits or hostile-input defenses.
- No network requirement for returning-board replay.
- No unrelated refactor or cleanup.
- No commit, push, deploy, flash, erase, unlock, UART, RF, or protection action.

## Preserved working tree

The server intentionally contains uncommitted accepted repairs from earlier suite tests. Preserve
them. Do not reset, checkout, clean, commit, or rewrite unrelated files. The pre-request production
source diff SHA-256 recorded by the main model is
`d7f6bfd3e1de96d1693c612b49153c5d467489ad4f8c957aae1f1a2e1441cda2`.

