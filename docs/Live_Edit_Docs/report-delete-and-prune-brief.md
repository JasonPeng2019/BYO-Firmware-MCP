# Brief — report delete-on-ACK + delivery_state.json pruning

Self-contained. If you are a fresh subagent reading this cold, you do not need
any other document to do your assigned piece, though `snapshot-change-handoff.md`
in this same folder has the full history of the change this builds on.

## Context (short version)

`DeliveryService` in `src/pyocd_debug_mcp/monitor/delivery.py` keeps a
"receipt book" — `delivery_state.json`, a JSON file holding a set called
`_acked`. Every time a file is successfully delivered, its identity is added
to `_acked`. Purpose: on restart, don't resend something already sent.

**Why the receipt book exists at all**, given delivered files get deleted:
there's a crash window between "ACK recorded + saved" and "file actually
unlinked" (see `_perform`, the `files`/`bootup` job path: acked is updated
and *saved to disk* before the delete loop runs). If the process dies in
that gap, or the delete itself fails, the file can still be sitting on disk
even though it was genuinely already delivered. The receipt book is what
stops that leftover file from being mistaken for "never sent" and resent.
That is the *only* thing it protects against — once a file is confirmed
gone, its receipt has done its one job and provides no further value ever.

**Two problems found, both real, neither cosmetic:**

1. **Report/summary bodies are never deleted after ACK.** Segments
   (`hash:run_id:0001`-style identities) get `os.unlink`'d once acknowledged
   — see the end of `_perform`'s `files`/`bootup` branch. Reports and
   summaries (`rpt-*`/`sum-*` identities, written by `_write_artifact_file`
   in `monitor.py` to `server_data/<workspace>/reports/<identity>.json`)
   never do. `_send_report` in `delivery.py` records the ACK and saves state
   but never removes the file. **Decision (confirmed with the user): fix
   this. Make reports symmetric with segments — delete the local file once
   acknowledged.** This was previously assumed to be deliberate (a comment
   in `_prior_report_files` says local copies "have always accumulated
   locally regardless of delivery outcome") — that turned out to be wrong;
   nothing in the spec or plan requires permanent local retention of
   delivered reports, and the comment's actual point (files aren't lost on
   a *failed* send) is already satisfied by delete-*on-ACK*, no different
   from segments.

2. **`_acked` only ever grows.** `_save_state()` (delivery.py ~line 119)
   rewrites the whole sorted set on every save; nothing ever removes an
   entry. Measured on the real local store after one afternoon of test
   runs: ~300 entries already. Segments' entries become dead weight the
   moment their file is deleted (which, today, is immediately) — the
   receipt keeps existing for zero remaining reason. Once (1) above ships,
   report entries will do the same.

## What to build

### A. Report delete-on-ACK (`delivery.py`, `_send_report`)

Mirror the segment path. After the transport confirms ACK and the identity
is added to `_acked` + `_save_state()` is called (existing code, don't
change the order — save-before-delete is what makes the crash window safe),
delete the local report file:

```
server_data/<workspace>/reports/<identity>.json
```

`self._store.server_data` and `self._workspace` are already instance
attributes; the path is directly constructible, no need to thread a `Path`
through the job.

**Before wiring the delete**, grep the codebase for any other reader of
`reports/*.json` besides `_prior_report_files` (e.g. a tool that lists past
reports, a health-check count, anything in `server.py` or the `tools/`
package). Report *counts* should already come from `counters.py` /
ledger `report` records, not from counting files on disk — but verify
this rather than assume it, since deleting a file something else depends on
would be a real regression, not a cleanup.

Update the docstring on `_prior_report_files` (delivery.py ~257-268) — it
currently says the local copy is "never unlinked after ACK" and "nothing
here changes that." That sentence becomes false; fix it to describe the new
behavior and why (mirrors segments now, same crash-window justification).

Also fix the module docstring at the top of `delivery.py` if it makes the
same claim anywhere.

**There is very likely an existing test** in `tests/test_monitor_delivery.py`
that asserts the opposite of the new behavior (report file survives / is
never deleted after ACK). Find it and correct it — don't leave it red,
don't delete it silently. If you can't find one, say so explicitly rather
than assuming there wasn't one.

### B. Prune `_acked` (`delivery.py`)

New named constant, distinct from every other cadence constant in this
codebase (N-11 discipline — see the constants table in
`issue-monitor-implementation-plan.md` §"Constants" for the existing set;
this is a new one, not a reuse):

```python
DELIVERY_STATE_PRUNE_INTERVAL = 200
```

Trigger the sweep based on **total size of `_acked`**, not "adds this
process lifetime" — a short-lived server process might add far fewer than
200 entries in one run, and if the trigger were session-scoped the file
would never get pruned across many short restarts. `len(self._acked) %
DELIVERY_STATE_PRUNE_INTERVAL == 0` (checked wherever `_save_state()` is
called after an add) is the right shape.

**The prune check itself — an entry is safe to drop once its file is
confirmed absent:**

- **Segment identity** (`workspace:run_id:0001`, 3 colon-separated parts —
  see `file_identity()` in `ledger.py`): reconstruct the path as
  `server_data/<workspace>/<run_id>.<segment>.jsonl` (see `_prior_run_files`
  for the exact naming — `f"{run_id}.{segment:04d}.jsonl"`, `_SEGMENT_DIGITS
  = 4`). If that path doesn't exist, the entry is prunable.
- **Report/summary identity** (`rpt-*` / `sum-*`, no colon, no embedded
  workspace): `delivery_state.json` is a single file shared across *all*
  workspaces (`server_data/delivery_state.json`, no per-workspace
  subfolder — confirm this by reading `_state_path()`), but report bodies
  live per-workspace (`server_data/<workspace>/reports/<id>.json`). A
  report identity's workspace isn't recoverable from the identity string
  alone, so check **every** workspace directory under `server_data` for a
  matching `reports/<id>.json`. Only prune if it's absent everywhere.

Do not prune by age or by "keep last N" — only by confirmed absence. An
entry whose file still exists (the crash-window case this whole mechanism
exists for) must never be pruned; pruning it would defeat the one thing the
receipt book is for.

## Standing rules (carried over, still in force)

- **A test you cannot make fail is not evidence.** Every new/changed test
  falsified by patching the behavior out and confirming the test fails.
- **Never fix flakiness by deleting an assertion or adding a bare `sleep`.**
  Synchronize on the real condition.
- **Never widen an exception catch to silence a failure.**
- **The manager (main session) audits every reviewer finding before anyone
  acts on it.** No agent self-approves.
- `unittest`'s `-k` is substring/glob, not boolean — `-k "not codex"`
  silently matches zero tests. List modules explicitly.

## Suggested falsifiable test coverage

- Report file is deleted from disk once ACKed (falsify: patch the delete
  call out, confirm the test then fails).
- Report file is **not** deleted before ACK / on a failed send.
- A segment `_acked` entry whose file is already gone gets pruned at the
  next `DELIVERY_STATE_PRUNE_INTERVAL` boundary.
- A `_acked` entry whose file **still exists** is never pruned, at any
  boundary (this is the regression test for the crash-window property —
  falsify by temporarily making prune ignore existence and confirm it
  now incorrectly drops a live entry).
- A report identity is checked across multiple workspace directories, not
  just the current one (falsify by scoping the check to `self._workspace`
  only and confirming a foreign-workspace entry then wrongly survives... or
  wrongly gets pruned, whichever direction the bug would go — check both).

## File ownership (avoid merge conflicts)

- Code editor: `src/pyocd_debug_mcp/monitor/delivery.py` (and any other
  `src/` file only if strictly required, called out explicitly if so).
- Test writer/executor: `tests/` only.
- Reviewer: read-only across both; reports findings back to the manager,
  does not edit directly.
