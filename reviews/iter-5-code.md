# Iteration 5 — Code Adversary (safety cap)

Scope: the code as it stands at HEAD (`418b17d`), attacked fresh, ignoring what changed to
get here — a hostile reviewer seeing this codebase for the first time. `reviews/ledger.md`
(38 adjudicated rows) read first; nothing re-raised. Hardest scrutiny on directed target 2
(`probe_families.py`, never reviewed cold before this iteration), then an independent
sweep.

**Suite state, reproduced this iteration:** `PYTHONPATH=src .venv/Scripts/python.exe -m
unittest discover -s tests` → **617 passed, 7 skipped, OK** (single run, 227s). `uv run
--locked ruff check src tests` → **All checks passed.** `git status --short` → clean except
the untracked `reviews/RESUME.md` review artifact. Baseline claims in `RESUME.md` hold.

## Summary

| ID | Severity | One-line |
| --- | --- | --- |
| C16 | LOW (cross-reference) | `HardwareInventoryService.vendor_uarts` is a fully-implemented, thoroughly-tested constructor parameter that is never given a real value anywhere in `src/` — a fresh reader has no way to tell this apart from a completed feature without checking the one production call site. Same underlying fact as D15 in `reviews/iter-5-diff.md`; filed here too because a diff-blind reader would independently flag it as dead-parameter code smell. |

No CRITICAL, HIGH, or MEDIUM code-only findings. Target 2 (`probe_families.py`) is clean;
details below, reported as a negative result per instruction rather than omitted.

---

## Target 2 — `src/pyocd_debug_mcp/probe_families.py`, reviewed cold

This file was adopted (M4) from a parallel session's independent attempt at the same task
and has never been through an adversary pass. Read the entire 181-line file end to end,
plus its one test file (`tests/test_probe_cli_command.py`, 21 lines — also never reviewed).

**Frozen vs. non-frozen interpreter.** `configured_probe_cli_commands()` (`:161-180`)
builds `(sys.executable, "-m", "pyocd", *suffix)` when the packaged default executable is
`"pyocd"` (confirmed via `probe_families.json:3-4`, `"executable": "pyocd"` — the only
value this branch is ever reached with in this repo). If the server process were ever a
frozen/bundled interpreter (PyInstaller etc.), `sys.executable` would be the frozen binary
itself, which does not understand `-m pyocd`. Grepped the whole `src/` tree for
`sys.frozen`, `_MEIPASS`, `PyInstaller`: **zero real hits** (the only `frozen` matches are
`@dataclass(frozen=True, ...)` decorators, unrelated). Checked `pyproject.toml` and the
guide's own verification commands (`uv run --locked python -c "..."`, `.venv/Scripts/python.exe`)
for any indication this project is ever packaged as a standalone frozen executable — none
found. **Not filed as a finding**: there is no evidence this deployment mode is in scope for
this codebase; flagging it would be manufacturing risk the project doesn't have.

**Spaces in the interpreter path.** `configured_probe_cli_commands()` returns a `tuple[str,
...]` argv, never a shell string, and every caller (`popen_owned`/`run_owned` via
`kernel/processes.py`) invokes `subprocess.Popen(validated, **kwargs)` without
`shell=True`. A path like `C:\Program Files\Python310\python.exe` is passed as a single
argv element and needs no quoting — this is exactly the class of bug shell-string
construction would have, and this code never constructs one. **Confirmed clean by reading
the call chain, not assumed.**

**`PYOCD_CLI` override honored in every branch.** The override check
(`os.environ.get(spec.executable_env, "").strip()`, `:170`) is the *first* thing the
function does; if truthy, it returns immediately, before either the `sys.executable`
branch or the `shutil.which` branch is reached. There is exactly one override check, and
it strictly dominates both other branches — there is no way to reach the `sys.executable`
or `shutil.which` code with an override set. `tests/test_probe_cli_command.py::
test_explicit_operator_override_remains_supported` confirms this behaviorally (sets
`PYOCD_CLI=C:/tools/pyocd.exe`, asserts the override string is used verbatim, un-resolved).
Both the override and the `shutil.which` fallback validate for NUL bytes and emptiness
(`:172-173`, `:178-179`); the `sys.executable` branch has no equivalent check, but
`sys.executable` is populated by the interpreter itself in every normal execution mode
this project runs in (confirmed no frozen-interpreter path exists, above) — this exact gap
was already reviewed and accepted in the ledger's M4 entry as matching the
already-reviewed `discovery_hooks.py:161-166` precedent; not re-raised.

**Does `-m pyocd` change argv shape, exit codes, or stderr format in ways
`probe_inventory.py`'s exit-124 path depends on?** Traced end to end:

- `list_connected_probes_detailed` (`probe_inventory.py:253-279`) treats `command` purely
  as an opaque tuple stored for diagnostics (`command=tuple(command)`) — nothing inspects
  `command[0]` or its shape.
- `timed_out = exit_code == PROBE_CLI_TIMEOUT_EXIT_CODE` (`= 124`, set by `_run_cmd` on
  `subprocess.TimeoutExpired`, independent of what the argv was) — unaffected by argv
  shape.
- `available = self.exit_code == 0` — the only other exit-code-sensitive logic. Actually
  ran `python -m pyocd list --probes` in this repo's own `.venv` to confirm pyOCD ships a
  usable `__main__.py` (`.venv/Lib/site-packages/pyocd/__main__.py` exists) and the
  invocation genuinely works: exit code 0, real probe-table output. If pyOCD were *not*
  installed as a module, `python -m pyocd` would exit nonzero with a "No module named
  pyocd" stderr rather than the previous behavior's `FileNotFoundError` → exit 127 — a
  real, intentional behavior change from M4's fix (this is what the fix *is*), but
  `available`'s `== 0` check treats both as equally "not available," so no logic branches
  on the distinction. Grepped for any `exit_code == 127` special-casing anywhere in
  `src/` — only the two definition sites (`server.py:883`, `swd_pyocd.py:159`), no
  consumer treats 127 specially. **No dependency found; confirmed clean, not assumed.**

**Test coverage gap, informational only.** `tests/test_probe_cli_command.py` covers only
the default (`sys.executable`) and explicit-override paths. It does not cover the
`shutil.which` fallback branch (`:177-180`, reached only if `spec.executable != "pyocd"`,
which the packaged config never sets) or a whitespace-only `PYOCD_CLI` value (e.g.
`"   "`, which `.strip()` would reduce to falsy and fall through to default — plausible
behavior, just unexercised). Not filed as a numbered finding: the untested branch is
provably unreachable with the shipped config, and the whitespace case is a straightforward
consequence of `.strip()` that would need a contrived, undocumented environment value to
matter.

**Conclusion: `probe_families.py` is clean.** No defects found on any of the four axes the
review brief named (frozen interpreter, spaces in path, `PYOCD_CLI` override coverage,
`-m pyocd` argv/exit-code/stderr shape versus the exit-124 dependency).

---

## C16 — LOW (cross-reference to D15) — `HardwareInventoryService.vendor_uarts` is dead-by-omission in production

**File:** `src/pyocd_debug_mcp/hardware_inventory.py:225-234`, `server.py:3010-3015`.

A cold read of `HardwareInventoryService`'s dataclass fields (`:225-234`) shows five
injected callables: `native_probes`, `native_uarts`, `active_connections`, `hook_snapshot`,
`vendor_uarts`, plus `run_hooks`. Reading forward into `_collect_uart_rows` (`:308-342`)
shows `vendor_uarts` backing a fully-built merge path (`_vendor_uart_rows`,
`_merge_uart_rows`), sharing the UART hook gate, with a docstring explicitly describing the
design intent. A fresh reader would reasonably conclude this is a completed, working
feature — the code reads as finished, and `tests/test_unified_inventory.py::
VendorProvenanceTests` exercises it thoroughly (three tests, all passing).

Only by checking every construction site of `HardwareInventoryService` (`grep -rn
"HardwareInventoryService(" src/` — exactly one, at `server.py:3010`) does it become
apparent that `vendor_uarts` is the sole parameter left unset, silently defaulting to
`lambda: ()`. Nothing in `hardware_inventory.py` itself signals this — no `TODO`, no
`NotImplementedError`, no test asserting the production wiring exists. A maintainer
reading only `hardware_inventory.py` and its test file would have no way to discover this
gap without independently thinking to check `server.py`'s construction call, which is a
different file, ~2800 lines away.

This is the same underlying fact as **D15** in `reviews/iter-5-diff.md` (which covers the
guide-compliance angle: step 4 explicitly required this wiring). Filed here too, under a
new ID, because it is independently discoverable by a diff-blind reader purely as a
code-smell/completeness question ("why does this well-built merge path have no real
caller?") — the two write-ups approach the same fact from different angles the review
brief asked for (diff-vs-spec, and cold code reading) and should be adjudicated together,
not duplicated as separate fixes. See D15 for the full analysis, including the further
finding that `resolve_serial_port`'s own vendor-CLI correlation (the mechanism that
continues to function via the pre-existing code path) requires non-empty native ports to
work at all, which bounds how much even a fully-wired `vendor_uarts` would have helped.

**Severity:** LOW as a standalone code-adversary observation (no behavior changes, no
regression) — the substantive severity assessment is D15's (MEDIUM), which owns the actual
guide-compliance judgment.

---

## Independent sweep

Time budget spent on areas no prior iteration has read closely, beyond the directed
targets:

- **`discovery_hooks.py`'s capped-execution machinery** (`_execute`, `:870-961`;
  `_CappedReader`, `:818-849`). Read end to end against the guide's detailed sketch
  (§1, "Capped execution") and traps 1/12. Found it *more* careful than the guide's own
  sketch: `join_readers()` is idempotent and tracks which streams actually finished
  (`joined` list) so the `finally` block only closes streams a reader thread is
  provably done with, never underneath a still-blocked reader (guide's trap 12
  requirement, met with an extra safety margin the sketch didn't have). `outcome` is
  correctly downgraded to `"cleanup_failed"` both when process-group termination isn't
  confirmed and, separately, when a reader thread fails to join in time (a case the
  guide's sketch doesn't explicitly enumerate but this implementation does). No defect
  found.
- **`tools/discovery.py::DiscoveryRetryStore`** (`:88-177`). Bounded via `OrderedDict`
  with oldest-evict-on-insert (`:132-135`), TTL checked lazily on `claim()` rather than a
  background sweep (no timer thread to leak or race), kind-mismatch refuses without
  consuming the ticket, all mutation under one `RLock`. Matches the guide's §2 spec
  exactly. No defect found.
- **`discovery_failures.py::open_failure_payload`** (`:277-308`). The "must not carry
  `hook_contract_call`" structural guarantee (guide §8) is real, not just tested: the
  returned dict is built from a fixed, enumerable key set with no code path that ever adds
  that key, backed by a defense-in-depth `assert`. No defect found.
- **`registered_provider_ids()`** (`probe_inventory.py:57-65`) — confirmed it derives from
  `PROBE_CLASSES` (`sorted(str(key).casefold() for key in PROBE_CLASSES)`), not
  `probe_families.json`, matching guide trap 8 exactly, and is what
  `get_discovery_hook_contract`'s `pyocd_providers` field actually calls
  (`tools/discovery.py:280`).

No further findings beyond C16.

---

## Ledger integrity (target 4, code-adversary lens)

Cross-checked a sample of "Fixed" ledger rows against the literal code at HEAD rather than
trusting the ledger's prose (full list and results in `reviews/iter-5-diff.md`'s Target 4
section, not duplicated here). All sampled rows matched. No drift found.
