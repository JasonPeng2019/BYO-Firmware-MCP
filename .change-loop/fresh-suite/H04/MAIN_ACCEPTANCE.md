# Main acceptance — H04 attachment-cache repair

- Main reviewer: `gpt-5.6-sol`
- Accepted at: `2026-07-25T14:36:00-07:00`
- Base commit: `6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876`
- Reviewed plan SHA-256:
  `d2afb0d35f4c08bad2096ce32e3821053e8ac2bfa42fe1a83fe83bad95db759b`
- Production diff snapshot:
  `.change-loop/fresh-suite/H04/state/h04_production_diff.patch`
- Production diff SHA-256:
  `e74a304fdd110e3bd580306d3b645d9f9eb66af0af3b0bf988199e46d0f615a6`

## Acceptance evidence

The persistent doer and both persistent adversarial testers reread
`../.codex/design_charter.md` at their required checkpoints. The final neutral gate passed in one
iteration:

- spec: 8 tests, PASS;
- regression: 3 tests, PASS;
- report: `.change-loop/fresh-suite/H04/state/test_report.md`.

Main independently reread the charter and inspected the complete production diff and both
tester-owned modules. Additional manager-owned verification:

- six H04 and setup-adjacent modules, 52 tests total: PASS;
- Ruff lint on the four production files and two H04 tests: PASS;
- Basedpyright on the three changed core production modules: 0 errors;
- `git diff --check`: PASS.

The repository-wide generic verification backend remains baseline-red for unrelated type/import
and formatting configuration. Its one focused type error is in a pre-existing setup-handler line
outside the H04 diff; the H04 core modules typecheck cleanly. No unrelated baseline item was edited.

## Accepted behavior

- A preflight-ready setup persists a stable selected probe/UART pair even when direct identity
  already proves the built-in UART mapping.
- Reconfirming the same active pair preserves exact cache bytes/timestamp.
- `get_setup_status` exposes a project-relative, explicitly non-authoritative attachment-cache
  diagnostic with missing/valid/corrupt state.
- A missing, malformed, or authority-shaped cache cannot suppress a unique direct identity match
  and cannot create profile, safety, live-identity, plan, permission, flash, or gate authority.
- External unproven adapters still require their existing confirmation route; no board, port, OS,
  probe family, or MCU-specific branch was introduced.

The accepted source was reinstalled offline into `.h01-venv-batchstrict`; installed source hashes
match the reviewed working-tree files. No commit, push, deployment, or hardware action occurred.
