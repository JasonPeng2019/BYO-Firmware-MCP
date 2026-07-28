# Change-loop launcher recovery

- `2026-07-26T06:21Z`: first launch used `workspace-write` as required by default.
- The Windows Codex sandbox reported the repository as read-only and rejected the doer's patch.
- The exact loop process tree was stopped before the spec tester completed, preventing concurrent roles.
- Resume uses the same recorded doer thread and the change-loop skill's documented Windows fallback: `--sandbox danger-full-access --ignore-user-config`, without `--dangerously-bypass-approvals-and-sandbox`.
- Model/reasoning/tier remain `gpt-5.6-terra`, `medium`, `priority` (Fast).
