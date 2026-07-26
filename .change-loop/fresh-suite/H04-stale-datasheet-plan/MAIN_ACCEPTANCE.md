# Main acceptance — H04 stale datasheet plan binding

## Verdict

`ACCEPTED`

The change-loop is green and the main-model independent oracle passes.

## Bound plan and review

- Plan SHA-256:
  `6c0d8e607e5fa1eaead62f35cbfc5054b911876279c9113a16083e91f907e4be`
- Independent plan review verdict: `PROCEED`
- Persistent implementation doer:
  `019f9bf1-e89e-7b63-a9f2-3d495f542d23`, `gpt-5.6-terra`, medium reasoning
- Persistent spec tester:
  `019f9bf3-1e8a-78f2-a642-f87f86e5cac9`, `gpt-5.6-terra`, medium reasoning
- Persistent regression tester:
  `019f9bf6-4921-7f80-9b03-8a1e8f027fd8`, `gpt-5.6-terra`, medium reasoning

## Production review

The focused production diff is exactly the planned declarative opt-in in
`src/pyocd_debug_mcp/guardrails/plan_defs.py`:

```python
artifact_binding_field="datasheet_path",
artifact_binding_suffixes=(".pdf",),
```

No second digest implementation, setup-specific cache, immutable-copy subsystem, board/vendor/OS
branch, hostile-input hardening, arbitrary limit, dependency, or unrelated production edit was
introduced by this repair.

This remains aligned with `../.codex/design_charter.md`: it catches a verified stale-plan mistake
by a compliant but fallible agent, while unchanged PDFs retain normal primary and paired setup
behavior.

## Neutral gate

Neutral report SHA-256:
`c7928fc22ebc72b07104c37d5fc3cdb0142f5546b71fdad5736e59a716921742`

- Spec suite: 4 tests, exit 0
- Regression suite: 1 test, exit 0
- Both commands passed in the same final neutral iteration.

## Independent main-model oracle

The exact installed public runtime was refreshed from the working tree:

`BYO-Firmware-MCP/.h01-venv-batchstrict/Scripts/pyocd-debug-mcp.exe`

A fresh isolated public MCP case accepted a plan for the official STM32 PDF, advertised the
standard `datasheet_path` byte-binding reminder, then flipped byte 128 after plan acceptance. The
subsequent public `board_setup` call returned:

> The selected artifact changed after plan acceptance. The plan was invalidated before execution;
> submit a replacement plan for the current bytes.

The public status remained `setup_not_ready`, and no durable board profile was created.

- Public state SHA-256:
  `000524cc9dce18e49aa470102d835d762b1f318656115444b183e13f56e9fa1a`
- Raw MCP JSONL SHA-256:
  `6a6c7ea91ece6828ffe21be18c78ecc646278b0bd054cf178d92117aac7ae584`

The main model also ran 51 focused/adjacent host-only tests covering stale primary and paired
plans, missing artifacts, unchanged controls, datasheet applicability, attachment-cache behavior,
and the strict MCP boundary. Result: 51 passed, 0 failed, 0 errors.

- Oracle test log SHA-256:
  `9034803e27493d69b465dbb21a90f315c51d6da87a5c3605393a65ff6a56d28e`
- `compileall`: PASS
- `git diff --check`: PASS

No flash, erase, reset, unlock, protection change, memory write, UART open, or RF action occurred.

## Artifact audit

The accidental preparation changes to the default `.change-loop/changes.md` and
`.change-loop/plan.md` were restored byte-for-byte to their prior tracked content. The consolidated
design-charter log was reconstructed from `controller.log` and role reports after a regression-role
write replaced the earlier checkpoint entries; this repair-artifact correction does not alter
production code, tests, or gate results.
