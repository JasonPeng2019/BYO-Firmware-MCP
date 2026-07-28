# Main-model verification — A23 signed-HEX companion repair

Verified by the authoritative suite manager after the serialized change-loop.

## Accepted repair boundary

- Main-authored plan:
  `plan.md`, SHA-256
  `112f77f7d2e524bd540f5a9cd1779fa429bda1ecb7ca7fc2c1958f1470a1b42c`.
- One-time adversarial plan review:
  `plan-review.md`, APPROVE.
- Production file changed by this repair:
  `src/pyocd_debug_mcp/safety/linker.py`, SHA-256
  `01a8b04f9622a98e80984b5eb37a80bd2d61fc3e7e81541827d77376352148d2`.
- Final role ownership is disjoint:
  - specification tester:
    `tests/test_a23_signed_hex_spec.py`;
  - regression tester:
    `tests/test_a23_signed_hex_regression.py`.

The final ownership-corrected neutral gate supersedes the earlier iteration in which a regression
role temporarily touched the specification role's file.

## Neutral and main checks

- Neutral specification command: PASS, 12 tests plus 8 subtests.
- Neutral regression command: PASS, 1 test.
- Main replay of both new files: PASS, 13 tests plus 8 subtests.
- Existing `tests/test_server_trust_model_round_4.py`: PASS, 9 tests.
- Focused Ruff: PASS.
- Pyright: PASS, 0 errors.
- `git diff --check`: PASS.

The exact canonical A23 artifact bundle was then evaluated through the production extractor:

- HEX SHA-256:
  `e902bd9e662f0a1716adc8004eb47925424e33b0304fef38e51856ddccbec3fa`;
- ELF SHA-256:
  `0c05e067992ca49b57ca7ac1679858b77b3c41647faaf5a48ae6e3034fa4ccd7`;
- MAP SHA-256:
  `bd39e3f9d893ce66dbc63bc7f5cfb12f63ea007d0727ba430e9a5524f0d7a808`;
- observed output:
  `A23_REAL_BUNDLE_ACCEPTED`;
- retained evidence:
  `flash_partition=('0xc200','0x40000')`,
  `hex_ranges=[('0xc000','0x38dcb')]`.

This proves the exact rejected same-build bundle reaches the unchanged downstream safety-policy
boundary with its real HEX ranges intact. It does not replace the required live public-MCP retest.

## Full-suite disclosure

`uv run --locked --no-sync pytest -q` completed with:

- 331 passed;
- 4 skipped;
- 168 subtests passed;
- 2 failed.

Neither failure is caused by the A23 `linker.py` change:

1. `tests/test_h00_repository_contract.py` embeds an obsolete baseline commit
   (`6f3da0...`) while the repository HEAD is now `4e139377...`. This is stale test-contract
   evidence, not an A23 production regression.
2. `RoundThreeRegressionTests.test_r3_07_exact_device_ambiguity_refuses` exercises the earlier
   dirty `device_support.py` repair and now receives
   `verified PDSC ancestry could not be parsed` for deliberately non-PDSC payload bytes before it
   reaches the expected multiple-match refusal. This is unrelated to A23 and is separately
   pending main classification; it is not concealed or folded into this repair.

## Repaired production snapshot

- Git HEAD:
  `4e1393775167166146c6ee1a0ce310c9747ca3bf`.
- Tracked dirty-diff fingerprint:
  `6ba9a5e777f0ea38c4af1aeaebfb0df3e72baaca`.
- Production `src/` tree SHA-256:
  `211f4da4db4a78f8e388ba9fdc646783415ee6eac2bf6f2f6d2e7a5ac8f930d6`.
- Manifest:
  `.agent-workspace/server-snapshot-20260727-a23-signed-hex-repair.json`,
  SHA-256
  `4464e932ee41973fe8f61d854645573543cae29a871591ed91d5876900dd8a3d`.

No commit, push, deploy, or hardware action was performed by the repair workflow.
