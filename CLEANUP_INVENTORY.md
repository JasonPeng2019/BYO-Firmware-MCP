# Shipment Cleanup Inventory

Inventory generated before cleanup deletion on branch `chore/ship-cleanup`. It covers **815** tracked/untracked repository paths. Existing working-tree moves/deletions are recorded as status and were not created by this inventory step.

## Classification summary

| Classification | Count |
|---|---:|
| `keep-customer-doc` | 3 |
| `keep-runtime` | 89 |
| `keep-shipment` | 7 |
| `remove-archived-doc` | 19 |
| `remove-backup` | 1 |
| `remove-captured-test-output` | 271 |
| `remove-internal-agent-doc` | 4 |
| `remove-internal-artifact` | 1 |
| `remove-internal-doc` | 4 |
| `remove-internal-history` | 1 |
| `remove-internal-plan` | 32 |
| `remove-internal-validation-doc` | 22 |
| `remove-run-artifact` | 44 |
| `remove-vendor-reference` | 2 |
| `uncertain-build-config` | 1 |
| `uncertain-ci-test` | 140 |
| `uncertain-current-design-doc` | 32 |
| `uncertain-customer-setup` | 5 |
| `uncertain-data-or-doc` | 2 |
| `uncertain-doc` | 2 |
| `uncertain-dynamic-config` | 5 |
| `uncertain-internal-script` | 16 |
| `uncertain-product-spec` | 1 |
| `uncertain-runtime-code` | 5 |
| `uncertain-runtime-data` | 4 |
| `uncertain-test-asset` | 102 |

## File-by-file inventory

| Path | Initial status | Classification | Reason |
|---|---|---|---|
| `.env.example` | `tracked-present` | `keep-shipment` | Customer setup, dependency, lock, or build metadata required by shipment. |
| `.firm/boards/nf_board.yaml` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/boards/p4_09_nrf52840_dk.yaml` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/safety/nf_board/memory_map.yaml` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/safety/nf_board/safety_report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/safety/nf_board/source_manifest.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/safety/p4_09_nrf52840_dk/memory_map.yaml` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/safety/p4_09_nrf52840_dk/safety_report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/safety/p4_09_nrf52840_dk/source_manifest.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/setup/setup-attempt-7d85f1e2d83e3884/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/setup/setup-attempt-7d85f1e2d83e3884/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/setup/setup-attempt-8bb56ce1af85f3cc/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/setup/setup-attempt-8bb56ce1af85f3cc/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-046eb768e6931a8c/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-046eb768e6931a8c/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-33a6594e47146661/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-33a6594e47146661/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-3aa2252f25dab829/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-3aa2252f25dab829/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-436818b2cf2d6cb4/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-436818b2cf2d6cb4/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-72de0c6f3b62b72d/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-72de0c6f3b62b72d/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-857c504cce6ef658/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-857c504cce6ef658/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-9cb3ef35802a8aa7/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-9cb3ef35802a8aa7/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-9e90b332321a214a/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-9e90b332321a214a/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-a39809ecfb45c284/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-a39809ecfb45c284/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-ac863e5d14cdef0f/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-ac863e5d14cdef0f/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-ad975cc5138cb87d/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-ad975cc5138cb87d/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-c16298a4b6aaa7fd/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-c16298a4b6aaa7fd/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-c2585d18ed8d1d6f/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-c2585d18ed8d1d6f/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-d5a5c7f2280aa33a/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-d5a5c7f2280aa33a/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-dce20142010e7529/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-dce20142010e7529/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-f319d180db519f3b/events.jsonl` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.firm/validation/validation-f319d180db519f3b/report.json` | `tracked-present` | `remove-run-artifact` | Committed runtime board/setup/validation state; customer workspaces must create their own. |
| `.gitignore` | `tracked-present` | `keep-shipment` | Customer setup, dependency, lock, or build metadata required by shipment. |
| `.gitmodules` | `tracked-present` | `uncertain-build-config` | Build/config file; inspect whether any live submodule remains before changing. |
| `.python-version` | `tracked-present` | `keep-shipment` | Customer setup, dependency, lock, or build metadata required by shipment. |
| `archive_docs/byo-server-extraction_process.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/byo-server-extraction_s1_review.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/byo-server-extraction_s2_review.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/byo-server-extraction_s3_review.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/byo-server-extraction_s4_review.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/byo-server-extraction_s5_review.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/byo-server-extraction_s6_review.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/byo-server-extraction_spec.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/byo-server-manifest-tree-digest_bug_review.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/byo-server-manifest-tree-digest_bug_spec.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/codex_prompts.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/codex_prompts_3.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/Design_Proto_Spec.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/do-not-read-trash.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/Implementation_Plan.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/New_Brain_Spec_Gap_Sheet.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/Plan_Prompt_Contents_Spec.md` | `tracked-present` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/v2_Brain_Spec_2_Gap_Assessment.md` | `untracked` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `archive_docs/v2_Brain_Spec_2_Gap_Sheet.md` | `untracked` | `remove-archived-doc` | Explicit archive of superseded/internal design material. |
| `boards/example_custom_board.yaml` | `tracked-present` | `uncertain-dynamic-config` | Board YAML may be loaded dynamically or used for legacy compatibility; inspect before removal. |
| `boards/example_custom_nrf52_board.yaml` | `tracked-present` | `uncertain-dynamic-config` | Board YAML may be loaded dynamically or used for legacy compatibility; inspect before removal. |
| `boards/nrf52833dk.yaml` | `tracked-present` | `uncertain-dynamic-config` | Board YAML may be loaded dynamically or used for legacy compatibility; inspect before removal. |
| `boards/nrf52840dk.yaml` | `tracked-present` | `uncertain-dynamic-config` | Board YAML may be loaded dynamically or used for legacy compatibility; inspect before removal. |
| `boards/nucleo_l476rg.yaml` | `tracked-present` | `uncertain-dynamic-config` | Board YAML may be loaded dynamically or used for legacy compatibility; inspect before removal. |
| `CLAUDE.md` | `tracked-present` | `remove-internal-agent-doc` | Internal agent prompt/workflow material, not customer product documentation. |
| `CLAUDE.md.pre-attach.bak` | `tracked-present` | `remove-backup` | Backup copy, not a shipped source of truth. |
| `codex_prompts_4.md` | `tracked-present` | `remove-internal-agent-doc` | Internal agent prompt/workflow material, not customer product documentation. |
| `decisions/ADR-0001-single-file-safety-authority.md` | `tracked-present` | `uncertain-current-design-doc` | ADR may still be normative/referenced; inspect references before deciding. |
| `decisions/ADR-0002-generic-datasheet-pack-device-authority.md` | `tracked-present` | `uncertain-current-design-doc` | ADR may still be normative/referenced; inspect references before deciding. |
| `decisions/ADR-0003-quarantined-runtime-device-support-onboarding.md` | `tracked-present` | `uncertain-current-design-doc` | ADR may still be normative/referenced; inspect references before deciding. |
| `decisions/ADR-0004-artifact-defined-generic-application-authority.md` | `tracked-present` | `uncertain-current-design-doc` | ADR may still be normative/referenced; inspect references before deciding. |
| `docs/adversarial-ideal-usability-gap-audit.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/adversarial-ideal-usability-remediation-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/agent-command-adapter.md` | `tracked-present` | `uncertain-doc` | Inspect currency, references, and customer relevance. |
| `docs/agent-contract.md` | `tracked-present` | `keep-customer-doc` | Current product architecture/agent/tool contract documentation. |
| `docs/architecture.md` | `tracked-present` | `keep-customer-doc` | Current product architecture/agent/tool contract documentation. |
| `docs/cmsis-svd-default-access-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/cmsis-svd-default-access-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/contract-history.md` | `tracked-present` | `remove-internal-history` | Internal contract change history, not current customer documentation. |
| `docs/datasheet-pack-universal-support-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/datasheet-pack-universal-support-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/debias-loop-report.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/debias-round-1-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/debias-round-1-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/debias-round-2-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/debias-round-2-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/debias-round-3-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/debias-round-3-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/debias-round-4-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/debias-round-4-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/debias-round-5-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/debias-round-5-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/debias-round-6-audit.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/dual-model-dual-board-acceptance-plan-2026-07-19.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/advertised-tools.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/assertions.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/.r11_agent_prompt.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/.r11_mcp_launch.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/advertised-tools.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/agent-config.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/assertions.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/claude-mcp.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/mcp-debug.log` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/mcp-tool-timeline.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/raw-stream.jsonl` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/README.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/result-schema.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/run-metadata.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/stderr.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/structured-result.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-01-auto-low-blocked/transcript.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-02-medium-auto/.r11_agent_prompt.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-02-medium-auto/.r11_mcp_launch.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-02-medium-auto/agent-config.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-02-medium-auto/mcp-debug.log` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-02-medium-auto/raw-stream.jsonl` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-02-medium-auto/run-metadata.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-02-medium-auto/stderr.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-03-medium-exact-allowed/.r11_agent_prompt.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-03-medium-exact-allowed/.r11_agent_result.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-03-medium-exact-allowed/.r11_mcp_launch.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-03-medium-exact-allowed/agent-config.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-03-medium-exact-allowed/mcp-debug.log` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-03-medium-exact-allowed/raw-stream.jsonl` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-03-medium-exact-allowed/run-metadata.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-03-medium-exact-allowed/stderr.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/attempt-history.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/claude-mcp.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/executed-prompt.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/mcp-debug.log` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/mcp-tool-timeline.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/provider-run-metadata.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/raw-stream.jsonl` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/README.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/result-schema.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/run-metadata.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/stderr.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/structured-result.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/successful-agent-config.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/transcript.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-claude-2026-07-17/transcript.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/advertised-tools.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/agent-config.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/assertions.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempt-history.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/advertised-tools.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/agent-config.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/assertions.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/context-usage.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/effective-prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/failed-launch-1-effective-prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/failed-launch-1-mcp-launch-manifest.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/failed-launch-1-metadata.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/failed-launch-2-effective-prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/failed-launch-2-mcp-launch-manifest.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/failed-launch-2-metadata.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/failed-launch-2-provider-exit.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/failed-launch-2-raw-events.jsonl` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/failed-launch-2-stderr.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/launch-metadata.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/mcp-launch-manifest.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/mcp-tool-timeline.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/provider-exit.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/raw-codex-events.jsonl` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/result-schema.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/scenario-prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/stderr.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/structured-result-status.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/attempts/gpt-5.6-luna-old-cli/transcript.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/context-usage.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/effective-prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/evidence-manifest.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/launch-metadata.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/mcp-launch-manifest.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/mcp-tool-timeline.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/provider-exit.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/provider.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/raw-codex-events.jsonl` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/result-schema.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/scenario-prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/stderr.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/structured-result.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-contract-smoke-codex-2026-07-17/transcript.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/advertised-tools-before.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/agent-config.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/claude-mcp.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/executed-prompt.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/mcp-debug.log` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/neutral-launch-manifest.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/pre-run-snapshot.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/provider-run-metadata.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/raw-stream.jsonl` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/result-schema.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/safety-before/memory_map.yaml` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/safety-before/safety_report.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/safety-before/source_manifest.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-2026-07-17/stderr.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/agent-config.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-2-user-auth/agent-config.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-2-user-auth/blocked-run.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-2-user-auth/claude-debug.log` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-2-user-auth/generated-prompt.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-2-user-auth/mcp-launch-manifest.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-2-user-auth/prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-2-user-auth/result-schema.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-3-validated-native-command/agent-config.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-3-validated-native-command/blocked-run.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-3-validated-native-command/claude-debug.log` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-3-validated-native-command/generated-prompt.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-3-validated-native-command/mcp-launch-manifest.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-3-validated-native-command/prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-3-validated-native-command/provider-run-metadata.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-3-validated-native-command/raw-stream.jsonl` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-3-validated-native-command/result-schema.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/attempt-3-validated-native-command/stderr.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/blocked-run.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/claude-debug.log` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/generated-prompt.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/mcp-launch-manifest.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/provider-run-metadata.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/raw-stream.jsonl` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/result-schema.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-claude-general-2026-07-18/stderr.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-codex-general-2026-07-18/agent-config.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-codex-general-2026-07-18/attempt-2-strict-schema/agent-config.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-codex-general-2026-07-18/attempt-2-strict-schema/prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-codex-general-2026-07-18/attempt-2-strict-schema/result-schema.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-codex-general-2026-07-18/generated-mcp-launch.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-codex-general-2026-07-18/generated-prompt.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-codex-general-2026-07-18/prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-codex-general-2026-07-18/provider-run-metadata.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-codex-general-2026-07-18/raw-codex-events.jsonl` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-codex-general-2026-07-18/result-schema.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/agent-hardware-acceptance-codex-general-2026-07-18/stderr.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/autonomous-current-tree-acceptance-2026-07-17.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/autonomous-static-client-acceptance-2026-07-17.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/autonomous-worker-thread-log-acceptance-2026-07-17.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/claude-usage-carve-out-dual-board-matrix-2026-07-20.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/dual-model-dual-board-acceptance-2026-07-19/nrf-baremetal-gpt-5.6-luna-medium-journey.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/dual-model-dual-board-acceptance-2026-07-19/nrf-bootloader-claude-sonnet-5-medium-journey.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/dual-model-dual-board-acceptance-2026-07-19/nrf-bootloader-gpt-5.6-luna-medium-journey.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/dual-model-dual-board-acceptance-2026-07-19/nrf-repair-gpt-5.6-luna-medium-journey.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/dual-model-dual-board-acceptance-2026-07-19/nrf-zephyr-claude-sonnet-5-medium-journey.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/dual-model-dual-board-acceptance-2026-07-19/nrf-zephyr-gpt-5.6-luna-medium-journey.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/dual-model-dual-board-acceptance-2026-07-19/stm-repair-gpt-5.6-luna-medium-journey.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/dual-model-dual-board-acceptance-2026-07-19/stm-repair-root-uart-15s.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/dual-model-dual-board-acceptance-2026-07-19/stm-threadx-gpt-5.6-luna-medium-journey.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/dual-model-dual-board-acceptance-2026-07-19/stm-threadx-rcc-cfgr-read.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/dual-model-dual-board-acceptance-2026-07-19/stm-threadx-root-uart-15s.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/dual-model-dual-board-acceptance-2026-07-20.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/fresh-setup-hardware-2026-07-17.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/from-scratch-claude-sonnet-5-medium-2026-07-18-journey.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/from-scratch-claude-sonnet-5-medium-2026-07-18-mcp-r2.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/from-scratch-claude-sonnet-5-medium-2026-07-18-mcp-r3.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/from-scratch-claude-sonnet-5-medium-2026-07-18-mcp.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/from-scratch-claude-sonnet-5-medium-2026-07-18-prompt-r3.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/from-scratch-claude-sonnet-5-medium-2026-07-18-prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/from-scratch-claude-sonnet-5-medium-2026-07-18-uart.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/from-scratch-dual-agent-hardware-acceptance-2026-07-18.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/from-scratch-gpt-5.6-terra-medium-2026-07-18-journey.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/from-scratch-gpt-5.6-terra-medium-2026-07-18-prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/from-scratch-gpt-5.6-terra-medium-2026-07-18-uart.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-claude-sonnet-5-medium-r2.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-claude-sonnet-5-medium-r3.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-stm32-claude-sonnet-5-medium-r1.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-stm32-claude-sonnet-5-medium-r2.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-stm32-claude-sonnet-5-medium-r3.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-stm32-gpt-5.6-luna-medium-r1.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-stm32-gpt-5.6-luna-medium-r2.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-stm32-gpt-5.6-luna-medium-r3.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-stm32-gpt-5.6-luna-medium-r4.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-stm32-gpt-5.6-luna-medium-r5.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-claude-sonnet-5-medium-debug-retry-r5.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-claude-sonnet-5-medium-r1.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-claude-sonnet-5-medium-r2.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-claude-sonnet-5-medium-r3.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-claude-sonnet-5-medium-r4.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-gpt-5.6-luna-medium-r1.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-gpt-5.6-luna-medium-r2.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-gpt-5.6-luna-medium-r3.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-gpt-5.6-luna-medium-r4.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-gpt-5.6-luna-medium-r5.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-gpt-5.6-luna-medium-r6.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-gpt-5.6-luna-medium-r7.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-gpt-5.6-luna-medium-r8.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/launch-wide-gpt-5.6-luna-medium-r9.ps1` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/m10-final-validation-2026-07-17.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/m10-final-validation-2026-07-17.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/m10-performance-2026-07-17.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/m10-software-boundary-2026-07-17.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/m10-task20-acceptance-plan-2026-07-17.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/m10-task20-execution-2026-07-17.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/m5-hardware-smoke-2026-07-17.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/m6-hardware-acceptance-2026-07-17.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/m7-hardware-acceptance-2026-07-17.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/m8-hardware-recovery-2026-07-17.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/m8-hardware-recovery-2026-07-17.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/m9-hardware-lifecycle-2026-07-17.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/m9-hardware-lifecycle-2026-07-17.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/p4-07-software-verification-2026-07-17.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/p4-08-agent-contract-smoke-2026-07-17.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/prompt-audit-through-15.3-2026-07-17.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/prompt-audit-through-20.1-2026-07-17.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/safety-layer-v2-agent-smoke-2026-07-18.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/stm32-claude-sonnet-5-medium-journey-2026-07-19.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/stm32-claude-sonnet-5-medium-root-debug-2026-07-19.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/stm32-claude-sonnet-5-medium-root-uart-2026-07-19.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/stm32-claude-sonnet-5-medium-rtos-mcp-r1.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/stm32-claude-sonnet-5-medium-rtos-mcp-r2.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/stm32-claude-sonnet-5-medium-rtos-mcp-r3.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/stm32-claude-sonnet-5-medium-rtos-prompt-2026-07-18.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/stm32-dual-client-hardware-acceptance-2026-07-19.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/stm32-gpt-5.6-luna-medium-boot-boundary-fix-2026-07-19.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/stm32-gpt-5.6-luna-medium-bootloader-prompt-2026-07-18.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/stm32-gpt-5.6-luna-medium-journey-2026-07-19.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/stm32-gpt-5.6-luna-medium-root-flash-2026-07-19.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/stm32-gpt-5.6-luna-medium-root-uart-2026-07-19.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/universal-onboarding-live-acceptance-2026-07-19.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/unsupported-path-audit-a.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/unsupported-path-audit-b.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/unsupported-path-final-hardware-regression-r2.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/unsupported-path-final-hardware-regression-r3.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/unsupported-path-final-hardware-regression-r4.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/unsupported-path-final-hardware-regression-r5.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/unsupported-path-final-hardware-regression.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/unsupported-path-final-uart-15s.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/unsupported-path-luna-high-journey.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/unsupported-path-luna-high-root-uart-15s.txt` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-breakpoint-direct-diagnostic-2026-07-18.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-breakpoint-mcp-handle-direct-write-2026-07-18.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-breakpoint-mcp-internals-2026-07-18.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-breakpoint-mcp-ready-probe-2026-07-18.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-breakpoint-mcp-reproduction-2026-07-18.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-breakpoint-mcp-serial-exchange-2026-07-18.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-breakpoint-write-helper-diagnostic-2026-07-18.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-claude-breakpoint-periodic-mcp-proof-2026-07-18.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-claude-sonnet-5-medium-2026-07-18-agent-raw-journey.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-claude-sonnet-5-medium-2026-07-18-mcp-r2.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-claude-sonnet-5-medium-2026-07-18-mcp-r3.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-claude-sonnet-5-medium-2026-07-18-mcp-r4.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-claude-sonnet-5-medium-2026-07-18-mcp.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-claude-sonnet-5-medium-2026-07-18-prompt-r2.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-claude-sonnet-5-medium-2026-07-18-prompt-r3.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-claude-sonnet-5-medium-2026-07-18-prompt-r4.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-claude-sonnet-5-medium-2026-07-18-prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-claude-sonnet-5-medium-2026-07-18-uart-self-verification.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-claude-sonnet-5-medium-debug-retry-2026-07-18-mcp.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-claude-sonnet-5-medium-debug-retry-2026-07-18-prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-dual-client-hardware-2026-07-18.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-failure-loops-2026-07-18.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-gpt-5.6-luna-medium-2026-07-18-journey.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-gpt-5.6-luna-medium-2026-07-18-mcp-r2.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-gpt-5.6-luna-medium-2026-07-18-mcp-r3.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-gpt-5.6-luna-medium-2026-07-18-mcp-r4.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-gpt-5.6-luna-medium-2026-07-18-mcp-r5.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-gpt-5.6-luna-medium-2026-07-18-mcp-r6.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-gpt-5.6-luna-medium-2026-07-18-mcp.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-gpt-5.6-luna-medium-2026-07-18-prompt.md` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-gpt-5.6-luna-medium-2026-07-18-uart-self-verification.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-provider-metadata-2026-07-18.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/evidence/wide-acceptance-software-verification-2026-07-18.json` | `tracked-present` | `remove-captured-test-output` | Internal hardware/agent acceptance transcripts, logs, and run evidence. |
| `docs/extraction-manifest.json` | `tracked-present` | `remove-internal-artifact` | Internal extraction ledger/artifact, not runtime package data. |
| `docs/fresh-workspace-automation-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/fresh-workspace-implementation-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/fresh-workspace-validation.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/function-symbol-memory-refusal-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/function-symbol-memory-refusal-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/general-native-build-helper-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/general-native-build-helper-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/generic-artifact-collector-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/generic-artifact-collector-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/generic-make-native-build-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/generic-make-native-build-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/generic-symbol-artifact-selection-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/generic-symbol-artifact-selection-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/new-brain-audit-fix-plan.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/new-brain-audit-fix-spec.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/new-brain-closure-validation.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/new-brain-gap-audit.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/new-brain-remediation-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/nonfatal-operation-failure-connection-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/nonfatal-operation-failure-connection-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/plan-tool-contract.md` | `tracked-present` | `keep-customer-doc` | Current product architecture/agent/tool contract documentation. |
| `docs/reviewed-peripheral-read-coverage-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/reviewed-peripheral-read-coverage-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/run-scoped-symbol-artifact-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/run-scoped-symbol-artifact-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/safety-evidence-schema-v1.md` | `tracked-present` | `uncertain-doc` | Inspect currency, references, and customer relevance. |
| `docs/safety-layer-v2-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/safety-layer-v2-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/setup-reviewed-connect-policy-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/setup-reviewed-connect-policy-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/static-client-plan-execution-gap-spec.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/static-client-plan-execution-remediation-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/stm32-pack-preflight-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/stm32-pack-preflight-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/stm32l476-reviewed-setup-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/stm32l476-reviewed-setup-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/uart-capture-duration-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/uart-capture-duration-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/universal-device-onboarding-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/universal-device-onboarding-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/universal-native-build-command-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/universal-native-build-command-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/universal-onboarding-audit-closure-plan.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/universal-onboarding-audit-closure-spec.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/universal-onboarding-final-outer-audit-plan.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/universal-onboarding-final-outer-audit-spec.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/universal-onboarding-flexibility-followup-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/universal-onboarding-flexibility-followup-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/universal-onboarding-memory-overlay-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/universal-onboarding-memory-overlay-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/universal-onboarding-multi-probe-routing-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/universal-onboarding-multi-probe-routing-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/universal-onboarding-outer-audit-closure-plan.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/universal-onboarding-outer-audit-closure-spec.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/universal-onboarding-project-authority-reuse-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/universal-onboarding-project-authority-reuse-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/universal-onboarding-refresh-association-plan.md` | `tracked-present` | `remove-internal-plan` | Implementation plan, not customer-facing product documentation. |
| `docs/universal-onboarding-refresh-association-spec.md` | `tracked-present` | `uncertain-current-design-doc` | Specification may still be normative/referenced; inspect before removal. |
| `docs/unsupported-path-audit-remediation-plan.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/unsupported-path-audit-remediation-spec.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/validation-pack-support-plan.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/validation-pack-support-spec.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/validation-probe-guidance-plan.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/validation-probe-guidance-spec.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `docs/verification.md` | `tracked-present` | `remove-internal-validation-doc` | Internal verification/audit/history document. |
| `firmware/nrf52833dk/bugs/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b001__wrong_boot_text/build/.gitignore` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b001__wrong_boot_text/build/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b001__wrong_boot_text/build_bug.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b001__wrong_boot_text/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b001__wrong_boot_text/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b001__wrong_boot_text/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b002__wrong_known_value/build/.gitignore` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b002__wrong_known_value/build/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b002__wrong_known_value/build_bug.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b002__wrong_known_value/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b002__wrong_known_value/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b002__wrong_known_value/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b003__silent_uart/build/.gitignore` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b003__silent_uart/build/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b003__silent_uart/build_bug.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b003__silent_uart/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b003__silent_uart/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b003__silent_uart/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b004__dual_signal_regression/build/.gitignore` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b004__dual_signal_regression/build/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b004__dual_signal_regression/build_bug.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b004__dual_signal_regression/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b004__dual_signal_regression/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/bugs/b004__dual_signal_regression/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/README.md` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/recovery/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/reference/build/.gitignore` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/reference/build/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/reference/build/firmware.elf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/reference/build/firmware.hex` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/reference/build_reference.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/reference/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/reference/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52833dk/reference/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b001__wrong_boot_text/build_bug.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b001__wrong_boot_text/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b001__wrong_boot_text/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b001__wrong_boot_text/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b002__wrong_known_value/build_bug.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b002__wrong_known_value/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b002__wrong_known_value/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b002__wrong_known_value/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b003__silent_uart/build_bug.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b003__silent_uart/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b003__silent_uart/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b003__silent_uart/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b004__dual_signal_regression/build_bug.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b004__dual_signal_regression/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b004__dual_signal_regression/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/bugs/b004__dual_signal_regression/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/README.md` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/recovery/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/reference/build/.gitignore` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/reference/build/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/reference/build/firmware.elf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/reference/build/firmware.hex` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/reference/build_reference.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/reference/src/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/reference/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/reference/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nrf52840dk/reference/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b001__wrong_boot_text/build/.gitignore` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b001__wrong_boot_text/build/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b001__wrong_boot_text/build_bug.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b001__wrong_boot_text/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b001__wrong_boot_text/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b001__wrong_boot_text/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b002__wrong_known_value/build/.gitignore` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b002__wrong_known_value/build/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b002__wrong_known_value/build_bug.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b002__wrong_known_value/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b002__wrong_known_value/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b002__wrong_known_value/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b003__silent_uart/build/.gitignore` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b003__silent_uart/build/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b003__silent_uart/build_bug.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b003__silent_uart/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b003__silent_uart/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b003__silent_uart/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b004__dual_signal_regression/build/.gitignore` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b004__dual_signal_regression/build/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b004__dual_signal_regression/build_bug.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b004__dual_signal_regression/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b004__dual_signal_regression/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/bugs/b004__dual_signal_regression/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/common/nucleo_l476rg.overlay` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/common/stage1_uart.h` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/README.md` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/recovery/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/reference/build/.gitignore` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/reference/build/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/reference/build/firmware.elf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/reference/build/firmware.hex` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/reference/build_reference.sh` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/reference/src/.gitkeep` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/reference/src/CMakeLists.txt` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/reference/src/prj.conf` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/nucleo_l476rg/reference/src/src/main.c` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `firmware/README.md` | `tracked-present` | `uncertain-test-asset` | Reference/bug firmware appears test or benchmark oriented; prove runtime and entrypoint reachability. |
| `forbidden_docs/Server_A_Codex_Prompts.md` | `tracked-present` | `remove-internal-doc` | Explicitly internal/forbidden documentation; classified by path without reading content. |
| `forbidden_docs/Server_A_functionality.md` | `tracked-present` | `remove-internal-doc` | Explicitly internal/forbidden documentation; classified by path without reading content. |
| `forbidden_docs/server_A_implementation_plan.md` | `tracked-present` | `remove-internal-doc` | Explicitly internal/forbidden documentation; classified by path without reading content. |
| `forbidden_docs/Server_A_Turnkey_Design_Spec.md` | `tracked-present` | `remove-internal-doc` | Explicitly internal/forbidden documentation; classified by path without reading content. |
| `host_bootstrap.py` | `tracked-present` | `uncertain-customer-setup` | Potential customer installation/bootstrap surface; verify README/build references. |
| `init.md` | `tracked-present` | `remove-internal-agent-doc` | Internal agent prompt/workflow material, not customer product documentation. |
| `Nano_BLE_MCU-nRF52840_PS_v1.1.pdf` | `tracked-present` | `remove-vendor-reference` | Large vendor datasheet/reference input, not part of the Python distribution. |
| `New_Brain_Spec.md` | `tracked-present` | `uncertain-product-spec` | Canonical product specification may be customer-facing or needed to support shipped docs. |
| `packs/.gitignore` | `tracked-present` | `uncertain-runtime-data` | Pack manifest/provisioning data may be loaded dynamically; inspect before removal. |
| `packs/live_index_repair.md` | `tracked-present` | `uncertain-runtime-data` | Pack manifest/provisioning data may be loaded dynamically; inspect before removal. |
| `packs/manifest.yaml` | `tracked-present` | `uncertain-runtime-data` | Pack manifest/provisioning data may be loaded dynamically; inspect before removal. |
| `packs/README.md` | `tracked-present` | `uncertain-runtime-data` | Pack manifest/provisioning data may be loaded dynamically; inspect before removal. |
| `pyproject.toml` | `tracked-present` | `keep-shipment` | Customer setup, dependency, lock, or build metadata required by shipment. |
| `README.md` | `tracked-present` | `keep-shipment` | Customer setup, dependency, lock, or build metadata required by shipment. |
| `repo_sync_to_brain.md` | `tracked-present` | `remove-internal-agent-doc` | Internal agent prompt/workflow material, not customer product documentation. |
| `scripts/__init__.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/close_m10_acceptance.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/m10_nucleo_nondestructive_acceptance.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/m6_hardware_acceptance.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/m7_hardware_acceptance.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/m9_hardware_lifecycle.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/m9_instrumented_server.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/measure_m10_performance.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/migrate_boards_to_firm.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/prepare_m10_acceptance.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/reconcile_m10_execution.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/reconcile_m10_software_acceptance.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/record_m10_remaining_hardware.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/run_fresh_workspace_e2e.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/run_m10_software_acceptance.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `scripts/validate_autonomous_acceptance.py` | `tracked-present` | `uncertain-internal-script` | Likely acceptance/migration script; prove build/runtime/config reachability before removal. |
| `SERVER_GUIDE.md` | `tracked-present` | `keep-shipment` | Customer setup, dependency, lock, or build metadata required by shipment. |
| `setup_host.ps1` | `tracked-present` | `uncertain-customer-setup` | Potential customer installation/bootstrap surface; verify README/build references. |
| `setup_host.sh` | `tracked-present` | `uncertain-customer-setup` | Potential customer installation/bootstrap surface; verify README/build references. |
| `src/pyocd_debug_mcp/__init__.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/adapters/__init__.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/adapters/swd_interface.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/adapters/swd_pyocd.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/adapters/uart_interface.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/adapters/uart_pyserial.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/agent_command_adapter.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/artifact_collector.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/benchmark_support.py` | `tracked-present` | `uncertain-runtime-code` | Potential internal/legacy helper; prove entrypoint/import/config reachability before removal. |
| `src/pyocd_debug_mcp/board_config.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/board_config_cli.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/firmstore/__init__.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/firmstore/cache.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/firmstore/profiles.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/firmstore/reports.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/firmstore/store.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/guardrails/__init__.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/guardrails/flash_gate.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/guardrails/gate.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/guardrails/permissions.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/guardrails/plan_contract.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/guardrails/plan_defs.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/guardrails/plan_engine.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/guardrails/recover_gate.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/kernel/__init__.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/kernel/finalizers.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/kernel/hygiene.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/kernel/operations.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/kernel/processes.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/kernel/registry.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/kernel/run_state.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/local_env.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/native_build.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/pack_index_repair.py` | `tracked-present` | `uncertain-runtime-code` | Potential internal/legacy helper; prove entrypoint/import/config reachability before removal. |
| `src/pyocd_debug_mcp/pack_provision.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/probe_families.json` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/probe_families.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/probe_inventory.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/reference_artifacts.py` | `tracked-present` | `uncertain-runtime-code` | Potential internal/legacy helper; prove entrypoint/import/config reachability before removal. |
| `src/pyocd_debug_mcp/reference_smoke.py` | `tracked-present` | `uncertain-runtime-code` | Potential internal/legacy helper; prove entrypoint/import/config reachability before removal. |
| `src/pyocd_debug_mcp/runtime_resources.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/safety/__init__.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/safety/enforce.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/safety/linker.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/safety/map_build.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/safety/refresh.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/safety/regions.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/safety/verify2.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/serial_fallbacks.json` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/serial_resolver.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/server.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/services/__init__.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/services/connections.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/services/convergence_watcher.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/services/session_runtime.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/services/symbols.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/services/target_control.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/services/uart_capture.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/services/uart_exchange_schema.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/__init__.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/board_catalog.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/datasheet_evidence.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/device_authority.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/device_support.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/evidence/nrf52840_device_support.json` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/evidence/nrf52840_official_document.json` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/evidence/stm32l476_device_support.json` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/evidence/stm32l476_official_document.json` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/packs.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/preflight.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/research.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/reviewed_boards.json` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/reviewed_evidence.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/setup.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/targets.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/setup_flow/validate.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/target_errors.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/timeouts.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/__init__.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/artifacts.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/batch.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/breakpoints.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/execution.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/flash.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/handshake.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/memory.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/misc.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/plans.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/registers.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/serial.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/session.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/setup.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/tools/unlock.py` | `tracked-present` | `keep-runtime` | Shipped Python package implementation; retain unless dead-code analysis proves unreachable. |
| `src/pyocd_debug_mcp/zephyr_build.py` | `tracked-present` | `uncertain-runtime-code` | Potential internal/legacy helper; prove entrypoint/import/config reachability before removal. |
| `stage0_check.py` | `tracked-present` | `uncertain-customer-setup` | Potential customer installation/bootstrap surface; verify README/build references. |
| `stage0_setup.md` | `tracked-present` | `uncertain-customer-setup` | Potential customer installation/bootstrap surface; verify README/build references. |
| `stm32l476je (2).pdf` | `tracked-present` | `remove-vendor-reference` | Large vendor datasheet/reference input, not part of the Python distribution. |
| `tests/cases/.gitkeep` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52833dk__b001_wrong_boot_text/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52833dk__b001_wrong_boot_text/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52833dk__b002_wrong_known_value/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52833dk__b002_wrong_known_value/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52833dk__b003_silent_uart/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52833dk__b003_silent_uart/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52833dk__b004_dual_signal_regression/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52833dk__b004_dual_signal_regression/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52833dk__f001_halted_target_silent_uart/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52833dk__f001_halted_target_silent_uart/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52833dk__k001_reference_green/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52833dk__k001_reference_green/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52840dk__b001_wrong_boot_text/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52840dk__b001_wrong_boot_text/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52840dk__b002_wrong_known_value/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52840dk__b002_wrong_known_value/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52840dk__b003_silent_uart/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52840dk__b003_silent_uart/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52840dk__b004_dual_signal_regression/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52840dk__b004_dual_signal_regression/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52840dk__f001_halted_target_silent_uart/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52840dk__f001_halted_target_silent_uart/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52840dk__k001_reference_green/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nrf52840dk__k001_reference_green/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nucleo_l476rg__b001_wrong_boot_text/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nucleo_l476rg__b001_wrong_boot_text/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nucleo_l476rg__b002_wrong_known_value/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nucleo_l476rg__b002_wrong_known_value/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nucleo_l476rg__b003_silent_uart/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nucleo_l476rg__b003_silent_uart/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nucleo_l476rg__b004_dual_signal_regression/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nucleo_l476rg__b004_dual_signal_regression/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nucleo_l476rg__f001_halted_target_silent_uart/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nucleo_l476rg__f001_halted_target_silent_uart/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nucleo_l476rg__k001_reference_green/case.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/nucleo_l476rg__k001_reference_green/prompt.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/r11_result_schema.json` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/README.md` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/cases/suites.yaml` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/contracts/adversarial-usability-server-tools.json` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/contracts/plan-prompt-server-tools.json` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/contracts/product-server-tools.json` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/contracts/source-server-tools.json` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/fixtures/fake_lifecycle_stdio_server.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/fixtures/r11_results/blocked_failure.json` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/fixtures/r11_results/diagnosed_only_partial.json` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/fixtures/r11_results/fixed_success.json` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/fixtures/safety/nucleo_reference.map` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/harness/.gitkeep` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/harness/r11_benchmark.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/harness/stage1_smoke.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_agent_command_adapter.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_artifact_collector.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_attachment_cache.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_autonomous_acceptance_evidence.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_batch.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_board_configs.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_breakpoint_artifact_binding.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_connections.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_datasheet_evidence.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_device_authority.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_extracted_server_contract.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_extraction_manifest.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_finalizers.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_firmstore.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_firmstore_ignore.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_firmstore_reports.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_fresh_workspace_runner.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_gate.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_generic_device_support.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_host_bootstrap.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_import_closure.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_initialization_handshake.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_kernel_operations.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_kernel_registry.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_lifecycle_stdio_integration.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_m10_acceptance_plan.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_m10_coverage_report.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_m10_final_validation_report.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_m10_performance.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_m10_relay_text.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_m10_security.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_m5_surface_contract.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_m9_audit.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_managed_operations.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_migrate_boards_to_firm.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_native_build.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_pack_index_repair.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_pack_provision.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_packaging_contract.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_permissions.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_plan_defs.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_plan_engine.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_plan_prompt_contents.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_plan_tools.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_probe_inventory.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_process_hygiene.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_product_server_contract.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_profiles_v2.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_r10_runtime.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_r11_benchmark.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_reference_artifacts.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_reviewed_setup_evidence.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_revised_memory_flash_misc.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_revised_session_execution_registers.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_s3_asset_closure.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_s4_benchmark_isolation.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_safety_enforcement.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_safety_linker.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_safety_map_build.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_safety_refresh.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_safety_regions.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_safety_v2_fresh_mcp_e2e.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_safety_verify2.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_serial_resolver.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_server_board_config.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_server_import.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_server_resource_binding.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_server_runtime_tools.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_server_safety_tools.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_setup_board_catalog.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_setup_hardware_inventory.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_setup_packs.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_setup_plan_allowance.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_setup_preflight.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_setup_research.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_setup_targets.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_setup_tools.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_setup_validation.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_setup_workflow.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_stage0_shared_errors.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_static_client_plan_fallback.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_symbols.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_target_control.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_target_unlock.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_uart_capture.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_uart_exchange_schema.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_universal_onboarding_e2e.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `tests/test_zephyr_build.py` | `tracked-present` | `uncertain-ci-test` | Test/fixture/harness; determine whether CI or shipment build requires it before removal. |
| `uv.lock` | `tracked-present` | `keep-shipment` | Customer setup, dependency, lock, or build metadata required by shipment. |
| `v2_Brain_Spec_2_Gap_Assessment.md` | `tracked-missing` | `uncertain-data-or-doc` | Inspect references and shipment relevance before removal. |
| `v2_Brain_Spec_2_Gap_Sheet.md` | `tracked-missing` | `uncertain-data-or-doc` | Inspect references and shipment relevance before removal. |
