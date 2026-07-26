# High-level H00/H01 audit repair request

## Verified production defect

At baseline `6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876`, H00 correctly repaired
cross-host access to Windows-only process APIs. High-level review of the
uncommitted candidate found that `_windows_start_token()` no longer preserves
its established `ProcessIdentityUnavailable` boundary. It now permits
contextual `OSError` to escape even though startup hygiene and marked-process
cleanup catch only `ProcessIdentityUnavailable`. A missing or inaccessible
Windows identity API can therefore escape fail-closed identity handling and
abort the caller unexpectedly.

The same review found four defects in the H00 verifier changes:

1. the contract test requires the wrong `OSError` class from
   `_windows_start_token()` instead of the process-identity abstraction;
2. the Pyright regression test uses unlocked
   `uv run --no-project --with pyright pyright`;
3. the README verifier contract and its assertion omit the accepted
   `uv lock --check` step; and
4. candidate cleanup can wait an arbitrary 60 seconds and uses a
   `type: ignore` for an imprecise callback.

The complete high-level audit is in
`../HIGH_LEVEL_H00_H01_VALIDATION.md` relative to the MCP-Trial-3 root.

## Required behavior

- `_windows_start_token(pid)` returns its token/`None` as before, and every
  missing/inaccessible/failing native identity operation becomes a contextual
  `ProcessIdentityUnavailable` with its original cause chained.
- Lower-level Job creation, resume, and close helpers continue to surface
  contextual `OSError`; real numeric Windows creation flags and actual
  Windows/POSIX process behavior remain unchanged.
- Startup hygiene treats unavailable owner/child identity as unresolved and
  fails closed; it does not leak the native `OSError`.
- Simplify the repetitive identity lookup/error flow without introducing a
  portability framework, broad `Any`, suppression, or unrelated refactor.
- The ordered README verifier includes `uv lock --check` immediately after
  locked sync.
- Pyright scope regression coverage invokes the Pyright installed by the
  explicitly locked project environment; it must not use `--with`,
  `--no-project`, or network-dependent resolution.
- Candidate cleanup terminates/verifies owned descendants before removal and
  uses only a short monotonic retry window for transient Windows handle
  release, with the final concrete error preserved. Remove the `type: ignore`.

## Ownership and exclusions

- Doer: production source and README only
  (`src/pyocd_debug_mcp/kernel/processes.py`, `README.md`).
- Spec tester: `tests/test_h00_repository_contract.py` only.
- Regression tester: `tests/test_h00_repository_regressions.py` only.
- Preserve the already accepted `pyproject.toml` and `uv.lock` bytes.
- Do not edit the H01 harness, fresh-experiment evidence, hardware behavior,
  MCP tools, board/setup/plan/permission behavior, cleanup constants in
  production, or unrelated files.
- Do not commit, push, deploy, flash, or operate hardware.

## Charter checkpoints

Every repair role must reread `../.codex/design_charter.md` before its first
analysis, immediately before editing, between distinct source/test features,
before verification, and before its final verdict. Each role must state those
checks in its final message. The main model records its checks in this
runtime's `DESIGN_CHARTER_CHECKS.md`.
