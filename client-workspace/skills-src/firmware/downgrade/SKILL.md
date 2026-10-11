---
name: downgrade
description: Human-only, single-use authorization to downgrade one named board to no-setup mode.
disable-model-invocation: true
user-invocable: true
---

# Downgrade a board's safety tier

This is a **manual-only** permission action. Never invoke it on the model's
initiative. The human must explicitly request `$downgrade` after seeing the
server's current board ID and policy digest.

1. Copy the exact `project_root`, `board_id`, and `policy_digest` from the current
   server response. Do not infer, shorten, or edit them.
2. Explain that downgrade removes the committed setup policy and restores raw,
   explicit-address operation for this board.
3. Ask the human to confirm this specific board and effect.
4. After confirmation, run:

   `python .agent-workspace/internal/manual_permissions.py --project-root <project_root> --action downgrade --board-id <board_id> --policy-digest <policy_digest>`

5. Relay the JSON result. On refusal, follow its remedy; never create or edit
   `.agent-workspace/runtime/manual-permissions` directly.

The grant is project-local, bound to the active server run and policy, and can be
consumed only once.
