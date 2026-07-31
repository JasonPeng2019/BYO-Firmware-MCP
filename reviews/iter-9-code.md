# Iteration 9 — Code Adversary

Scope: the codebase at HEAD (`937fa0b`), attacked fresh as a hostile reviewer with no diff
context, per `reviews/REVIEW_POLICY.md`. Weight on the newest production code (the D25–D28
fixes) per the coordinator's brief, plus a cold-read pass over `BoardValidator`'s connect
path — the file that produced this round's one finding — since it had never been read
closely by any prior round. `reviews/ledger.md` (through D24, M1–M9) and all prior
iteration reports read first. M9 and D29 not re-raised, per instruction.

**Verification performed this round:** `uv run --locked ruff check src tests` → all
checks passed. `uv run --locked pyright src` → 0 errors. `PYTHONPATH=src python -m
unittest discover -s tests` → 677 passed, 7 skipped, OK.

## Summary

| ID | Severity | One-line |
| --- | --- | --- |
| C20 | HIGH (cross-reference to D30) | Reading `ProbeSelectionStore.resolve()` cold and asking "what else in this codebase resolves a probe identity into something pyOCD will open" surfaces two more such places independently of any diff history: `setup_flow/validate.py::BoardValidator._select_probe` and `setup_flow/preflight.py::PreflightEngine._select_by_id`, both filtering by a bare string/ID-equality check with no registration validation, each feeding its own `target_control.open_session` call in `server.py` (`_validation_connect` and `_setup_connection_phase`'s `connect()` closure respectively). |

No further findings from a fresh pass beyond what's detailed in `reviews/iter-9-diff.md`
(D30). `tools/serial.py`'s new exception handling — the coordinator's named top
priority — was attacked directly (see below) and holds.

---

## Cold read: `tools/serial.py`'s new exception-handling code, as a reviewer who has never seen `uart_capture.py`'s internals would encounter it

A diff-blind reader sees three new `try/except RuntimeError as exc: if _is_cancellation(exc): raise; _raise_uart_open_failure(...)` blocks and one new helper, `_is_cancellation`, whose docstring makes a specific, checkable claim: that the functions it wraps "wrap *every* exception... in a plain `RuntimeError`" with "the original type surviv[ing] only as `__cause__`." A docstring making a precise claim about another module's behavior is exactly the kind of statement this task has learned to verify rather than trust (the D21 documentation error was exactly this shape, one file over). Opened `services/uart_capture.py` cold and checked the claim directly, function by function and checkpoint by checkpoint — full results in `reviews/iter-9-diff.md`'s re-verification section, not duplicated here. The claim holds exactly, including for the two checkpoints (`capture_uart_output:114,174`) that sit *outside* any try/except and therefore escape as a raw, unwrapped `OperationCancelledError` rather than a wrapped `RuntimeError` — a case the docstring's own prose doesn't spell out but which `_is_cancellation`'s first condition (`isinstance(exc, OperationCancelledError)`) already handles correctly, and which the file's own belt-and-suspenders test
(`test_a_cancellation_raised_directly_is_never_relabeled_either`) exists specifically to prove.

**Broke it to be sure, not satisfied by reading alone.** Removed the `__cause__` half of
the check; the one test built to guard exactly that half failed, cleanly and immediately.
Reverted. Full detail in the diff report.

**Checked for the class of bug the coordinator asked about directly: could the new
`try/except` change what *succeeds*?** All three new blocks wrap only the call to
`services.capture_uart`/`write_uart`/`exchange_uart`; the success-path assignment
(`capture = ...`, `write_result = ...`, `exchange = ...`) and everything after it in each
function is textually unchanged and only reached when no exception occurs. No new
`return`, no new early exit, no altered argument passed to the wrapped call. Confirmed by
reading, not merely assumed clean because the diff is additive.

---

## Cold read: `BoardValidator`'s connect path (`setup_flow/validate.py`), never previously reviewed closely

This is the file that first produced D30. Read `_validate_locked` (`:242-460`ish) and
`_select_probe` (`:524-560`) end to end as if encountering `BoardValidator` for the first
time, with the specific question the D25 fix raises: *every place that turns a probe
identity into something handed to pyOCD needs the same registration check — where are all
of those places?*

- `_select_probe`'s docstring-free, four-line filter
  (`probe.probe_family.casefold() == profile.board.probe_family.casefold()`) reads, on
  its own, like an ordinary compatibility filter — nothing about it signals "this is a
  trust boundary." That is exactly why it was missed by the original D25 fix: the
  boundary D25 patched (`ProbeSelectionStore.resolve()`) has an explicit, named purpose
  ("the choke point every real connect path shares") that invites the question "does
  everything really go through here?" `_select_probe` has no such framing, and isn't
  textually close to `ProbeSelectionStore` at all — different file, different module,
  different naming convention (`ValidationProbe` vs `ProbeSelection`).
- Checked whether `ValidationProbe` and `ProbeSelection` are the same type or convertible
  in a way that might mean they share validation incidentally: they don't.
  `ValidationProbe` (`setup_flow/validate.py`) is a plain frozen dataclass with no
  relationship to `hardware_inventory.ProbeSelection`/`ProbeSelectionStore` — confirmed by
  reading both definitions. No incidental coverage.
- Checked the other two `ValidationBackend` callbacks that touch a probe/connection
  (`_validation_target_supported`, `_validation_read`, `_validation_capture`,
  `_validation_close`) for a similar "opens something new" exposure:
  `_validation_target_supported` only checks target-name support (no probe open, string
  lookup only); `_validation_read`/`_validation_capture`/`_validation_close` all operate
  on the `_ValidationConnection`/handle `_validation_connect` already produced — they
  don't independently open anything, so they don't need their own check. `_validation_connect`
  is the one and only site in this file that calls `target_control.open_session`.
- **Followed the same question into `setup_flow/preflight.py`**, since `PreflightEngine`
  is the other "picks a probe by ID from an inventory list" module in the tree.
  `_select_by_id` (`:253-263`) has the identical shape:
  `next((candidate for candidate in candidates if getattr(candidate, id_field) ==
  selected_id), None)` — pure ID equality, no provider check. Its result
  (`context.preflight.selected_probe`) feeds `server.py:4117-4170`'s `connect()` closure
  inside `_setup_connection_phase`, `board_setup`'s own connect step, via
  `probe.usb_serial` straight into a sixth-if-you-count-it `target_control.open_session`
  call. This is what turned the original one-site finding into the two-site D30 recorded
  in `reviews/iter-9-diff.md` — caught by generalizing the question ("everywhere a probe
  is chosen by matching, not resolving") rather than stopping once the first instance of
  it was confirmed.

No further findings from either file beyond D30/C20.

---

## Independent sweep, diff-blind

Given the significant finding already surfaced by asking "what else resolves a probe
identity," spent remaining budget checking whether the *same* question has a similar
answer for the **UART** side (is there an analogous UART-identity-resolution site that
bypasses whatever guards UART paths have) rather than re-deriving already-settled ground:

- UART identity resolution has exactly one real site,
  `_resolve_serial_port_for_session` (confirmed unchanged and singly-located across
  iterations 5-8's repeated review of this exact function) — no `BoardValidator`-side
  parallel exists for UART, because validation's UART step
  (`_validation_capture`/`_validation_read`) operates on an already-open connection's
  already-resolved port, not a fresh one it opens itself. Confirmed by reading
  `ValidationBackend`'s four callbacks (above) — none of them independently resolves a
  UART port; only a probe *connection* has the `_validation_connect`-shaped "open fresh if
  none exists" branch. No UART-side analogue of D30 exists.
- **Correction made during this same pass, not after it.** An earlier version of this
  sweep claimed `grep -rn "target_control.open_session(" src/pyocd_debug_mcp/server.py`
  returns exactly two matches. It returns **five** (`:1274, 2879, 4136, 5547, 5601`). The
  claim was wrong because it was written right after finding `_validation_connect` and
  not re-checked against the actual grep output before being asserted. Redone properly:
  all five call sites traced back to where their probe UID originates (table in
  `reviews/iter-9-diff.md`'s D30 section). Three are transitively safe (`_connect_impl`
  directly, plus two `_setup_continue`-driven closures that both receive
  `_resolved_probe_uid_for_connection(user_input.connection_id)` as an argument, already
  passed through `ProbeSelectionStore.resolve()` before either closure runs); two are not
  (`_validation_connect`, `_setup_connection_phase`'s `connect()`). This is disclosed here
  rather than silently corrected, since asserting a grep result without checking its
  actual output is exactly the kind of unverified claim this task's review standard
  exists to catch, including when made by the reviewer itself.

No further findings.
