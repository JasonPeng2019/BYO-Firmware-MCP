# Iteration 4 — Code Adversary

Scope: the code as it now stands at `ced6231`, attacked fresh, ignoring what changed to
get here. `reviews/ledger.md` (27 adjudicated rows, C1-C14/D1-D13) read first; none
re-raised. Hardest scrutiny on the four directed targets, then an independent sweep for
anything new, per instruction not to manufacture findings where none exist.

## Summary

| Severity | Count |
| --- | --- |
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 1 (C15) |
| LOW | 0 |

The four directed targets (prefix-length/lexical-sort correctness, legacy-token
reachability, C13 atomicity, skip/test-integrity audit) are covered in
`reviews/iter-4-diff.md` rather than duplicated here, since all four are equally
"current code as it stands" questions and none turned up a defect distinct from what's
already written up there — repeating the same negative results under a second heading
would pad this file without adding information. This file's one new finding (C15)
came out of the target-4 "holistic suite integrity" sweep but is a pure code-as-it-
stands defect (a hardcoded, load-sensitive timeout in a test file), independent of
anything in the reviewed diff, so it belongs here rather than in the diff file.

---

## C15 — MEDIUM (test infrastructure, not the discovery-hook feature) — `test_swd_process_isolation.py` has hardcoded subprocess/worker-startup deadlines too tight to survive real machine load, causing reproducible, non-deterministic suite failures unrelated to any code under test

**File:** `tests/test_swd_process_isolation.py:71,110,169` (three `subprocess.run(..., timeout=10)` calls); `src/pyocd_debug_mcp/adapters/swd_process.py:241,310-312` (worker startup deadline / `_read`'s `Empty` -> `TimeoutError` path)

Directed target 4 asked for a holistic sweep of test-suite integrity, including tests
that could produce false signal. Running the full suite repeatedly (needed anyway to
confirm the C14/D13 concurrency fix holds under stress) surfaced a second, independent,
reproducible flake — in a file this feature never touches:

```
git log --oneline -- tests/test_swd_process_isolation.py
860e4d1 complete trusted-input and multi-board server remediation   # single commit,
                                                                      # predates this
                                                                      # whole feature
```

**Reproduction:** ran `python -m unittest discover -s tests` five times back to back on
this machine. Two of five runs were clean (617 passed, 7 skipped); one run failed with
two errors, both in `test_swd_process_isolation.ProcessIsolationContractTests`:

1. `test_selection_and_discovery_chatter_cannot_corrupt_protocol_result` — spawns a real
   `python.exe -c "..."` child (importing `pyocd_debug_mcp.adapters.swd_pyocd`, patching
   two classmethods, running a scenario) and calls `subprocess.run(..., timeout=10)`.
   Under load, the interpreter-startup-plus-import cost alone exceeded 10 seconds:
   `subprocess.TimeoutExpired: ... timed out after 10 seconds`.
2. `test_startup_and_first_call_share_one_absolute_deadline` — constructs a
   `_WorkerClient` against a fixture worker script; the worker's reply raced the
   client's own `startup_deadline` in `swd_process.py:241`
   (`ready = self._read(startup_deadline)`), which raised `TimeoutError` internally at
   `swd_process.py:312` and surfaced as `TargetConnectionError: Worker startup failed:
   TimeoutError... Worker was terminated.` This one is more concerning than #1 because
   the timeout being raced is *production* code (`swd_process.py`), not just the test's
   own subprocess call — the test is asserting a real timing contract the shipped
   worker-startup path actually has to meet, and the assertion is written tightly
   enough that ordinary system load defeats it.

Re-ran `test_inventory_snapshot_concurrency.py` (the C14/D13 file) in isolation 8 times
in a tight loop specifically to check whether that fix's flakiness had merely moved
elsewhere — it stayed clean all 8 times, so this is a genuinely separate, second source
of flakiness, not a recurrence of C14/D13 under a new name.

**Why this matters for the termination-gate question:** it doesn't — this file and
`swd_process.py`'s startup-deadline plumbing are untouched by any commit in
`0fff3f1..HEAD` (or by any of the three iterations' fixes; `git log` shows one commit
predating the whole feature), so it is not a regression introduced by this work and
does not block Phase 1. It is reported because (a) it is a real, reproducible defect in
code that ships in this repository, surfaced by the same repeated-run methodology this
review used to re-verify C14/D13, and (b) it means a bare "the suite is green" claim
from a single run — including the ones in this review's own iterations 1-3 and in the
task brief's CURRENT STATE line — is not a reliable signal on this machine; roughly a
1-in-5 chance of an unrelated false failure was observed empirically in this session.
Anyone re-running the suite once to confirm a fix and seeing it fail should check
whether the failure is in `test_swd_process_isolation.py` before concluding the fix is
wrong.

**Not fixed, per instruction to review only.** Minimal fix direction for the record:
widen the three `timeout=10` call sites (and whatever governs `startup_deadline` in the
test's `_client(...)` helper) to a machine-load-tolerant value, or make them
configurable via an env var the way other timeout-sensitive tests in this suite already
do, rather than a bare literal.

---

## Independent sweep beyond the directed targets

Spent the remaining review budget re-reading the token-comparison surface
(`_same_setup_connection`, `_setup_connection_key`, `_connection_matches_probe`,
`derive_selection_from_token`) and the `RunAssignmentStore`/`ProbeSelectionStore`
locking discipline end to end, specifically hunting for anything a fresh read would
catch that three prior iterations of the same code missed. Nothing new turned up:

- Confirmed `probe.probe_family` is typed `str` (never `Optional`) on both
  `ProbeCandidate` (`setup_flow/preflight.py:74`) and `ValidationProbe`
  (`setup_flow/validate.py:57-60`), so `_connection_matches_probe`'s
  `probe.probe_family.strip().casefold()` (`server.py:3686`) cannot raise
  `AttributeError` on `None` — this is a pre-existing type contract, not something
  iteration 3 introduced, and every construction site already supplies a string.
- Confirmed no documentation (`docs/*.md`) describes the `connection_id` token format
  at all (grepped for `probe:`/`probeid:` across `docs/`), so there is no doc-vs-code
  drift for C12/D11's prefix change to have created.
- Re-verified `MAX_PROBE_SELECTIONS` eviction (`hardware_inventory.py:590,627-628`) and
  `find_selected_row`'s provider-scoped matching (`:826-838`) are unchanged since
  iteration 2's FIX 9 and still correct on a fresh read — no new gap.

No new finding beyond C15.
