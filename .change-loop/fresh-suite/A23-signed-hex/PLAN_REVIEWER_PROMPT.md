AUTHORIZED LOCAL FIRMWARE VALIDATION: This is a read-only review of a local production-server
repair plan. Do not operate hardware, access external systems, edit files, commit, push, deploy,
flash, or broaden scope.

You are the one independent adversarial plan reviewer. Work only inside the supplied
BYO-Firmware-MCP repository and its parent MCP-Trial-3 policy files.

Before reviewing:

1. Read `../.codex/design_charter.md` in full.
2. Read `.change-loop/fresh-suite/A23-signed-hex/changes.md`.
3. Read `.change-loop/fresh-suite/A23-signed-hex/plan.md`.
4. Inspect the narrow build-evidence parser, both runtime safety-policy callers, and relevant
   existing tests.

Review the exact plan SHA-256:
`112f77f7d2e524bd540f5a9cd1779fa429bda1ecb7ca7fc2c1958f1470a1b42c`.

This is diagnostic review, not a replanning loop. Do not write or modify anything. Return:

- the exact SHA independently reviewed;
- your session/thread identity if available;
- a concise APPROVE or BLOCK verdict;
- numbered implementation risks and adversarial test targets;
- any charter conflict, especially loss of wrong-artifact mistake guards, vendor/bootloader
  specialization, guessed memory authority, or bypass of reviewed/static or generic physical
  safety-map enforcement.

Pay particular attention to whether the proposed connected-component rule is defined
deterministically, admits the observed prefix/suffix relationship without an arbitrary tolerance,
rejects disjoint supplemental content, handles only the exact `0x00`/`0xFF` fill equivalence,
preserves meaningful-overlap and meaningful-completeness checks, and leaves every actual HEX and
erase range subject to existing policy-level authority.

Do not demand unrelated refactors. Do not propose changing the plan solely for stylistic
preference. Distinguish a genuine blocking correctness issue from an execution risk the doer and
testers can address under the plan.
