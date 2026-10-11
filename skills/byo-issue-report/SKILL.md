---
name: byo-issue-report
description: Use when a BYO Firmware MCP conversation shows a concrete S-4 through S-14 problem, or when the server explicitly prompts for its routine check-in. Do not use for a normal refusal with a workable remedy.
---

# BYO issue reporting and routine check-ins

This is the single **tool-agnostic** client skill for conversation-visible BYO
monitor signals. It does not diagnose a root cause. It records the observed
symptom through `report_agent_issue`, using the exact JSON shape in
`templates/issue-report.json`. It submits a healthy-session check-in through
`submit_routine_checkin`, using the separate exact shape in
`templates/routine-checkin.json`.

## Non-negotiable classification rule

**A refusal that names a workable remedy is not an issue.** Follow the remedy
first. Examples that are not reports: a locked action naming its `*-plan` tool;
an all-null plan response returning a populated-call template; a closed gate
naming validation; a `no board` response naming setup or connection; a busy or
timeout response that asks for a bounded retry; and a containment refusal that
correctly identifies a prohibited operation the user did not reasonably expect
to be allowed.

File only after a concrete criterion below is met. When several criteria describe
one incident, choose the signal that best captures the failure and submit **one
collapsed report**, mentioning the co-occurring symptoms in the narrative. Never
submit internal IDs, connection tokens, digests, raw memory, UART bytes, firmware
or artifact contents, full command lines, credentials, or full host paths.

## Signal criteria

### S-4 · Plan-protocol failure

**Report when:** the same plan tool rejected two or more submissions because the
agent repeatedly flattened `action_parameters`, added prose or wrapper keys,
sent extra/missing fields, used placeholders or partial nulls, or attached
`user_permission` where the plan did not accept it.

**Do not report when:** the first all-null request returned a template, or one
malformed attempt was corrected by copying that template and then succeeded.

### S-5 · Discovery/binding failure

**Report when:** the agent called an unlisted, hidden, or relocked action; ignored
`notifications/tools/list_changed`; changed the board, child name, or arguments
in the returned one-child `action_batch`; or combined a primary action with its
paired repair.

**Do not report when:** discovery changed and the agent refreshed the tool list,
then used the newly advertised route or exact fallback successfully.

### S-6 · Guidance not followed or not usable

Set `signal_subcase` to exactly `ignored_usable_guidance` or
`guidance_was_unusable`.

**Report when:** a response supplied an exact `load_call`, `next_call`,
`plan_initialization_call`, `plan_action_parameters_template`,
`accepted_response`, or `preferred_call`, and the agent ignored usable guidance
or the supplied call was ambiguous, incomplete, or contradicted actual behavior.

**Do not report when:** the returned call was followed as written and resolved
the condition, even if it required an ordinary extra step.

### S-7 · Remedy dead-end

Set `signal_subcase` to exactly `no_remedy`,
`remedy_repeated_same_refusal`, or `cycle_never_converged`.

**Report when:** a refusal supplied no actionable remedy; its named remedy led
back to the same refusal; or setup and validation cycled through research,
candidate, refusal, and research without converging.

**Do not report when:** the refusal named a feasible plan, connection,
validation, retry, or setup step that had not yet been attempted.

### S-8 · Coverage gap

**Report when:** the user requested a reasonable board operation and, after
current discovery and capability inspection, the server had no tool, plan,
fallback, setup route, or documented alternative for it.

**Do not report when:** a route exists but is currently locked or refused; follow
its remedy, or use S-7 only if that route becomes a dead-end.

### S-9 · Unusable output

**Report when:** a successful, non-refusal response had the wrong shape, omitted
expected data, was empty, was truncated, or was too large to use, such as an
oversized memory, UART, setup, or tool-index result.

**Do not report when:** output was paginated or bounded as documented and the
next page/window call was available and usable.

### S-10 · Safety surprise

**Report when:** containment correctly refused an operation the user reasonably
expected to succeed because of an incomplete map, UNKNOWN span, prohibited
region, write-only peripheral read, artifact drift, or insufficient partition
authority. Describe it as product feedback, not a server defect.

**Do not report when:** the request was plainly outside the disclosed safe
region or the refusal's restriction matched the user's stated expectation.

### S-11 · Abandonment or safety bypass

**Report when:** the agent stopped using the server and drove a debugger/vendor
CLI directly, hand-edited `.firm` authority, or otherwise completed the hardware
task outside the guarded route because the server path was unusable.

**Do not report when:** the user explicitly chose an unrelated external workflow
before attempting BYO, or a server-provided safe fallback stayed inside BYO.

### S-12 · Relay-boundary violation

**Report when:** the agent exposed a structured payload, continuation token,
internal field name, board/connection identifier, or digest to the user, or asked
the user to invent or repeat one instead of relaying it internally.

**Do not report when:** the agent displayed ordinary human-facing status,
warnings, disclosures, or remedies without internal authority material.

### S-13 · User frustration

**Report when:** the user says the workflow is broken, repeats or rephrases the
same request because progress failed, or contradicts the agent's claim that the
task worked. Treat this as a soft signal rather than proof of a code defect.

**Do not report when:** the user merely asks a follow-up, changes scope, or seeks
clarification without expressing dissatisfaction.

### S-14 · Explicit user report

**Report when:** the user directly states that a BYO capability is broken,
missing, regressed, or behaving incorrectly. Preserve that observed claim as a
summary, not a verbatim data dump.

**Do not report when:** the user asks whether a capability exists or proposes a
future feature without saying the current product is broken or missing it.

## Issue-report procedure

Use these exact classification field values. `null` is the JSON null value, not
the string `"null"`.

| Observed signal | `signal_type` | `signal_subcase` |
|---|---|---|
| S-4 | `S-4` | `null` |
| S-5 | `S-5` | `null` |
| S-6 | `S-6` | `ignored_usable_guidance` or `guidance_was_unusable` |
| S-7 | `S-7` | `no_remedy`, `remedy_repeated_same_refusal`, or `cycle_never_converged` |
| S-8 | `S-8` | `null` |
| S-9 | `S-9` | `null` |
| S-10 | `S-10` | `null` |
| S-11 | `S-11` | `null` |
| S-12 | `S-12` | `null` |
| S-13 | `S-13` | `null` |
| S-14 | `S-14` | `null` |

1. Confirm at least one positive criterion above and none of its negative cases.
2. Choose one primary `signal_type` from S-4 through S-14. Collapse related
   symptoms into that report.
3. Copy every key from `templates/issue-report.json`; add no keys. Replace the
   example values with the current narrative.
4. For S-6 or S-7 use the required exact `signal_subcase` above; otherwise use
   `null`.
5. Include the last five actions individually, oldest to newest (or every action
   if the session has fewer than five). Each needs `action`, `result`, and
   `code_context`. Compress earlier work into progressively broader one-line
   `earlier_phases`; make `session_start` exactly one line.
6. Call `report_agent_issue` with the JSON object's fields as arguments. If the
   server rejects a named field, correct that field rather than adding a wrapper.

## Routine check-in procedure

Routine check-ins are health records, not issues. **Do not submit a routine
check-in until a server response says it is due.** When prompted, copy every key
from `templates/routine-checkin.json`, add no keys, and summarize only the window
since the prior check-in. `tools_used` contains generic tool names plus purposes.
`effectiveness_observed` states outcomes, retries, and blockers only; never give
a self-rating, grade, severity, signal type, or grouping identity. Then call
`submit_routine_checkin`. Never substitute `server_health_check` for submission.
