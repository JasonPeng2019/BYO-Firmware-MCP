## P3-01 - Make plan/action schemas truthful and minimal (GAP-05, GAP-18, GAP-10, GAP-12)

You are in the top-level BYO-Server repo. Start with `git status --short` and preserve any unrelated user edits. Patch GAP-05, GAP-18, GAP-10, and GAP-12 from `New_Brain_Spec_Gap_Sheet.md`. The product priority is flexibility and ease of use for a compliant agent and a regular firmware developer: every schema field must be something the agent can actually obtain, and no schema may advertise a parameter the runtime always refuses. Keep authority fail-closed throughout: no caller-supplied allowed memory ranges, no persisted gates or permissions.

Tasks:

1. GAP-18 (regression — fix first): remove `serial_port` from the `board_setup` plan/action schema. Keep `serial_id` (the stable choice from setup inventory) as the agent-supplied selector, and make the server resolve the current port path from that stable identity at execution time, exactly as the spec's attachment-cache section prescribes ("a change from COM7 to COM11 does not prompt again"). Record the resolved path in setup reports. Update `tools/setup.py` handlers, preflight filtering, and the fresh-workspace runner (`scripts/run_fresh_workspace_e2e.py`) accordingly — the runner should pass only the stable UART identity.
2. GAP-05: make `datasheet_sha256` nullable in the `board_setup` plan/action schema. When null, the server binds its own computed digest before commit (it already computes and enforces the digest internally, so this is a schema change, not an authority change). Continue accepting an agent-supplied digest as an optional cross-check; keep reviewed-byte enforcement and keep recording the digest. Do not add a new hashing tool.
3. GAP-10: remove `target_address` from the `flash_application-plan` and `flash_bootloader-plan` plan/action schemas — the ELF/HEX backend rejects every non-null value, so the field is a guaranteed-failure trap. State in the NULL prompts that load addresses come from the artifact.
4. GAP-12: in the same pass, reconcile `Plan_Prompt_Contents_Spec.md` with the final schemas — the flash sections currently document `{artifact_path, halt_after}` while live definitions use different fields; audit every tool section against `plan_defs.py`. Then generalize the sync test in `tests/test_plan_prompt_contents.py` to iterate every `PLAN_DEFINITIONS` entry (action name, plan name, exact action-parameter field names, budget mode, permission mode) instead of the two hand-picked tools, so no section can drift silently again.
5. Update NULL-prompt examples, contracts, README, and `docs/agent-contract.md` for all removed/nullable fields. If you discover a new genuine product gap, append it to `New_Brain_Spec_Gap_Sheet.md`.

Required smoke tests (schema layer only — setup-flow and flash runtime behavior are exercised again by later prompts, so do not run their suites here):

```text
uv run --locked pytest tests/test_plan_defs.py tests/test_plan_engine.py tests/test_plan_prompt_contents.py tests/test_m5_surface_contract.py tests/test_product_server_contract.py
```

Acceptance: no plan schema requires a value the agent cannot reliably obtain, no schema advertises a parameter the runtime always refuses, and the human spec cannot drift from any live plan definition without a failing test.

---

## P3-02 - Make setup responses self-guiding end to end (GAP-01, GAP-03, GAP-04, GAP-11)

You are in the top-level BYO-Server repo. Start with `git status --short` and preserve any unrelated user edits. Run this after P3-01 so route templates are written once against the final schemas. Patch GAP-01, GAP-03, GAP-04, and GAP-11 from `New_Brain_Spec_Gap_Sheet.md`. Keep user-facing setup conversational: the user is never asked for JSON, board IDs, connection IDs, continuation IDs, port paths, or permission enums — the server supplies every internal value machine-readably so the agent copies instead of guessing or scraping. Keep responses lean and bounded; do not add ceremony or edge-case hardening beyond what the tasks name.

Tasks:

1. GAP-01: make `setup_overview(["no board"])` and case/Unicode-normalized equivalents return the same safe `setup_no_board` status as `setup_overview([])`, with no route emitted. Reject mixed lists containing `no board` plus other names with a clarification status that tells the agent to re-ask conversationally — again with no route emitted. Update the `setup_overview` docstring and initialization-handshake prose so `no board` reads as a literal sentinel, never a candidate board name.
2. GAP-03: make `load_setup_tool` return real guidance keyed to the requested `tool_name` only (`board_setup-plan`, `board_validate`, `board_safety_setup`, `board_safety_refresh`): purpose, exact next call with argument shape, expected statuses, accepted response shape, common remedies, when not to use the tool, and the no-internals conversational rule. Each of the four responses must be distinct and bounded — no whole-manual dumps that bloat agent context.
3. GAP-04: extend every `setup_overview` route with machine-readable next-call composition: `load_call` (exact `load_setup_tool` arguments), `next_call` where no plan is needed, and for setup routes a `plan_action_parameters_template` with all server-known fields pre-filled (`board_id`, `mode`, and `connection_id`/`serial_id` when exactly one candidate exists) and only genuine user facts marked as needed. Add `port_path` and stable USB identity to `serial_choices` rows. Make `required_user_facts` truthful and complete for the final P3-01 schema (UART selection and baud rate included). When hardware is ambiguous, include friendly choices plus an `accepted_response` shape. Optionally add `known_board_types` beside `supported_reviewed_board_types` so the agent can truthfully tell the user which board types are known but not automatic.
4. GAP-11: for `validation_needs_user_input`, populate `accepted_response` with the exact retry call and field mapping — `board_validate(board_id=..., probe_id="<one choice_id>")` or `serial_id` as appropriate. Keep `accepted_response: None` for terminal statuses.
5. Update tests and docs so they assert the agent never has to invent `connection_id`, `serial_id`, hash commands, or validation retry fields. If you discover a new genuine product gap, append it to `New_Brain_Spec_Gap_Sheet.md`.

Required smoke tests (these also re-cover P3-01's setup schema changes at the workflow level):

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

## P3-03 - Separate normal connect from guarded connect override (GAP-02)

You are in the top-level BYO-Server repo. Start with `git status --short` and preserve any unrelated user edits. Patch GAP-02 from `New_Brain_Spec_Gap_Sheet.md`. This matters for a normal compliant agent: the visible `connect` schema currently advertises `unique_id`, `target`, and `board_config`, which invites an unplanned wrong-probe/wrong-target connection that the spec reserves for guarded `connect_override`.

Tasks:

1. Make public `connect` profile-only: schema requires `board_id` and exposes no `unique_id`, `target`, or `board_config` fields.
2. If internals still pass override values to the shared implementation, reject them at the public handler boundary with a redirect to `connect_override-plan`.
3. Keep hidden `connect_override` capable of run-scoped probe/target/config overrides after `connect_override-plan`.
4. Add a regression proving `action_batch` cannot use visible `connect` to smuggle override fields.
5. Update tool contracts, README, `docs/agent-contract.md`, and tests. If you discover a new genuine product gap, append it to `New_Brain_Spec_Gap_Sheet.md`.

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

## P3-04 - Memory-read containment (GAP-09)

You are in the top-level BYO-Server repo. Start with `git status --short` and preserve any unrelated user edits. Patch GAP-09 from `New_Brain_Spec_Gap_Sheet.md`. This is a normal-debugging-path issue, not edge-case hardening: a compliant agent inspecting memory can currently read prohibited or unknown regions, and some hardware registers are read-sensitive (read-to-clear, FIFO pops). Keep the policy lean — deny prohibited and unknown memory, allow everything mapped, and add no extra ceremony, logging framework, or per-read plan changes.

Tasks:

1. Add `SafetyPolicy.check_memory_read(board_id, address, size_bytes)` using `ActionCategory.MEMORY_READ`.
2. Tighten the `MEMORY_READ` allowed-kind table in `safety/regions.py` — it currently allows every `RegionKind`, so a containment check wired to it as-is would still permit prohibited-region reads.
3. Apply the check to `read_memory_address` for scalar and block reads, and to `read_memory_symbol` (use resolved symbol size when available, at least the requested width). Denials must name the region kind and a clear remedy.
4. Add tests proving prohibited/unknown reads are refused while mapped RAM/flash/peripheral reads pass — scalar, block, and symbol paths. These tests also re-cover the flash schema change from P3-01 at the runtime level via the shared suite below.
5. Update docs. If you discover a new genuine product gap, append it to `New_Brain_Spec_Gap_Sheet.md`.

Required smoke tests:

```text
uv run --locked pytest tests/test_safety_enforcement.py tests/test_safety_regions.py tests/test_revised_memory_flash_misc.py
```

Acceptance: memory reads cannot touch prohibited or unknown regions, and ordinary mapped reads keep working exactly as before.

---

## P3-05 - Complete safety refresh scopes and honest safety-setup terminal status (GAP-08)

You are in the top-level BYO-Server repo. Start with `git status --short` and preserve any unrelated user edits. Patch GAP-08 (narrowed scope) from `New_Brain_Spec_Gap_Sheet.md`. Keep authority fail-closed: no caller-supplied ranges, and nothing here opens a gate.

Tasks:

1. Extend `board_safety_refresh` with bootloader artifact parameters (`bootloader_elf`, `bootloader_hex`, `bootloader_map`) mirroring the application parameters, rebuilding only bootloader-derived regions on bootloader drift — this is part of the real firmware loop for anyone iterating on a bootloader.
2. Add explicit statuses/remedies for pack drift, official-evidence drift, geometry drift, anchor changes, and unclear scope; unclear scope redirects to full `board_safety_setup`, per spec.
3. Replace the dead-end `safety_setup_research_required` for boards without complete reviewed catalog evidence with an honest fail-closed terminal status (e.g. `safety_setup_unsupported_board`) whose `agent_prompt` says automatic safety evidence exists only for reviewed board types, names the reviewed list, and states what would extend it. Return no continuation ID when no public tool can consume it.
4. Ensure validation and guarded writes name the correct remedy: refresh for scoped artifact/evidence drift; full safety setup plus validation for anchor changes.
5. Update the `load_setup_tool` guidance for `board_safety_refresh` (from P3-02) and `docs/agent-contract.md`. Do **not** build a general agent-supplied evidence continuation for arbitrary boards — that is descoped pending an ADR; do not partially implement it. If you discover a new genuine product gap, append it to `New_Brain_Spec_Gap_Sheet.md`.

Required smoke tests:

```text
uv run --locked pytest tests/test_safety_refresh.py tests/test_safety_map_build.py
```

Acceptance: bootloader build drift is first-class in refresh, and every non-completed safety-setup response is either actionable through a public tool or an honest terminal status — no response advertises a continuation nothing can consume.

---

## P3-06 - Consolidated verification checkpoint (verify-only gaps, runner residuals, full suite)

You are in the top-level BYO-Server repo. Start with `git status --short` and preserve any unrelated user edits. Run this after P3-01 through P3-05. This prompt implements almost nothing: it closes the verify-only gaps and runs the one full verification pass. The full pytest run subsumes every focused suite from earlier prompts and the verify-only suites for GAP-06, GAP-07, GAP-13, GAP-14, GAP-15, and GAP-17 — do not run those focused suites separately, and do not re-implement anything `New_Brain_Spec_Gap_Sheet.md` marks "Already implemented"; if one of its claims turns out false, patch only the failing piece.

Tasks:

1. GAP-16 residuals: align every doc/prompt/README reference to the fresh-workspace runner with its real CLI flags (`--artifact-root --board-id --display-name --board-type --mcu-part-number --probe-uid --uart-id --uart-port --baudrate --datasheet-path --timeout-seconds --authorize-setup`, minus whatever P3-01 changed for UART identity) and its real evidence path (`<artifact_root>/acceptance/fresh-setup-evidence.json`). Skip the optional evidence-validator unification unless it comes free.
2. Run the full software suite and static checks below.
3. Update `New_Brain_Spec_Gap_Sheet.md`: move gaps patched by P3-01 through P3-05 into its "Already implemented" section with file/line citations, and append any new genuine gaps discovered.
4. Update docs/evidence with exact commands, dates, tool versions, and outcomes.

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

Acceptance: everything is green in one pass, the gap sheet reflects reality, and no verify-only gap needed re-implementation. If anything fails, patch minimally and rerun only the failing check, then the full suite once more.

---

## P3-07 - Non-destructive hardware smoke: clean-root setup and validation only

You are in the top-level BYO-Server repo with real hardware attached. Run only after P3-06 is green. This prompt is non-destructive: no flash, erase, bootloader write, or target unlock. If hardware is unavailable or identity mismatches, record the exact blocker as a blocked result and stop — do not simulate success and do not substitute another board.

Required user-provided bench inputs: familiar display name; reviewed board type (e.g. `nrf52840dk`); exact MCU part; stable probe UID; stable UART identity; baud rate; local authoritative datasheet PDF path; and a fresh artifact root with no `.firm/boards/<board>.yaml`.

Tasks:

1. Run the fresh-workspace runner in its setup-only barrier mode with the bench inputs (flag names per the P3-06-aligned docs; evidence lands at `<artifact_root>/acceptance/fresh-setup-evidence.json`).
2. Verify no profile exists before setup and exactly one schema-v2 `.firm/boards/<board_id>.yaml` exists only after live identity succeeds.
3. Verify `board_validate` passes in the same Server Run and `get_setup_status.ready_for_code` is true.
4. Restart the MCP server and verify the gate is closed until validation is repeated.
5. Copy the evidence JSON (MCP timeline, report IDs/hashes, readiness) into `docs/evidence/fresh-setup-hardware-<date>.json` and record the exact command used.

Required hardware smoke command template:

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

Acceptance: setup-first readiness is proven on real hardware with zero destructive actions, and the recorded evidence shows the full MCP timeline from handshake to readiness barrier.

---

## P3-08 - Full hardware acceptance: setup -> build -> refresh -> flash_application -> serial_exchange

You are in the top-level BYO-Server repo with real hardware attached. Run only after P3-07 passes on a recoverable board and the user has authorized application flashing. This prompt does not run `target_unlock`, mass erase, or bootloader flash, and permission is passed only through the plan tools — conversation alone is never authorization.

Tasks:

1. Start from the freshly validated artifact root from P3-07.
2. Create or use a simple Zephyr application with a UART shell/console supporting at least: `blink on` -> `BLINK ON`; `blink status` -> `BLINK STATUS: ON` or `OFF`; `blink off` -> `BLINK OFF`.
3. Build using the exact command returned in `get_setup_status.build_guidance` (the `python -m pyocd_debug_mcp.zephyr_build ...` module form).
4. Call `board_safety_refresh` with the built application ELF/HEX/map and verify it cannot widen the reviewed deployment envelope.
5. Use `flash_application-plan` then `flash_application`, through direct call or the exact returned static-client fallback.
6. Use `serial_exchange-plan` then `serial_exchange` with one open UART conversation: `blink on`/`BLINK ON`; `blink status`/`BLINK STATUS: ON`; `blink off`/`BLINK OFF`; `blink status`/`BLINK STATUS: OFF`.
7. Record evidence in `docs/evidence/`: prompt, MCP timeline, plan IDs, report hashes, artifact hashes, flash result, UART transcript, and cleanup/final board state.
8. Software verification was completed in P3-06 — do not rerun suites here unless a hardware step forced a code change; in that case rerun only the suites covering the changed code, then the full suite once.

Acceptance: the evidence shows the ideal New Brain journey — setup first, no hidden assumptions, safety refresh from build artifacts, guarded application flash, and state-preserving UART validation. If any step fails, append the genuine gap to `New_Brain_Spec_Gap_Sheet.md`, patch, rerun the affected checks, and retry only the minimum necessary hardware phase.
