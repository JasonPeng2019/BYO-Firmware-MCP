# Autonomous Issue Monitor — Implementation Plan (HOW)

Companion to `mcp-issue-monitor-spec-new-f.md` (the WHAT). That spec is the ground
truth; this document only says *how* to satisfy it in
`src/pyocd_debug_mcp/`. Requirement IDs (`F-n`, `W-n`, `A-n`, `AC-n`) refer to it.
Where this plan and the spec disagree, the spec wins — raise the conflict rather
than implementing around it.

**Four decisions are settled and are assumed throughout (§4 records them):**
1. There is **no cloud sink yet** — "Sentry" today *is* the local
   `simulated_remote/` folder. The `sentry-sdk` still ships now, driven by a
   custom local transport, so report payloads are built against the real Sentry
   data model from day one and cutover is configuration only.
2. `narrative_logging` is a **plain module constant** edited by hand when a build
   is cut. No env var, no build hook. It becomes immutable when the code is
   compiled to a binary.
3. Delivery and local cleanup are **fully automatic and invisible to the user**.
   A firmware engineer never pushes, clears, or manages a log. Push happens on
   its own, through a decoupled background sender that never blocks a request
   (F-167); ACKed records delete themselves. `simulated_remote/` is a
   test-only placeholder and **is never deployed** — the build ships when the
   real remote exists.
4. **The ledger records occasions, not calls.** There is no per-call record.
   The only entries are boot, a usage snapshot every 100 calls (cumulative
   counts, never a since-last-tick delta), a check-in every 500 calls in a
   personal build, a filed report, and close (F-38, F-165).

---

## 0. Grounding — what the existing code already gives us

Four facts about this codebase decide the whole design.

**0.1 There is exactly one dispatch funnel, and it is the right one.**
`RegistryFastMCP.call_tool` ([registry.py:294-389](../../src/pyocd_debug_mcp/kernel/registry.py#L294-L389))
sees, for every inbound call: tool name, raw arguments, resolved `board_id`, the
locked-handler refusal from `require_unlocked`, the guard refusal raised by
`before_execution`, the *original* exception before Layer-2 `ToolError` wrapping,
and the returned string. `action_batch` children re-enter it recursively
(`build_batch_handlers(mcp.call_tool, ...)`, [server.py:5592](../../src/pyocd_debug_mcp/server.py#L5592)).
This is the F-6 "single managed dispatch funnel". Nothing goes in
`kernel/operations.py::dispatch` — that would put instrumentation inside the
board worker thread and inside the board lock, violating W-3 and N-3.

**0.2 Recording at `call_tool` is provably outside every lock.**
In the sync path, `dispatch` acquires the board lock inside the worker thread
(`run_synchronous`, [operations.py:704-752](../../src/pyocd_debug_mcp/kernel/operations.py#L704-L752)),
runs the handler, runs cleanup, releases, and only then does `dispatch` return to
`call_tool`. So a post-`dispatch` hook is structurally incapable of executing
inside a board lock, a flash transaction, or managed cleanup. **This is what makes
AC-11 provable rather than measured.**

**0.3 Refusals arrive in three distinct shapes** — this is the W-1 classification
problem stated in code terms:
- as raised `PolicyRefusal`/`PlanRefusal` ([session_runtime.py:61](../../src/pyocd_debug_mcp/services/session_runtime.py#L61), [plan_engine.py:33](../../src/pyocd_debug_mcp/guardrails/plan_engine.py#L33)),
- as a raised `ToolError` from `ToolRegistry.require_unlocked` ([registry.py:182-190](../../src/pyocd_debug_mcp/kernel/registry.py#L182-L190)),
- as a **successful return value**: `"Refused [code]: msg session_id=..."` from
  `_format_refusal` ([server.py:784](../../src/pyocd_debug_mcp/server.py#L784)), or JSON with
  `"status": "..._refused"` from e.g. `collect_build_artifacts` ([artifacts.py:56](../../src/pyocd_debug_mcp/tools/artifacts.py#L56)).

**0.4 There is already an event record, and it is a leak hazard, not a base.**
`ToolEvent` ([session_runtime.py:71-100](../../src/pyocd_debug_mcp/services/session_runtime.py#L71-L100))
carries `probe_uid` and raw `normalized_args` and writes under `.firm/runs/`.
W-19 names this exactly. The monitor **does not reuse or extend `ToolEvent`** and
**does not write under `.firm/`** (F-31, §4.8). It defines its own record type
with fingerprints only.

---

## 1. Module map

New package `src/pyocd_debug_mcp/monitor/`. **It imports nothing from
`server.py`.** `server.py` is the composition root and injects a
`MonitorContext` of read-only callables — the same `*ToolServices` dataclass
pattern already used by `tools/misc.py`, `tools/memory.py`, etc.

| Module | Owns | Key requirements |
|---|---|---|
| `build_profile.py` | Generated constant `NARRATIVE_LOGGING: bool` | F-140, F-143, F-147 |
| `paths.py` | Store root resolution + workspace identity (two separate answers) | F-131, F-132, F-37, F-161…F-164 |
| `redaction.py` | Salted fingerprints, mechanical-layer content bar, narrative-layer payload bar | F-3, F-149, F-150, F-153 |
| `counters.py` | Live per-run counter state (cumulative, never per-window) | F-64…F-69, F-138, F-165 |
| `trail.py` | Board-scoped ring buffers (~100) | F-1, F-2, F-5, W-10 |
| `ledger.py` | Per-run append-only chained JSONL of boot/snapshot/check-in/report/close records — never one record per call | F-38…F-45, F-75…F-80, F-86, F-91…F-97, F-154…F-160, F-165, F-166 |
| `classify.py` | Exception/result → outcome class, signal type, triage class | F-7, F-22, F-23, W-1, W-4 |
| `thrash.py` | Deterministic repetition detector + legitimate-pattern exclusions | F-9, F-10 |
| `reports.py` | Report contract, grouping key, dedup/rate limit, collapse | F-24…F-26, F-165, §5 |
| `narrative.py` | Pydantic models for the two model-authored forms | §5.2, §5.3, F-19 |
| `transport.py` | Delivery seam; report (Sentry envelope) + spool (ledger/summary) destinations | F-58…F-63, F-133 |
| `delivery.py` | Decoupled background sender, three occasions, ACK drain, anchor | F-52…F-57, F-111…F-118, F-134…F-136, F-167 |
| `block.py` | Staleness backstop | F-119…F-127 |
| `monitor.py` | `IssueMonitor` facade + `MonitorContext` injection dataclass | wiring |
| `tools.py` | The three agent-facing MCP tools | F-16…F-21, F-48…F-51, F-70…F-74 |

Existing files touched — **six, all small**:
`kernel/registry.py`, `kernel/operations.py` (no change if 0.2 holds; see §3.2),
`tools/handshake.py`, `tools/batch.py`, `server.py`, `pyproject.toml`, `.gitignore`.

New dependencies: `platformdirs>=4` (F-131 mandates a standard app-dirs
mechanism, not hardcoded paths — also charter §3) and `sentry-sdk>=2`.

**Sentry ships now, pointed at local storage.** The SDK does not require a live
remote: `sentry_sdk.init(transport=...)` accepts a custom transport that receives
the serialized envelope, so envelopes are written into `simulated_remote/` today
and the cloud cutover is *removing* the custom transport and supplying a real DSN.
This is what makes F-62/AC-49 ("no change beyond configuration") literally true —
building a bespoke report format now would force a translation layer into Sentry's
data model at cutover, which is exactly the change that requirement forbids.

The SDK's logging integrations and background worker thread are the W-2/N-1
hazard the spec names by name. They are neutralized structurally by the Phase 0
fd-dup guard, not by configuration discipline — which is why Phase 0 comes first.

---

## 2. Phased build order

Each phase is independently shippable and independently testable. Phase 0 and
Phase 3's classifier gate everything else — W-1 says get classification right
before anything else, and W-2 says a stdout mistake breaks the server
intermittently and looks like a different bug.

### Phase 0 — stdout containment (N-1, W-2, A-1, AC-10)

The strongest available fix, and it covers child processes too.

In `RegistryFastMCP.run_stdio_async` ([registry.py:406-417](../../src/pyocd_debug_mcp/kernel/registry.py#L406-L417)),
before entering `stdio_server()`:

```python
protocol_fd = os.dup(sys.stdout.fileno())          # private copy of the wire
os.dup2(sys.stderr.fileno(), sys.stdout.fileno())  # fd 1 now points at stderr
protocol_stream = anyio.wrap_file(
    io.TextIOWrapper(io.FileIO(protocol_fd, "wb", closefd=True), encoding="utf-8")
)
async with stdio_server(stdout=protocol_stream) as (read_stream, write_stream):
    ...
```

`stdio_server` already accepts an injected `stdout` and only falls back to
`sys.stdout.buffer` when none is given (verified in the pinned `mcp` package).
After the `dup2`, *nothing* that writes to fd 1 can reach the protocol pipe —
not a stray `print`, not a logging handler, not the Sentry SDK's worker thread,
and **not an owned child process that inherits fd 1** (`kernel/processes.py:541`,
`adapters/swd_process.py:235`). That last clause is A-1, and no
handler-configuration discipline can achieve it.

Also: `sentry_sdk.init(..., debug=False)` and an explicit
`logging.getLogger("pyocd_debug_mcp.monitor").propagate = False` with a
stderr-only handler. Belt and braces — the `dup2` is the actual guarantee.

**Test (AC-10):** run the server under a harness that asserts every byte on fd 1
parses as JSON-RPC framing, while a test tool deliberately `print()`s, logs, and
spawns a child that writes to stdout.

### Phase 1 — storage, redaction, ledger

**`paths.py`** (F-131, F-132, F-37, F-161…F-164). Two separate questions that must
not be merged — this is the single most likely place to get the design wrong.

***Where* to write** (the store root), resolved once and cached:

1. `platformdirs.user_data_dir("BYO", appauthor=False, roaming=False)` →
   `%LOCALAPPDATA%\BYO`, `~/Library/Application Support/BYO`, `$XDG_DATA_HOME/BYO`.
2. `BYO_MCP_ARTIFACT_ROOT` → `<root>/.byo-monitor/` — last resort only, for
   locked-down or sandboxed machines (F-132).
3. Neither available → `LogRootState.BUFFERING`: records queue in a bounded
   in-memory deque and the state is reported by the health check. **Never a silent
   no-op** (F-37).

***Which* workspace** (the subdirectory under `server_data/`): the handshake path
(F-34…F-36), reduced to a salted digest (F-162), or `unbound/` if none was ever
supplied (F-164).

**The handshake path is an identity input, never a write target.** F-30 and AC-13
forbid writing anything inside the workspace project directory, so there is no
`<workspace>/.byo-monitor/` fallback and the handshake path must never enter the
store-root chain above. It is easy to slip here — the handshake hands the code a
perfectly good writable directory — which is why the two questions are answered by
two separate functions that do not share a return type.

Subdirectories `server_data/` and `simulated_remote/`. Only root (2) can land
inside a repo → `.gitignore` gains `.byo-monitor/` (the redirected N-5 action).

**`redaction.py`** (F-3, F-149, F-153). Two bars, because F-153 splits by layer:

- *Mechanical bar* (trail, ledger, fingerprints, all server-detected reports —
  and the **only** thing a professional build emits): no payloads, no full host
  paths, no raw IDs. Paths → `basename + salted digest`. Probe serials / device
  UIDs / MCU part numbers → truncated salted digest.
- *Narrative bar* (personal builds only, F-153): the narrative **may** name real
  symbols, files, and describe the codebase — that is its purpose. It may **not**
  embed verbatim payloads. Regex rejection of: hex runs ≥32 bytes, base64 blobs
  ≥256 chars, ANSI/UART control-byte runs, and full argv command lines.

Salt (F-149): 32 random bytes generated once into `<app-data>/BYO/fingerprint.salt`,
mode `0600`, never logged, never delivered.
`fingerprint(args) = blake2b(salt || canonical_json(args), digest_size=16).hex()`.
`canonical_json` already exists in `guardrails/plan_engine.py:358` — reuse it.

**`ledger.py`** (F-38…F-45, F-75…F-80, F-86, F-91…F-97, F-154…F-160, F-165, F-166).
One sealed file per run segment:
`server_data/<workspace_id>/<run_id>.<segment>.jsonl`. **Append and unlink are
the only operations that ever touch it** — never rewrite (F-154). This is F-86's
per-run chain scoping made literal, it stops concurrent instances sharing a root
from breaking each other's chains (AC-39), and it is what lets the append-only
ACL hold for the file's entire life (F-157).

**There is no per-call record (F-38).** The only five record `kind`s are `boot`,
`usage_snapshot` (every `USAGE_SNAPSHOT_CADENCE` calls, all builds, F-129/F-43),
`checkin` (every `CHECKIN_CADENCE` calls, personal builds only, F-128/F-129),
`report` (whenever one is filed, F-16), and `close`. Per-call sequence detail
lives only in the bounded in-memory trail (F-1) and is attached to `report`
records (F-2) — it is never durably logged on its own (AC-18). Record shape,
a generic envelope plus a kind-specific `detail`:

```json
{"seq": 2, "run_id": "...", "ts": "...", "kind": "usage_snapshot",
 "detail": {"total_calls": 100, "per_tool": {...}, "per_outcome": {...},
            "per_error_class": {...}, "never_exercised": [...]},
 "prev": "<hash of seq-1 or genesis>", "hash": "<blake2b of this record>"}
```

`detail` for a `report` record additionally carries the run's cumulative counts
at filing time (F-165, §5.1) — a plain `counters.snapshot()` call, the same one
the periodic tick uses, not a second accumulator. `usage_snapshot.detail` always
holds the **running total since run start**, never a since-last-tick delta
(F-165, AC-103). This is what makes the anti-under-report property of F-166
hold: a dropped, delayed, or withheld snapshot cannot lower the total the
*next* delivered one carries, and the per-run chain (F-86, F-39) turns a
decreasing count or a gap in the snapshot sequence into a detectable finding
instead of a silent shortfall. State the three honesty tiers of F-166 in this
module's docstring, verbatim, right beside the existing F-87/F-88/W-17 bound
(AC-104) — both are the same kind of statement: a precise claim about what the
guarantee is **not**, so nobody later reads "detectable" as "prevented," or
"cumulative" as "unforgeable at the source."

Durability from the append (F-40, AC-20): `open(..., "a")` → `write` → `flush` →
`os.fsync`. No shutdown path is load-bearing.

Hardening (F-94…F-97): on creation, Windows → `icacls <file> /deny "<user>:(WD)" /grant "<user>:(AD)"`
(bounded subprocess, ≤2 s, fd 1 already safe from Phase 0). POSIX → `chmod 0600`
and report hardening as `not_supported` (append-only there needs `chattr +a`,
i.e. root — do not pretend otherwise). Any failure → log and continue with an
ordinary file (F-96), state surfaced in the health check and every summary (F-97).

Startup verification (F-91, F-92, AC-40…AC-44) returns one of:
`verified` / `chain_invalid` / `truncated_vs_checkpoint` / `run_absent` /
`verification_impossible`. Default today is **`verification_impossible`** — there
is no checkpoint pipeline (A-11). Wholesale absence of a run's file is routine
(F-93, W-23, AC-43); only partial inconsistency *within a present file* is a
finding. ACK-deleted files are sanctioned deletions, not gaps (F-93, F-156).

Docstrings on this module state the F-87/F-88/W-17 bound verbatim: the chain is
an accident-and-corruption detector; it is defeated by stopping the server first;
a local key would not fix it. **The word "untamperable" must not appear** (W-17).

**Workspace scoping** (F-161…F-164). Two distinct identifiers, and conflating
them is the whole trap:

- the **local directory name** is a salted digest of the workspace path (F-149).
  Its purpose is **auditable anonymization, not secrecy from the owner**: no
  plaintext project path is written to disk or carried in anything that could be
  listed, shipped, or screen-shared, so the owner can open the store and confirm at
  a glance that every workspace is represented by an anonymized name. It hides
  nothing from the owner, who can map their own digests back (F-150) — the
  guarantee is that the plaintext path is *nowhere in the artifact*. Implication
  for the code: never write a `workspace_path` field, a `README`, or a
  path-bearing manifest inside the workspace folder as a convenience for humans;
  that would defeat the entire property.
- the **pushed identifier** is an opaque random token, generated once per
  workspace and persisted inside that workspace's folder. It carries zero path
  information, so the owner can see exactly what will represent the repo off-box
  and confirm it is fully anonymized. **Never derive it from the path, even
  hashed** — a workspace path is a small, guessable input, which is exactly the
  F-149 reconstruction channel.

Consequence (F-163): the same project on two machines yields two unrelated
tokens, and nothing local may try to reconcile them. Grouping them into one
project is the receiver's job, by operator labelling or explicit registration at
the remote.

Binding is late (F-164, W-13): the workspace path arrives on
`initialization_handshake`, but the boot record (F-76) is produced before it.
Records written pre-binding buffer in memory and flush into the workspace file
once the path is known; if no workspace is ever bound they are written under a
literal `unbound/` workspace and delivered as such. Never discarded (F-37).

### Phase 2 — counters, trail, funnel instrumentation

**`counters.py`** (F-64…F-69). A single lock-guarded object holding: `total`,
`per_tool`, `per_outcome`, `per_error_class`, `first_at`/`last_at`,
`total_appended` (durable monotonic, F-138), and `advertised` (snapshot for
coverage, F-42/F-67). It is **process-local, not on `ServerRun`** (N-7), and is
**not** cleared by `ServerRun.clear_authority` ([run_state.py:33](../../src/pyocd_debug_mcp/kernel/run_state.py#L33))
or by disconnect (F-66, F-109, AC-30, AC-55). Reads are copy-out and lock-free-ish
(F-69). Counters are authoritative; ledger and summaries are derived from them —
never the reverse (F-64, AC-31). Because the state is cumulative and never
reset mid-run, `counters.snapshot()` directly satisfies F-165's "cumulative,
never a per-window delta" requirement — the usage-snapshot record and the
per-report usage count (§5.1) are both plain reads of this one object, not a
second accumulator built for the ledger.

**`trail.py`** (F-1, F-5, W-10). `dict[str | None, deque(maxlen=100)]` keyed by
`board_id`; `None` is the no-board bucket. A report attaches **only its own
board's deque** (AC-12). Entries carry F-4's fields: `board_id`,
connection-identity token, `outcome ∈ {success, policy_refusal, unexpected_error}`,
the refusal's named remedy when present, and any observed transition in
plan/permission/gate/tool-visibility state (detected by diffing the guard-state
snapshot between calls). Cadence stays at its own ~100 (`TRAIL_MAXLEN`)
regardless of the 100-call usage snapshot (`USAGE_SNAPSHOT_CADENCE`) or the
500-call check-in (`CHECKIN_CADENCE`) — three separate constants, two of which
happen to share a value today (F-1, F-129, N-11).

**Funnel instrumentation** — the only meaningful edit to existing code. In
`RegistryFastMCP.call_tool`, wrap the existing body:

```python
observation = self._monitor.begin(name, arguments, board_id) if self._monitor else None
try:
    result = <existing body unchanged>
except BaseException as exc:
    if observation: observation.failed(exc)   # records; never suppresses (N-6)
    raise
else:
    if observation: observation.completed(result)
    return result
finally:
    # existing list_changed notification stays exactly where it is
```

Rules that make this satisfy W-3/N-6:
- `observation.*` never swallows, softens, delays, or reorders anything. It is
  called on the way past.
- Every monitor call is wrapped in `try/except BaseException: pass` internally
  (fail-open, N-6) — and the swallowed error is counted, not reported (W-8,
  report-path recursion).
- Nested `action_batch` children are tagged via a `ContextVar` depth counter and
  counted individually with `parent="action_batch"`.
- **Per call, the synchronous work is in-memory only:** a `counters` increment
  and a `trail` `deque.append` (N-3, F-27, F-106). **There is no per-call ledger
  write and no per-call queue item** — F-38 removed that record entirely, so
  the hot path has nothing to hand to the delivery worker on the common case.
- `observation.completed`/`.failed` also does the one piece of tick arithmetic
  this phase owns: compare the post-increment `counters.total` against
  `USAGE_SNAPSHOT_CADENCE` and, in personal builds, `CHECKIN_CADENCE`. On a
  miss this is a single modulo check and nothing else happens. On a hit it
  hands a small `usage_snapshot` (or a check-in-due flag) to `ledger.py` —
  still a synchronous local append (F-40), cheap and bounded — while any
  segment roll or transport handoff for the just-sealed file happens off the
  calling thread (Phase 5, F-167). This tick check must never delay or
  interleave with the call that tripped it (W-21, AC-28).

`ManagedOperation` already exposes `state`, `error`, `execution_started_at`, and
`resources.fatal_cleanup_errors` — but they are not visible from `call_tool`
after `dispatch` returns. **Small addition to `kernel/operations.py`:** a
`ContextVar[OperationOutcome]` that `dispatch` sets in its `finally`, carrying
`(state, non_interruptible, fatal_cleanup_errors, owned_count)`. This is read-only
telemetry — it adds no branch, no lock, and no deadline effect (W-3).

**Named constants (N-11, AC-106).** Every tunable this phase and Phase 5
introduce is a single module-level constant, following the pattern `thrash.py`
already uses for `THRESHOLD`/`WINDOW_SECONDS`:

| Constant | Value | Lives in | Governs |
|---|---|---|---|
| `USAGE_SNAPSHOT_CADENCE` | 100 | `counters.py` | snapshot production (F-43) and the segment roll (F-158) — **the same constant**, not a second one, so raising the cadence moves both together (AC-106) — and the periodic recording occasion (F-77) |
| `CHECKIN_CADENCE` | 500 | `counters.py`, beside the above | the check-in prompt tick (F-128/F-129) |
| `TRAIL_MAX_EVENTS` | 100 | `trail.py` | the board-scoped ring buffers (F-1) — a **separate** constant from `USAGE_SNAPSHOT_CADENCE` even though both are 100 today (F-1, N-11) |
| `STALENESS_THRESHOLD` | `timedelta(days=14)` | `block.py` | the remote-logging staleness backstop (F-120) |
| `DEDUPE_WINDOW_SECONDS` | 300.0 | `reports.py` | the dedup/rate-limit window (F-26) |
| `CLOSEOUT_BUDGET_SECONDS` | 0.4 | `delivery.py` | the closeout send bound (F-113) — **measured, not tunable**: it is fit inside `CLIENT_KILL_GRACE_SECONDS` (0.5, observed), not set equal to a knob we turn (N-11 closing bullet) |

None of these may appear as a bare literal at its point of use. The snapshot
cadence is the load-bearing one: `IssueMonitor` takes it as a single
constructor value and uses that same value for the snapshot record, the segment
roll, and the delivery handoff, so AC-106 ("changing the cadence in one place
moves snapshot production, the segment roll, and the periodic occasion
together, with no other edit") holds by construction rather than by convention.

**Test hooks for the two cadences.** `counters.resolve_snapshot_cadence()` and
`resolve_checkin_cadence()` return the constants unless
`BYO_MCP_SNAPSHOT_CADENCE` / `BYO_MCP_CHECKIN_CADENCE` override them; the
composition root passes the resolved values in. This exists for the same reason
F-127 mandates an injectable test transport: without it the snapshot path — the
centrepiece of the recording design — is unreachable end-to-end, because no
test session makes 100 real tool calls. An absent, unparsable, or non-positive
value changes nothing, and the constants remain the shipped values.

### Phase 3 — classification, reports, local sink

**`classify.py`** — the highest-value module in the project (W-1). Explicit
tables, not heuristics:

*Policy refusal → trail + ledger only, never S-1* (F-7):
`PolicyRefusal`, `PlanRefusal`, `SetupWorkflowError`, `RegisterPreconditionError`,
`BatchValidationError`, `BoardBusyError`, safety-region `Refusal`; `ToolError`
whose message matches the locked-handler shape from `require_unlocked`; returned
strings matching `^Refused \[` or JSON `status` matching `_refused$`; the
`no board` sentinel (`NO_BOARD_CONFIG_MESSAGE`, [server.py:296](../../src/pyocd_debug_mcp/server.py#L296));
terminal setup/validation statuses.

*Environment fault → S-3, triage `environment_fault`* (A-7, W-4, AC-8):
`ProbeNotFoundError`, `LockedTargetError`, `TargetConnectionError`,
`BoardNotConnectedError`, `serial.SerialException`, native-build
toolchain-missing. The existing `_error_code` map ([server.py:327-342](../../src/pyocd_debug_mcp/server.py#L327-L342))
is already exactly this taxonomy — lift it into `classify.py` and have
`server.py` import it from there, so there is one home for it (charter §4).

*Unexpected → S-1, triage `server_defect*` (F-6, AC-1):
everything else, plus `OperationTimeoutError` (hard-deadline termination of an
unreturned worker) and `OperationCleanupError` (unconfirmable provider closure /
cleanup failure) — both already defined in `kernel/operations.py`.

**`reports.py`** — §5 contract, `dataclass` → dict → JSON. Grouping key (F-24):
`blake2b(signal_type | triage_class | tool_name | error_signature_or_refusal_code)`.
**No `run_id`, no timestamp, no `board_id`** — board out of the key is what makes
AC-8's "one grouped report per unplugged probe" hold. `error_signature` =
`f"{exc.__module__}.{type(exc).__name__}"` + message with digits, hex, GUIDs,
and paths normalized out.

Dedup/rate limit (F-26, W-12): per-group token bucket, first occurrence emits
immediately, subsequent occurrences within the window increment
`suppressed_count` and re-emit at most once per window with the running total.

Collapse (F-25, W-6, AC-9): model-side signals arrive as one form, so collapse is
naturally satisfied for S-4…S-14. Server-side, two reports landing in the same
group within the debounce window merge.

Usage snapshot on every report (F-165, §5.1, AC-103): each report attaches the
run's cumulative counts at filing time, a plain `counters.snapshot()` call
alongside the trail (F-2) — not a separate accumulator. This is the "usage
count on every problem report" companion to the periodic snapshot, and it
means a report filed instead of a routine tick still carries the true running
total for the anti-under-report property of F-166.

Sinks — both, always (F-30, AC-13): the report is written as a Sentry event
through the SDK (landing in `simulated_remote/` until cutover, §Phase 5) *and* as
one JSON file in the run's per-workspace area of the per-user store,
`<app-data>/BYO/server_data/<workspace_id>/`. Two placement rules, both hard:
**never under `.firm/`** (F-31) and **never inside the workspace project
directory itself** (F-30, AC-13). The second is easy to get wrong precisely
because the handshake hands the code a workspace path — that path selects *which*
workspace folder in the per-user store, and is never a write target. Write-only:
no code path reads a report back (F-32, N-10, AC-47).

### Phase 4 — thrash detector (F-9, F-10, AC-3, AC-4)

Key: `(board_id, tool_name, args_fingerprint)`. Fire when count ≥ N within a
sliding window **and** outcome class + error code are identical across all
occurrences **and** the guard-state snapshot (plan id, gate state, permission
mode, `tool_registry.list_revision`) is unchanged. Repetition alone is never
thrashing (F-10 closing sentence).

Hardcoded exclusions, each traced to F-10:
- polling tools: `get_state`, `read_execution_state`, `get_setup_status`, `wait`;
- any `*-plan` tool (the all-NULL → populated pair has *different* fingerprints
  anyway, but the exclusion is explicit so a future change cannot regress AC-4);
- previous occurrence's outcome was `BoardBusyError` or `OperationTimeoutError`
  (retry after board-busy/timeout);
- a `board_validate` whose arguments equal the server's last returned
  `accepted_response`;
- the `board_safety_refresh` → `board_validate` pair;
- paginated/windowed reads — naturally excluded because a changed address or
  length changes the fingerprint.

The threshold and window are the *only* tunables and both trace to F-10's
"repetition with an identical outcome and no state transition", not to a magic
number (charter anti-pattern list).

### Phase 5 — delivery seam, anchor, staleness block

**`transport.py`** (F-58…F-63, F-62). One Protocol, two destinations behind it,
because Sentry is an *issue* tracker and the ledger is not a stream of issues:

```python
class Transport(Protocol):
    name: str
    def send(self, batch: Sequence[Record]) -> DeliveryResult: ...
# DeliveryResult(state: SENT | FAILED | NOT_CONFIGURED | FILLER_SIMULATED,
#                acked_ids: frozenset[str], at: datetime)
```

- **Reports → Sentry envelopes.** `sentry_sdk.init(transport=<local writer>)`
  builds a genuine Sentry event per report and the custom transport writes the
  serialized envelope to `simulated_remote/reports/`. The §5 report contract maps
  onto the SDK's native fields rather than a bespoke schema: grouping identity
  (F-24) → `fingerprint`; signal type, triage class, origin (F-22, F-23) → tags;
  severity → `level`; the board-scoped trail (F-2, F-5) → breadcrumbs; guard
  state and environment → contexts. **Cutover = drop the custom transport, supply
  a real DSN** (F-62, AC-49). The redaction bar of §Phase 1 is applied *before*
  handing anything to the SDK, and `send_default_pii=False`,
  `attach_stacktrace=False`, `server_name=None`, `max_breadcrumbs=100`.
- **Ledger + summaries → plain spool records** in `simulated_remote/spool/`,
  awaiting the A-11 bulk pipeline. Pushing bulk usage-snapshot ledgers through
  Sentry events would be both wrong-shaped and expensive (W-12, W-19).

Implementations: `NullTransport` (default when nothing is configured, always
`NOT_CONFIGURED`, F-58); `SimulatedRemoteTransport` (the F-59 filler — both
destinations above, returning `FILLER_SIMULATED` with ACKs, F-133, F-123); `TestTransport` (F-127: failure-always plus injectable anchor
timestamps). The real remote later replaces `SimulatedRemoteTransport` at the same
seam with the ledger format, summary contents, and lifecycle unchanged (F-62).

`FILLER_SIMULATED` is a **distinct state from `SENT`** and is surfaced in the
health check and every summary (F-60, F-123, W-18, AC-54, AC-62, AC-72). No code
path may treat it as a *durable off-box copy* (§4.14 Definitions, F-61, F-103).

**Verify against the pinned `sentry-sdk` at implementation time:** the exact
custom-transport signature (`Transport` subclass vs. a callable receiving an
`Envelope`) and whether a syntactically valid DSN must still be supplied when a
custom transport is installed. Both are small and local to this module; nothing
else in the design depends on the answer.

**`delivery.py`** (F-167) — one daemon thread with a bounded queue. Three
occasions (F-52, rebalanced by F-111):

| Occasion | Trigger | Budget | Notes |
|---|---|---|---|
| Bootup recovery | after readiness signalled | generous, async | the workhorse (F-116); never blocks handshake (W-14) or early calls (F-57, AC-59) |
| Periodic | every `USAGE_SNAPSHOT_CADENCE` (100) calls (F-77/F-129) | light, fails silently | must not interleave with the operation that tripped it (W-21, AC-28) |
| Closeout | SIGINT/SIGTERM/EOF | **configurable, default 400 ms** | capped by client kill grace (F-113: Claude Code ≈500 ms) |

**The sender is decoupled by construction (F-167).** The server's obligation
for any record ends at the local append (F-40); a sealed segment is left in
`server_data/` for the daemon thread to find, or handed to a bounded queue, and
the sender ships it on its own schedule. The handoff itself must be
non-blocking: nothing on the request path ever `await`s a send, holds a board
lock, or waits on the queue while a send is in flight. If the queue is full,
the **handoff** is dropped, never the record — the sealed file simply stays on
disk and the next bootup recovery (F-116) finds and ships it. A hung socket or
a wedged retry inside the sender must therefore be invisible to every tool call
(F-29, AC-105). The one place the sender's *outcome* is allowed to reach the
server at all is the staleness backstop (§4.18), and even there only as a
local timestamp comparison against the delivery anchor — never a wait on the
sender itself. The closeout attempt of F-113/F-159 remains the sole exception
where the server touches sending directly, and it stays capped by the client
kill grace, with the remainder left to background recovery.

Delete-on-ACK is **whole-file** (F-155): push a complete sealed file, receive an
ACK for that file's identity, `os.unlink` it. No rewrite, no compaction, no
partial-file bookkeeping. Sealed files only — a file still being appended to is
never pushed and never deleted.

This is what makes the round-3 trailing-window problem (retired F-137) disappear rather than get
managed: chains never span a deletion boundary, so **every resident file verifies
completely** and an absent file is simply absent (F-156). It also makes the
append-only ACL of F-94 coherent — under record-level deletion the code would
have had to strip its own hardening, rewrite, and re-apply it, destroying the
protection precisely when it was doing work (F-157).

Segment rolling (F-158) bounds the only-local window, which would otherwise equal
the whole run duration: at the `USAGE_SNAPSHOT_CADENCE` tick the current file is
sealed and `<run_id>.<n+1>` opens, carrying the predecessor's head hash in its
genesis record so the run's chain stays verifiable end to end. The roll reads
the same constant the snapshot tick uses rather than carrying its own copy
(N-11, AC-106) — changing it touches nothing else (F-101, F-102, AC-96).

Which occasion pushes what (F-159, AC-97):

| Occasion | Pushes |
|---|---|
| Bootup recovery | all sealed files from prior runs — the workhorse |
| Periodic | sealed segments of the current run; **never the live file** |
| Closeout | seals and attempts the final segment — the only occasion that can reach it; if killed, next boot takes it |

**This is the whole cleanup story — there is no manual step** (Decision 3, and
now the spec's own §2, F-33, F-118, F-134). Push and drain happen on their own,
in the background, with no user-facing surface. Implementation consequences:

- **ACK-driven deletion is the only code path that removes a local record.** No
  rotation timer, no size cap, no cleanup tool, no operator-facing command. AC-60
  is then a test that *no such path exists*, not a test that it behaves well.
- **Nothing undelivered can be discarded**, because un-ACKed records are the only
  ones ever resident (F-136).
- The verifier tolerates both a fully drained store and a wholesale-removed folder
  without a tamper finding (F-93, W-23, AC-43).
- **The exposure moves rather than disappears** (W-12 as revised): local growth is
  now purely a function of delivery health. A drained store stays small on its own;
  a stalled one grows unbounded with no cleanup step to relieve it.
- **Surface it; build no detector on it** (F-85). The health check and summaries
  report the counter, `total_appended`, the un-ACKed backlog size, and the last
  write failure if there was one. No threshold, no trend analysis, no issue raised.
  Three reasons: the staleness backstop already enforces the consequence (broken
  recording or delivery produces no ACKs → anchor goes stale → dispatch refused); a
  report about an unwritable store generally cannot be written either; and W-8
  forbids the monitoring path filing reports about its own failures.

  One reading note for whoever implements the health check: counter ahead of
  `total_appended` is a real shortfall, but counter ahead of the *resident file
  count* is expected and normal — delivered files delete themselves (F-138, AC-73).

Progress tracking (F-55, F-56, W-20): `server_data/delivery_state.json` holds
per-file ACK marks. **At-least-once with stable file identity** — the
`(workspace_id, run_id, segment)` triple of F-155 — never exactly-once (AC-25).

Closeout shape (F-115, F-148, AC-58, AC-82) — flag-then-drain, no work in the
handler:
```
signal handler:  shutdown_event.set(); return           # nothing else, ever
main() finally:  1. existing disconnect loop  (reset release, owned children)   # F-112
                 2. write close record locally                                   # F-80, F-79
                 3. bounded closeout send within CLOSEOUT_BUDGET                 # F-111, F-113
```
On Windows, signals are unreliable and kill-on-close job objects give no
notification (F-148 closing sentence) — so this is best-effort by construction
and append+bootup-recovery is the real net (F-40, F-114).

**`block.py`** (F-119…F-127). Anchor file `server_data/delivery_anchor.json`
holds `{last_confirmed_at, transport_name, origin: "filler"|"real"}`.
`staleness = now - anchor.at`, threshold **exactly 14 days** (F-120, F-145).
States: **dormant** (no anchor ever — bootstrap, F-122), **armed**,
**tripped**, **clock-unusable → fail-open + log** (F-120, AC-66).

Enforcement point: the first statement of `_enforce_guarded_invocation`
([server.py:647](../../src/pyocd_debug_mcp/server.py#L647)), which is exactly and only
"guarded hardware dispatch". It raises a `PolicyRefusal` that **names its
remedy** ("restore network connectivity and let the server deliver its logs"), so
§1.1 classifies it as correct behavior and the monitor must not self-report it as
S-1 or S-7 (F-121). Because `before_execution` runs at the dispatch boundary and
never mid-handler, F-125(b) (never interrupt an in-flight flash or board lock) is
satisfied structurally. The three monitor tools bypass it entirely — they are not
guarded-dispatch tools (F-125(a), AC-63).

Three deployment states, and Decision 3 collapses the risk I previously flagged:

| State | Transport | Block behavior |
|---|---|---|
| Dev/test now | `SimulatedRemoteTransport` | **armed but self-clearing** — every filler delivery re-anchors, so it never trips while the machinery is exercised for real (F-123, AC-61) |
| Production, pre-cloud | *does not exist* | `simulated_remote/` is never deployed (Decision 3); the build ships when the real remote does |
| Production, post-cloud | real remote | live — anchor reflects genuine off-box delivery, block protects real audit coverage (F-124) |

Because the filler never leaves the bench, the failure mode raised earlier — a
locked-down machine where `simulated_remote/` cannot be written, producing no ACK
and arming the block for real after 14 days — cannot occur in a shipped build. It
can still occur *on the bench*, which is what the F-127 test hook is for. The
arming condition is read off the health check's anchor age and backlog size; do
**not** add a "N consecutive delivery failures" detector on top, for the same
reason F-85 forbids one — the block is already the enforcement.

**N-9 is narrowed, not removed** (F-126). Offline still starts, runs, and exits
normally; the block can refuse guarded dispatch only once an anchor exists (F-122)
*and* delivery has been unconfirmed for 14 days (F-120). Both conditions must be
checked, in that order — an implementation that reads a missing anchor as
"infinitely stale" inverts the requirement and bricks a fresh install on its first
operation, which is exactly the bootstrap case F-122 exists to prevent. Throughout
the filler era, where the filler self-anchors (F-123), "no network" is fully
normal operation.

### Phase 6 — the three agent-facing tools, handshake, build flag

**`build_profile.py`** (F-140, F-143, F-147). One module, one constant, edited by
hand when a build is cut (Decision 2):

```python
"""Build-time feature profile. Edit this constant when cutting a build."""

# True  -> personal build: model-authored narrative is present.
# False -> professional build: no check-in tool, no narrative fields, no S-4..S-14.
NARRATIVE_LOGGING: bool = True
```

No env var, no build hook, no config file — so no misconfiguration, flag-check
bug, or misbehaving agent can re-enable narrative in a build cut without it
(F-143). Once the code is compiled to a binary the constant is no longer editable,
which is where F-143's guarantee becomes literal. Distribution distinguishes the
two builds by filename (F-147), and the server self-declares
`narrative_logging: enabled | not_built` in the health check and every summary
(F-142, AC-78).

Every consumer reads it as `from .build_profile import NARRATIVE_LOGGING` and
branches at **registration** time, not per call — the professional build must not
merely decline narrative, it must not register `submit_routine_checkin` at all
(F-140), and the narrative pydantic models must never be constructed.

**Exactly three agent-facing actions** (F-128 §9.9 numbering) — registered plainly
with `mcp.add_tool`, and deliberately **not** passed to `configure_layer2` or
`configure_guarded_dispatch`:

1. `report_agent_issue` — issue intake (F-16…F-21).
2. `submit_routine_checkin` — server-prompted, agent-authored check-in (F-48…F-51, F-128).
   **Absent entirely in a professional build** (F-140, F-146).
3. `server_health_check` — read-only readout (F-70…F-74).

Their structural properties fall out of the existing kernel for free — this is
why they are cheap (F-17, F-18, F-49, F-72):
- no `board_id` parameter → `call_tool` resolves `board_id=None` →
  `manager.worker_lock(None)` returns `nullcontext()` → **no board serialization**;
- never `tool_registry.configure(hidden=…, locked=…)` → always advertised, never
  locked, no plan, no permission, no budget;
- registered at import time before any client connects → their
  `list_revision` bumps happen pre-session → **no `tools/list_changed` churn**
  (F-21, W-15).

The one thing that is *not* free: **F-17's "must not be usable as an
`action_batch` child."** Today `_validate_children` ([batch.py:30-68](../../src/pyocd_debug_mcp/tools/batch.py#L30-L68))
would reject them only incidentally, via the `board_id` requirement. Add an
explicit `MONITOR_TOOL_NAMES` deny check with its own error message (AC-22).

`server_health_check` must be **side-effect free** (F-71, AC-29): it reads the
counter snapshot, ledger resident count + chain head, log-root binding state,
transport state (`sent` / `failed` / `not_configured` / `filler_simulated`),
anchor origin and staleness, hardening state, `narrative_logging`, and the
counter-vs-ledger delta. It emits no record and triggers no send. It is the
**test oracle** (F-74, AC-34): every "which tools ran with what outcomes"
assertion in the test suite goes through this tool's response, not through server
internals.

**`narrative.py`** — pydantic models, personal builds only, per §9.15:

*Issue report* (§5.2, AC-85, AC-88): `codebase_objective`, `hypothesis`, `goal`,
`plan`, `failure_point{action_taken, observed_result, named_step}`,
`signal_subcase` (required only for S-6/S-7, validated against that signal's
enum), `recent_actions` — **exactly the last 5** (`min_length=1, max_length=5`,
each `{action, result, code_context}`), `earlier_phases` (ordered one-liners),
`session_start` (one line).

*Check-in* (§5.3, AC-86): `codebase_summary`, `work_summary`,
`tools_used[{tool, purpose}]`, `effectiveness_observed`. A validator rejects
self-rating language in `effectiveness_observed` — §5.3 prohibits self-grading
outright, so it is a rejection, not a warning.

Both are validated against the **narrative bar** (F-153), not the mechanical bar:
they may name real code, they may not embed payloads. Server-attached fields —
trail, guard state, board scope, grouping identity, environment, counts — come
from the server and are never accepted from the model (F-20, F-104, AC-53).

In a professional build (F-146, AC-81): `report_agent_issue` stays **registered
and visible** and returns a remedy-naming refusal — *"this is a professional
license; remote bug reporting is disabled to avoid describing your codebase; the
feature is available in personal mode"* — authoring, storing, and sending nothing.
Keeping it present-and-explaining is what stops the agent from hunting a missing
tool and misfiling an S-5. `submit_routine_checkin` is absent, and because the
check-in is server-*prompted*, no prompt means no S-5 either.

**Check-in prompting** (F-128): on the `CHECKIN_CADENCE`th call and every
`CHECKIN_CADENCE` after, the periodic tick sets a one-shot flag; the **next
tool response** gets a short
appended line asking the agent to write and submit a routine check-in. Mechanism:
the same append point `wrap_layer2_response` already uses
([operations.py:436](../../src/pyocd_debug_mcp/kernel/operations.py#L436)) — one more
idempotent suffix, cleared once consumed. Personal builds only.

This is **the one place monitoring writes into a tool response instead of
observing passively**, and F-128 sanctions it explicitly as a bounded exception to
W-3. The exception is narrow, so the implementation must stay inside it: the
suffix is appended to the server's own response text and must not alter the
tool's result, its ordering, its timing, any lock, or any authority (A-8); it
reads nothing from the conversation (A-2 is untouched); and the agent's
compliance is **behavioral, not gate-enforced** — a missing check-in must never
block, refuse, or degrade anything. Concretely: no state may key off whether a
check-in arrived, and the flag is cleared when the prompt is *emitted*, not when a
check-in comes back. Because the boundary lands mid-operation by design (W-21,
AC-28), appending the suffix must not delay or interleave with the call that
tripped the counter — same constraint as the summary itself and the segment roll.

**Handshake** (F-34…F-36, AC-15). `initialization_handshake` gains
`workspace_path: str | None = None` ([handshake.py:132](../../src/pyocd_debug_mcp/tools/handshake.py#L132)).
Validated: absolute, exists, is a directory. It does exactly two things, both
covered in §Phase 1's `paths.py`: it selects the **workspace identity** (salted
digest → folder name, random token → pushed id), and it releases the F-164 buffer
of pre-binding records into that folder. It is **not** a store location and **not**
a write target (F-30). It governs logging only — A-10 forbids relocating `.firm`,
whose root is resolved at import
([server.py:278](../../src/pyocd_debug_mcp/server.py#L278),
[session_runtime.py:14](../../src/pyocd_debug_mcp/services/session_runtime.py#L14)).

### Phase 7 — the workspace skill (F-11…F-15, F-104, F-144)

Ships in-repo at `skills/byo-issue-report/`, **tool-agnostic — one criteria file,
no Codex/Claude variants** (F-144, AC-79):

- `SKILL.md` — when to invoke, the three actions and how never to confuse them
  (F-128 §9.9), and the §1.1 rule stated first and loudly.
- `criteria.md` — S-4…S-14 with **concrete BYO-specific triggers** (F-12): a
  rejected plan envelope, a call to an unlisted action, an edited `action_batch`
  fallback, a containment refusal on an UNKNOWN span, a remedy that repeated
  itself. **Negative examples carry equal weight** (F-13): a locked-tool refusal
  naming its `*-plan`, an all-NULL plan guide, a closed gate naming
  `board_validate`, a `no board` sentinel — *none of these are reportable.*
- `templates/issue_report.json`, `templates/checkin.json` — the exact fillable
  shapes from §5.2/§5.3, so the server's fixed-schema validation is tractable
  (F-104, W-7).

Not needed by a professional build (narrative is absent), but the error-report
*tool* still ships there per F-146.

### Phase 8 — tests

`tests/test_monitor_*.py`, following the existing `unittest` + in-process-fake
style (`tests/test_server_trust_model_round_*.py`). Assertions go through
`server_health_check` wherever possible (F-74). No hardware.

| File | Covers |
|---|---|
| `test_monitor_stdout.py` | AC-10 |
| `test_monitor_classification.py` | **AC-2 (the primary gate)**, AC-1, AC-6, AC-7, AC-8 |
| `test_monitor_thrash.py` | AC-3, AC-4 |
| `test_monitor_trail.py` | AC-12, AC-14, AC-84 |
| `test_monitor_ledger.py` | AC-18…AC-20, AC-35, AC-39…AC-47, AC-73, AC-91…AC-96, AC-103, AC-104 |
| `test_monitor_workspace.py` | AC-98…AC-101, AC-13 placement: disjoint workspace stores; pushed token is random and path-free; two machines don't reconcile; pre-handshake records flush or land under `unbound/`; **no plaintext workspace path appears anywhere under the store** (F-162); **nothing is written inside the workspace project directory** (F-30) |
| `test_monitor_counters.py` | AC-29…AC-31, AC-36, AC-37, AC-38 (broken writing is *shown* in the health check and files no issue), AC-73, AC-106 |
| `test_monitor_delivery.py` | AC-23…AC-26, AC-54…AC-60, AC-71, AC-72, AC-75, AC-105 |
| `test_monitor_sentry_envelope.py` | report → Sentry event mapping: fingerprint is restart-stable (F-24), tags carry signal/triage/origin, breadcrumbs carry only the failing board's trail, no payload survives redaction into the envelope (AC-14, AC-27, AC-84) |
| `test_monitor_block.py` | AC-61…AC-66, AC-80 |
| `test_monitor_tools.py` | AC-22, AC-29, AC-34, AC-51…AC-53, AC-68, AC-69 |
| `test_monitor_narrative.py` | AC-76…AC-78, AC-81, AC-85…AC-90 |
| `test_monitor_passivity.py` | AC-11, AC-17 (byte-for-byte baseline diff) |

**AC-2 and AC-17 are the two that decide whether this ships.** AC-2 replays a full
correct-guarded-behavior session (locked-tool refusal, all-NULL plan guide, closed
gate, containment rejection, `no board` sentinel) and asserts **zero**
server-defect reports. AC-17 re-runs the existing `tests/baseline_capture.py` /
`baseline_transcript.json` with the monitor enabled and with the sink unreachable
and the log path invalid, and asserts the transcript is byte-for-byte unchanged.
`baseline_transcript.json` already exists for exactly this kind of comparison.

---

## 3. Exact edits to existing files

| File | Edit | Size |
|---|---|---|
| `kernel/registry.py` | fd-dup stdout guard in `run_stdio_async`; `_monitor` hook wrapping `call_tool`'s body | ~40 lines |
| `kernel/operations.py` | `ContextVar[OperationOutcome]` set in `dispatch`'s `finally` (read-only telemetry) | ~15 lines |
| `tools/handshake.py` | optional validated `workspace_path` parameter | ~15 lines |
| `tools/batch.py` | explicit monitor-tool deny in `_validate_children` (F-17) | ~5 lines |
| `server.py` | build `MonitorContext` from existing read-only accessors; register the three tools; staleness check as first line of `_enforce_guarded_invocation`; ordered closeout in `main()`'s `finally`; import `_error_code`'s taxonomy from `classify.py` | ~70 lines |
| `pyproject.toml` | `platformdirs>=4`, `sentry-sdk>=2` | 2 lines |
| `.gitignore` | `.byo-monitor/` | 1 line |

Nothing in `guardrails/`, `safety/`, `setup_flow/`, `adapters/`, or `firmstore/`
changes. That is the W-3 test: if a monitoring change requires editing a safety
module, the design is wrong.

---

## 4. Decisions — settled

**Decision 1 — Sentry ships now; its destination is local.** The `sentry-sdk` is
a real dependency from day one, driven by a custom transport that writes envelopes
into `simulated_remote/reports/`. The SDK does not require a live remote, so
report payloads are built against Sentry's actual data model immediately —
`fingerprint`, tags, `level`, breadcrumbs, contexts — and the cloud cutover is
removing the custom transport and supplying a DSN. That is what makes F-62/AC-49's
"no change beyond configuration" literally true; a bespoke interim format would
have forced a translation layer at cutover. The ledger and summaries do **not**
go through Sentry — they are bulk activity records, not issues — and spool
separately for the A-11 pipeline.

*Spec-text consequence:* AC-1 and AC-13's "appears in Sentry" is satisfied
structurally today (a real Sentry event, locally stored) and becomes literally
true at cutover with no test change. Transport state still reports
`FILLER_SIMULATED`, never `SENT`, because no off-box copy exists yet (F-60,
F-103, W-18, AC-54).

**Decision 2 — `narrative_logging` is a hand-edited module constant.** One
`bool` in `monitor/build_profile.py`, flipped when a build is cut. No env var, no
build hook, no config file — the property F-143 protects (nothing at runtime can
re-enable it) holds now, and becomes literal immutability once the code is
compiled to a binary. Builds are distinguished by filename (F-147) and the server
self-declares its profile (F-142). This is also the simplest thing that works,
which the design charter's §2 prefers over a build-system mechanism.

**Decision 3 — delivery and cleanup are automatic and invisible; the filler never
ships.** Anti-paternalism here means not treating the engineer as a log
administrator. Pushing records, receiving ACKs, and deleting the local copies are
all background behavior the user never sees, is never asked about, and is never
responsible for. Concretely this deletes three things the spec described as human
procedure: the §2 weekly manual clear, F-118's deliver-then-clear instruction, and
W-23's tamper-vs-cleanup collision as a recurring event. They are re-expressed as
delete-on-ACK behavior in §Phase 5.

`simulated_remote/` is a bench placeholder for exercising the delivery paths
against Sentry, and **is never deployed** — the build ships when the real remote
exists. That removes the one genuine hazard in the staleness block: a shipped
build cannot arm itself for real because a filler write failed, since no shipped
build has a filler. The block is built exactly as F-119…F-127 specify, with the
F-127 test hook landing before it. The arming condition is visible through the
health check's anchor age and backlog size — **not** through a bespoke
"N consecutive delivery failures" detector, which would duplicate the block's own
enforcement and falls to the same argument as F-85's.

The push itself is a **decoupled background sender** (F-167): the server's
obligation for any record ends at the local append, the sealed segment is
handed off non-blocking, and a stuck or slow sender must be invisible to every
tool call. A full queue drops the handoff, never the record — the file stays
on disk for the next bootup recovery to find.

**Decision 4 — the ledger records occasions, not calls.** F-38 replaced the
original per-call ledger entirely. The only records are `boot`, the
`USAGE_SNAPSHOT_CADENCE`-call usage snapshot (cumulative counts, never a
since-last-tick delta, F-165), the `CHECKIN_CADENCE`-call check-in (personal
builds only), a filed report, and `close`. Per-call sequence detail lives only
in the bounded in-memory trail (F-1) and is attached to reports (F-2) — it is
not durably logged on its own.

*Spec-text consequence:* this is also what makes the anti-under-report
property of F-166 hold — because every delivered snapshot carries the
cumulative total rather than a window's worth of activity, dropping or
withholding intermediate snapshots cannot lower the number the next one
carries, and the per-run chain (F-86) turns a decreasing count or a missing
sequence entry into a detectable finding. That property is documented honestly
as three tiers, not oversold as unforgeable: casual under-reporting is
defeated now; deliberate post-hoc editing is detectable only via an off-box
witness and only after OAuth cutover; source-level forgery by the machine's
own owner is never addressed, in any era (F-166, F-87, F-88, AC-104). It is
also what keeps the ledger small in practice: a run of thousands of calls
produces a handful of ledger records, not thousands, so the background sender
of Decision 3 has little to push and the only-local window of F-101 stays
small well before any real transport exists.

---

## 5. Build order summary

Phase 0 (stdout) and Phase 3 (classifier) first — W-1 and W-2 are the two risks
that invalidate everything downstream, and Phase 0 is now a hard prerequisite for
the Sentry SDK rather than just good hygiene. Phases 1-2 are pure additive
infrastructure. Phase 5's block should land only after Phase 8's
`test_monitor_block.py` exists, since it is the one component that can refuse real
work. Phases 6-7 are the agent-facing surface and are the only part a professional
build changes.
