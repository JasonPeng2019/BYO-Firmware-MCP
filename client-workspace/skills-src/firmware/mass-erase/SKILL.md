---
name: mass-erase
description: Human-only, single-use authorization for the server's exact mass-erase disclosure.
disable-model-invocation: true
user-invocable: true
---

# Authorize mass erase

This is a **manual-only destructive permission**. Never invoke it on the model's
initiative. The human must explicitly request `$mass-erase` after seeing the
server's exact recovery disclosure.

1. Copy the exact `project_root`, `board_id`, `policy_digest`, and
   `binding_digest` from the current server response. Do not infer or edit them.
2. Display the server's disclosure and state plainly that mass erase destroys
   all recoverable target contents covered by that operation.
3. Ask the human to confirm this exact board and disclosure.
4. After confirmation, run:

   `python .agent-workspace/internal/manual_permissions.py --project-root <project_root> --action mass-erase --board-id <board_id> --policy-digest <policy_digest> --binding-digest <binding_digest>`

5. Relay the JSON result. On refusal, follow its remedy; never create or edit
   `.agent-workspace/runtime/manual-permissions` directly.

The grant is project-local, active-run-bound, policy-bound, disclosure-bound,
and single-use. Any changed or cancelled plan needs a new human invocation.
