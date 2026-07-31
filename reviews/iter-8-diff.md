# Iteration 8 — Diff Adversary

Scope: `git diff 6f3da0a..HEAD` in full, fresh, per `reviews/REVIEW_POLICY.md` and the
coordinator's explicit steer toward breadth: iterations 5-7 concentrated narrowly on
`hardware_inventory.py`'s vendor path and `kernel/operations.py`'s budget sites (now swept
four times with no CRITICAL/HIGH/MEDIUM findings in the last two rounds). This round
deliberately went to territory those passes did not reach: `discovery_failures.py`'s
call-site completeness, `setup_flow/preflight.py`, `tools/setup.py`, `firmstore/store.py`.
`reviews/ledger.md` (C1–C18, D1–D24, M1–M6), all prior iteration reports, and
`reviews/narrow-d15-m6.md` read first. Nothing already ruled INVALID/EXTRANEOUS is
re-raised without new grounds.

**Verification performed:** `uv run --locked ruff check src tests` → all checks passed.
`uv run --locked pyright src` → 0 errors. `PYTHONPATH=src python -m unittest discover -s
tests` → **666 passed, 7 skipped, OK**. `git status --short` clean throughout — every
finding this round was established by reading source and existing tests, not by breaking
and reverting code, since all four are about **missing** wiring rather than existing
behavior to attack.

## Summary

| ID | Severity | One-line |
| --- | --- | --- |
| D25 | HIGH | `discovery/unsupported-provider` — a guide-mandated, dedicated failure code for "a hook found a real device but this pyOCD installation has no driver for its provider" — is never produced anywhere in `src/`. No code validates a hook-reported provider string against `PROBE_CLASSES`. The row is treated as an ordinary, connectable probe; a connect attempt then fails with a generic, actively misleading "no matching debug probe found" instead of the guide's intended "a hook cannot fix this, install a plug-in" guidance. |
| D26 | MEDIUM | `discovery/selection-disappeared` is never produced in its structured form. Both real occurrence sites catch the typed `SelectionDisappeared` exception and re-raise a plain-text `RuntimeError`/`TargetControlError` instead of routing through `selection_disappeared_failure()`, losing the guide-mandated `code`/`kind`/`remedies` JSON shape (though the prose message remains actionable). |
| D27 | MEDIUM | `uart/open-failed` is never produced anywhere. `open_failure_payload`'s one call site hardcodes `PROBE_OPEN_FAILED`; `tools/serial.py`'s UART write/read/exchange functions have no exception handling around the underlying port I/O at all, so a UART open failure gets no structured remedy (driver/contention/permission/baud-rate checks) — just whatever the generic tool-failure wrapper does with a raw exception. |
| D28 | MEDIUM-HIGH | `_no_native_probe_overview` (`server.py:4933-4967`) selects `snapshot.hook_failures[0]` without filtering by kind, then unconditionally labels it `hook_failure(code, "probe", ...)`. When zero probe hooks are configured (or none failed) but a configured UART hook fails, this reports a UART hook's failure as a **probe** discovery failure — wrong `kind`, and the `get_discovery_hook_contract` call the agent is pointed at asks for the probe contract, not the one for the kind that actually needs fixing. Related: `_setup_overview`'s UART branch (`server.py:5210-5217`) never checks for UART hook failures at all, so a hook that ran and failed is indistinguishable from "no hook configured yet." |

All four are instances of the same root pattern: **a guide-mandated typed failure code was
implemented and unit-tested in isolation, but the production call site that should trigger
it under its real condition was never written** — the same shape as D15 (a feature
scaffolded and tested but never wired to production), just found in a different subsystem
this round.

---

## D25 — HIGH — `discovery/unsupported-provider` is never produced; no validation exists anywhere

**Files:** `src/pyocd_debug_mcp/discovery_failures.py:237-259` (`unsupported_provider_failure`,
defined, never called from `src/`); `src/pyocd_debug_mcp/discovery_hooks.py:731-773`
(`parse_hook_output`, validates `provider` is non-empty text, never checks it against a
registered set); `src/pyocd_debug_mcp/probe_inventory.py:57-65`
(`registered_provider_ids`, the source of truth, used only for the informational
`pyocd_providers` field in `get_discovery_hook_contract`'s response).

**Verified, not inferred.** Grepped every reference to `unsupported_provider_failure`,
`DISCOVERY_UNSUPPORTED_PROVIDER`, and `registered_provider_ids`/`PROBE_CLASSES` across all
of `src/`:

```
grep -rn "unsupported_provider_failure\|selection_disappeared_failure" src/ tests/
  -> discovery_failures.py:237,262 (definitions)
  -> tests/test_discovery_hook_safety.py:28,29,233,243,297,298 (unit tests of the
     functions' own output shape, called directly — never through production code)
```

No other match. `registered_provider_ids`/`PROBE_CLASSES` are referenced in exactly four
places, all either the definition or the `pyocd_providers` info field
(`tools/discovery.py:280`, `server.py:6061`) — none of them a validation gate.

**Confirmed by the existing test suite's own evidence, not just by absence.**
`tests/test_discovery_hook_workflow.py:402-412`:

```python
def test_an_unsupported_provider_is_diagnostic_only(self) -> None:
    self.flow.native_ports = [_port("COM3")]
    self.flow.install([hook_entry("odd", "probe", argv=["probe_provider", "nosuchprovider"])])
    self.flow.refresh()
    snapshot = self.flow.snapshot()
    self.assertEqual(snapshot.probes[0].provider, "nosuchprovider")
    self.assertNotIn("nosuchprovider", registered_provider_ids())
```

The test's own name claims "is diagnostic only." Its body never asserts anything of the
kind — it only confirms the row exists with the given provider string and that the string
genuinely isn't registered. `self.flow.snapshot()` (`_WorkflowCase`'s helper, line 141)
calls `HardwareInventoryService.snapshot()` directly; the resulting `ProbeRow` is
structurally identical to any other probe row, with no marker, flag, or separate handling.
This test is consistent with — and inadvertently documents — the absence of any real
"diagnostic only" treatment; it does not prove one exists.

**Traced the concrete downstream consequence.** An unsupported-provider row is surfaced to
`setup_overview`'s `connections[]` exactly like a normal probe (confirmed: no provider
check anywhere in `_setup_overview`'s row-building loop). If selected and connected:
`_connect_impl` → `_resolve_probe_uid_for_connect` → eventually
`PyOCDSWDInterface.open()` (`adapters/swd_pyocd.py:439-497`, unchanged by this diff, but
the interaction is new) → `self._choose_session(probe_uid=probe_uid, options=options)` →
`ConnectHelper.session_with_chosen_probe(unique_id=probe_uid, ...)` — note **no provider is
ever passed to pyOCD**, only the UID; pyOCD searches all its own registered probe classes
for a UID match. Since no pyOCD-registered class exists for the hook's made-up provider,
`session` is `None`, and `open()` raises `ProbeNotFoundError("No matching debug probe
found.")` (`swd_pyocd.py:497`). `ProbeNotFoundError` and `TargetConnectionError` are
**sibling** subclasses of `TargetControlError` (`target_errors.py:10,14`), not
parent/child — verified by reading the class hierarchy directly. `_connect_impl`'s
exception handler (`server.py:1255-1268`) only builds `open_failure_payload(...)` when
`isinstance(exc, TargetConnectionError)`; `ProbeNotFoundError` fails that check, so
`open_failure` stays `None` and the agent receives the bare, generic exception text — not
even the (already-imperfect but at least structured) `PROBE_OPEN_FAILED` remedy set, let
alone the guide's intended "a hook cannot fix this, tell the user which providers are
supported" message.

**Failure scenario, concrete and realistic:** discovery hooks exist specifically for
hardware pyOCD's native enumeration cannot fully handle — the exact class of situation
where a provider mismatch is plausible, not exotic. An agent writes a discovery hook for a
custom or rare debug adapter, correctly reporting its USB-level UID but using a provider
label pyOCD doesn't register (a typo, an aspirational name, or genuinely unsupported
hardware). `setup_overview` shows it as a normal, selectable connection. The agent
connects. It gets "No matching debug probe found" — text that reads as a physical/cabling
problem. The agent (per its own tool guidance) is likely to tell the user to re-seat the
connector or retry, when the actual, only fix is either installing a different pyOCD
plug-in or accepting the hardware is unsupported — neither of which "No matching debug
probe found" suggests, and neither of which retrying will ever resolve.

**Severity rationale:** HIGH. This is not merely an unmet documentation clause (like D21
was) — it is a missing validation gate whose absence produces **actively misleading**
guidance for a scenario the discovery-hook feature exists to serve, in a configuration a
realistic hook author can reach without any unusual input.

---

## D26 — MEDIUM — `discovery/selection-disappeared` is never produced in its structured form

**Files:** `src/pyocd_debug_mcp/discovery_failures.py:262-274`
(`selection_disappeared_failure`, defined, never called from `src/`);
`src/pyocd_debug_mcp/server.py:1000-1022` (`_assigned_probe_uid_for_connect`),
`:1056-1074` (`_resolved_probe_uid_for_connection`) — the two, and only two, real
occurrence sites of the typed `SelectionDisappeared` exception (confirmed by grepping
every reference to `SelectionDisappeared`/`SelectionNotRecorded` across `src/`).

Both catch sites convert the typed exception into a bare-text exception instead of the
structured payload:

```python
except SelectionDisappeared as exc:
    raise RuntimeError(
        f"The assigned probe for {board_id} is no longer present; rerun setup routing "
        f"to choose the current physical connection. ({exc.code}: {exc.reason})"
    ) from exc
```
```python
except SelectionDisappeared as exc:
    raise TargetControlError(f"{exc.code}: {exc.reason}") from exc
```

Neither constructs a `DiscoveryFailure`/calls `selection_disappeared_failure()`. The
guide's step-8 table specifies this code's payload must carry "reroute through
setup_overview; do not substitute" as a structured remedy — `selection_disappeared_failure`
implements exactly that (`"rerun setup_overview and reselect the connection"` in
`remedies`, `carries_hook_contract()` false, confirmed by the isolated unit test), but it
is unreachable from either real trigger site.

**Practical impact softer than D25.** The prose message in both fallback exceptions does
contain the essential guidance ("rerun setup routing to choose the current physical
connection"), so an agent reading the raw exception text would likely still recover
correctly. What's missing is the promised JSON shape (`code: "discovery/selection-
disappeared"`, structured `remedies`) that a client parsing responses programmatically
(rather than reading prose) would rely on, and which the guide's acceptance table lists as
a distinct, required condition class alongside the others.

**Severity rationale:** MEDIUM — a genuine, verified guide-requirement gap with a real
(if narrower than D25) risk that a structured-response-consuming client gets an
unstructured error where the contract promises a typed one.

---

## D27 — MEDIUM — `uart/open-failed` is never produced anywhere

**Files:** `src/pyocd_debug_mcp/discovery_failures.py:29,44,71-75,277-308`
(`UART_OPEN_FAILED`, `UART_OPEN_FAILED_CHECKS`, `open_failure_payload`'s dispatch logic
for it); `src/pyocd_debug_mcp/tools/serial.py:189-278` (`write_serial`, representative —
same absence in `read_serial`/`serial_exchange`).

`open_failure_payload` has exactly one call site in the whole codebase
(`server.py:1261`), and it hardcodes `PROBE_OPEN_FAILED`:

```python
open_failure = (
    open_failure_payload(PROBE_OPEN_FAILED, detail=str(exc), identity=uid)
    if isinstance(exc, TargetConnectionError) else None
)
```

`UART_OPEN_FAILED` is referenced only within `discovery_failures.py` itself (its own
constant definition and the `checks = ... if code == PROBE_OPEN_FAILED else
UART_OPEN_FAILED_CHECKS` branch inside `open_failure_payload`, which no caller ever
reaches with that code). Read `write_serial` (`tools/serial.py:189-278`) end to end:
`services.write_uart(resolved_port.device, resolved_baudrate, payload,
timeout_seconds=timeout_seconds)` (`:255-260`) has no surrounding `try`/`except` at all —
any exception from the underlying serial I/O (permission denied, port vanished, timeout)
propagates unstructured to whatever the generic Layer-2 tool-failure wrapper does with a
raw exception, never through `UART_OPEN_FAILED_CHECKS`'s specific remedies ("no other
process holds the serial port open," "the port path still exists and the user has
permission to open it," "the configured baud rate matches the firmware").

**Severity rationale:** MEDIUM. Unlike D25, there is no *misleading* diagnosis here — just
an absent one; the generic exception wrapper presumably still surfaces the underlying
error text. But this is a fully-specified, guide-mandated failure code with concrete,
useful remedies that a realistic UART hardware failure (permission error, port removed
mid-session, baud mismatch) would benefit from and never receives, in a tool
(`write_serial`/`read_serial`) any board with a UART workflow uses routinely.

---

## D28 — MEDIUM-HIGH — a UART hook's failure can be reported as a probe discovery failure

**File:** `src/pyocd_debug_mcp/server.py:4933-4967` (`_no_native_probe_overview`).

```python
hook_rows = snapshot.hook_diagnostic_rows()
failures = snapshot.hook_failures
if failures:
    first = failures[0]
    failure = hook_failure(
        first.failure_code or DISCOVERY_HOOK_FAILED,
        "probe",
        hook_diagnostics=tuple(hook_rows),
        retry_id=_issue_overview_retry("probe", board_names),
    )
else:
    failure = no_native_probe_failure(...)
```

`InventorySnapshot.hook_failures` (`hardware_inventory.py:283-284`) is **not** filtered by
kind — `tuple(execution for execution in self.hook_diagnostics if not execution.ok)`
returns failures of both `"probe"` and `"uart"` kind indiscriminately. `first = failures[0]`
takes whichever failed execution happens to be first in `snapshot.hook_diagnostics`'s
order, then `hook_failure(..., "probe", ...)` **hardcodes** the kind to `"probe"`
regardless of what `first.kind` actually was.

**Traced reachability.** `_no_native_probe_overview` fires only when `not connection_rows`
(zero probe rows after native+hook+vendor merge). Per the §0 gating rule, probe hooks run
only when `hooks.has_hooks_for("probe")` is true — so if **no probe hook is configured at
all** (a project that only needs a UART hook, e.g. all its boards' debug probes enumerate
fine natively but the UART bridge needs vendor-specific discovery), `snapshot.hook_diagnostics`
can contain **only** UART-kind entries, since UART hooks are gated independently on native
UART being empty (confirmed via `_collect_uart_rows`, reviewed and confirmed accurate in
iteration 7). If that UART hook fails (times out, bad output, source drift) in the same
snapshot that also happens to have zero visible probes (a plausible simultaneous condition,
not a contrived one — e.g. the board is fully unplugged, so both its probe and its UART
are absent), `_no_native_probe_overview` fires, `failures` contains only the UART failure,
and it gets relabeled and reported as a **probe** discovery failure.

**Concrete consequence:** the resulting payload's `agent_prompt` and `code` describe a
probe hook problem ("A discovery hook did not complete successfully... repair the hook
file"), while the `hook_contract_call` (via `contract_call("probe", ...)`) points the agent
at `get_discovery_hook_contract(kind="probe")` — the **wrong contract**, for a hook kind
that isn't the one failing. The raw `hook_diagnostics` array in the payload does still
contain the real (UART) execution's true `hook_id`/`kind`/`detail`, so a careful reader
could in principle work out the truth from the diagnostics list even though the primary
`code`/`message`/`contract_call` are wrong — this is why the severity is MEDIUM-HIGH
rather than the HIGH given to D25, whose misdiagnosis has no such buried correction.

**Related, same root cause:** `_setup_overview`'s UART-missing branch
(`server.py:5210-5217`) never inspects `snapshot.hook_failures` for UART-kind entries at
all — it unconditionally attaches a bare `contract_call("uart")`, so a UART hook that ran
and failed produces the *identical* response as "no UART hook has ever been configured."
The probe side of `_setup_overview` doesn't have this specific instance of the gap (it
uses the correctly-branching `_no_native_probe_overview`), but the *overview* path's UART
half was never given the same "did a hook already try and fail" distinction its probe
counterpart has.

**Severity rationale:** MEDIUM-HIGH. Reachable without contrived inputs (any project using
a UART-only hook and no probe hook), produces a `code` and `contract_call` that actively
point the agent at the wrong tool/kind, though the raw evidence to self-correct remains
present in the same payload — a careful agent could recover, an average one likely
would not.

---

## Independent sweep, breadth-first

Beyond the discovery-failures cluster above, covered without finding further material
issues (each checked directly against source, not assumed clean by precedent):

- **`setup_flow/preflight.py`**'s full diff (`hook_contract_call`/`uart_hook_contract_call`
  fields, the "append not replace" message construction, the `observed | {...} if ... else
  observed` conditional-merge expressions) — traced Python's operator precedence for the
  `A | B if C else D` construct by hand (parses as `(A | B) if C else D`, confirmed
  correct and non-crashing since the unevaluated branch is never touched) and confirmed
  `PreflightEngine`'s `if current.hook_contract_call is not None:` check, while always-true
  in practice given `_setup_inventory` always populates it via `contract_call(...)` (which
  never returns `None`), is the *same, already-reviewed-and-accepted* pattern as C6/D6's
  resolution (`hooks_available` removed because it was always `True`) — here the
  `Optional` field genuinely serves test-fixture flexibility, not dead production
  conditionality. No new finding.
- **`tools/setup.py`**'s 30-line diff (opaque-token passthrough in `load_setup_tool` and
  `board_validate`) — matches FIX 8's already-reviewed provider-qualified equality-only
  design exactly. No new finding.
- **`firmstore/store.py`**'s 6-line diff (`FirmLayout.discovery_hooks` field,
  `ensure_layout()` addition) — matches the guide's minimal instruction exactly, trivial
  and correct. No new finding.
- **`discovery_failures.py`**'s remaining structural guarantees (`open_failure_payload`
  cannot carry `hook_contract_call`, `carries_hook_contract`'s recursive walk) — re-verified
  present and correct; this is what led to noticing the call-site gap above rather than a
  defect in the guarantees themselves.

No further findings from this round's sweep.
