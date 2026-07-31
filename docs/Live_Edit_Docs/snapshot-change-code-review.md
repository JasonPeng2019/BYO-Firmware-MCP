# Round 1 — adversarial code review (snapshot-cadence rework)

Scope: source only (`counters.py`, `ledger.py`, `monitor.py`, `reports.py`, `delivery.py`,
`trail.py`, `server.py`), against F-38/F-42/F-43/F-44/F-45/F-75/F-77/F-129/F-154..F-160/
F-165/F-166/F-167/N-8/N-11 and the §5.1 report contract. The two bugs the brief lists as
already fixed (stale-advertised-set in the usage-snapshot tick; AC-105 stuck-sender) are not
re-reported.

---

### C-1. `submit_checkin` computes coverage from a stale advertised-tool set — the exact bug class already fixed elsewhere, missed here — HIGH
**Where:** `src/pyocd_debug_mcp/monitor/monitor.py`, `IssueMonitor.submit_checkin` (lines 841-861), vs. `_build_summary` (lines 709-720)

**Requirement:** F-42 (a usage record must correctly answer "which advertised tools were never exercised"), F-130 (server-attached coverage/ledger state on a checkin), N/A-parallel to the already-fixed regression covered by `test_coverage_is_computed_against_the_live_advertised_set`.

**Failure scenario:** `submit_checkin` does:
```python
snapshot = self._counters.snapshot()                 # captured BEFORE refresh
summary = self._build_summary(trigger="agent-invoked", narrative=validated, snapshot=snapshot)
self._append("checkin", detail={"summary_id": summary["summary_id"], **self._usage(snapshot)})
```
`_build_summary` calls `self._refresh_advertised()` first, but since `snapshot` is already
built and passed in, `_build_summary`'s internal `if snapshot is None: snapshot = ...` never
re-reads counters — the refresh has no effect on this call. Every other snapshot-producing
path (`_usage_snapshot_tick`, `_file_report`, `health`) calls `_refresh_advertised()` **before**
taking the snapshot; `submit_checkin` is the one path that gets the order backwards. Concretely:
a plan is accepted mid-run, newly exposing tool X; the agent immediately calls
`submit_routine_checkin` in the same turn. `_counters.snapshot()` runs against the
still-stale `_advertised` tuple (captured at boot or the last refresh), so `never_exercised`
in both the checkin's ledger record and the delivered summary can wrongly include a tool that
is no longer hidden, or omit one that just became visible — the same wrong-answer failure mode
the brief says was already fixed for the usage-snapshot tick, just left standing in this one
call site.

**Why it matters:** Coverage is one of F-42's two required answers ("which advertised tools
were never exercised"), and this is now durably wrong in the ledger's `checkin` record, not
just transiently wrong in a response.

---

### C-2. Segment-open hardening runs a blocking subprocess (up to 2s) synchronously on the tool-dispatch path, inside the ledger lock — HIGH
**Where:** `src/pyocd_debug_mcp/monitor/ledger.py`, `_harden()` (lines 119-157, `_HARDENING_TIMEOUT_SECONDS = 2.0`), called from `_write_locked()` (lines 259-276) which is called from `append()` (lines 278-308) while holding `self._guard`.

**Requirement:** F-27 ("Recording a report must not block or measurably delay tool execution"), F-46 (summary production must be bounded and must not block or delay tool execution), F-167 (a stuck/slow sender — and by extension the local path that gates it — "must be invisible").

**Failure scenario:** The change ties the segment roll to the on-request-path 100-call tick
(`monitor.py` `_usage_snapshot_tick` → `self._ledger.roll()`). The first *write* to each freshly
rolled segment triggers `Hardening.NOT_ATTEMPTED` → `_harden(path)`, which on Windows runs
`subprocess.run(["icacls", ...], timeout=2.0, ...)` synchronously. This call happens:
- inline inside `IssueMonitor._observe()`, which is awaited (not backgrounded) by
  `kernel/registry.py`'s `call_tool()` before the tool result is returned to the client
  (`observation.completed(result)` at registry.py:354, before `_maybe_append_checkin_prompt`
  returns) — so a slow/hung `icacls` (antivirus interception, a loaded/contended disk, Group
  Policy processing) stalls the actual tool response by up to 2 seconds, four times the
  measured Claude-Code kill grace (~500 ms, per `CLIENT_KILL_GRACE_SECONDS`);
- while holding `SegmentLedger._guard`, so any concurrent board's call trying to append to the
  same ledger (report, checkin, next snapshot) blocks behind it too.

This is deterministic, not rare: it fires once per segment roll, i.e., once every
`USAGE_SNAPSHOT_CADENCE` calls, on whichever call happens to be the first append after a roll.

**Why it matters:** This is precisely the "blocking call sneaked back onto the request path"
the amendment was supposed to eliminate — the per-call ledger write was removed for this
reason, but the segment-roll hardening reintroduces an unbounded, synchronous, lock-held
subprocess call on the same path, on a fixed, predictable cadence.

---

### C-3. Closeout spends up to ~0.8-0.9s across two sequential budgets, blowing past `CLIENT_KILL_GRACE_SECONDS` — HIGH
**Where:** `src/pyocd_debug_mcp/monitor/monitor.py`, `IssueMonitor.closeout()` (lines 308-332, specifically 329-330); `src/pyocd_debug_mcp/monitor/delivery.py`, `DeliveryService.stop()` (lines 137-149) and `drain_for_closeout()` (lines 289-322).

**Requirement:** N-11 ("`delivery.CLOSEOUT_BUDGET_SECONDS = 0.4`, fit inside
`CLIENT_KILL_GRACE_SECONDS = 0.5`"), F-113 ("the closeout budget must fit inside the tightest
client grace the deployment targets (~500 ms today)").

**Failure scenario:** `closeout()` calls, back to back, with no explicit budgets:
```python
self._delivery.drain_for_closeout(segments)   # worker.join(timeout=CLOSEOUT_BUDGET_SECONDS) = 0.4s
self._delivery.stop()                          # thread.join(timeout=CLOSEOUT_BUDGET_SECONDS) = 0.4s, then transport.close()
```
Both `drain_for_closeout`'s `budget` parameter and `stop`'s `timeout` parameter default to the
*same* module constant `CLOSEOUT_BUDGET_SECONDS = 0.4`, and neither caller overrides it or
accounts for time already spent. Worst case (transport stalled on both the closeout drain
job and whatever the daemon thread is mid-`_perform` on), the sequence blocks for up to
0.4 + 0.4 (+ ~0.1 for `SimulatedRemoteTransport.close()`) ≈ 0.9s — nearly double the
documented, and spec-mandated, 0.5s Claude Code kill grace. Under Claude Code's observed
SIGINT→SIGTERM(+100ms)→SIGKILL(+400ms) timeline, this means the process is a realistic
candidate for being SIGKILLed *during its own shutdown drain* rather than exiting cleanly —
harmless for durability (append already happened) but a direct violation of the stated
"budget fits inside the grace" invariant, and it defeats the purpose of having the constant
be a single, reasoned number in the first place.

**Why it matters:** N-11 explicitly calls out `CLOSEOUT_BUDGET_SECONDS` as "fit inside"
`CLIENT_KILL_GRACE_SECONDS`; the composed closeout path silently reuses the same budget twice
instead of splitting or discounting it, so the actual runtime guarantee is roughly double what
the constant advertises.

---

### C-4. Dropped "report" delivery jobs have no bootup-recovery backstop — the durability guarantee F-167 states only actually covers segment files — MEDIUM
**Where:** `src/pyocd_debug_mcp/monitor/delivery.py`, `enqueue_report()` (lines 187-188) vs. `_prior_run_files()` (lines 197-222, globs only `*.jsonl`) and `enqueue_bootup()`/`_perform()` (job kind `"bootup"`, lines 190-193, 224-232).

**Requirement:** F-167 ("If the queue fills, the server drops the *handoff*, not the record:
the file stays on disk and the next boot's recovery ships it"); F-42/F-2 (a report's narrative
and trail are meant to reach the sink for problem-watching).

**Failure scenario:** `_file_report()` writes the full report JSON to
`server_data/<workspace>/reports/<report_id>.json` (durable, fine) and then calls
`self._delivery.enqueue_report(report)`. If the bounded queue (`QUEUE_MAX = 256`) is full at
that moment — e.g. a burst of segment/report jobs queued while the transport is slow —
`_enqueue()` silently increments `_dropped` and the job is gone. Unlike segments, there is no
second path that ever resends it: `_prior_run_files()`, the only function bootup recovery
calls, globs `*.jsonl` ledger files exclusively and never looks at `reports/*.json`. The
ledger *does* retain the report's existence (a `report` occasion record with `report_id`,
`signal_type`, `grouping_key`, and the usage counts, via `_file_report`'s `self._append("report",
...)`), and that record is recoverable through the normal segment path — but the report's
`title`, `description`, `trail`, and `narrative` are not, and never will be, delivered
off-box, with no observable signal distinguishing "the report's ledger anchor made it" from
"the report body made it."

**Why it matters:** F-167's stated guarantee ("the record" survives a dropped handoff) reads
as covering delivery generally, but as implemented it only actually holds for segment files;
a dropped report handoff is a permanent, silent loss of the report's substantive content, not
a delayed delivery.

---

### C-5. `bind_workspace` flips `_bound` under the lock but reassigns `_ledger`/`_delivery` after releasing it — a narrow window where a concurrent append targets the wrong ledger — MEDIUM
**Where:** `src/pyocd_debug_mcp/monitor/monitor.py`, `IssueMonitor.bind_workspace()` (lines 276-306) vs. `_append()`/`_write_record()` (lines 388-410).

**Requirement:** F-154 ("a run writes its own file and never rewrites it"), F-86 (chain scoped
per run, not mixed).

**Failure scenario:** `bind_workspace()` takes `self._guard`, sets `self._bound = True` and
swaps `self._workspace`, then **releases the guard** before doing
`self._ledger = SegmentLedger(...)` / `self._delivery = DeliveryService(...)`. If another
thread's `Observation.completed()` calls `self._monitor._append(...)` in that window, `_append`
sees `self._bound is True` (already flipped), skips buffering, and calls
`self._write_record(...)` → `self._ledger.append(...)` — but `self._ledger` at that instant may
still be the pre-bind, unbound-workspace `SegmentLedger` instance, because the reassignment on
the other thread hasn't executed yet. The record lands in the unbound workspace's segment file
instead of being buffered for replay into the real workspace's chain (which is what every other
pre-bind record for this run gets), silently splitting one run's occasions across two
unrelated ledgers.

**Why it matters:** Low probability (the window is a handful of Python bytecodes) but a real
TOCTOU: multiple boards operating concurrently is an explicitly supported scenario elsewhere in
this same module (trail.py's whole rationale for board-scoping), so concurrent dispatch during
the handshake window is not a hypothetical.

---

## Severity summary

- **HIGH:** 3 (C-1 stale-coverage checkin; C-2 blocking icacls subprocess on dispatch path;
  C-3 closeout budget doubled)
- **MEDIUM:** 2 (C-4 report delivery has no recovery backstop; C-5 bind_workspace TOCTOU on
  ledger reassignment)
- **LOW:** none found worth listing — everything else checked (cadence wiring in `server.py`,
  constant separateness in `counters.py`/`trail.py`/`block.py`/`reports.py`, ledger record
  shape/kinds, dedupe/thrash ordering relative to counting, `never_exercised` refresh ordering
  in the tick/report/health paths, the two previously-fixed bugs) held up against the named
  spec sections.
