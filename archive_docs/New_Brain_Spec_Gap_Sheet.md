# New Brain Spec gap sheet for BYO-Server

Audit date: 2026-07-17

Revision 2 (2026-07-17): the codebase changed again after revision 1 and every gap was re-verified against the new tree. What changed: (a) `scripts/run_fresh_workspace_e2e.py` and `tests/test_fresh_workspace_runner.py` now exist, largely implementing GAP-16; (b) the `board_setup` plan gained two new **required** agent-supplied fields, `serial_id` and `serial_port` — this change is flagged as a regression against the product goal (new GAP-18) because the server does not expose a machine-readable source for `serial_port` and binding a volatile port path into an immutable plan contradicts the spec's stable-identity principle; (c) the plan-prompt spec's setup section and its sync test were updated for the new fields, but the flash sections remain drifted (GAP-12 still open). All other open gaps were re-verified unchanged. Open gaps keep their original IDs. Revision claims were verified by direct code inspection with file/line citations (this session cannot execute pytest); each "already implemented" entry names the focused test command that must pass before it is treated as closed.

Selection filter: this sheet intentionally lists only gaps that genuinely affect real firmware work — a compliant agent and a regular firmware developer setting up a board, building, flashing, and talking to it over UART. Edge-case-only defects that such a pair would be unlikely to ever hit are excluded on purpose; flexibility and ease of use for the agent and user outrank exhaustive hardening. Every open gap below either blocks/derails the normal workflow (GAP-01, 03, 04, 05, 11, 18), lets a compliant agent silently do the wrong thing because the schema invites it (GAP-02, 10), leaves a spec-mandated safety default unenforced on a normal debugging path (GAP-08, 09), or misleads whoever reads the contract docs (GAP-12).

Scope: top-level BYO-Server checkout compared against `New_Brain_Spec.md`, with emphasis on first-run setup, model-facing MCP usability, Layer-1 plan discoverability, and Layer-2 safety effectiveness.

## Evidence inspected

- `New_Brain_Spec.md`
- live MCP tool list and NULL-plan responses from `python -m pyocd_debug_mcp.server`
- `src/pyocd_debug_mcp/server.py`
- `src/pyocd_debug_mcp/tools/{setup,handshake,plans,session,memory,flash,serial,unlock}.py`
- `src/pyocd_debug_mcp/guardrails/{plan_defs,plan_engine,permissions}.py`
- `src/pyocd_debug_mcp/safety/{regions,map_build,refresh,enforce,verify2}.py`
- `src/pyocd_debug_mcp/setup_flow/{setup,validate,preflight,board_catalog,reviewed_evidence}.py`
- `scripts/validate_autonomous_acceptance.py`, `scripts/run_fresh_workspace_e2e.py`
- `Plan_Prompt_Contents_Spec.md`, `README.md`, `docs/agent-contract.md`, `docs/architecture.md`
- existing audits in `docs/new-brain-gap-audit.md` and `docs/adversarial-ideal-usability-gap-audit.md`

Focused verification run (original audit):

```text
uv run --locked pytest tests/test_setup_board_catalog.py tests/test_setup_workflow.py tests/test_plan_prompt_contents.py tests/test_initialization_handshake.py tests/test_uart_capture.py tests/test_safety_verify2.py
```

Original result: 95 passed, 1 failed. That failure was the original GAP-14; the failing test
has since been rewritten in the working tree exactly as GAP-14 prescribed, so rerun the same
command and confirm green before closing it (see "Already implemented" below).

## High-level result

The repo has a strong New Brain skeleton: hidden guarded actions, all-NULL plan teaching, immutable exact plan binding, board-scoped gates, FirmStore persistence, validation reports, static-client `action_batch` fallback, pinned two-source reviewed-evidence reconciliation, truthful reviewed-board advertising, advisory build guidance, negative UART-exchange coverage, and now a trusted setup-only fresh-workspace runner. The remaining open gaps concentrate exactly where the product prompt worried: unambiguous startup/setup routing, self-guiding setup responses so the model never invents internal fields — a burden the new `serial_id`/`serial_port` plan fields have made **heavier**, not lighter — and a few enforcement/spec-drift seams (`connect` overrides, memory-read containment, flash schema mismatch).

## Open gaps

### GAP-01 - `setup_overview(["no board"])` routes to first-time setup

**Spec target:** If the user says `no board`, setup, validation, and hardware actions must not begin.

**Verified current behavior (re-checked rev 2):** `_setup_overview` (`server.py` route loop, ~3515-3558) returns `setup_no_board` only for an empty list. A literal `["no board"]`, or a mixed list like `["left", "no board"]`, falls through the routing loop, gets a proposed `board_id` (e.g. `no_board`), and returns a `setup` route.

**Impact:** A compliant agent may pass the user's literal phrase and be instructed to create a bogus logical board named `no board`. The initialization handshake explicitly teaches the user to say "no board" (`tools/handshake.py:64`), so this is a first-contact failure, not an edge case.

**Patch:** Treat a singleton normalized `no board` as `setup_no_board`; reject any mixed list containing `no board` with a plain clarification prompt (no route emitted). Update the `setup_overview` docstring and handshake prose so `no board` is described as a literal sentinel, never a candidate name. Add unit and stdio contract tests for `["no board"]`, `["No Board"]`, and mixed lists.

### GAP-02 - Always-visible `connect` still accepts manual override fields

**Spec target:** `connect` is always available for normal profile-backed connection. Manual probe UID, target override, and external board config belong to guarded `connect_override` after `connect_override-plan`.

**Verified current behavior (re-checked rev 2):** The visible `connect` schema (`server.py:956-961`) still exposes `unique_id`, `target`, and `board_config` and passes them straight to `_connect_impl`.

**Impact:** An agent can accidentally connect to the wrong probe/target without the L1 override plan, defeating the guarded/`connect_override` split the spec defines.

**Patch:** Make public `connect(board_id)` profile-only. Reject non-null override values at the public handler boundary with a redirect to `connect_override-plan`. Keep override parameters only on hidden `connect_override`. Add a regression proving `action_batch` cannot smuggle override fields through visible `connect`. Update contracts, README, docs, and tests.

### GAP-03 - `load_setup_tool` does not load detailed setup workflow knowledge

**Spec target:** `Load_setup_tool(board_id, tool)` returns detailed setup/validation workflow instructions and unlocks the selected setup tool.

**Verified current behavior (re-checked rev 2):** `SetupToolLoadState.load` (`tools/setup.py:43-48`) still returns only `{status, board_id, tool_name, redirect}`. The per-tool docstrings and the NULL setup plan are detailed, but the loader — the spec's designated knowledge-delivery point — teaches nothing, and `board_validate`, `board_safety_setup`, and `board_safety_refresh` have no NULL-plan channel at all.

**Impact:** The model can pass the loader gate and still not know how to proceed safely or conversationally.

**Patch:** Return tool-specific guidance keyed to the requested `tool_name` only (not a whole-manual dump — keep each response bounded so it does not bloat agent context): purpose, exact next call with argument shape, expected statuses, accepted response shape, conversational no-internals rules, common remedies, and when not to use the tool. Add contract tests asserting each of the four loadable tools returns its own distinct guidance.

### GAP-04 - Setup routes do not provide exact next-call templates (worsened in rev 2)

**Spec target:** The server should keep setup easy for the agent while keeping internal IDs away from the user.

**Verified current behavior (re-checked rev 2):** Setup routes (`server.py:3532-3544`) still carry only `next_tool` and `required_user_facts`, and the `required_user_facts` list was **not** updated for the new plan fields — it names board type, MCU part, and datasheet, but the plan now also demands `serial_id`, `serial_port`, and `serial_baudrate`. The payload lists `connections` (with `connection_id`) and `serial_choices` (with only `choice_id` + `friendly_name`; no machine-readable `port_path`), and the route never composes any of it: no `load_call`, no `plan_action_parameters_template`, no resolution of `connection_id` or `serial_id` even when exactly one candidate exists.

**Impact:** The agent must assemble a nine-plus-field `action_parameters` object by joining route, connections, and serial rows itself, and must scrape the UART port path out of human-facing label text — precisely the "model doesn't know how to use setup" failure the product prompt called out.

**Patch:** For every route include `load_call` (exact `load_setup_tool` arguments), `next_call`, and, for setup routes, a `plan_action_parameters_template` with all server-known fields pre-filled (`board_id`, `mode`, `connection_id` and `serial_id` when unambiguous) and only genuine user facts marked as needed. Add machine-readable fields (`port_path`, and stable USB identity where known) to `serial_choices` rows. Keep `required_user_facts` truthful and complete (UART selection and baud rate included). For ambiguity, include friendly choices plus an `accepted_response` shape instead of making the agent guess. Optionally add a `known_board_types` list beside `supported_reviewed_board_types` (absorbs the residual of resolved GAP-07). Coordinate with GAP-18, which questions whether `serial_port` should be agent-supplied at all.

### GAP-05 - Setup asks the model to provide a SHA-256 the server can compute

**Spec target:** Users provide ordinary board facts and local authoritative documents; low-level facts should be server-owned when possible.

**Verified current behavior (re-checked rev 2):** `board_setup-plan` still requires `datasheet_sha256` as non-nullable TEXT (`plan_defs.py:202-206`; the example now at least shows the real reviewed digest). The server computes the real digest itself and rejects any mismatch, so the agent-supplied hash adds friction without adding assurance. Telling evidence: the new fresh-workspace runner has to compute the SHA-256 itself (`run_fresh_workspace_e2e.py`, `_sha256`) just to satisfy the plan schema — a pure-MCP agent without shell access cannot do that at all.

**Impact:** Many MCP agents cannot hash local files; even shell-capable agents get a needless out-of-band step in the critical first-setup path.

**Patch:** Make `datasheet_sha256` nullable in the plan/action schema; when null, the server binds its own computed digest before commit (the reviewed-evidence path already keys off the server-computed digest, so this is a schema change, not an authority change). Continue accepting an agent-supplied digest as an optional cross-check. Still record the digest and require reviewed bytes before attach/commit. Prefer this over adding a new hashing tool — do not grow the tool surface.

### GAP-08 - Safety setup/refresh remedies are incomplete (revised scope)

**Spec target:** When research is required, the server returns a focused prompt and the agent retries with requested evidence. Refresh handles linker/ELF, bootloader, pack, datasheet/evidence, and unclear scope.

**Verified current behavior (re-checked rev 2), two real seams:**

1. **No usable continuation for standalone safety setup.** `_run_board_safety_setup` (`server.py:2374+`) returns `safety_setup_research_required` with a `continuation_id`, but no public tool accepts that continuation (`continue_setup` resolves only setup-workflow continuations). For a board without complete reviewed catalog evidence this is a dead end dressed as a research prompt.
2. **Refresh is application-only.** `board_safety_refresh` still accepts only application ELF/HEX/map paths; no `bootloader_*` parameters exist anywhere in the tree.

**Impact:** The agent gets told research is required when research cannot succeed, and bootloader rebuilds cannot re-anchor the map.

**Patch (revised):**

- Extend `board_safety_refresh` with bootloader artifact parameters (`bootloader_elf/hex/map`) mirroring the application path, plus explicit statuses/remedies for pack drift, evidence drift, geometry/anchor changes, and unclear scope (unclear scope redirects to full safety setup, per spec).
- Replace the dead-end `safety_setup_research_required` for unreviewed boards with an explicit fail-closed terminal status (e.g. `safety_setup_unsupported_board`) whose `agent_prompt` truthfully says automatic safety evidence exists only for reviewed board types, names the reviewed list, and states what would extend it. Return no continuation ID when no public tool can consume it.

**Descoped:** a general agent-supplied official-document evidence continuation for arbitrary boards. The spec's research-handoff language supports it, but the repo has committed to a stronger pinned reviewed-evidence authority model (`setup_flow/reviewed_evidence.py`) that deliberately fails closed; grafting agent-supplied hardware evidence back on is a one-way-door authority decision, not a patch. If wanted, record it as an ADR and design it separately.

### GAP-09 - Raw memory reads and symbol reads do not apply map containment

**Spec target:** Unknown/prohibited memory is denied by default, and `ActionCategory.MEMORY_READ` exists.

**Verified current behavior (re-checked rev 2):** `SafetyPolicy` still has no `check_memory_read`; `read_memory_address` and `read_memory_symbol` (`tools/memory.py`) validate width/length/address syntax, then read with no region classification. `ActionCategory.MEMORY_READ` is still mapped to *every* region kind (`safety/regions.py:185`), so even a naive containment check wired to that table would permit prohibited-region reads.

**Impact:** Read access can touch prohibited or unknown regions; some hardware registers are read-sensitive (read-to-clear, FIFO pops), so this is a real behavioral hazard, not just hygiene.

**Patch:** Add `SafetyPolicy.check_memory_read`, tighten the `MEMORY_READ` allowed-kind set to exclude prohibited and unknown memory, call it from both raw and symbol reads (using resolved symbol size when available and at least the requested width), and deny with clear remedies. Cover scalar, block, and symbol paths in tests.

### GAP-10 - `target_address` is advertised for flash but runtime always rejects it

**Spec target:** If an explicit target address is part of the action, it should be validated; otherwise the plan should not ask for it.

**Verified current behavior (re-checked rev 2):** `flash_application-plan` and `flash_bootloader-plan` still include nullable `target_address` in `plan_defs.py`; `tools/flash.py` still rejects every non-null value ("unavailable for the current ELF/HEX backend").

**Impact:** The model can form a valid plan that is guaranteed to fail at execution — the exact schema-vs-runtime lie the New Brain design forbids.

**Patch:** Remove `target_address` from the public plan/action schema and state in the NULL prompt that load addresses must come from the artifact. Update contracts, `Plan_Prompt_Contents_Spec.md` (same pass as GAP-12), and tests proving the live plan schema cannot accept parameters the runtime always refuses.

### GAP-11 - `board_validate` choice responses lack an accepted-response recipe

**Spec target:** When a tool needs user input, the response should tell the agent how to ask conversationally and how to continue without exposing internals.

**Verified current behavior (re-checked rev 2):** `ValidationResult.to_payload()` (`setup_flow/validate.py:181`) still hardcodes `accepted_response: None` for every status, including `validation_needs_user_input`.

**Impact:** Ambiguous validation is harder for agents and more likely to leak internal IDs or stall.

**Patch:** For `validation_needs_user_input`, include `accepted_response` with the exact retry tool and field mapping, e.g. `board_validate(board_id=..., probe_id="<one choice_id>")` or `serial_id` as appropriate. Keep `None` for terminal statuses. Add tests per choice type.

### GAP-12 - `Plan_Prompt_Contents_Spec.md` flash sections drift from live schemas (narrowed; re-confirmed rev 2)

**Spec target:** The plan prompt spec should match live MCP behavior.

**Verified current behavior (re-checked rev 2):** The setup section and its sync test were updated for the new `serial_id`/`serial_port` fields, and `serial_exchange` remains covered. But the flash sections still document `action_parameters: {artifact_path, halt_after}` (spec lines ~581-623) while live definitions use `{artifact, target_address}` — and the sync test still hand-picks only `board_setup` and `serial_exchange` (`tests/test_plan_prompt_contents.py:207-211`), so flash and every other section can drift silently.

**Impact:** A developer or agent using the top-level spec for flash tools implements or calls the wrong schema.

**Patch:** Reconcile the flash (and any other unsynced) sections with `plan_defs.py` in the same pass that resolves GAP-10, then extend the sync test to iterate **every** `PLAN_DEFINITIONS` entry — action name, plan name, action-parameter field names, budget mode, permission mode — so no section can drift again.

### GAP-16 - Fresh-workspace runner (largely implemented in rev 2; small residuals)

**Spec target:** A bounded runner should drive real MCP stdio through handshake, setup, readiness, evidence capture, then stop before coding unless `ready_for_code` is true.

**Verified current behavior (new in rev 2):** `scripts/run_fresh_workspace_e2e.py` exists and is well-shaped: explicit CLI identity inputs only (`--artifact-root --board-id --display-name --board-type --mcu-part-number --probe-uid --uart-id --uart-port --baudrate --datasheet-path --timeout-seconds --authorize-setup`), no code-generation/build/flash/callback/argv surface, hard `ready_for_code` barrier, timeout-bounded, atomic evidence write to `<artifact_root>/acceptance/fresh-setup-evidence.json` with an operations timeline. `tests/test_fresh_workspace_runner.py` covers readiness success, non-success stop, validation refusal, false readiness, UART/probe identity mismatch, and a no-arbitrary-execution CLI assertion.

**Residuals (small):**

1. Runner docs/prompt templates elsewhere still describe pre-implementation flag names (`--board-name`, `--mcu-part`, `--uart`, `--setup-only`, `--evidence-out`); align them with the real CLI so a hardware run doesn't start with a wrong command.
2. Run the runner test suite to confirm green: `uv run --locked pytest tests/test_fresh_workspace_runner.py`.
3. Optional, not required: routing the runner's evidence JSON through `scripts/validate_autonomous_acceptance.py` would give one evidence authority instead of two formats. The runner's own tests already validate its behavior, so skip this unless it comes free during hardware acceptance.

### GAP-18 - NEW (rev 2, regression): `board_setup` plan now demands `serial_id` and `serial_port` the agent cannot reliably obtain

**Product-goal target (from the original audit prompt):** setup must be easy for the agent; the model should never have to invent or scrape internal identifiers; the server resolves everything it can itself.

**Verified current behavior:** The `board_setup` plan action parameters grew two new **required** fields (`plan_defs.py:187-196`): `serial_id` ("stable UART identity selected from current setup inventory") and `serial_port` ("current UART port path paired with serial_id"). They are threaded through `board_setup`/`board_fix_setup` (`tools/setup.py`) and used by preflight to filter serial candidates. But:

1. `setup_overview.serial_choices` exposes only `choice_id` and a human-facing `friendly_name` — there is **no machine-readable `port_path`**, so the agent's only source for `serial_port` is scraping text meant for the user (or asking the user for a COM path, which the spec forbids).
2. Binding a volatile port path into an immutable exact-parameter plan contradicts the spec's stable-identity principle ("A change from COM7 to COM11 does not prompt again" — the attachment cache exists precisely so the *server* resolves the current port from stable USB identity). A port renumbering between plan creation and execution invalidates the plan for a reason invisible to the user.
3. The setup route's `required_user_facts` was not updated, so the route under-declares what the plan actually needs (see GAP-04).

**Why it happened / what's good about it:** the intent — deterministically binding one UART to the setup — is right; the mechanism (agent-supplied volatile path) is what's wrong.

**Patch (pick one, prefer the first):**

- **Server-resolved port (spec-aligned):** keep `serial_id` (stable choice) as the agent-supplied selector and drop `serial_port` from the plan schema; the server resolves the current port path from the stable identity at execution time, exactly as the attachment-cache section of the spec prescribes. Record the resolved path in reports.
- **Machine-readable exposure (minimum):** if `serial_port` must stay, add `port_path` and stable USB identity to `serial_choices` rows and pre-fill both fields in the GAP-04 route template so the agent copies, never scrapes.

Add tests either way: the agent-facing payload must contain, in machine-readable form, every value any setup plan field can require.

## Already implemented in the current tree (verify, don't re-patch)

Each item below was prescribed by the original audit and already exists in the working tree.
Before closing it, run the named focused tests; add coverage only if a listed test is missing.

### GAP-06 - Two-source reconciliation in automatic safety setup — implemented

`_build_automatic_catalog_safety` routes through `setup_flow/reviewed_evidence.load_reviewed_evidence`, which verifies evidence-asset SHA-256 pins, binds official evidence to the supplied datasheet digest, checks the installed pyOCD target/SVD identity hashes, calls `reconcile_hardware_evidence`, fails closed on any conflict, and persists both source documents/hashes via the source record. Negative-path tests exist in `tests/test_reviewed_setup_evidence.py` (missing pins, asset/runtime drift, reconciliation conflict, cross-binding). Verify: `uv run --locked pytest tests/test_reviewed_setup_evidence.py tests/test_safety_verify2.py`.

### GAP-07 - Reviewed setup support advertised too broadly — implemented

`_setup_overview` returns `supported_reviewed_board_types: list(reviewed_setup_board_types())`, so unreviewed catalog entries are no longer advertised as automatic setup. The optional `known_board_types` companion list is folded into GAP-04's route improvements. Verify: `uv run --locked pytest tests/test_setup_board_catalog.py tests/test_setup_workflow.py`.

### GAP-13 - Build guidance after setup readiness — implemented

`get_setup_status` returns an advisory-only `build_guidance` block with the reviewed Zephyr board target, an exact runnable module command (`python -m pyocd_debug_mcp.zephyr_build ...`, PowerShell-quoted on Windows with short-scratch-path handling), and an explicit safety-boundary warning that guidance never authorizes flash. Verify: `uv run --locked pytest tests/test_setup_tools.py`.

### GAP-14 - Failing catalog test — fixed

`tests/test_setup_board_catalog.py` now expects the digest-mismatch rejection when passing a wrong digest, and adds the prescribed fail-closed tests for empty datasheet allowlists plus internally-computed-hash enforcement. Verify: `uv run --locked pytest tests/test_setup_board_catalog.py`.

### GAP-15 - `serial_exchange` stop-on-failure regression coverage — implemented

`tests/test_uart_capture.py` contains exactly the three prescribed negative tests: readiness mismatch sends no commands and closes; first-step mismatch sends no followups and closes; middle-step mismatch sends no later steps and closes. Verify: `uv run --locked pytest tests/test_uart_capture.py`.

### GAP-17 - Strict acceptance evidence schema — implemented

`scripts/validate_autonomous_acceptance.py` strictly validates autonomous evidence: initialization handshake present, exact MCP request/response timeline, report and plan IDs linked from the timeline, artifact hashes/sizes with symlink/traversal defenses, persisted reviewed-evidence re-verification, and final UART proof. Residual (validating the fresh-workspace runner's evidence output) is folded into GAP-16. Verify: `uv run --locked pytest tests/test_autonomous_acceptance_evidence.py tests/test_m10_final_validation_report.py`.

## Gaps-to-prompts map (codex_prompts_3.md)

Prompts are grouped so each area is implemented once against final schemas and tested once;
downstream prompts (full verification, hardware) re-cover earlier work instead of repeating
focused suites.

| Gap | Covered by prompt in `codex_prompts_3.md` | Status note |
| --- | --- | --- |
| GAP-05, GAP-18, GAP-10, GAP-12 | P3-01 | Schema truth first: fix plan/action schemas, then sync the spec and generalize its test in the same pass. |
| GAP-01, GAP-03, GAP-04, GAP-11 | P3-02 | Self-guiding setup responses, built on the final P3-01 schemas so templates are written once. |
| GAP-02 | P3-03 | Guarded connect separation. |
| GAP-09 | P3-04 | Memory-read containment; keep the policy lean (deny prohibited/unknown, no extra ceremony). |
| GAP-08 | P3-05 | Bootloader refresh + honest fail-closed terminal status; evidence continuation stays descoped (ADR). |
| GAP-06, GAP-07, GAP-13, GAP-14, GAP-15, GAP-17 (verify-only) + GAP-16 residuals + full software verification | P3-06 | One consolidated checkpoint: the full suite subsumes the focused verify-only suites, so nothing is run twice. |
| hardware smoke (setup-only) | P3-07 | Uses the runner's real CLI flags. |
| full hardware acceptance | P3-08 | Setup → build → refresh → flash_application → serial_exchange. |
