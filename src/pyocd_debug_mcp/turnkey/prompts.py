"""Prompt contracts for the Server A middleman."""

from __future__ import annotations

from pathlib import Path

from pyocd_debug_mcp.turnkey.contracts import TurnkeyContext

ACTION_INDEX = """next_step | continue_step | return_text_to_user | request_green_check |
validate_green_check | finish_task | fail_task | finalize_needs_user_permission"""

SCHEMA_TEXT = """{
  "action": "<one action name from ACTIONS>",
  "action_params": {},
  "observation_summary": "<only observed facts from this turn>",
  "problem_hypotheses": [],
  "current_strategy": "<one concrete next strategy>",
  "failed_strategies": [],
  "carry_forward_warnings": []
}"""

MEMORY_GUIDE = """[MEMORY - TIER 1]
Fill one parameter per turn for your last four turns: memory_tier1_turn1 (most recent) through
memory_tier1_turn4 (fourth-most-recent). Each parameter has exactly action, reasoning,
codebase_changes, and result, each written in explicit detail with a goal of 100-500 tokens.
action records every command, tool call, and edit. reasoning says why it was chosen.
codebase_changes names every file and substantive change, or "none". result quotes every observed
output, value, and error. Report facts as they happened; do not merge turns.

[MEMORY - TIER 2]
Compact the 12 turns before tier 1 into memory_tier2, with a goal of 250-1000 tokens total. Keep
the action/reasoning/codebase-changes/result story recognizable for every turn and retain concrete
paths, symbols, commands, boards, and outputs.

[MEMORY - TIER 3]
Compact the useful whole session into memory_tier3, with a goal of 250-1000 tokens. Cover every
codebase change, build, flash, hardware check, test, decision, and the current state. Filter wrong
directions and abandoned errors so a fresh agent can resume without red herrings."""

ACTION_GUIDE = """next_step - current step is complete; action_params is empty and evidence belongs
  in observation_summary.
continue_step - keep working the current step; action_params is empty.
return_text_to_user - action_params is {"text": "plain user-relayable text"}; Server A surfaces it
  through Client A's MCP elicitation channel and continues the same provider session. If the client
  cannot elicit, Server A returns the exact text and a delta-call remedy. Never expose internal
  identifiers or payloads.
request_green_check - action_params is empty; Server A returns the preparation guide.
validate_green_check - action_params contains script_args (an array of exact inputs) and
  preparation_summary (observed proof that the guide's preparation is complete). Server A, not the
  middleman, executes and judges the check.
finish_task - action_params contains task_result. Server A rejects this until a green check passes
  in this tool call.
fail_task - action_params contains failure_reason with evidence and what was tried.
finalize_needs_user_permission - action_params contains permission_request naming the exact action,
  permission, and reason; this ends the call and does not grant permission."""

FIELD_GUIDE = """observation_summary: only exact commands, tool calls, edits, and concrete results
observed this turn; no intentions or predictions.
problem_hypotheses (optional): specific, testable explanations and evidence that would confirm or
reject each one.
current_strategy: one concrete approach to execute next, expected evidence, and why it follows from
the hypotheses.
failed_strategies: the complete ordered list of abandoned approaches and evidence; carry it forward
unchanged and append only genuinely new failures.
carry_forward_warnings: the complete ordered list of hard constraints and traps; carry it forward
unchanged and append new discoveries."""


def load_guide(action: str) -> str:
    specific = {
        "bug_fix": "diagnose -> locate root cause -> patch -> rebuild -> flash -> green check",
        "complex_implementation": (
            "understand requirement -> implement -> rebuild -> flash -> green check"
        ),
        "complex_task": "execute the caller's contiguous step_1 through step_n plan object",
    }[action]
    return f"""{action} runs one fresh middleman for one bounded task.
Workflow: {specific}.

On the first call supply tool_summary, task, four Tier-1 turn objects, Tier-2 and Tier-3 memory,
relevant_files, board_facts, reference_artifacts, build_context, iteration_max,
green_check_guide, green_check_script as {{"filename": "green.py", "content": "<script text>",
"command": ["{{python}}", "{{script}}"]}},
and green_check_expected_outputs. Server A materializes that script only in its disposable call
root. bug_fix additionally needs
bug; complex_implementation needs feature; complex_task adds contiguous top-level text fields
step_1 through step_n (not a nested plan object).
A later call for the same tool and task may instead supply tool_summary, task, and
continue_instruction. Server A restores the full context from this process's memory.

{MEMORY_GUIDE}

The middleman never grants user permission and never self-declares success. Server A blocks
finish_task until it runs the deterministic green check and observes every expected output."""


def init_prompt(
    tool_name: str,
    context: TurnkeyContext,
    steps: tuple[str, ...],
    artifact_root: Path,
    *,
    start_step_index: int = 0,
    continuation: str | None = None,
    prior_last_result: str | None = None,
    failed_strategies: tuple[str, ...] = (),
    carry_forward_warnings: tuple[str, ...] = (),
) -> str:
    tier1 = "\n\n".join(
        f"turn{index}:\n{turn.render()}" for index, turn in enumerate(context.tier1, start=1)
    )
    resumed_state = ""
    if continuation is not None:
        resumed_state = f"""
RESUMED STATE
last_result: {prior_last_result or "No prior action result was recorded."}
failed_strategies: {list(failed_strategies)!r}
carry_forward_warnings: {list(carry_forward_warnings)!r}
Carry both ordered lists forward exactly and do not retry a failed strategy.
"""
    task_detail = (
        f"\n{context.task_detail_label}: {context.task_detail}"
        if context.task_detail_label is not None and context.task_detail is not None
        else ""
    )
    return f"""[TURNKEY BRAIN - INIT]
You are the middleman firmware agent for one task. You are driven by an automated brain, not a
human. Your tools are the workspace repository and guarded Server B. Work only the current step;
do not skip ahead. Reply with exactly one JSON decision object and no surrounding text.

TOOL: {tool_name}
SUMMARY: {context.tool_summary}
TASK: {context.task}{task_detail}
CURRENT STEP ({start_step_index + 1}/{len(steps)}): {steps[start_step_index]}
CONTINUATION: {continuation or "new call"}
{resumed_state}

CONTEXT
memory_tier1:
{tier1}
memory_tier2: {context.memory_tier2}
memory_tier3: {context.memory_tier3}
relevant_files: {context.relevant_files}
board_facts: {context.board_facts}
reference_artifacts: {context.reference_artifacts}
build_context: {context.build_context}
temporary_artifact_root: {artifact_root}
Store plans, strategy notes, guides, and other call-only documents only in that temporary root.
Server A deletes the complete root on every exit path.

GREEN CHECK
guide: {context.green_check_guide}
script: {context.green_check_script.filename} (materialized only in the call-owned root)
expected outputs: {list(context.green_check_expected_outputs)!r}

ACTIONS
{ACTION_GUIDE}

RETURN SCHEMA
{SCHEMA_TEXT}
{FIELD_GUIDE}

FOOTER
Iterations remaining: {context.iteration_max}. Schema mismatches are discarded and consume one
iteration. finish_task requires a validated green check in this call. Leave Server B safe after
every board action: close sessions and leave the board running unless the task intentionally halts."""


def delta_prompt(
    tool_name: str,
    context: TurnkeyContext,
    steps: tuple[str, ...],
    step_index: int,
    last_result: str,
    remaining: int,
) -> str:
    task_detail = (
        f"\n{context.task_detail_label}: {context.task_detail}"
        if context.task_detail_label is not None and context.task_detail is not None
        else ""
    )
    return f"""[TURNKEY BRAIN - TURN]
Automated brain turn. Reply with exactly one JSON decision object; no surrounding text.
TOOL: {tool_name}
SUMMARY: {context.tool_summary}
TASK: {context.task}{task_detail}
LAST ACTION RESULT: {last_result}
CURRENT STEP ({step_index + 1}/{len(steps)}): {steps[step_index]}
ACTIONS: {ACTION_INDEX}
SCHEMA: action, action_params, observation_summary, problem_hypotheses?, current_strategy,
failed_strategies, carry_forward_warnings
FOOTER: Iterations remaining: {remaining}. Schema mismatches are discarded and consume an
iteration. finish_task requires a validated green check in this call."""


def schema_rejection(reason: str, remaining: int) -> str:
    return f"""[TURNKEY BRAIN - SCHEMA CORRECTION]
Your previous reply was discarded: {reason}
Reply with exactly one JSON object matching this compact schema and no other text:
{SCHEMA_TEXT}
ACTIONS: {ACTION_INDEX}
Iterations remaining: {remaining}."""
