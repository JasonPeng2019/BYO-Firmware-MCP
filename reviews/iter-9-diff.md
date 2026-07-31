# Iteration 9 — Diff Adversary

Scope: `git diff 6f3da0a..HEAD` in full, fresh, per `reviews/REVIEW_POLICY.md`. Priority
weight on the newest code — the D25–D28 production fixes plus their ~11 new tests, landed
in four commits since the last pass — with `tools/serial.py`'s new exception handling as
the coordinator's named highest-risk item. `reviews/ledger.md` (through D24, M1–M9,
including the not-to-be-re-raised M9 and D29) and all prior iteration reports read first.
M9 and D29 are not re-raised per the coordinator's explicit instruction; nothing else
already ruled INVALID/EXTRANEOUS is re-raised without new grounds.

**Verification performed:** `uv run --locked ruff check src tests` → all checks passed.
`uv run --locked pyright src` → 0 errors. `PYTHONPATH=src python -m unittest discover -s
tests` → **677 passed, 7 skipped, OK**. `git status --short` clean before and after every
experimental break (each reverted via `Edit`, confirmed via `git diff`/`git status`, never
`git checkout`/`restore`/`stash`).

## Summary

| ID | Severity | One-line |
| --- | --- | --- |
| D30 | HIGH | The D25 fix (provider validation in `ProbeSelectionStore.resolve()`) has **two** uncovered call paths, not one — corrected from an initial finding of one after auditing every `target_control.open_session` call site in `server.py`, not just the one this was first found through. `_validation_connect` (`server.py:2852-2902`, `board_validate`'s fresh-connect path) and `_setup_connection_phase`'s `connect()` closure (`server.py:4117-4170`, `board_setup`'s own connect step) both open a pyOCD session directly from a probe selected by string-matching alone (`BoardValidator._select_probe`, `setup_flow/validate.py:524-540`; `PreflightEngine._select_by_id`, `setup_flow/preflight.py:253-263`), with no `PROBE_CLASSES` check and no call to `ProbeSelectionStore.resolve()` anywhere in either path. Two of `server.py`'s five `target_control.open_session` call sites are unprotected; the other three (`_connect_impl`, and the two reached only via `_resolved_probe_uid_for_connection`) are correctly covered. |

One finding this round, but a significant one, and broader than first identified: it is the
exact "a fix covers some sites and misses others" shape that produced D17 (three months of
this task's history ago in review time, one iteration ago in code-churn terms), found by
asking the question the coordinator's brief posed directly — is the newest fix actually
complete — rather than re-verifying that what it does fix, it fixes correctly (already
thoroughly re-confirmed; see below). The correction from one site to two happened during
this same review pass, by auditing the full call-site list rather than stopping at the
first gap found; recorded here rather than silently folded in, since the draft that named
only `_validation_connect` was itself incomplete in exactly the way it was warning about.

---

## Re-verification of D25–D28 and M7/M8 (the fixes themselves)

Read `git diff a8eb383..937fa0b` in full before looking for anything new. Re-proved the
highest-risk pieces by breaking them, not by re-reading the coordinator's summary of the
implementer's own proof.

**D27 (`tools/serial.py`'s new exception handling) — traced exhaustively, confirmed
correct.** The coordinator specifically asked whether `_is_cancellation` can misclassify.
Read every `cancellation_checkpoint()` call site in `services/uart_capture.py` (unchanged,
pre-existing, not part of this diff, but the site whose behavior the new code depends on)
against its enclosing `try`/`except Exception as exc: raise RuntimeError(...) from exc`
boundary:

| Function | Checkpoint site | Inside the function's own try/except? | Escapes as |
| --- | --- | --- | --- |
| `capture_uart_output` | `:114` (top of retry loop) | No | raw `OperationCancelledError` |
| `capture_uart_output` | `:133` (inner read loop) | Yes (`:127-165`) | `RuntimeError` with `__cause__` set |
| `capture_uart_output` | `:174` (reopen-delay sleep) | No | raw `OperationCancelledError` |
| `write_uart_output` | `:204,206,208` (all three) | Yes (`:203-213`) | `RuntimeError` with `__cause__` set |
| `exchange_uart_output` | `:253,261,272,295` (all) | Yes (`:252-314`) | `RuntimeError` with `__cause__` set |

Every `except Exception as exc: raise RuntimeError(...) from exc` in the file uses `from
exc`, so `__cause__` is reliably set whenever wrapping occurs. `_is_cancellation`'s two
conditions (`isinstance(exc, OperationCancelledError)` for the raw-escape rows,
`isinstance(exc.__cause__, OperationCancelledError)` for the wrapped rows) cover every row
in the table with no gap. `OperationCancelledError(RuntimeError)` — verified it *is* a
`RuntimeError` subclass (`kernel/operations.py:167`), which is what makes the raw-escape
rows reachable by `tools/serial.py`'s `except RuntimeError as exc:` at all.

**Proved, not just traced.** Removed the `__cause__` half of `_is_cancellation`
(`tools/serial.py:54-56`) and ran
`tests.test_uart_capture_evidence.UartOpenFailureTests` — one test failed exactly as
predicted:
`test_a_cancelled_write_is_never_relabeled_as_an_open_failure` — `'uart/open-failed'
unexpectedly found in ...`. Reverted the one line; `git diff` confirmed clean. The other
five tests in the class, including the belt-and-suspenders raw-escape test, stayed green
throughout — consistent with the raw-escape path being covered by the *first* half of the
check, which I did not touch.

**D28's kind-filter, re-proved.** Removed the `if execution.kind == "probe"` filter
(`server.py:4988`) and ran
`tests.test_setup_overview_no_probe.NoNativeProbeOverviewHookKindTests` — both tests
failed: the UART-only case reported `discovery/hook-failed` instead of
`discovery/no-native-probe`, and the mixed-kinds case reported the (deliberately-listed-
first) UART hook's `discovery/hook-timeout` instead of the probe hook's
`discovery/hook-failed`. The deliberate ordering in that second test (UART hook listed
first in the fixture) is what makes this a real proof of kind-filtering rather than a
coincidence of dict/tuple order — confirmed by reading the test's own comment explaining
that choice, then confirming the failure direction matched it exactly. Reverted; clean.

**D25/D26/M7's flattened-payload approach — verified deliberate, not a shortcut.** Both
`SelectionDisappeared` and `UnsupportedProvider` handlers build a real `DiscoveryFailure`
via `selection_disappeared_failure()`/`unsupported_provider_failure()`, then flatten it
into one string (`f"{failure.code}: {failure.message} {' '.join(failure.remedies)}"`)
before raising. This looked, on first read, like a step down from genuine structured JSON.
Traced where the resulting exception actually goes: `_connect_impl`'s outer `except
Exception as exc:` only special-cases `TargetConnectionError` for `open_failure_payload`;
everything else (including these new exceptions) propagates to
`kernel/registry.py:383-386`'s `except Exception as exc: raise ToolError(wrap_layer2_response(str(exc)))`
— which preserves `str(exc)` **verbatim**, not truncated or reformatted. Since an MCP
tool error is text an LLM agent reads, not a JSON object client code parses, delivering
the code and remedies as one complete, `assertIn`-checkable string is a reasonable,
deliberate fit for this call path's actual architecture, not a shortfall. Not filed as a
finding.

No further defect found in D25–D28/M7/M8 themselves — all four hold up under a second,
independent attempt to break them.

---

## D30 — HIGH — the D25 provider check has two uncovered call sites: `board_validate`'s and `board_setup`'s own fresh-connect paths

**Files:** `src/pyocd_debug_mcp/server.py:2852-2902` (`_validation_connect`);
`src/pyocd_debug_mcp/setup_flow/validate.py:524-560` (`BoardValidator._select_probe`).

D25 placed the provider check inside `ProbeSelectionStore._require_registered_provider`,
called from both branches of `ProbeSelectionStore.resolve()` — the shared choke point
`_assigned_probe_uid_for_connect` and `_resolved_probe_uid_for_connection` both go
through. That covers the `connect`/`connect_override`/`connect_under_reset` tools and the
`continue_setup`-adjacent UID-resolution helper. It does **not** cover `board_validate`'s
own ability to open a fresh connection when none exists yet.

**Traced end to end.** `board_validate` → `BoardValidator._validate_locked`
(`setup_flow/validate.py:242`) → `self._select_probe(profile, inventory, request.probe_id)`
(`:289`) → `BoardValidator._select_probe` (`:524-560`):

```python
compatible = [
    probe
    for probe in inventory.probes
    if probe.probe_family.casefold() == profile.board.probe_family.casefold()
]
```

This is the **entire** filter — a case-insensitive string match against the profile's
declared `probe_family`, sourced from `inventory.probes` (the unified inventory, which
includes hook-discovered rows with whatever `provider` string a hook printed — confirmed
in iteration 8's D25 investigation that nothing validates this against `PROBE_CLASSES`
anywhere upstream of here either). No call to `ProbeSelectionStore.resolve()`, no
reference to `registered_provider_ids()`, no `UnsupportedProvider` anywhere in this
function or its caller.

The resulting `ValidationProbe` reaches `_validation_connect` (`server.py:2852`), and when
`connection_manager.maybe_connection(profile.board_id)` is `None` (no live session for
this board yet — the ordinary case right after `board_setup`, or after any disconnect):

```python
handle = target_control.open_session(
    board=profile.board,
    unique_id=probe.usb_serial,
    target=profile.board.pyocd_target,
    ...
)
```

`probe.usb_serial` is passed straight to `target_control.open_session` → (traced in
iteration 8) `PyOCDSWDInterface.open()` → `ConnectHelper.session_with_chosen_probe`,
which searches only pyOCD's own registered probe classes by UID. For an unsupported
provider, this fails exactly as iteration 8 documented for the `connect` path:
`ProbeNotFoundError("No matching debug probe found.")` — a sibling of
`TargetConnectionError`, not a subclass, so `_validation_connect`'s own `except
TargetConnectionError as exc: last_error = exc; continue` (`:2896-2898`) doesn't catch it
either; it propagates further up, unstructured, with none of D25's guidance.

**Confirmed genuinely untested, not just unfixed.** Grepped every reference to
`_validation_connect` and `nosuchprovider` across the test suite:
`tests/test_server_assignment_connect.py:407,419` and
`tests/test_validation_honesty.py:166,192,506,541,576` all call `_validation_connect`
directly, but every one exercises protocol/timeout/policy-replay behavior or the
"existing connection matches/doesn't match" branch (`existing is not None`) — none
constructs a `ValidationProbe` with an unregistered provider and drives the `existing is
None` fresh-open branch. The one `nosuchprovider` fixture in the suite
(`test_server_assignment_connect.py:251`) is D25's own regression test, which drives
`_connect_impl`, not `board_validate`/`_validate_locked` — it does not, and was never
meant to, exercise this path.

**Why HIGH, not MEDIUM like the softer half of the D25–D28 cluster.** `board_validate` is
not a rare or secondary entry point — its own docstring (`tools/setup.py:539`) states it
triggers "instead of board_setup when the user's familiar board name already matches a
healthy profile," and separately "validate after setup, repair, or reconnect." Reaching a
board with a healthy profile but no live connection yet is the *ordinary* state right
after `board_setup` completes (which does not itself open a connection) — an agent calling
`board_validate` next, exactly as its own docstring instructs, hits this path before
`connect` is ever involved. This is at least as reachable as the original D25 scenario,
arguably more so, since it does not require the agent to have gone through `connect`
first at all.

### The second site, found by auditing rather than stopping at the first gap

After confirming `_validation_connect`, checked whether it was the *only* other place
`target_control.open_session` is called, rather than treating one found gap as the whole
answer. `grep -rn "target_control.open_session(" src/pyocd_debug_mcp/server.py` returns
**five** matches, not the two an earlier draft of this finding assumed
(`_connect_impl` and `_validation_connect`):

| Line | Function | Where the probe UID comes from | Covered by D25? |
| --- | --- | --- | --- |
| `:1274` | `_connect_impl` | `_assigned_probe_uid_for_connect` → `ProbeSelectionStore.resolve()` | Yes — this is D25's own fix site. |
| `:2879` | `_validation_connect` | `BoardValidator._select_probe` (string match only) | **No.** |
| `:4136` | `_setup_connection_phase`'s `connect()` closure | `context.preflight.selected_probe`, chosen by `PreflightEngine._select_by_id` (`setup_flow/preflight.py:253-263` — matches `probe.probe_id == selected_id` only, no registration check) | **No.** |
| `:5547` | `_setup_pack_pipeline`'s `live_connect` closure | `probe_uid` parameter, supplied by its caller in `_setup_continue` as `_resolved_probe_uid_for_connection(user_input.connection_id)` (confirmed at the call site) | Yes — transitively, through `ProbeSelectionStore.resolve()`. |
| `:5601` | `_live_test_builtin_setup_target` | `probe_uid` parameter, supplied the same way by `_setup_continue` | Yes — transitively, same as above. |

Two of the five are unprotected, not one. `:4136` (`_setup_connection_phase`) is
`board_setup`'s own connect step — even earlier in the workflow than `board_validate`,
reachable the first time an agent runs setup end to end for a board whose only visible
probe came from a hook reporting an unsupported provider, before a profile even exists yet.
Traced its own exception handling (`:4158-4161`): `except TargetConnectionError as exc:
last_error = exc; continue` — the identical gap, `ProbeNotFoundError` uncaught by name.

**Severity rationale:** HIGH, matching D25's own rating for the identical user-facing
consequence (misleading "no matching debug probe found" instead of "install a plug-in, a
hook cannot fix this") reached through two paths D25's fix does not cover, one of which
(`board_setup`'s own connect step) is reachable earlier in a project's lifecycle than the
scenario D25 itself was written against.
