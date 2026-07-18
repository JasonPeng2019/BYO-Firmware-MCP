## Mandatory subagent rule for Claude and Codex test runs

All Claude and Codex test runs described in this document must be executed through a subagent. Do not run a Claude or Codex test session directly. Every subagent launch must use the built-in default full-access/auto permission configuration. When Codex launches a Claude subagent, it must configure that subagent to use Claude Sonnet 4.5 with effort low (not Haiku 4.5), and Codex must determine the correct supported launch syntax/settings needed to enforce that configuration in this environment.

## P4-01 - Make plan/action schemas truthful and minimal (GAP-05, GAP-18, GAP-10, GAP-12)
### Subagent required for all Claude and Codex test runs

All Claude and Codex test runs in this prompt must use a subagent; do not run a Claude or Codex test session directly. Every subagent launch must use the built-in default full-access/auto permission configuration. When Codex launches a Claude subagent for this prompt, it must configure that subagent to use Claude Sonnet 4.5 with effort low (not Haiku 4.5), and Codex must determine the correct supported launch syntax/settings needed to enforce that configuration in this environment.

Before implementing anything below, check whether a prior session already implemented, satisfied, or explicitly dismissed part of this task as meritless (unneeded, already correct, or actively wrong for the product goal) — check `v2_Brain_Spec_2_Gap_Sheet.md`, `git log`, and the current state of the named files, not just this prompt's wording. Do not redo, re-argue, or re-implement anything already resolved or already rejected for a documented reason; only implement what is genuinely still open. If you find part of this prompt is itself meritless given current code, skip it and state why instead of implementing it anyway.

You are in the top-level BYO-Server repo. Start with `git status --short` and preserve any unrelated user edits. Patch GAP-05, GAP-18, GAP-10, and GAP-12 from `v2_Brain_Spec_2_Gap_Sheet.md` (the current audit; `New_Brain_Spec_Gap_Sheet.md` is its superseded predecessor). The product priority is flexibility and ease of use for a compliant agent and a regular firmware developer: every schema field must be something the agent can actually obtain, and no schema may advertise a parameter the runtime always refuses. Do not add edge-case hardening beyond the named tasks. Keep authority fail-closed throughout: no caller-supplied allowed memory ranges, no persisted gates or permissions.

Tasks:

1. GAP-18 (regression — fix first): remove `serial_port` from the `board_setup` plan/action schema (`guardrails/plan_defs.py:187-196`). Keep `serial_id` (the stable choice from setup inventory) as the agent-supplied selector, and make the server resolve the current port path from that stable identity at execution time, exactly as the spec's attachment-cache section prescribes ("a change from COM7 to COM11 does not prompt again"). Record the resolved path in setup reports. Update `tools/setup.py` handlers, preflight filtering, and the fresh-workspace runner (`scripts/run_fresh_workspace_e2e.py`) accordingly — the runner should pass only the stable UART identity, and its `--uart-port` flag should disappear with the schema field (update `tests/test_fresh_workspace_runner.py` and every doc that lists the runner flags).
2. GAP-05: make `datasheet_sha256` nullable in the `board_setup` plan/action schema. When null, the server binds its own computed digest before commit (it already computes and enforces the digest internally, so this is a schema change, not an authority change). Continue accepting an agent-supplied digest as an optional cross-check; keep reviewed-byte enforcement and keep recording the digest. Do not add a new hashing tool.
3. GAP-10: remove `target_address` from the `flash_application-plan` and `flash_bootloader-plan` plan/action schemas (`guardrails/plan_defs.py:341,360`) — the ELF/HEX backend rejects every non-null value, so the field is a guaranteed-failure trap. State in the NULL prompts that load addresses come from the artifact.
4. GAP-12: in the same pass, reconcile `Plan_Prompt_Contents_Spec.md` with the final schemas — the flash sections (~lines 581-623) still document `{artifact_path, halt_after}` while live definitions differ; audit every tool section against `plan_defs.py`. Then generalize the sync test in `tests/test_plan_prompt_contents.py` (currently hand-picking only `board_setup` and `serial_exchange` at lines 207-226) to iterate every `PLAN_DEFINITIONS` entry — action name, plan name, exact action-parameter field names, budget mode, permission mode — so no section can drift silently again.
5. Update NULL-prompt examples, contracts, README, and `docs/agent-contract.md` for all removed/nullable fields. If you discover a new genuine product gap, append it to `v2_Brain_Spec_2_Gap_Sheet.md`.

Required smoke tests (schema layer only — setup-flow and flash runtime behavior are exercised again by later prompts, so do not run their suites here):

```text
uv run --locked pytest tests/test_plan_defs.py tests/test_plan_engine.py tests/test_plan_prompt_contents.py tests/test_m5_surface_contract.py tests/test_product_server_contract.py tests/test_fresh_workspace_runner.py
```

Acceptance: no plan schema requires a value the agent cannot reliably obtain, no schema advertises a parameter the runtime always refuses, the runner passes only stable identities, and the human spec cannot drift from any live plan definition without a failing test.

---

## P4-02 - Make setup responses self-guiding end to end (GAP-01, GAP-03, GAP-04, GAP-11)
### Subagent required for all Claude and Codex test runs

All Claude and Codex test runs in this prompt must use a subagent; do not run a Claude or Codex test session directly. Every subagent launch must use the built-in default full-access/auto permission configuration. When Codex launches a Claude subagent for this prompt, it must configure that subagent to use Claude Sonnet 4.5 with effort low (not Haiku 4.5), and Codex must determine the correct supported launch syntax/settings needed to enforce that configuration in this environment.

Before implementing anything below, check whether a prior session already implemented, satisfied, or explicitly dismissed part of this task as meritless (unneeded, already correct, or actively wrong for the product goal) — check `v2_Brain_Spec_2_Gap_Sheet.md`, `git log`, and the current state of the named files, not just this prompt's wording. Do not redo, re-argue, or re-implement anything already resolved or already rejected for a documented reason; only implement what is genuinely still open. If you find part of this prompt is itself meritless given current code, skip it and state why instead of implementing it anyway.

You are in the top-level BYO-Server repo. Start with `git status --short` and preserve any unrelated user edits. Run this after P4-01 so route templates are written once against the final schemas. Patch GAP-01, GAP-03, GAP-04, and GAP-11 from `v2_Brain_Spec_2_Gap_Sheet.md`. Keep user-facing setup conversational: the user is never asked for JSON, board IDs, connection IDs, continuation IDs, port paths, or permission enums — the server supplies every internal value machine-readably so the agent copies instead of guessing or scraping. Keep responses lean and bounded; do not add ceremony or edge-case hardening beyond what the tasks name.

Tasks:

1. GAP-01: make `setup_overview(["no board"])` and case/Unicode-normalized equivalents return the same safe `setup_no_board` status as `setup_overview([])`, with no route emitted (routing loop at `server.py:3515-3558`). Reject mixed lists containing `no board` plus other names with a clarification status that tells the agent to re-ask conversationally — again with no route emitted. Update the `setup_overview` docstring and the initialization-handshake prose (`tools/handshake.py`) so `no board` reads as a literal sentinel, never a candidate board name.
2. GAP-03: make `load_setup_tool` (`tools/setup.py:35-48`) return real guidance keyed to the requested `tool_name` only (`board_setup-plan`, `board_validate`, `board_safety_setup`, `board_safety_refresh`): purpose, exact next call with argument shape, expected statuses, accepted response shape, common remedies, when not to use the tool, and the no-internals conversational rule. Each of the four responses must be distinct and bounded — no whole-manual dumps that bloat agent context.
3. GAP-04: extend every `setup_overview` route with machine-readable next-call composition: `load_call` (exact `load_setup_tool` arguments), `next_call` where no plan is needed, and for setup routes a `plan_action_parameters_template` with all server-known fields pre-filled (`board_id`, `mode`, and `connection_id`/`serial_id` when exactly one candidate exists) and only genuine user facts marked as needed. Add `port_path` and stable USB identity to `serial_choices` rows. Make `required_user_facts` truthful and complete for the final P4-01 schema (UART selection and baud rate included; `serial_port` and any nullable digest no longer belong there). When hardware is ambiguous, include friendly choices plus an `accepted_response` shape. Optionally add `known_board_types` beside `supported_reviewed_board_types` so the agent can truthfully tell the user which board types are known but not automatic.
4. GAP-11: for `validation_needs_user_input`, populate `accepted_response` in `ValidationResult.to_payload()` (`setup_flow/validate.py:181`) with the exact retry call and field mapping — `board_validate(board_id=..., probe_id="<one choice_id>")` or `serial_id=` as appropriate (the tool already accepts both). Keep `accepted_response: None` for terminal statuses.
5. Update tests and docs so they assert the agent never has to invent `connection_id`, `serial_id`, hash commands, or validation retry fields. If you discover a new genuine product gap, append it to `v2_Brain_Spec_2_Gap_Sheet.md`.

Required smoke tests (these also re-cover P4-01's setup schema changes at the workflow level):

```text
uv run --locked pytest tests/test_setup_tools.py tests/test_setup_workflow.py tests/test_setup_validation.py tests/test_setup_board_catalog.py tests/test_initialization_handshake.py
```

Required MCP flow smoke:

```text
uv run --locked python - <<'PY'
import asyncio, json, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "pyocd_debug_mcp.server"])
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            for names in ([], ["no board"], ["No Board"]):
                res = await session.call_tool("setup_overview", {"board_names": names})
                payload = json.loads(res.content[0].text)
                assert payload["status"] == "setup_no_board", payload
                assert not payload["routes"], payload
            mixed = json.loads((await session.call_tool("setup_overview", {"board_names": ["left", "no board"]})).content[0].text)
            assert mixed["status"] not in {"setup_routes_ready", "setup_no_board"}, mixed
            assert not mixed.get("routes"), mixed
            res = await session.call_tool("setup_overview", {"board_names": ["brand new board"]})
            payload = json.loads(res.content[0].text)
            route = payload["routes"][0]
            assert "load_call" in route and "plan_action_parameters_template" in route, route
            load = await session.call_tool("load_setup_tool", {"board_id": route["board_id"], "tool_name": route["next_tool"]})
            loaded = json.loads(load.content[0].text)
            assert "guidance" in loaded or "next_call" in loaded, loaded
asyncio.run(main())
print("setup self-guidance smoke passed")
PY
```

Acceptance: a static or dynamic MCP model can follow setup and validation entirely from returned payloads — no invented fields, no scraped labels, no bogus `no board` profile.

---

## P4-03 - Separate normal connect from guarded connect override (GAP-02)
### Subagent required for all Claude and Codex test runs

All Claude and Codex test runs in this prompt must use a subagent; do not run a Claude or Codex test session directly. Every subagent launch must use the built-in default full-access/auto permission configuration. When Codex launches a Claude subagent for this prompt, it must configure that subagent to use Claude Sonnet 4.5 with effort low (not Haiku 4.5), and Codex must determine the correct supported launch syntax/settings needed to enforce that configuration in this environment.

Before implementing anything below, check whether a prior session already implemented, satisfied, or explicitly dismissed part of this task as meritless (unneeded, already correct, or actively wrong for the product goal) — check `v2_Brain_Spec_2_Gap_Sheet.md`, `git log`, and the current state of the named files, not just this prompt's wording. Do not redo, re-argue, or re-implement anything already resolved or already rejected for a documented reason; only implement what is genuinely still open. If you find part of this prompt is itself meritless given current code, skip it and state why instead of implementing it anyway.

You are in the top-level BYO-Server repo. Start with `git status --short` and preserve any unrelated user edits. Patch GAP-02 from `v2_Brain_Spec_2_Gap_Sheet.md`. This matters for a normal compliant agent: the visible `connect` schema (`server.py:956-961`) advertises `unique_id`, `target`, and `board_config`, which invites an unplanned wrong-probe/wrong-target connection that the spec reserves for guarded `connect_override`.

Tasks:

1. Make public `connect` profile-only: schema requires `board_id` and exposes no `unique_id`, `target`, or `board_config` fields.
2. If internals still pass override values to the shared implementation, reject them at the public handler boundary with a redirect to `connect_override-plan`.
3. Keep hidden `connect_override` capable of run-scoped probe/target/config overrides after `connect_override-plan`.
4. Add a regression proving `action_batch` cannot use visible `connect` to smuggle override fields.
5. Update tool contracts, README, `docs/agent-contract.md`, and tests. If you discover a new genuine product gap, append it to `v2_Brain_Spec_2_Gap_Sheet.md`.

Required smoke tests (the surface contract is re-run here because this prompt changes the surface again):

```text
uv run --locked pytest tests/test_connections.py tests/test_batch.py tests/test_static_client_plan_fallback.py tests/test_m5_surface_contract.py
```

Required MCP schema smoke:

```text
uv run --locked python - <<'PY'
import asyncio, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "pyocd_debug_mcp.server"])
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            props = set(tools["connect"].inputSchema.get("properties", {}))
            assert props == {"board_id"}, props
            assert "connect_override-plan" in tools
asyncio.run(main())
print("connect schema smoke passed")
PY
```

Acceptance: no unplanned manual connection override remains reachable through visible `connect`.

---

## P4-04 - Memory-read containment (GAP-09)
### Subagent required for all Claude and Codex test runs

All Claude and Codex test runs in this prompt must use a subagent; do not run a Claude or Codex test session directly. Every subagent launch must use the built-in default full-access/auto permission configuration. When Codex launches a Claude subagent for this prompt, it must configure that subagent to use Claude Sonnet 4.5 with effort low (not Haiku 4.5), and Codex must determine the correct supported launch syntax/settings needed to enforce that configuration in this environment.

Before implementing anything below, check whether a prior session already implemented, satisfied, or explicitly dismissed part of this task as meritless (unneeded, already correct, or actively wrong for the product goal) — check `v2_Brain_Spec_2_Gap_Sheet.md`, `git log`, and the current state of the named files, not just this prompt's wording. Do not redo, re-argue, or re-implement anything already resolved or already rejected for a documented reason; only implement what is genuinely still open. If you find part of this prompt is itself meritless given current code, skip it and state why instead of implementing it anyway.

You are in the top-level BYO-Server repo. Start with `git status --short` and preserve any unrelated user edits. Patch GAP-09 from `v2_Brain_Spec_2_Gap_Sheet.md`. This is a normal-debugging-path issue, not edge-case hardening: a compliant agent inspecting memory can currently read prohibited or unknown regions, and some hardware registers are read-sensitive (read-to-clear, FIFO pops). Keep the policy lean — deny prohibited and unknown memory, allow everything mapped, and add no extra ceremony, logging framework, or per-read plan changes.

Tasks:

1. Add `SafetyPolicy.check_memory_read(board_id, address, size_bytes)` using `ActionCategory.MEMORY_READ`.
2. Tighten the `MEMORY_READ` allowed-kind table in `safety/regions.py:185` — it currently allows every `RegionKind`, so a containment check wired to it as-is would still permit prohibited-region reads.
3. Apply the check to `read_memory_address` for scalar and block reads, and to `read_memory_symbol` (use resolved symbol size when available, at least the requested width). Denials must name the region kind and a clear remedy.
4. Add tests proving prohibited/unknown reads are refused while mapped RAM/flash/peripheral reads pass — scalar, block, and symbol paths. These tests also re-cover the flash schema change from P4-01 at the runtime level via the shared suite below.
5. Update docs. If you discover a new genuine product gap, append it to `v2_Brain_Spec_2_Gap_Sheet.md`.

Required smoke tests:

```text
uv run --locked pytest tests/test_safety_enforcement.py tests/test_safety_regions.py tests/test_revised_memory_flash_misc.py
```

Acceptance: memory reads cannot touch prohibited or unknown regions, and ordinary mapped reads keep working exactly as before.

---

## P4-05 - Complete safety refresh scopes and honest safety-setup terminal status (GAP-08)
### Subagent required for all Claude and Codex test runs

All Claude and Codex test runs in this prompt must use a subagent; do not run a Claude or Codex test session directly. Every subagent launch must use the built-in default full-access/auto permission configuration. When Codex launches a Claude subagent for this prompt, it must configure that subagent to use Claude Sonnet 4.5 with effort low (not Haiku 4.5), and Codex must determine the correct supported launch syntax/settings needed to enforce that configuration in this environment.

Before implementing anything below, check whether a prior session already implemented, satisfied, or explicitly dismissed part of this task as meritless (unneeded, already correct, or actively wrong for the product goal) — check `v2_Brain_Spec_2_Gap_Sheet.md`, `git log`, and the current state of the named files, not just this prompt's wording. Do not redo, re-argue, or re-implement anything already resolved or already rejected for a documented reason; only implement what is genuinely still open. If you find part of this prompt is itself meritless given current code, skip it and state why instead of implementing it anyway.

You are in the top-level BYO-Server repo. Start with `git status --short` and preserve any unrelated user edits. Patch GAP-08 (narrowed scope) from `v2_Brain_Spec_2_Gap_Sheet.md`. Keep authority fail-closed: no caller-supplied ranges, and nothing here opens a gate.

Tasks:

1. Extend `board_safety_refresh` with bootloader artifact parameters (`bootloader_elf`, `bootloader_hex`, `bootloader_map`) mirroring the application parameters, rebuilding only bootloader-derived regions on bootloader drift — this is part of the real firmware loop for anyone iterating on a bootloader.
2. Add explicit statuses/remedies for pack drift, official-evidence drift, geometry drift, anchor changes, and unclear scope; unclear scope redirects to full `board_safety_setup`, per spec.
3. Replace the dead-end `safety_setup_research_required` for boards without complete reviewed catalog evidence (`server.py:2395-2428`) with an honest fail-closed terminal status (e.g. `safety_setup_unsupported_board`) whose `agent_prompt` says automatic safety evidence exists only for reviewed board types, names the reviewed list, and states what would extend it. Return no continuation ID when no public tool can consume it.
4. Ensure validation and guarded writes name the correct remedy: refresh for scoped artifact/evidence drift; full safety setup plus validation for anchor changes.
5. Update the `load_setup_tool` guidance for `board_safety_refresh` (from P4-02) and `docs/agent-contract.md`. Do **not** build a general agent-supplied evidence continuation for arbitrary boards — that is descoped pending an ADR; do not partially implement it. If you discover a new genuine product gap, append it to `v2_Brain_Spec_2_Gap_Sheet.md`.

Required smoke tests:

```text
uv run --locked pytest tests/test_safety_refresh.py tests/test_safety_map_build.py
```

Acceptance: bootloader build drift is first-class in refresh, and every non-completed safety-setup response is either actionable through a public tool or an honest terminal status — no response advertises a continuation nothing can consume.

---

## P4-06 - Dual-provider, model-pinned agent harness (GAP-19)
### Subagent required for all Claude and Codex test runs

All Claude and Codex test runs in this prompt must use a subagent; do not run a Claude or Codex test session directly. Every subagent launch must use the built-in default full-access/auto permission configuration. When Codex launches a Claude subagent for this prompt, it must configure that subagent to use Claude Sonnet 4.5 with effort low (not Haiku 4.5), and Codex must determine the correct supported launch syntax/settings needed to enforce that configuration in this environment.

Before implementing anything below, check whether a prior session already implemented, satisfied, or explicitly dismissed part of this task as meritless (unneeded, already correct, or actively wrong for the product goal) — check `v2_Brain_Spec_2_Gap_Sheet.md`, `git log`, and the current state of the named files, not just this prompt's wording. Do not redo, re-argue, or re-implement anything already resolved or already rejected for a documented reason; only implement what is genuinely still open. If you find part of this prompt is itself meritless given current code, skip it and state why instead of implementing it anyway. If either model's context runs out (or it otherwise cannot complete a step) while you are exercising the harness, skip only that model's portion and record why — do not fail the whole prompt or fabricate a result for the exhausted model.

You are in the top-level BYO-Server repo. Start with `git status --short` and preserve any unrelated user edits. Patch GAP-19 from `v2_Brain_Spec_2_Gap_Sheet.md`. Context you must honor: this server has only ever been agent-tested with Codex, the agent-run layer in `src/pyocd_debug_mcp/benchmark_support.py` hardwires the Codex CLI (`_ensure_codex_registration` at ~654-663, `_run_codex` at ~666-707, Codex-specific session-dir reconciliation), and no agent run has ever pinned a model. Both CLIs are installed on this host (claude 2.1.76 at `claude`, codex-cli 0.142.2 at `codex`). Cost policy, non-negotiable: every Claude invocation anywhere in this repo's tests, harnesses, or docs must use Claude Sonnet 4.5 with effort low (not Haiku 4.5), and the harness must hard-refuse any other Claude model/effort configuration; Codex invocations must pin the Codex 5.6 luna model. Codex must determine the exact supported Claude CLI flags/settings needed to enforce Sonnet 4.5 plus low effort in this environment and fail closed if it cannot. This prompt builds the harness only - it must not make any live model call; live runs happen in P4-08 and P4-10.

Tasks:

1. Refactor the agent-run layer into a provider abstraction (keep the existing behavior as the `codex` provider). Each provider owns: registration preflight, headless invocation of one prompt, structured-result retrieval, and run-artifact capture. Preserve the existing Codex result parsing and session reconciliation unchanged.
2. Add a `claude` provider using Claude Code headless mode: invoke `claude -p "<prompt>"` with the built-in default full-access/auto subagent permission configuration and with Claude Sonnet 4.5 plus effort low enforced. Codex must determine the exact supported launch flags/settings for the installed Claude CLI in this environment rather than guessing; if the CLI cannot be configured to that exact model/effort pair, fail closed. Keep MCP registration scoped to this checkout (prefer a per-run `--mcp-config` JSON pointing at `uv run --project <checkout> --locked pyocd-debug-mcp` over mutating the user's global config; also document the `claude mcp add pyocd-debug -- uv run --project <checkout> --locked pyocd-debug-mcp` alternative). The claude CLI has no `--output-schema`: obtain the structured result by instructing the model in the prompt to write the exact result JSON object to a provider-supplied absolute file path, then validate that file with the same parser/schema used for Codex results, treating a missing or invalid file as a failed run.
3. Pin the Codex provider's model: pass an explicit `--model` set to the Codex 5.6 luna identifier as accepted by the installed CLI (resolve the exact string from `codex exec --help` / the CLI's model listing rather than guessing; if the exact 5.6 luna identifier cannot be resolved, stop and report — do not silently fall back to a default model).
4. Record provider name, exact model string, CLI version, prompt, and timestamps in every run artifact, and surface them in the run report JSON.
5. Add unit tests that exercise both providers end-to-end against mocked CLI executables (stub `claude`/`codex` scripts on PATH or injected launchers): registration missing, CLI absent, model/effort-pin refusal (a Claude provider constructed with anything other than Sonnet 4.5 with effort low must raise), result file missing, result invalid, permission-profile misconfiguration, and the happy path. pytest must stay offline and model-free.
6. Update README (`mcpServers` example plus `claude mcp add` and `codex mcp add` registration commands) and `docs/verification.md` so the agent benchmark is no longer described as Codex-specific. If you discover a new genuine product gap, append it to `v2_Brain_Spec_2_Gap_Sheet.md`.

Required smoke tests:

```text
uv run --locked pytest tests/test_r11_benchmark.py tests/test_s4_benchmark_isolation.py
```

plus the new provider test module you add.

Acceptance: one harness drives either agent CLI with a pinned model; a Claude run cannot be started with anything but Sonnet 4.5 with effort low and the built-in default full-access/auto subagent permission profile; no pytest path invokes a real model; every run artifact names its provider, model, effort, permission profile, and CLI version.

---

## P4-07 - Consolidated software verification checkpoint
### Subagent required for all Claude and Codex test runs

All Claude and Codex test runs in this prompt must use a subagent; do not run a Claude or Codex test session directly. Every subagent launch must use the built-in default full-access/auto permission configuration. When Codex launches a Claude subagent for this prompt, it must configure that subagent to use Claude Sonnet 4.5 with effort low (not Haiku 4.5), and Codex must determine the correct supported launch syntax/settings needed to enforce that configuration in this environment.

Before doing anything below, check whether a prior session already implemented, satisfied, or explicitly dismissed part of this task as meritless (unneeded, already correct, or actively wrong for the product goal) — check `v2_Brain_Spec_2_Gap_Sheet.md`, `git log`, and the current state of the named files, not just this prompt's wording. Do not redo, re-argue, or re-implement anything already resolved or already rejected for a documented reason; only do what is genuinely still open. If you find part of this prompt is itself meritless given current code, skip it and state why instead of doing it anyway.

You are in the top-level BYO-Server repo. Start with `git status --short` and preserve any unrelated user edits. Run this after P4-01 through P4-06. This prompt implements almost nothing: it is the one full software verification pass. The full pytest run subsumes every focused suite from earlier prompts and the execution-verified suites for GAP-06, GAP-07, GAP-13, GAP-14, GAP-15, GAP-16, and GAP-17 — do not run those focused suites separately, and do not re-implement anything `v2_Brain_Spec_2_Gap_Sheet.md` marks "already implemented"; if one of its claims turns out false, patch only the failing piece. Note the large post-audit `zephyr_build.py` rework (workspace/SDK reuse, cache locking, build-dir ownership) has so far only been inspected and unit-run in isolation — this full pass is its execution gate; treat any `tests/test_zephyr_build.py` failure as a genuine product bug, not test debt.

Tasks:

1. Run the full software suite and static checks below.
2. Update `v2_Brain_Spec_2_Gap_Sheet.md`: move gaps patched by P4-01 through P4-06 into its "already implemented" section with file/line citations, and append any new genuine gaps discovered.
3. Update docs/evidence with exact commands, dates, tool versions, and outcomes.

Required commands:

```text
uv run --locked pytest
uv run --locked ruff check .
uv run --locked pyright
uv build
uv run --locked python - <<'PY'
import asyncio, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "pyocd_debug_mcp.server"])
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert any(tool.name == "initialization_handshake" for tool in tools)
asyncio.run(main())
print("stdio smoke passed")
PY
```

Acceptance: everything is green in one pass and the gap sheet reflects reality. If anything fails, patch minimally, rerun only the failing check, then the full suite once more.

---

## P4-08 - Board-free dual-agent contract smoke (GAP-20 part 1)
### Subagent required for all Claude and Codex test runs

All Claude and Codex test runs in this prompt must use a subagent; do not run a Claude or Codex test session directly. Every subagent launch must use the built-in default full-access/auto permission configuration. When Codex launches a Claude subagent for this prompt, it must configure that subagent to use Claude Sonnet 4.5 with effort low (not Haiku 4.5), and Codex must determine the correct supported launch syntax/settings needed to enforce that configuration in this environment.

Before doing anything below, check whether a prior session already ran part of this scenario, already captured passing evidence for it, or explicitly dismissed part of it as meritless (unneeded, already proven, or actively wrong for the product goal) — check `v2_Brain_Spec_2_Gap_Sheet.md`, `docs/evidence/`, and `git log`, not just this prompt's wording. Do not re-run a scenario that already produced clean, current evidence, and do not re-argue a dismissal that was already documented with a reason; only run what is genuinely still open. If you find part of this prompt is itself meritless given current code or existing evidence, skip it and state why instead of running it anyway. If either model's context runs out (or it otherwise cannot complete its run) partway through, skip only that model's portion of this smoke, record why, and still report the other model's result — do not fail the whole prompt or fabricate a result for the exhausted model.

You are in the top-level BYO-Server repo. Run only after P4-07 is green. No hardware is required or permitted: this prompt proves that real agent models can follow the server's interaction contract purely from returned payloads, on exactly the paths the later hardware run never re-exercises - the "no board" refusal, the no-unlisted-tool discipline on an empty bench, and NULL-plan comprehension without execution. This is the first Claude-driven use of this server ever, so treat any Claude-side friction (tool-name prefixing problems, permission prompts blocking MCP calls, context blowups from handshake/NULL-plan text, misread payloads) as a genuine product gap to record, not as a test annoyance. Cost policy: the Claude runs must use Sonnet 4.5 with effort low - the P4-06 harness already enforces this; do not override it. The Codex runs must use the pinned Codex 5.6 luna model. Bound each agent turn with the harness timeout; two models x one scenario is the whole budget - do not add extra scenarios or repeat runs that already produced clean transcripts.

Tasks:

1. Using the P4-06 harness, run the same board-free scenario once per provider (Claude Sonnet 4.5 with effort low, then Codex 5.6 luna): the agent connects to the MCP server, calls `initialization_handshake`, is told by the (simulated) user that no board is connected, and is then asked to describe what setup would require for a hypothetical new board — which should lead it to read the all-NULL `board_setup-plan` without executing setup, hardware, or validation actions.
2. Assert from each recorded transcript/timeline: `setup_overview` was called with the no-board answer and the agent stopped hardware-path work on the `setup_no_board` status; no tool outside the advertised list was requested; the NULL-plan response was read without submitting a populated plan, inventing parameter fields, or fabricating internal IDs; and no internal IDs, JSON, or continuation tokens were surfaced in user-facing prose.
3. For the Claude run additionally record whether MCP tool calls flowed without interactive permission blocking and whether the session stayed within a sane context budget; if either fails, that is a product/docs gap — record it.
4. Store both evidence bundles (prompt, provider, exact model string, CLI version, full MCP timeline, transcript, pass/fail per assertion) under `docs/evidence/agent-contract-smoke-<provider>-<date>/`, and summarize both runs in `docs/verification.md`.
5. If either model violates the contract because a server payload was genuinely unclear (not model negligence), append the specific gap to `v2_Brain_Spec_2_Gap_Sheet.md`, patch the payload text minimally, rerun only the affected suites plus this scenario for the failing provider, and keep both the failing and passing evidence.

Acceptance: both a Claude Sonnet 4.5 low-effort session and a Codex 5.6 luna session follow the handshake, refuse hardware work with no board, and read NULL-plan teaching correctly, with recorded evidence - closing the board-free half of GAP-20 (or the harness honestly recorded why one model's context ran out and could not).

---

## P4-09 - Non-destructive hardware smoke: clean-root setup and validation only
### Subagent required for all Claude and Codex test runs

All Claude and Codex test runs in this prompt must use a subagent; do not run a Claude or Codex test session directly. Every subagent launch must use the built-in default full-access/auto permission configuration. When Codex launches a Claude subagent for this prompt, it must configure that subagent to use Claude Sonnet 4.5 with effort medium (not Haiku 4.5), and Codex must determine the correct supported launch syntax/settings needed to enforce that configuration in this environment.

Before doing anything below, check whether a prior session already ran this smoke and captured current, passing evidence, or already dismissed part of it as meritless (unneeded, already proven, or actively wrong for the product goal) — check `v2_Brain_Spec_2_Gap_Sheet.md`, `docs/evidence/`, and `git log`, not just this prompt's wording. Do not repeat a run that already produced clean, current evidence, and do not re-argue an already-documented dismissal; only do what is genuinely still open. If you find part of this prompt is itself meritless given current code or existing evidence, skip it and state why instead of running it anyway.

You are in the top-level BYO-Server repo with real hardware attached. Run only after P4-07 is green (P4-08 does not gate this prompt, but running it first is preferred so contract regressions are caught cheaply). This prompt is scripted (no agent model) and non-destructive: no flash, erase, bootloader write, or target unlock. If hardware is unavailable or identity mismatches, record the exact blocker as a blocked result and stop — do not simulate success and do not substitute another board.

Required user-provided bench inputs: familiar display name; reviewed board type (e.g. `nrf52840dk`); exact MCU part; stable probe UID; stable UART identity; baud rate; local authoritative datasheet PDF path; and a fresh artifact root with no `.firm/boards/<board>.yaml`.

Tasks:

1. Run the fresh-workspace runner in its setup-only barrier mode with the bench inputs, using the exact post-P4-01 CLI (per `--help`; the volatile `--uart-port` flag was removed with the `serial_port` schema field). Evidence lands at `<artifact_root>/acceptance/fresh-setup-evidence.json`.
2. Verify no profile exists before setup and exactly one schema-v2 `.firm/boards/<board_id>.yaml` exists only after live identity succeeds.
3. Verify `board_validate` passes in the same Server Run and `get_setup_status.ready_for_code` is true.
4. Restart the MCP server and verify the gate is closed until validation is repeated.
5. Copy the evidence JSON (MCP timeline, report IDs/hashes, readiness) into `docs/evidence/fresh-setup-hardware-<date>.json` and record the exact command used.

Required hardware smoke command template (adjust flags only to match the real post-P4-01 `--help` output):

```text
uv run --locked python scripts/run_fresh_workspace_e2e.py \
  --artifact-root <fresh-root> \
  --board-id <board_id> \
  --display-name "<BOARD_NAME>" \
  --board-type <BOARD_TYPE> \
  --mcu-part-number <MCU_PART> \
  --probe-uid <PROBE_UID> \
  --uart-id <UART_ID> \
  --baudrate <BAUD> \
  --datasheet-path <DATASHEET_PDF> \
  --authorize-setup
```

Acceptance: setup-first readiness is proven on real hardware with zero destructive actions, the server resolved the UART port itself from the stable identity, and the recorded evidence shows the full MCP timeline from handshake to readiness barrier.

---

## P4-10 - Full hardware acceptance driven by real agents: Sonnet 4.5 medium-effort journey plus Codex GPT 5.4 parity pass (GAP-20 part 2)
### Subagent required for all Claude and Codex test runs

All Claude and Codex test runs in this prompt must use a subagent; do not run a Claude or Codex test session directly. Every subagent launch must use the built-in default full-access/auto permission configuration. When Codex launches a Claude subagent for this prompt, it must configure that subagent to use Claude Sonnet 4.5 with effort medium (not Haiku 4.5), and Codex must determine the correct supported launch syntax/settings needed to enforce that configuration in this environment.

Before doing anything below, check whether a prior session already ran part of this acceptance and captured current, passing evidence, or already dismissed part of it as meritless (unneeded, already proven, or actively wrong for the product goal) — check `v2_Brain_Spec_2_Gap_Sheet.md`, `docs/evidence/`, and `git log`, not just this prompt's wording. Do not repeat a phase that already produced clean, current evidence, and do not re-argue an already-documented dismissal; only do what is genuinely still open. If you find part of this prompt is itself meritless given current code or existing evidence, skip it and state why instead of running it anyway. If either model's context runs out (or it otherwise cannot complete its journey/pass) partway through, stop that model's phase at the last verifiable step, record why and what was and was not proven, and do not fabricate completion for the exhausted model — the other model's evidence still stands on its own.

You are in the top-level BYO-Server repo with real hardware attached. Run only after P4-08 and P4-09 pass on a recoverable board and the user has authorized application flashing. This prompt closes the hardware half of GAP-20: the server has never been driven by a Claude model, so the primary journey here is executed by a Claude Sonnet 4.5 medium-effort agent session through the P4-06 harness, not by a script. Cost policy: Claude runs use Sonnet 4.5 with effort medium only; Codex runs use the pinned GPT 5.4 model. This prompt does not run `target_unlock`, mass erase, or bootloader flash, and permission is passed only through the plan tools — conversation alone is never authorization. Do not repeat work: the application is built once, software suites are not rerun here unless a hardware step forces a code change (then rerun only the affected suites, then the full suite once).

Tasks:

1. Start from the freshly validated artifact root from P4-09. Create or reuse a simple Zephyr application with a UART shell/console supporting at least: `blink on` -> `BLINK ON`; `blink status` -> `BLINK STATUS: ON` or `OFF`; `blink off` -> `BLINK OFF`. Build it once using the exact command returned in `get_setup_status.build_guidance` (the `python -m pyocd_debug_mcp.zephyr_build ...` module form).
2. **Sonnet 4.5 medium-effort full journey (primary):** through the P4-06 harness, a Claude Sonnet 4.5 medium-effort session performs `initialization_handshake`; `setup_overview` with the familiar board name; `board_validate` via the loader; `board_safety_refresh` with the built application ELF/HEX/map (verify it cannot widen the reviewed deployment envelope); `flash_application-plan` (all-NULL first, then the populated plan, with user permission relayed conversationally and passed only through the plan parameter); `flash_application`; then `serial_exchange-plan` and one `serial_exchange` conversation: `blink on`/`BLINK ON`, `blink status`/`BLINK STATUS: ON`, `blink off`/`BLINK OFF`, `blink status`/`BLINK STATUS: OFF`.
3. Assert from the Sonnet transcript/timeline: after plan acceptance the session called the **dynamically exposed direct action** (Claude Code refreshes on `tools/list_changed`), not the static `action_batch` fallback — this is the first live proof of the dynamic path; every guarded call carried its plan-bound exact parameters; no unlisted tool was requested; no internal IDs leaked into user-facing prose.
4. **Codex GPT 5.4 parity pass (bounded):** a Codex session then re-proves cross-client parity on the same board and already-built artifact — `initialization_handshake`, `setup_overview`, `board_validate`, `flash_application-plan`/`flash_application`, and one shortened `serial_exchange` (`blink on`/`BLINK ON`, `blink off`/`BLINK OFF`). No rebuild, no repeated safety refresh (the map is already current for this artifact); note whether Codex used the direct action or the exact returned static fallback, and assert whichever path it used was followed unchanged.
5. Record evidence in `docs/evidence/` per provider: prompt, provider, exact model string, CLI version, MCP timeline, plan IDs, report hashes, artifact hashes, flash result, UART transcript, dynamic-vs-fallback determination, and cleanup/final board state. Update `docs/verification.md`'s client matrix: Claude/Sonnet 4.5 medium-effort and Codex/GPT 5.4 live acceptance statuses.
6. If any step fails, determine whether it is model negligence or a genuine server/product gap; for genuine gaps, append to `v2_Brain_Spec_2_Gap_Sheet.md`, patch, rerun the affected software suites, then retry only the minimum necessary hardware phase for the affected provider.

Acceptance: the evidence shows the ideal New Brain journey executed end to end by a real Claude Sonnet 4.5 medium-effort agent — setup first, no hidden assumptions, safety refresh from build artifacts, guarded application flash through the dynamic path, state-preserving UART validation — plus a bounded Codex GPT 5.4 run proving the same server serves both client families. GAP-19 and GAP-20 close only when both providers' evidence is recorded (or the header's context-exhaustion carve-out was invoked and documented for the affected model).
