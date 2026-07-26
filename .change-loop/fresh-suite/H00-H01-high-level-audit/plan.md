# Change implementation plan

## Source change list

- Source: `.change-loop/fresh-suite/H00-H01-high-level-audit/changes.md`
- Goal summary: Correct the high-level-review defects in the otherwise accepted H00 cross-host repository repair: restore the fail-closed process-identity exception boundary, keep native Job operations honest, make the documented verifier and Pyright regression fully locked, and replace an arbitrary 60-second test-cleanup loop with short bounded teardown.

## Repository context and assumptions

- Verified architecture and relevant entry points: `src/pyocd_debug_mcp/kernel/processes.py::_start_token()` dispatches to `_windows_start_token()` on Windows. `kernel/hygiene.py::cleanup_stale_owned_processes()` catches only `ProcessIdentityUnavailable` for owner and child identity probes, and `processes.py::terminate_marked_group()` also treats that exception as a fail-closed false result. At baseline, `_windows_start_token()` normalized `AttributeError`/`OSError` into `ProcessIdentityUnavailable`; the current uncommitted repair accidentally removed that final normalization. The lazy `_windows_library()`/`_windows_get_last_error()` helpers also serve Job creation, process resume, and Job cleanup, whose existing public failure surface is contextual `OSError`.
- Verified repository-contract surface: `README.md` contains the ordered verifier; `[tool.pyright] include = ["src"]` and the locked dev dependencies are in `pyproject.toml`/`uv.lock`; `tests/test_h00_repository_contract.py` owns the clean-candidate, README, native-platform, and identity-contract assertions; `tests/test_h00_repository_regressions.py` owns the isolated Pyright-scope negative control.
- Existing commands relevant to the change: `uv sync --locked`; `uv lock --check`; `uv build`; `uv run --locked --no-sync ruff check .`; `uv run --locked --no-sync pyright`; `uv run --locked --no-sync pytest --collect-only -q`; `uv run --locked --no-sync pytest -q`. A clean temporary Windows project proved that `uv run --project LOCKED_PROJECT_PATH --locked --no-sync pyright --project SENTINEL_PROJECT_PATH` excludes the sentinel test error and reports the sentinel source error without `--with`.
- Diff identity before this plan: tracked diff SHA-256 `7dd488174251f060a5174e538906aebfc8b7441ff645f695797b5a6061db81bc`; spec-test hash `e70507cf2e09f6ec4b8d2569b7eade4651db70951944115a447e665f87af21f9`; regression-test hash `6cb0d14e55a899dbbcc28a322d944eb9226adf5b42ed4cd8ef5f724253a8acd1`.
- Charter checkpoint: the current main model reread `../.codex/design_charter.md` before high-level review and again between the verified request and this plan. The plan favors the original single identity abstraction and flat control flow, preserves truthful failure causes, uses only locked project tools, and bounds test teardown solely to prevent a stuck cleanup loop.

## Plan items

### CL-001 — Restore the Windows process-identity abstraction

**Assumption:** Native access failures while *identifying* a process are semantically “identity unavailable,” not a distinct operational Job error. Preserve the baseline abstraction for every `AttributeError`/`OSError` in `_windows_start_token()`, including loader, function lookup/call, last-error lookup, and close access; do not normalize unrelated programming exceptions.

- **What to change:** Simplify `_windows_start_token()` so its ordinary control flow uses the existing typed lazy Windows-library boundary without one nested attribute wrapper per API. Retain the specific `ProcessIdentityUnavailable` messages for a failed `OpenProcess`, liveness query, or birth-time query. At one outer boundary, normalize native `AttributeError` or `OSError` into a contextual `ProcessIdentityUnavailable` chained from the original cause. Do not change `_windows_library()` or the Job/resume/close helpers to hide their contextual `OSError`.
- **Where:** `src/pyocd_debug_mcp/kernel/processes.py`, limited to `_windows_start_token()` and only the smallest adjacent typing cleanup made unnecessary by simplifying that function.
- **Exact intended behavior:** `_windows_start_token(pid)` returns `None` for Windows errors 87/1168 or an exited process, returns the same `win:CREATION_TIME` token for a live process, raises the existing specific `ProcessIdentityUnavailable` for native calls that return failure, and raises contextual `ProcessIdentityUnavailable` with a chained cause when the native loader/function/accessor is absent or raises `AttributeError`/`OSError`. `cleanup_stale_owned_processes()` consequently increments `unresolved` and retains the marker instead of leaking native `OSError`; `require_clean_startup()` continues to fail closed through its existing unresolved-marker error. `_create_windows_kill_job()`, `_resume_windows_process()`, and `_close_windows_job()` continue to raise contextual `OSError` when their native facilities are unavailable. Windows creation flags remain exactly `0x00000204`; POSIX remains `start_new_session=True`.
- **Must remain intact:** Preserve public signatures, process-marker schema/lifecycle, Job Object semantics, POSIX group semantics, existing creation/cleanup deadlines, primary failure identity, marker retention, actual Windows error codes, and all non-H00 behavior. Do not add a portability framework, broad `Any`, `type: ignore`, Pyright suppression, or configuration change.
- **Objective verification:** The spec suite must distinguish the identity entry point from lower-level native operations: fake unavailable ctypes makes `_windows_start_token()` raise `ProcessIdentityUnavailable` with a cause, while Job creation/resume/close raise `OSError` with causes. Add a caller-level hygiene assertion showing that unavailable identity yields one unresolved retained marker and no leaked `OSError`. Preserve real POSIX group coverage and exact Windows flags. Existing process-cleanup/trust-model tests plus full Pyright and pytest must pass.

### CL-002 — Make the verifier contract consistently locked

**Assumption:** `uv lock --check` is part of the accepted H00 verifier and belongs immediately after `uv sync --locked`, before build or no-sync commands.

- **What to change:** Add the exact `uv lock --check` line to the README's single ordered contributor/verifier sequence and to its exact documentation assertion. Replace the regression test's `uv run --no-project --with pyright pyright` command with Pyright from the explicit locked server project: `uv run --project SERVER_PROJECT_PATH --locked --no-sync pyright --project SENTINEL_PROJECT_PATH`. Keep both the test-scaffolding exclusion and injected shipped-source failure controls.
- **Where:** Doer-owned `README.md`; spec-tester-owned `tests/test_h00_repository_contract.py`; regression-tester-owned `tests/test_h00_repository_regressions.py`.
- **Exact intended behavior:** A reader can execute the README commands in order and verify lock consistency before build. The regression test never resolves or downloads an ad hoc Pyright, never uses `--with`/`--no-project`, succeeds when only `tests/` contains the intentional type error, and fails while naming the file when `src/pyocd_debug_mcp/` contains the intentional type error. `uv.lock` remains byte-for-byte unchanged throughout.
- **Must remain intact:** Preserve `pytest>=8`, `[tool.pyright] include = ["src"]`, all other README content, the unrelated-working-directory import proof, locked no-sync commands, and every negative control. Do not change dependencies, lock resolution, typecheck scope, or diagnostics.
- **Objective verification:** Assert the README contains every verifier command exactly once in the required order, now including `uv lock --check`. Assert the regression command contains `--project SERVER_PROJECT_PATH`, `--locked`, and `--no-sync`, contains no `--with`/`--no-project`, and produces the expected excluded/included results. The clean six-file candidate must run the same documented sequence without lockfile mutation on native Windows and POSIX.

### CL-003 — Bound clean-candidate teardown without suppressions

**Assumption:** After descendant termination and absence checks, removal should normally succeed immediately. A maximum five-second monotonic retry window is a test-teardown/thrash bound for transient Windows handle release, not a production input limit; exceeding it must fail with the last concrete `PermissionError`.

- **What to change:** In the spec tester's existing candidate cleanup helper, retain readonly-bit recovery, but remove the 120 × 0.5-second loop and the callback `type: ignore`. Type the callback honestly. Try removal immediately; retry only a real `PermissionError` while the target still exists and the five-second monotonic deadline has not expired, using short sleeps capped by the remaining time. Preserve and report the final concrete error. Continue to terminate and wait for candidate-related descendants before removing the candidate tree.
- **Where:** Spec-tester-owned `tests/test_h00_repository_contract.py` only.
- **Exact intended behavior:** Successful cleanup returns as soon as the tree is absent. A transient Windows permission failure gets bounded retry, but a persistent failure terminates in at most approximately five seconds with the last `PermissionError` in the assertion. The helper contains no `type: ignore`, never silently leaves the candidate root, and does not kill processes unrelated to the unique candidate path.
- **Must remain intact:** Preserve the fixed six-file manifest/materialization, baseline commit check, descendant path matching, descendants-before-removal order, readonly recovery, final absence assertion, nested clean-suite recursion guard, and real Windows/POSIX host evidence. Do not weaken cleanup failure into a warning or skip.
- **Objective verification:** Add deterministic helper-level controls for immediate success and persistent `PermissionError` using patched monotonic/sleep/removal behavior so the bound is tested without a real five-second delay. The existing full nested clean-candidate transaction must still remove its root and pass. Ruff/Pyright/diff checks must find no new suppression.

## Out of scope / must not change

- H01 harness/spec/evidence, firmware experiments, boards, hardware, MCP tool contracts, plan/permission/setup behavior, serial/UART, flash/debug/reset, provider behavior, or deployment.
- `pyproject.toml`, `uv.lock`, `tests/test_process_cleanup.py`, trust-model tests, production cleanup constants, or production files other than the narrowly authorized `processes.py`.
- No new dependency, test file, production helper module, OS simulation as acceptance evidence, skip/xfail, diagnostic suppression, broad `Any`, unrelated refactor, formatting sweep, commit, push, deploy, flash, or generated build artifact.
- Existing contracts not named for change remain unchanged.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises `kernel/hygiene.py` and the lower-level native helpers without editing those production callers.
- The spec tester edits only `tests/test_h00_repository_contract.py`; the regression tester edits only `tests/test_h00_repository_regressions.py`; the doer edits only `src/pyocd_debug_mcp/kernel/processes.py` and `README.md`.
- The already accepted `pyproject.toml` and `uv.lock` hashes remain unchanged.
- Both tester-recorded commands exit 0 in the same neutral harness iteration, with no ownership/tamper failure.
- Root independently runs `uv lock --check`, Ruff, Pyright, focused H00/process-cleanup/trust-model tests, collection, and full pytest from a clean locked environment. Final H00 acceptance retains the existing native Windows and Debian/ext4 ordinary plus space/Unicode host evidence requirement.
- Every server-repair role rereads `../.codex/design_charter.md` at the request checkpoints and records its attestation; the main model independently reviews the final diff and neutral evidence before accepting the repair.
