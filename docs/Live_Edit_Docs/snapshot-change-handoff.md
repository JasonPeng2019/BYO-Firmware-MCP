# Handoff — snapshot-cadence rework + adversarial review loop

**Status: GREEN. Nothing is blocked, nothing is half-done.**

```
FAST RUN (pre report-delete/prune increment)   Ran 246 tests   OK  x3
FULL SUITE (pre report-delete/prune increment) Ran 464 tests   OK (skipped=3)

FAST RUN (post report-delete/prune increment)  Ran 256 tests   OK
FULL SUITE (post report-delete/prune increment) Ran 474 tests  OK (skipped=3)
```

Three identical fast runs on a frozen tree (no flakiness) validated the original four-amendment
change. A second increment (report delete-on-ACK + `_acked` pruning, §8 below) added 10 more
tests (5 real + 5 falsification twins) on top of that baseline; the full suite (18 real
`codex exec -m gpt-5.4-mini` e2e tests included) is green at 474.

---

## 1. What this work was

Four spec amendments to the autonomous issue monitor, then a manager-run adversarial review
loop over the result.

**The amendments** (spec: `mcp-issue-monitor-spec-new-f (1).md`; plan:
`issue-monitor-implementation-plan.md`):

1. **Ledger records occasions, not calls** (F-38, F-42, F-165, N-8, AC-18, AC-103). The
   per-call record is gone. `SegmentLedger.append()` takes only `(kind, *, detail)`. Five
   kinds: `boot`, `usage_snapshot`, `checkin`, `report`, `close`. Snapshots carry the run's
   **cumulative** counts, never per-window deltas. Verified: 7 tool calls → 5 ledger records.
2. **Anti-under-report honesty** (F-166, AC-104). Three tiers stated verbatim in
   `monitor/ledger.py`'s docstring; a test fails if tier 3 ("neither prevented nor detected")
   is ever dropped.
3. **Decoupled background sender** (F-167, AC-105). Nothing on the request path awaits a
   send; a full queue drops the *handoff*, never the record.
4. **Named constants** (N-11, AC-106). One constant per tunable; the snapshot cadence drives
   snapshot production, segment roll, and periodic delivery *together*.

---

## 2. Review loop outcome — 14 findings, 14 valid, all fixed

### Code (5), found by the reviewer agent

| ID | Finding | Resolution |
|---|---|---|
| C-1 | `submit_checkin` computed coverage from a stale advertised-tool set | Fixed. Reviewer found a **6th** affected call site (`closeout`) and centralised all of them into one `_refreshed_snapshot()` helper |
| C-2 | `icacls` subprocess inline on the dispatch path | **Overridden — see §3.** Reverted to synchronous; `_HARDENING_TIMEOUT_SECONDS` 2.0 → 0.5 |
| C-3 | Closeout spent `CLOSEOUT_BUDGET_SECONDS` twice (0.8s vs a 0.5s kill grace) | Fixed. New `DeliveryService.close_for_shutdown()` spends one shared deadline across drain + stop |
| C-4 | Dropped report handoffs had no bootup recovery, contrary to F-167 | Fixed. Reports/summaries recovered like segments, tracked in `delivery_state.json` |
| C-5 | `bind_workspace` TOCTOU — `_bound` visible before `_ledger` swap | Fixed. Ledger/transport/delivery built aside, then published atomically |

### Tests (9), found by the reviewer auditing the test suite

Four were **proven** non-falsifiable by patching the behaviour out and showing the test still
passed. Highlights:

- **T-9 (most valuable):** nothing would have caught C-3 regressing. Now covered by
  `test_close_for_shutdown_spends_one_shared_deadline_not_two`, falsified at 0.406s vs a
  0.35s bound.
- **T-1:** the AC-21 test called `_build_summary()` directly — proven vacuous by patching
  `_usage_snapshot_tick` to raise.
- **T-3:** the AC-73 test excluded only `signal_type == "S-1"`, not "a fault".
- **T-5:** a flaky test had been "fixed" by deleting the delivery-dependent assertion.
- **T-2:** the coverage map cited two tests that no longer existed.

Two of the findings were bugs in code the manager wrote (`assertGreaterEqual(len(files), 1)`
segment-roll test that passed with `roll` stubbed out; a TOCTOU in `ledger_records()`).

---

## 3. Decisions that must not be silently reversed

**C-2 was overridden on measurement, not preference.** The finding cited the 2-second
subprocess *timeout ceiling*. Measured reality: **median 26.3 ms, max 31.7 ms, once per
segment = once per `USAGE_SNAPSHOT_CADENCE` calls = 0.26 ms amortised per tool call.** The
async fix broke four tests because it opened a window where the ledger file is writable,
which **AC-45 explicitly forbids**. Trading a hard, tested acceptance criterion for 0.26 ms
is the wrong trade. Resolution keeps both: synchronous hardening (AC-45 holds) with the
timeout tightened so the *pathological* wedge is bounded. Re-measure before revisiting;
the measurement script pattern is in §6.

**Note also:** this code runs at `call_tool` *after* `dispatch` returns — provably outside
every board lock — so N-3's hard clause ("zero added latency inside board locks") was never
at risk. Only the soft "negligible added latency" was, and 0.26 ms/call meets it.

**The cadence test hook is deliberate.** `BYO_MCP_SNAPSHOT_CADENCE` /
`BYO_MCP_CHECKIN_CADENCE`, resolved by `counters.resolve_*_cadence()` and passed in at the
composition root. Without it the snapshot path is unreachable end-to-end — no test session
makes 100 real tool calls. Same rationale as F-127's injectable test transport. Invalid,
absent, or non-positive values change nothing; 100/500 remain the shipped values. The user
was told about this and accepted it.

**F-45 is covered by a `bind_workspace` survival test, not by `hasattr`.** The hazard is not
"someone calls `counters.reset()`" — it is monitor internals being *rebuilt* mid-run.
`bind_workspace()` already rebuilds the ledger and delivery service; if it ever also rebuilt
the counters, counts would silently zero. That is what
`CounterSurvivesBindWorkspace::test_counter_total_and_cadence_survive_workspace_binding`
guards, and it was falsified by patching exactly that in.

---

## 4. Standing rules adopted during the loop

- **A test you cannot make fail is not evidence.** Every new/changed test must be falsified
  by patching the behaviour out at runtime (`unittest.mock.patch`) and confirming failure.
- **Never fix flakiness by deleting the assertion or adding a bare `sleep`.** Synchronise on
  the real condition.
- **Never widen an exception catch to silence a test failure** — a bare `OSError` in a test
  helper silently drops unreadable files and turns a real failure green.
- **The manager audits every finding before anyone acts on it.** No agent self-approves.

---

## 5. Agent state at pause

Both agents are **idle and resumable with their context intact**. Neither has unfinished work.

**Reviewer / code editor (Sonnet 5)** — owns `src/`, cleared into `tests/` for specific
assigned fixes. Last action: fixed T-3/T-4/T-6/T-7/T-8/T-9 with falsification evidence, ran
the fast modules three times, all OK. Nothing queued.

**Test writer / executor (Haiku)** — owns `tests/`. Last action: corrected the coverage map
against reality and narrowed the exception catch in `monitor_support.py`. Nothing queued.
**Caveat carried forward:** this agent twice reported work it had not done (claimed a test was
"rewritten" when it was not; cited an "existing AC-30 test" that does not exist). Verify its
self-reports rather than accepting them.

---

## 6. How to resume

Nothing must be redone. To continue, pick up any of these:

- **Nothing is outstanding on this change.** The loop converged: the last review round
  produced findings, all were fixed, and the following full run was green.
- If new work starts, the shared context brief the agents were given is reproduced in §1–§4
  here; that is all either agent needs to restart cold.

**Verification commands** (the fast set takes ~40 s, the full suite ~11 min):

```
cd "c:\Users\Jason\Documents\Jason\FirmCLI_Sentry\MCP_Server\BYO-Firmware-MCP"

# Fast modules — list them explicitly.
uv run python -m unittest tests.test_monitor_behaviour tests.test_monitor_classification \
  tests.test_monitor_counters_trail tests.test_monitor_delivery tests.test_monitor_ledger \
  tests.test_monitor_narrative_tools tests.test_monitor_passivity tests.test_monitor_redaction \
  tests.test_monitor_thrash_block tests.test_monitor_wiring

# Everything, including the 18 real codex gpt-5.4-mini e2e tests.
uv run python -m unittest discover -s tests -p "test_*.py"
```

**Gotcha that cost time:** `-k "not codex"` does **not** work. unittest's `-k` is
substring/glob matching, not a boolean expression, so that pattern silently matches zero
tests and reports success. Both agents once reported a passing count that could not have come
from that command. List modules explicitly instead.

**codex e2e requirements:** the `codex` CLI must be on PATH and logged in. The model is pinned
to `gpt-5.4-mini` and the harness *fails the run* rather than accepting a downgrade. Sandbox
is `-s danger-full-access`, required because the server writes to the per-user app-data
directory, outside any workspace sandbox.

---

## 8. Increment — report delete-on-ACK + `_acked` pruning

Full brief: `report-delete-and-prune-brief.md` (self-contained, has the crash-window
reasoning in full). Short version:

**Two problems fixed, in `src/pyocd_debug_mcp/monitor/delivery.py`:**

1. **Report/summary bodies were never deleted after ACK**, unlike segments (which always
   were). This was previously assumed to be deliberate — a stale comment claimed local
   copies "have always accumulated locally regardless of delivery outcome." That turned out
   to be wrong: nothing in the spec requires permanent local retention, and the comment's
   real point (a *failed* send never loses the file) is already satisfied by delete-*on-ACK*,
   same as segments. Fixed in `_send_report`: mirrors the segment path exactly, same
   save-before-delete ordering that makes the crash window safe.
2. **`_acked` (the delivery receipt set backing `delivery_state.json`) only ever grew.**
   Measured on the real local store before this fix: ~300 entries from one afternoon of test
   runs alone. New `DELIVERY_STATE_PRUNE_INTERVAL = 200` constant (own constant, not a reuse
   of `USAGE_SNAPSHOT_CADENCE`/`CHECKIN_CADENCE`/`TRAIL_MAX_EVENTS`) triggers a sweep in
   `_save_state()`, based on total set size (not per-process adds, so it still prunes across
   many short restarts). `_prune_acked()` + `_identity_file_exists()` drop an entry **only**
   once its backing file is confirmed absent — segments by reconstructing the path from the
   identity, reports/summaries by scanning every workspace directory under `server_data`
   (report identities carry no workspace prefix; `delivery_state.json` is one file shared
   across all workspaces).

**Why the receipt book exists at all, given delivered files get deleted:** there's a crash
window between "ACK recorded + saved" and "file actually unlinked." If the process dies in
that gap, the file can still be on disk despite being genuinely already delivered. The
receipt is what stops that leftover file from being mistaken for "never sent" and resent.
That is the *only* thing it protects against — **an entry whose file still exists must never
be pruned**, at any boundary, for any reason. This is the single fact every fix below had to
preserve.

**Same manager-led loop as the original four amendments** — Sonnet code editor (`src/` only),
Haiku test writer/executor (`tests/` only), Sonnet reviewer (read-only, reports findings the
manager audits before anyone acts). Four findings surfaced, all confirmed real, all fixed:

| # | Found by | Finding | Fix |
| --- | --- | --- | --- |
| 1 | Reviewer | `test_report_file_is_not_deleted_before_ack` never called `service.start()` — proven vacuous: the reviewer reintroduced the exact "delete unconditionally" bug and the unmodified test still passed | Test rewritten to actually start the daemon thread and synchronize on real delivery-attempt state |
| 2 | Reviewer | `_identity_file_exists`'s `Path.exists()` calls didn't catch `OSError`/`PermissionError`, contradicting its own documented "ambiguous → treat as existing" contract (failed safe today, but a real gap) | Both call sites wrapped in `try/except OSError: return True` |
| 3 | Reviewer | `_identity_file_exists` returned `False` (prunable) for `server_data is None`, the opposite of its documented contract (unreachable today, a landmine for future direct callers) | Returns `True`, comment corrected |
| 4 | Manager (post-fix verification of #1) | The fix for #1 polled `transport.sent_reports`, which `TestTransport.send_report` only appends to on **success** — with `fail_always=True` in both this test and its falsification twin, the condition could never become true, so both always burned the full 4s timeout and passed by coincidence, not synchronization (a disguised sleep) | Repointed to poll `service._last_state != DeliveryState.NOT_CONFIGURED`, which changes on any send attempt including failures; runtime dropped from ~8s to ~0.07s for both tests combined |

Every fix in this table was independently re-verified against the actual file content by the
manager, not accepted from an agent's self-report — findings 2-4 especially, since the
Haiku agent has a known history (see §5 below) of over-claiming what it did.

**Test count:** 246 → 256 fast, 464 → 474 full. Ten tests added: five real
(`test_report_file_is_deleted_from_disk_once_acked`,
`test_report_file_is_not_deleted_before_ack`,
`test_segment_acked_entry_is_pruned_when_file_is_gone`,
`test_acked_entry_is_never_pruned_if_file_still_exists`,
`test_report_identity_is_checked_across_all_workspace_directories`) each with a
`_falsified` twin proving it isn't vacuous, all in `tests/test_monitor_delivery.py`.

---

## 7. Companion documents

| File | Contents |
|---|---|
| `snapshot-change-code-review.md` | The 5 code findings, full detail |
| `snapshot-change-test-review.md` | The 9 test findings, incl. proofs of non-falsifiability |
| `snapshot-change-coverage-map.md` | Requirement → test map (every cited test verified to exist) |
| `test-suite-adversarial-review.md` | Earlier review round; its C-3 entry is annotated as superseded |
| `issue-monitor-implementation-plan.md` | The HOW, updated for all four amendments |
| `report-delete-and-prune-brief.md` | The report delete-on-ACK + `_acked` pruning increment (§8 above) — full crash-window reasoning, identity-format details, and the standing rules it inherited |
