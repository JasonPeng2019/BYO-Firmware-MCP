# v2 Brain Spec gap sheet for BYO-Server

Audit date: 2026-07-17 (revision 3; successor to `New_Brain_Spec_Gap_Sheet.md` revision 2)

## How this revision was produced

The previous sheet (rev 2) was finalized at 17:42 on 2026-07-17 and committed together with the
day's code in `ffdcffd` ("Codex fixed a bunch of bugs - added smoothness to use"). Change analysis
for this revision used `git diff b8628bf..HEAD` plus file modification times to separate what
changed before vs. after rev 2 was written, then re-verified every gap by direct code inspection.
Unlike rev 2, this session **did** execute the focused suites: the combined run of
`tests/test_setup_board_catalog.py test_setup_workflow.py test_plan_prompt_contents.py
test_initialization_handshake.py test_uart_capture.py test_safety_verify2.py
test_fresh_workspace_runner.py test_reviewed_setup_evidence.py` passed **135/135**, so every
"already implemented" claim below is execution-verified, not just inspected.

### What actually changed since rev 2 was written

1. **`codex_prompts_3.md` was never executed.** Every open gap in rev 2 (GAP-01, 02, 03, 04, 05,
   08, 09, 10, 11, 12, 18) was re-verified **still open** in the current tree, with citations
   refreshed below. Nothing in rev 2's open list is out of date because of code fixes; the sheet
   drifted only in its residuals and its "cannot execute pytest" caveats.
2. **Only `zephyr_build.py` and `tests/test_zephyr_build.py` changed after rev 2** (mtimes
   17:42:43–47 vs. the sheet at 17:42:16; ~2,755 inserted lines across both). The change is build
   smoothness: workspace/SDK discovery and reuse, cache locks, build-dir ownership markers, managed
   toolchain install, patch-compatible SDK version matching. **Assessed good for the product goal**
   (removes Zephyr build friction for the agent) and orthogonal to the gap list; no regression
   found. It is large and was only inspected here — the full suite in the consolidated
   verification prompt is its execution gate.
3. **GAP-16 residuals are closed.** No doc, prompt, or README anywhere in the tree still uses the
   pre-implementation runner flags (`--board-name`, `--mcu-part`, `--uart`, `--setup-only`,
   `--evidence-out`) — the only match is the old gap sheet itself. `README.md:58-65` and
   `docs/agent-contract.md:147-151` describe the real runner correctly, and
   `tests/test_fresh_workspace_runner.py` passes. The optional evidence-validator unification
   remains skipped by choice.

### Changes marked bad for the original goal

- **GAP-18 stands as the one confirmed regression**: the `board_setup` plan's required
  `serial_id` + `serial_port` fields (added before rev 2, still present at
  `guardrails/plan_defs.py:187-196`) force the agent to supply a volatile COM path the server
  never exposes machine-readably, contradicting the spec's stable-identity principle. Details and
  patch under GAP-18 below.
- The post-rev-2 zephyr_build work introduced **no** new goal regressions.

## Selection filter (unchanged policy)

This sheet lists only gaps that genuinely affect real firmware work — a compliant agent and a
regular firmware developer setting up a board, building, flashing, and talking to it over UART.
Edge-case-only defects that such a pair would be unlikely to hit are excluded on purpose;
flexibility and ease of use for the agent and user outrank exhaustive hardening. Every open gap
below either blocks/derails the normal workflow (GAP-01, 03, 04, 05, 11, 18), lets a compliant
agent silently do the wrong thing because the schema invites it (GAP-02, 10), leaves a
spec-mandated safety default unenforced on a normal debugging path (GAP-08, 09), misleads whoever
reads the contract docs (GAP-12), or leaves the primary untested client family unproven
(GAP-19, 20).

## New scope in this revision: Claude-agent coverage

The server has been agent-tested primarily with Codex. The scripted layers (pytest, the MCP SDK
stdio smokes, `scripts/run_fresh_workspace_e2e.py`) are client-agnostic, but **no Claude model has
ever driven this server**, and the only agent-in-the-loop harness is hardwired to the Codex CLI.
Because Claude Code refreshes tools on `notifications/tools/list_changed` (which the server emits —
`kernel/registry.py:389-404`), a Claude session exercises the **dynamic** post-plan exposure path,
which is precisely the path Codex's static bindings never proved live. Cost policy for all new
agent-driven tests: Claude runs must use **Haiku 4.5** (`claude-haiku-4-5-20251001`) — never
Sonnet/Opus/Fable — and Codex runs must pin the **Codex 5.6 luna** model. Both CLIs are installed
on the bench host (claude 2.1.76, codex-cli 0.142.2). If either model's context runs out (or it
otherwise cannot complete a run) during a live agent test, the harness must skip only that
model's portion and record why, rather than failing the whole test or fabricating a result. See
GAP-19 and GAP-20.

## Open gaps

### GAP-01 - `setup_overview(["no board"])` routes to first-time setup

**Spec target:** If the user says `no board`, setup, validation, and hardware actions must not begin.

**Re-verified:** the routing loop (`server.py:3515-3558`) returns `setup_no_board` only for an
empty list (`board_names == []`, line 3557). A literal `["no board"]`, or a mixed list like
`["left", "no board"]`, falls through, gets a proposed `board_id` (e.g. `no_board`), and returns a
`setup` route. The handshake explicitly teaches the user to say "no board" (`tools/handshake.py:73`),
so this is a first-contact failure, not an edge case.

**Patch:** Treat a singleton normalized `no board` as `setup_no_board`; reject any mixed list
containing `no board` with a plain clarification status and no route emitted. Update the
`setup_overview` docstring and handshake prose so `no board` reads as a literal sentinel, never a
candidate name. Add unit and stdio contract tests for `["no board"]`, `["No Board"]`, and mixed lists.

### GAP-02 - Always-visible `connect` still accepts manual override fields

**Spec target:** `connect` is for normal profile-backed connection; probe UID / target / external
board-config overrides belong to guarded `connect_override` after `connect_override-plan`.

**Re-verified:** the visible `connect` schema (`server.py:956-961`) still exposes `unique_id`,
`target`, and `board_config` and passes them straight to `_connect_impl`.

**Patch:** Make public `connect(board_id)` profile-only; reject non-null override values at the
public handler boundary with a redirect to `connect_override-plan`. Keep override parameters only
on hidden `connect_override`. Add a regression proving `action_batch` cannot smuggle override
fields through visible `connect`. Update contracts, README, docs, and tests.

### GAP-03 - `load_setup_tool` does not load detailed setup workflow knowledge

**Spec target:** `Load_setup_tool(board_id, tool)` returns detailed setup/validation workflow
instructions and unlocks the selected setup tool.

**Re-verified:** `SetupToolLoadState.load` (`tools/setup.py:35-48`) still returns only
`{status, board_id, tool_name, redirect}`. The loader — the spec's designated knowledge-delivery
point — teaches nothing, and `board_validate`, `board_safety_setup`, and `board_safety_refresh`
have no NULL-plan channel at all.

**Patch:** Return tool-specific guidance keyed to the requested `tool_name` only (bounded, no
whole-manual dumps): purpose, exact next call with argument shape, expected statuses, accepted
response shape, conversational no-internals rules, common remedies, and when not to use the tool.
Contract tests must assert each of the four loadable tools returns its own distinct guidance.

### GAP-04 - Setup routes do not provide exact next-call templates

**Spec target:** Setup stays easy for the agent while internal IDs stay away from the user.

**Re-verified:** setup routes (`server.py:3532-3544`) still carry only `next_tool` and
`required_user_facts`, and `required_user_facts` still under-declares — it names board type, MCU
part, and datasheet but omits the plan's required `serial_id`, `serial_port`, and
`serial_baudrate`. `serial_choices` rows (`server.py:3493-3506`) expose only `choice_id` and a
human-facing `friendly_name` — no machine-readable `port_path`. No route composes a `load_call`,
`next_call`, or `plan_action_parameters_template`, even when exactly one candidate exists.

**Patch:** For every route include `load_call` (exact `load_setup_tool` arguments), `next_call`,
and for setup routes a `plan_action_parameters_template` with all server-known fields pre-filled
(`board_id`, `mode`, `connection_id`/`serial_id` when unambiguous) and only genuine user facts
marked as needed. Add `port_path` and stable USB identity to `serial_choices` rows. Make
`required_user_facts` truthful and complete for the final schema (UART selection and baud rate
included). For ambiguity, include friendly choices plus an `accepted_response` shape. Optionally
add `known_board_types` beside `supported_reviewed_board_types`. Coordinate with GAP-18.

### GAP-05 - Setup asks the model to provide a SHA-256 the server can compute

**Spec target:** Users provide ordinary board facts and local documents; low-level facts should be
server-owned when possible.

**Re-verified:** `board_setup-plan` still requires `datasheet_sha256` as non-nullable TEXT
(`guardrails/plan_defs.py:202-206`). The server computes and enforces the real digest itself, so
the agent-supplied hash adds friction without assurance; the fresh-workspace runner has to hash the
file itself just to satisfy the schema, and a pure-MCP agent without shell access cannot.

**Patch:** Make `datasheet_sha256` nullable; when null, the server binds its own computed digest
before commit (schema change, not an authority change). Keep accepting an agent-supplied digest as
an optional cross-check; still record the digest and require reviewed bytes before attach/commit.
Do not add a hashing tool.

### GAP-08 - Safety setup/refresh remedies are incomplete (narrowed scope)

**Spec target:** Research prompts must be consumable, and refresh must handle linker/ELF,
bootloader, pack, datasheet/evidence, and unclear scope.

**Re-verified, two seams:**

1. `_run_board_safety_setup` (`server.py:2395-2428`) still returns `safety_setup_research_required`
   with a `continuation_id` no public tool can consume (`continue_setup` resolves only
   setup-workflow continuations) — a dead end dressed as a research prompt.
2. `board_safety_refresh` still accepts only application ELF/HEX/map paths; no `bootloader_*`
   parameters exist anywhere in the tree.

**Patch:** Extend `board_safety_refresh` with `bootloader_elf/hex/map` mirroring the application
path, plus explicit statuses/remedies for pack drift, evidence drift, geometry/anchor changes, and
unclear scope (unclear scope redirects to full safety setup). Replace the dead-end research status
for unreviewed boards with an honest fail-closed terminal status (e.g.
`safety_setup_unsupported_board`) that names the reviewed list and returns no continuation ID.
The general agent-supplied-evidence continuation stays descoped pending an ADR.

### GAP-09 - Raw memory reads and symbol reads do not apply map containment

**Spec target:** Unknown/prohibited memory is denied by default; `ActionCategory.MEMORY_READ` exists.

**Re-verified:** `SafetyPolicy` still has no `check_memory_read`; `read_memory_address` and
`read_memory_symbol` (`tools/memory.py`) read with no region classification, and
`ActionCategory.MEMORY_READ` is still mapped to every region kind (`safety/regions.py:185`), so
even a naive containment check wired to that table would permit prohibited-region reads. Some
hardware registers are read-sensitive (read-to-clear, FIFO pops) — a real behavioral hazard.

**Patch:** Add `SafetyPolicy.check_memory_read`, tighten the `MEMORY_READ` allowed-kind set to
exclude prohibited and unknown memory, call it from raw and symbol reads (resolved symbol size when
available, at least the requested width), deny with clear remedies. Cover scalar, block, and symbol
paths in tests. Keep it lean — no per-read plans or extra ceremony.

### GAP-10 - `target_address` is advertised for flash but runtime always rejects it

**Re-verified:** `flash_application-plan` and `flash_bootloader-plan` still include nullable
`target_address` (`guardrails/plan_defs.py:341,360`); `tools/flash.py` still rejects every
non-null value. The model can form a valid plan guaranteed to fail at execution — the exact
schema-vs-runtime lie the New Brain design forbids.

**Patch:** Remove `target_address` from the public plan/action schemas; state in the NULL prompts
that load addresses come from the artifact. Update contracts, `Plan_Prompt_Contents_Spec.md`
(same pass as GAP-12), and tests proving live schemas cannot accept always-refused parameters.

### GAP-11 - `board_validate` choice responses lack an accepted-response recipe

**Re-verified:** `ValidationResult.to_payload()` (`setup_flow/validate.py:181`) still hardcodes
`accepted_response: None` for every status, including `validation_needs_user_input`.

**Patch:** For `validation_needs_user_input`, include `accepted_response` with the exact retry
call and field mapping — `board_validate(board_id=..., probe_id="<one choice_id>")` or
`serial_id=` as appropriate (the tool already accepts both: `tools/setup.py:325-340`). Keep `None`
for terminal statuses. Add tests per choice type.

### GAP-12 - `Plan_Prompt_Contents_Spec.md` flash sections drift from live schemas

**Re-verified:** the flash sections still document `action_parameters: {artifact_path, halt_after}`
(spec lines ~581-623) while live definitions use `{artifact, target_address}`, and the sync test
still hand-picks only `board_setup` and `serial_exchange`
(`tests/test_plan_prompt_contents.py:207-226`), so every other section can drift silently.

**Patch:** Reconcile the flash (and any other unsynced) sections with `plan_defs.py` in the same
pass as GAP-10, then extend the sync test to iterate **every** `PLAN_DEFINITIONS` entry — action
name, plan name, action-parameter field names, budget mode, permission mode.

### GAP-18 - Regression: `board_setup` plan demands `serial_id` and `serial_port` the agent cannot reliably obtain

**Re-verified:** the required fields stand at `guardrails/plan_defs.py:187-196`; `serial_choices`
still has no machine-readable `port_path` (`server.py:3493-3506`); the route's
`required_user_facts` still under-declares (GAP-04). Binding a volatile COM path into an immutable
exact-parameter plan contradicts the spec's stable-identity principle ("a change from COM7 to
COM11 does not prompt again"); the agent's only source for `serial_port` is scraping a
human-facing label or asking the user for a COM path, which the spec forbids.

**Patch (prefer the first):**

- **Server-resolved port (spec-aligned):** keep `serial_id` as the agent-supplied stable selector
  and drop `serial_port` from the plan schema; the server resolves the current port from stable
  USB identity at execution time and records it in reports. Update `tools/setup.py`, preflight
  filtering, and `scripts/run_fresh_workspace_e2e.py` (whose `--uart-port` flag should disappear
  with it).
- **Minimum fallback:** if `serial_port` must stay, expose `port_path` + stable USB identity in
  `serial_choices` and pre-fill both fields in the GAP-04 route template.

Either way, add tests that the agent-facing payload contains, machine-readably, every value any
setup plan field can require.

### GAP-19 - NEW: agent-in-the-loop testing is Codex-only and model-unpinned

**Product-goal target:** the server must be proven easy to use for the agents that will actually
drive it. The primary untested client family is Claude; all prior agent runs were Codex, and even
those never pinned a model.

**Verified current behavior:** `src/pyocd_debug_mcp/benchmark_support.py` hardwires the Codex CLI:
`_ensure_codex_registration` (654-663) fails without `codex` on PATH, `_run_codex` (666-707)
shells out to `codex exec` with `--output-schema`, and result/session reconciliation is
Codex-session-specific. No `claude` provider exists anywhere in the tree; no `--model` argument is
passed to any agent CLI; `docs/verification.md:93-102` labels R11 "Codex-specific" and README
documents only generic `mcpServers` JSON with no `claude mcp add` / `codex mcp add` registration
examples.

**Impact:** the client family most likely to exercise the dynamic tool-exposure path has zero
harness support, and no agent result is reproducible to a model version.

**Patch:** introduce a provider abstraction in the agent-run layer with two pinned providers:

- **Claude Code headless:** `claude -p "<prompt>" --model claude-haiku-4-5-20251001` with
  checkout-scoped MCP registration (`--mcp-config` or documented `claude mcp add pyocd-debug --
  uv run --project <checkout> --locked pyocd-debug-mcp`) and tool allowlisting for MCP-only runs.
  The claude CLI has no `--output-schema`; obtain the structured result by instructing the model
  to write the exact JSON object to a named result file, then validate it with the existing
  parser. **Haiku 4.5 is required for every Claude run** (cost policy); the harness must refuse
  other Claude models.
- **Codex pinned:** the existing runner plus an explicit `--model` pinned to the Codex 5.6 luna
  model (resolve the exact identifier from the installed CLI; record the resolved string).

Record provider, exact model ID, and CLI version in every run artifact. If either model's context
runs out mid-run, the harness must skip only that model's portion and record why, never fail the
whole run or fabricate a result. Unit-test the harness with mocked CLI executables so pytest stays
offline and model-free.

### GAP-20 - NEW: no live Claude-agent proof of the interaction contract

**Product-goal target:** a Claude session must be able to follow the handshake, setup routing,
NULL-plan teaching, and post-plan dynamic tool exposure purely from server payloads.

**Verified current behavior:** the server emits `notifications/tools/list_changed`
(`kernel/registry.py:389-404`, tested in `tests/test_kernel_registry.py`) and the handshake/NULL
prompts describe both dynamic and static-fallback behavior — but every live proof to date is
either a scripted MCP SDK client or a Codex agent. Claude Code refreshes its tool list on
`list_changed`, so a Claude session takes the **dynamic direct-action path after plan acceptance**
— a path no agent has ever exercised live. Whether Claude's tool-name prefixing, permission
prompts, and description budget interact cleanly with the ~20-tool advertised surface and the
long handshake/NULL-plan texts is unproven.

**Impact:** the product could ship working-for-Codex and broken-or-clumsy-for-Claude — the exact
worry in the audit prompt.

**Patch:** two staged live proofs using the GAP-19 harness, both dual-model (Haiku 4.5 + Codex 5.6
luna). If either model's context runs out during a proof, record that model's partial result and
the reason, and let the other model's evidence stand rather than blocking the whole proof:

1. **Board-free contract smoke** (no hardware): each agent connects, calls
   `initialization_handshake`, handles the "no board" answer (must refuse setup/hardware), reads
   an all-NULL plan without inventing fields or calling unlisted tools; the transcript is
   recorded and asserted (correct refusal, no invented tool names, no internal IDs shown to the
   user). This covers the refusal/no-board paths that the hardware acceptance never exercises.
2. **Hardware acceptance**: a Haiku 4.5 session drives the full journey (setup → build guidance →
   safety refresh → `flash_application-plan`/`flash_application` → `serial_exchange`), asserting
   it used the dynamically exposed direct action (not the static fallback) after plan acceptance;
   a bounded Codex 5.6 luna pass then re-proves cross-client parity on the already-built artifact
   (validate → flash → serial) without redundant rebuild. Evidence (prompts, MCP timeline, model
   IDs, transcripts) lands in `docs/evidence/`.

## Already implemented — execution-verified this revision

All previously "verify, don't re-patch" items passed in this session's 135-test focused run:

- **GAP-06** two-source reviewed-evidence reconciliation (`tests/test_reviewed_setup_evidence.py` green).
- **GAP-07** reviewed-support advertising via `supported_reviewed_board_types` (catalog/workflow suites green).
- **GAP-13** advisory `build_guidance` from `get_setup_status` (covered by setup tools suite; re-covered at hardware time).
- **GAP-14** catalog digest-mismatch test rewritten and green.
- **GAP-15** `serial_exchange` stop-on-failure negative tests green (`tests/test_uart_capture.py`).
- **GAP-16** fresh-workspace runner implemented, suite green, doc-flag residuals closed (see change log above).
- **GAP-17** strict acceptance evidence validation (validated by its suite in the full run; re-run in the consolidated checkpoint).

## Gaps-to-prompts map (codex_prompts_4.md)

Prompts are grouped so each area is implemented once against final schemas and tested once;
downstream prompts re-cover earlier work instead of repeating focused suites.

| Gap | Covered by prompt | Status note |
| --- | --- | --- |
| GAP-05, GAP-18, GAP-10, GAP-12 | P4-01 | Schema truth first: fix plan/action schemas, then sync the human spec and generalize its test in one pass. |
| GAP-01, GAP-03, GAP-04, GAP-11 | P4-02 | Self-guiding setup responses, built on the final P4-01 schemas so templates are written once. |
| GAP-02 | P4-03 | Guarded connect separation. |
| GAP-09 | P4-04 | Memory-read containment; lean policy. |
| GAP-08 | P4-05 | Bootloader refresh + honest fail-closed terminal status. |
| GAP-19 | P4-06 | Dual-provider, model-pinned agent harness (mocked-CLI tests only; no live model calls). |
| Verify-only gaps + full software verification | P4-07 | One consolidated checkpoint; the full suite subsumes all focused suites. |
| GAP-20 (part 1, board-free) | P4-08 | Live dual-agent contract smoke: Haiku 4.5 + Codex 5.6 luna, no hardware, covers only paths hardware runs never re-exercise. |
| hardware setup smoke (scripted) | P4-09 | Fresh-workspace runner, setup-only, non-destructive. |
| GAP-20 (part 2) + full hardware acceptance | P4-10 | Haiku 4.5 full journey, then bounded Codex 5.6 luna parity pass. |
