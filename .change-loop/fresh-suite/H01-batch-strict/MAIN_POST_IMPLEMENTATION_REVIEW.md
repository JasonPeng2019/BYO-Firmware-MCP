# Main-model post-implementation review — H01 registered-boundary repair

- Reviewer: root main/orchestrating model (`gpt-5.6-sol`)
- Scope: authorized local, host-only firmware-server validation; no hardware
- Baseline commit: `6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876`
- Reviewed plan SHA-256:
  `ca11a4c2775ce75c7f0ac92369679506a5c0d62e1c17193aaf8b3ed356246f72`
- Reviewed amendment SHA-256:
  `d7aba9a5ac5c1fdadac03f0453b496d913e835f748a050ebb3b745ef65e6cf1b`
- Final target-only `git diff --no-ext-diff --binary HEAD` SHA-256:
  `7cb04020ee4882eb2186dcc6ac5323a909e10cc44c2d38f589dd80c9e39f20eb`
- Final full accepted production-scope `git diff --no-ext-diff --binary HEAD` SHA-256:
  `23b6deb3f0a8f24124e9ec4905b6a0c5e828a815d2e6c4ed849189fe01f6f0d5`

## Source verdict

`ACCEPT`

The production repair remains limited to:

- `src/pyocd_debug_mcp/kernel/registry.py`
- `src/pyocd_debug_mcp/tools/batch.py`

The registry change uses the existing FastMCP/Pydantic model as the one strict schema authority,
validates exactly once after the physical registry lock, preserves raw arguments for the existing
guard/timeout/finalizer contracts, invokes the already validated one-level mapping through the
existing managed dispatch, and mirrors pinned `Tool.run()` context/result/error semantics. A
per-instance `ContextVar` gives the outer request sole ownership of an aggregate list-change
notification and resets on success, refusal, cancellation, and timeout.

The batch change preserves the existing ordered/first-failure payload and successful return, but
raises the ordinary MCP `ToolError` for `batch_failed` so wire clients receive `isError: true`
without losing the machine-readable failure body.

This is a correctness guard for a compliant but fallible caller, not hostile-input hardening. The
diff adds no board/tool/OS special case, arbitrary cap, dependency, retry, new authority, or
unrelated cleanup. It satisfies the charter's correctness, simplicity, generality, neatness, and
honest-reporting requirements.

## Adversarial points checked

1. Locked calls still refuse before schema validation.
2. Unlocked malformed calls do no guard, handler, finalizer, provider, or notification work.
3. FastMCP pre-parsing/Pydantic validation, context injection, sync/async invocation, result
   conversion, and handler error wrapping occur once.
4. Generated-plan permission and literal-text metadata compose with global strict registration.
5. Outer, child-envelope, and child-argument extras are wire-level MCP errors.
6. Failed batches keep the completed prefix, original child refusal, one structured JSON body, and
   one safe-exit reminder; successful batches remain successes.
7. Direct, nested, concurrent, failed, and cancelled dispatches preserve context-local
   notification ownership; the real in-process MCP transport observes one notification for one
   nested relock.
8. Protected H00/H01 production/test hashes and tester ownership manifests remain exact.

## Neutral and manager-owned evidence

- Neutral gate:
  - spec: `9 passed, 7 subtests passed`
  - regression: `4 passed`
- Focused current/accepted H01:
  `19 passed, 43 subtests passed`
- Accepted H00 contract:
  `19 passed, 7 subtests passed`
- Accepted H00 regression plus prior H01 plan tests:
  `7 passed, 36 subtests passed`
- Ruff repository gate: pass
- Pyright configured source gate: `0 errors, 0 warnings, 0 informations`
- `uv lock --check`: pass
- `uv build`: pass
- Collection: `225 tests collected` before the final three additive spec proofs
- Final full repository gate after those proofs:
  `226 passed, 2 skipped, 117 subtests passed`
- Protected hashes: all exact
- Final tester Git object snapshots:
  - spec: `c9fe2018c3537945437fc9bb07ff9b495511daf8`
  - regression: `e762698e920f39b673bd275d1f4bd3aa60393940`

The first controller gate failure was infrastructure-only: a Windows role had replaced the
repository environment with an incomplete Windows/WSL-incompatible `.venv`, so the WSL
`--no-sync` gate could not find pytest. The main model established one small deletable WSL-native
environment at `/root/mcp-trial-3-h01-gate-venv`, reran the same recorded commands neutrally, and
removed every temporary repository `.venv`/symlink afterward. No dependency or production state
was changed to make tests pass.

## Charter checkpoints

The root main model reread the complete `../.codex/design_charter.md` before planning, before
verification, after the risky diff, and immediately before this acceptance. The doer and both
persistent test roles recorded their required full-charter checkpoints as well.
