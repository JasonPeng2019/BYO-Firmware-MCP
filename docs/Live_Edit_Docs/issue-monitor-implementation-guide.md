# Autonomous Issue Monitor — Implementation Guide

**Ground truth:** `mcp-issue-monitor-spec-new-f.md` (WHAT).
**Design:** `issue-monitor-implementation-plan.md` (HOW, at the architecture level).
**This document:** the exact sequence of repo edits — files, anchors, signatures,
algorithms, and a verification command per step.

Repo root for every path below: `MCP_Server/BYO-Firmware-MCP/`.

Rules that apply to every step:

- **The spec wins.** If a step here contradicts the spec, stop and raise it.
- **Never edit `guardrails/`, `safety/`, `setup_flow/`, `adapters/`, or `firmstore/`.**
  If a step seems to require it, the design is wrong (W-3).
- **Every monitor entry point is wrapped in `try/except BaseException: pass`.** The
  monitor may never change what dispatch does (N-6). The only exception is the
  staleness block (F-126).
- Run `uv run ruff check src tests && uv run pyright src` after each step.

**House conventions — match them or the build breaks:**

- `requires-python = ">=3.10"`. **Use `datetime.now(timezone.utc)`, never
  `datetime.UTC`** (3.11+), matching `services/session_runtime.py:22`. No
  `itertools.batched`, no `typing.Self`, no PEP 695 generics.
- Every module starts with `from __future__ import annotations`, as every existing
  module does — this is what makes `X | None` annotations legal on 3.10.
- `line-length = 100`, `target-version = "py310"` (ruff, `pyproject.toml:47-49`).
- Timestamps are the repo's existing UTC-Z text form: `.isoformat().replace("+00:00", "Z")`.
- Services are injected as frozen dataclasses of callables, following
  `tools/misc.py:13` — not by importing `server`.

---

## Step order and dependencies

| Step | Produces | Depends on |
|---|---|---|
| 1 | Package skeleton, `build_profile`, `paths`, `redaction` | — |
| 2 | `ledger` (segmented chain, hardening, verification) | 1 |
| 3 | `counters`, `trail` | 1 |
| 4 | `classify` | 1 |
| 5 | `reports` | 1, 3, 4 |
| 6 | `thrash` | 3, 4 |
| 7 | `transport`, `delivery`, `block` | 1, 2 |
| 8 | `monitor` facade + `MonitorContext` | 2–7 |
| 9 | `narrative`, `tools` (the three MCP tools) | 5, 8 |
| 10 | Wiring: `registry.py`, `batch.py`, `handshake.py`, `server.py` | 8, 9 |
| 11 | `pyproject.toml`, `.gitignore` | — |
| 12 | Tests | all |

Step 10 is last on purpose: until it lands, nothing in `monitor/` is reachable, so
steps 1–9 cannot regress the server.

---

## Step 1 — package skeleton, build profile, paths, redaction

### 1.1 `src/pyocd_debug_mcp/monitor/__init__.py`

Exports only what step 10 wires: `IssueMonitor`, `MonitorContext`,
`build_monitor_tools`. Nothing else is public.

### 1.2 `src/pyocd_debug_mcp/monitor/build_profile.py`

```python
"""Build-time feature profile. Edit this constant when cutting a build."""

# True  -> personal build: model-authored narrative is present.
# False -> professional build: no check-in tool, no narrative fields, no S-4..S-14.
NARRATIVE_LOGGING: bool = True
```

No env var, no config file, no build hook (F-140, F-143). Every consumer imports
the constant and branches at **registration** time, not per call.

### 1.3 `src/pyocd_debug_mcp/monitor/paths.py`

Two independent questions, two functions, deliberately different return types so
they cannot be swapped (F-30, F-131, F-132, F-161, F-162, F-164).

```python
class StoreState(str, Enum):
    APP_DATA = "app_data"        # platformdirs
    OPERATOR_ROOT = "operator"   # BYO_MCP_ARTIFACT_ROOT fallback
    BUFFERING = "buffering"      # nowhere writable yet

@dataclass(frozen=True, slots=True)
class StoreRoot:
    state: StoreState
    root: Path | None            # None iff BUFFERING
    @property
    def server_data(self) -> Path | None: ...
    @property
    def simulated_remote(self) -> Path | None: ...

def resolve_store_root() -> StoreRoot: ...          # cached, resolved once
def workspace_id(path: Path | None) -> str: ...     # salted digest, or "unbound"
def workspace_token(store: StoreRoot, wid: str) -> str: ...  # random, persisted

def _reset_cache(override: Path | None = None) -> None: ...  # tests only
```

`_reset_cache` is the **only** way a test redirects the store. It clears the cached
root and, when given an `override`, pins it. Without it every test would write into
the developer's real `%LOCALAPPDATA%\BYO`, because `resolve_store_root()` tries
platformdirs *first* and would ignore a `BYO_MCP_ARTIFACT_ROOT` pointed at
`tmp_path`. Do **not** add a second shipping env var to work around this —
F-132 fixes `BYO_MCP_ARTIFACT_ROOT` as the one operator override, and a test-only
hook keeps that contract intact.

`resolve_store_root()` order: `platformdirs.user_data_dir("BYO", appauthor=False,
roaming=False)` → `$BYO_MCP_ARTIFACT_ROOT/.byo-monitor/` → `BUFFERING`. Each
candidate is accepted only after `mkdir(parents=True, exist_ok=True)` **and** a
write probe succeed; a candidate that raises `OSError` falls through to the next.

**The handshake path is not in that chain.** It is only ever an argument to
`workspace_id()`. Writing under a caller-supplied workspace path violates F-30 and
AC-13.

`workspace_id(path)` = `"unbound"` if `path is None`, else the first 16 hex chars
of `blake2b(salt + str(path.resolve()).casefold(), digest_size=8)`. On Windows,
casefold before hashing so path casing does not produce two workspace folders for
one project.

`workspace_token(store, wid)` reads `server_data/<wid>/workspace.token` if present,
else writes `secrets.token_hex(16)` there and returns it. It is **random, never
derived from the path** (F-162). Nothing else may be written into that folder that
carries the plaintext path — no README, no `workspace_path` field, no manifest.

**Because it can write, it is called only from `bind_workspace()` — never from the
health check.** The health check must be side-effect free (F-71, AC-29: "calling it
twice in a row must produce the same answer"), and a lazily-created token file is a
side effect. Provide `read_workspace_token(store, wid) -> str | None` for readers:
it returns `None` rather than creating anything.

### 1.4 `src/pyocd_debug_mcp/monitor/redaction.py`

```python
def fingerprint(value: object) -> str: ...            # salted blake2b, 16 hex
def digest_id(value: str, keep: int = 4) -> str: ...  # truncated salted digest
def safe_path(value: str) -> str: ...                 # "<basename>#<digest>"
def scrub_mechanical(payload: Mapping) -> dict: ...   # F-3 bar
def check_narrative(text: str) -> None: ...           # F-153 bar; raises ValueError
def result_text(result: object) -> str: ...           # ContentBlock list -> text
```

**`deployment_salt()` lives in `paths.py`, not here.** `paths.workspace_id()` needs
the salt and `redaction` needs the store root, so putting the salt in `redaction`
creates a `paths` ↔ `redaction` import cycle. The salt is a store-level artifact,
so it belongs with the store: `paths.deployment_salt() -> bytes`, and `redaction`
imports one-directionally from `paths`.

Salt lives at `<store.root>/fingerprint.salt`, mode `0o600`, 32 bytes from
`secrets.token_bytes`, created on first use. If the store is `BUFFERING`, hold a
process-local salt and **do not persist it** — fingerprints for that run simply do
not correlate with later runs, which is acceptable and must not crash.

**`result_text(result)` is not optional.** Every tool in this repo is registered
`structured_output=False`, so FastMCP's `convert_result` returns a
**`list[ContentBlock]`**, never a `str` (verified in the pinned package:
`func_metadata.convert_result` returns `_convert_to_content(result)` when
`output_schema is None`). Feeding that straight into a string matcher would make
every non-error refusal — `Refused [...]`, `"status": "..._refused"`, the `no
board` sentinel — classify as a plain success, silently gutting A-6 handling and
most of AC-2 / AC-6 / AC-7. `result_text` must handle all four shapes:

- `str` → itself;
- `list` → `"\n".join(getattr(b, "text", "") for b in blocks)`;
- `tuple` of `(content, structured)` → recurse on element 0;
- anything else → `""` (classify as success, never guess).

`fingerprint()` canonicalises with the existing
`guardrails.plan_engine.canonical_json` — do not write a second canonicaliser.
Import it directly; that is a read-only use and does not violate the "never edit
guardrails" rule.

Two bars, per F-153 / §5.5:

- **`scrub_mechanical`** — for trail, ledger, counters, and all server-detected
  report fields. Drops any value that is `bytes`, longer than 512 chars, or matches
  a payload pattern; replaces path-like strings via `safe_path`; replaces
  `probe_uid`/`serial`/`unique_id`/`mcu_part_number` values via `digest_id`.
- **`check_narrative`** — for personal-build model prose only. It **allows** real
  symbol names, file names, and codebase description. It **rejects** verbatim
  payloads: hex runs ≥ 32 bytes (`(?:[0-9A-Fa-f]{2}[\s:,-]?){32,}`), base64 runs
  ≥ 256 chars, and full command lines (a line containing an absolute path plus two
  or more ` -` flags).

**Verify step 1:** `uv run python -c "from pyocd_debug_mcp.monitor import paths;
print(paths.resolve_store_root())"` prints an `APP_DATA` root, and no file under it
contains any plaintext project path.

---

## Step 2 — `monitor/ledger.py`

Implements F-38…F-42, F-86, F-91…F-97, F-154…F-160.

```python
@dataclass(frozen=True, slots=True)
class LedgerRecord:
    seq: int; run_id: str; ts: str; kind: str      # "boot"|"call"|"summary"|"close"
    tool: str | None; board: str | None
    args_fp: str | None; outcome: str | None; error_class: str | None
    duration_ms: int | None
    prev: str; hash: str

class VerificationOutcome(str, Enum):
    VERIFIED = "verified"
    CHAIN_INVALID = "chain_invalid"
    TRUNCATED = "truncated_vs_published_head"
    RUN_ABSENT = "run_absent"
    IMPOSSIBLE = "verification_impossible"

class SegmentLedger:
    def __init__(self, store: StoreRoot, workspace: str, run_id: str) -> None
    def append(self, **fields) -> LedgerRecord | None
    def roll(self) -> Path | None            # seal current, open next
    def seal(self) -> Path | None            # close current, no successor
    @property
    def resident_files(self) -> tuple[Path, ...]
    @property
    def head(self) -> str
    @property
    def total_appended(self) -> int
    @property
    def hardening(self) -> str               # "applied"|"unsupported"|"failed"
```

**File naming:** `server_data/<workspace_id>/<run_id>.<segment>.jsonl`, segment
zero-padded to 4 (`0001`). Sorting the names must equal sorting by segment.

**Record hashing:** `hash = blake2b(prev + canonical_json(record_without_hash),
digest_size=32).hexdigest()`. The genesis record of segment *n>1* carries
`prev = <head of segment n-1>`; segment 1's genesis carries
`prev = "genesis:" + run_id` (F-158). Never salt this — it is an integrity hash, and
verification must work from the file alone (contrast F-149, which is a privacy salt).

**Durability (F-40, AC-20):** open with `"a"`, `write`, `flush`, `os.fsync(fileno)`.
Keep the handle open for the segment's life; close on `roll`/`seal`.

**Thread safety is mandatory, not incidental.** Different boards dispatch
concurrently by design (W-10, F-68), so `append` and `roll` are called from
multiple threads at once. `SegmentLedger` owns one `threading.Lock` covering the
sequence counter, the `prev` head, the file handle, and the roll transition — a
race here produces a genuinely corrupt chain that then reads as tampering.

**Append/unlink only (F-154, F-157).** There is no code path that rewrites, seeks,
truncates, or compacts a ledger file. Deletion is `Path.unlink()` of a whole sealed
file and nothing else.

**Hardening (F-94…F-97):** Windows →
`icacls <file> /deny "<user>:(WD)" /grant "<user>:(AD)"` via `subprocess.run` with
`timeout=2`, `stdin=DEVNULL`, `stdout=DEVNULL`, `stderr=DEVNULL`, `shell=False`.
POSIX → `os.chmod(0o600)` and report `"unsupported"` (append-only there needs
`chattr +a`, i.e. root — do not claim otherwise). Any failure logs and continues
with an ordinary file (F-96); the state is reported (F-97).

**Order is load-bearing: open the append handle first, harden second.** Windows
grants file access at open time, so a handle opened before the deny survives it —
but `open(path, "a")` requests `GENERIC_WRITE`, which *includes* `WRITE_DATA`, so
any attempt to (re)open the file after the deny is applied will fail. Harden first
and the ledger cannot write at all. Concretely, per segment: `open("a")` → write
the genesis record → apply hardening → keep that handle for the segment's life.
Never reopen a hardened segment for writing; `roll`/`seal` close it permanently and
the next segment is a new file. Readers (verification, delivery) open read-only and
are unaffected; deletion needs `DELETE`, not `WRITE_DATA`, so delete-on-ACK still
works. If a write ever fails after hardening, degrade per F-96 — do not attempt to
strip the ACL to recover, which F-157 forbids outright.

**Verification (F-91, F-92, AC-40…AC-44):** `verify_prior_runs(store, workspace)`
walks resident files that are not the current run's, recomputes each chain, and
returns `{path: VerificationOutcome}`. With no published head available — always,
today (A-11) — a file whose internal chain is consistent reports **`IMPOSSIBLE`,
not `VERIFIED`** (AC-44). A *missing* file is never a finding (F-93): report
`RUN_ABSENT` only when a delivery record claims a file should still be resident.
A segment whose `prev` names a head not present locally is **normal** (F-156) and
must not produce `CHAIN_INVALID`.

**Docstring requirements (F-87, F-88, W-17):** the module docstring states that the
chain detects accidents and corruption only, is defeated by stopping the server
first, and that a local key would not fix it. **The word "untamperable" must not
appear anywhere in the repo.**

**Verify step 2:** unit test appends 10 records, kills nothing, and re-verifies the
file; then flips one byte in record 5 and asserts `CHAIN_INVALID`; then deletes
segment 1 and asserts segment 2 still verifies without a finding.

---

## Step 3 — `monitor/counters.py`, `monitor/trail.py`

### counters.py (F-64…F-69, F-138)

```python
class RunCounters:
    def record(self, tool: str, outcome: str, error_class: str | None) -> int
    def note_appended(self) -> None          # bump durable total_appended
    def note_write_failure(self, exc: BaseException) -> None
    def set_advertised(self, names: Iterable[str]) -> None
    def snapshot(self) -> CountersSnapshot   # copy-out, side-effect free
```

One `threading.Lock`. `snapshot()` returns an immutable dataclass with `total`,
`per_tool`, `per_outcome`, `per_error_class`, `first_at`, `last_at`,
`total_appended`, `never_exercised`, `last_write_error`.

**It is not on `ServerRun`** (N-7) and **nothing clears it** — not disconnect, not
`clear_authority()`, not gate closure (F-66, AC-30). There is no `reset()` method;
omitting it is the enforcement.

### trail.py (F-1, F-2, F-5, W-10)

`dict[str | None, deque(maxlen=100)]` keyed by `board_id`, guarded by one lock.
`for_board(board_id)` returns **only that board's** entries (AC-12). Entry fields
per F-4: `ts`, `tool`, `board`, `args_fp`, `outcome`
(`success|policy_refusal|unexpected_error`), `error_class`, `remedy`,
`guard_transition`, `duration_ms`. Buffer stays at 100 even though the summary
cadence is 500 (F-1, F-129).

**Verify step 3:** counts survive a simulated `clear_authority()`; two boards'
trails do not interleave.

---

## Step 4 — `monitor/classify.py`

The highest-risk module (W-1). Tables, not heuristics.

```python
class Outcome(str, Enum):
    SUCCESS = "success"
    POLICY_REFUSAL = "policy_refusal"
    UNEXPECTED_ERROR = "unexpected_error"

class TriageClass(str, Enum):
    SERVER_DEFECT = "server_defect"
    PRODUCT_FEEDBACK = "product_feedback"
    ENVIRONMENT_FAULT = "environment_fault"
    AGENT_BEHAVIOR = "agent_behavior"
    SOFT_SIGNAL = "soft_signal"

def classify_exception(exc: BaseException) -> tuple[Outcome, TriageClass, str]
def classify_result(text: str) -> tuple[Outcome, str | None]   # outcome, remedy
def error_signature(exc: BaseException) -> str
def error_code(exc: Exception) -> str          # the mapping lifted from server.py
```

`error_code` is the function `server.py:327` currently implements inline; §10.6
makes `server.py` delegate to it, so the taxonomy has one home. Keep its return
strings **byte-identical** to today's (`"probe/not-found"`, `"target/locked"`, …,
`f"runtime/{type(exc).__name__}"`) — they are written into `.firm` event records
and a changed string is a silent evidence-format change.

**Policy refusal → trail only, never S-1 (F-7, AC-2).** Match by type:
`PolicyRefusal` (covers `PlanRefusal`), `SetupWorkflowError`,
`RegisterPreconditionError`, `BatchValidationError`, `BoardBusyError`.

**Every `ToolError` raised by `registry.call_tool` itself must also be here.**
Because `begin()` runs before `require_unlocked` (§10.2), all of them reach the
classifier, and any that falls through to the default branch files as an S-1
*server defect* — false entries straight through AC-2, the gate the spec says
nothing else matters without. The complete set, with the classification each takes:

| `ToolError` message shape | Source | Classification |
|---|---|---|
| `^Tool '.+' is locked` | `require_unlocked`, `registry.py:188` | policy refusal |
| `^Unknown tool: ` | `_require_definition` / `get_tool`, `registry.py:196,319` | policy refusal, **triage `agent_behavior`** (this is S-5, not a defect) |
| `^Guarded tool '.+' requires a non-empty board_id` | `registry.py:309` | policy refusal |
| `^Tool '.+' cannot accept an on_exit finalizer` | `registry.py:325` | policy refusal |
| finalizer `ValueError` passthrough | `registry.py:329` | policy refusal |

Matching on message text is fragile, so pin it: add a test that raises each of
these five through a real `call_tool` and asserts the classification, so a reworded
message fails the test rather than silently producing false defect reports.

**Environment fault → S-3 (A-7, W-4, AC-8):** `ProbeNotFoundError`,
`LockedTargetError`, `TargetConnectionError`, `BoardNotConnectedError`,
`serial.SerialException`, plus `FileNotFoundError` raised from a native build.
Move the existing `_error_code` mapping (`server.py:327-342`) into this module and
have `server.py` import it, so the taxonomy has one home.

**Unexpected → S-1 (F-6, AC-1):** everything else, explicitly including
`OperationTimeoutError` (deadline termination of an unreturned worker) and
`OperationCleanupError` (unconfirmable provider closure / cleanup failure).

**`classify_result`** handles A-6's non-error refusals: text starting with
`Refused [` (the `_format_refusal` shape, `server.py:784`), JSON whose `status`
ends in `_refused`, and the `no board` sentinel.

`classify.py` **must not import `NO_BOARD_CONFIG_MESSAGE` from `server.py`** —
`server.py` imports `classify`, so that is a circular import. Hold a local
fragment constant (`"No project board profile is loaded"`) and add a test asserting
it is a substring of `server.NO_BOARD_CONFIG_MESSAGE`, so the two cannot drift
apart silently.

It extracts the
named remedy when the payload carries a `remedy` key or a `Call '<tool>' first`
clause — F-4 wants the remedy recorded, and §1.1 turns on whether one exists.

**`error_signature`** = `f"{type(exc).__module__}.{type(exc).__name__}"` plus the
message with digits, `0x…`, GUIDs, and path-like runs replaced by `#`. This is the
grouping input (F-24) and must not vary between runs.

**Verify step 4:** a table-driven test asserts every refusal type maps to
`POLICY_REFUSAL` and produces no report — this is AC-2, the primary gate.

---

**Verify step 4:** the classification table test above passes and `error_code` returns
strings byte-identical to the pre-change `server._error_code` for every branch.

---

## Step 5 — `monitor/reports.py`

```python
def build_report(signal, triage, *, tool, board, ctx, trail, counters,
                 narrative=None, error=None) -> dict
def grouping_key(signal, triage, tool, anchor) -> str
class Deduper:
    def admit(self, key: str) -> tuple[bool, int]   # (emit?, suppressed_since)
```

`grouping_key` = `blake2b(f"{signal}|{triage}|{tool}|{anchor}")` where `anchor` is
the error signature or the refusal code. **No `run_id`, no timestamp, no
`board_id`** — `run_id` is banned by F-24, and keeping `board_id` out is what makes
AC-8's "one grouped report per unplugged probe" hold.

`Deduper` is a per-key window: first occurrence emits; later ones inside the window
increment a counter and re-emit at most once per window carrying the running total
(F-26, W-12).

The report body is §5.1. Every field except the narrative is server-supplied
(F-20). Board scope must record whether the connection identity is hardware-stable
(a provider UID from `gate_manager.live_identity(board).probe_identity`) or
session-local — §5.1 says they are not interchangeable for triage.

---

**Verify step 5:** two reports built in different runs for the same fault share a
grouping key; a third inside the debounce window is suppressed with a running count.

---

## Step 6 — `monitor/thrash.py`

```python
class ThrashDetector:
    def observe(self, board, tool, args_fp, outcome, error_class,
                guard_fp) -> bool        # True == raise one S-2
```

Key `(board, tool, args_fp)`. Fire when count ≥ `THRESHOLD` (4) inside `WINDOW`
(60 s) **and** every occurrence shares an identical `(outcome, error_class)`
**and** `guard_fp` never changed. `guard_fp` is a hash of (active plan id,
remaining calls, gate stamp presence, permission grant id, `list_revision`).

Hard exclusions, each from F-10 (AC-4):

- tool in `{"get_state", "read_execution_state", "get_setup_status", "wait"}`;
- tool ends with `-plan`;
- previous outcome for the key was `BoardBusyError` or `OperationTimeoutError`;
- tool == `"board_validate"` and args equal the last returned `accepted_response`;
- the `board_safety_refresh` → `board_validate` pair.

Paginated reads need no rule: a changed address or length changes `args_fp`.

---

**Verify step 6:** AC-4's three sequences (all-NULL-then-populated plan, a `get_state`
poll loop, a `board_validate` retry reusing `accepted_response`) each produce no S-2,
while four identical failing calls produce exactly one.

---

## Step 7 — `monitor/transport.py`, `monitor/delivery.py`, `monitor/block.py`

### transport.py (F-58…F-63, F-133)

```python
class DeliveryState(str, Enum):
    SENT = "sent"; FAILED = "failed"
    NOT_CONFIGURED = "not_configured"; FILLER_SIMULATED = "filler_simulated"

@dataclass(frozen=True, slots=True)
class DeliveryResult:
    state: DeliveryState
    acked: frozenset[str]        # file identities
    at: datetime | None

class Transport(Protocol):
    name: str
    def send_files(self, paths: Sequence[Path]) -> DeliveryResult: ...
    def send_report(self, report: Mapping) -> DeliveryResult: ...
```

- `NullTransport` — default; always `NOT_CONFIGURED`, empty ACK set.
- `SimulatedRemoteTransport` — the F-59 filler. Copies each sealed file to
  `simulated_remote/<workspace>/` and ACKs it; sends reports as **Sentry
  envelopes** through a `sentry_sdk` client configured with a custom transport that
  writes the envelope to `simulated_remote/<workspace>/reports/`. Returns
  `FILLER_SIMULATED` — **never `SENT`** (F-60, W-18, AC-54).
- `TestTransport` — F-127: `fail_always` plus an injectable anchor timestamp.

**Verified against the pinned `sentry-sdk==2.66.1`:**

- Pass a **`Transport` subclass instance**, not a callable. `make_transport`
  accepts an instance directly; the callable form emits a `DeprecationWarning`
  and is slated for removal.
- **`dsn=None` is fine.** `make_transport` returns a supplied instance regardless
  of DSN, and the resulting client is active — empirically confirmed: a
  `capture_event` with `dsn=None` returns an event id and the envelope arrives at
  the custom transport. No placeholder DSN is needed.
- Subclass `sentry_sdk.transport.Transport` and implement `capture_envelope`,
  `flush`, and `kill`.

Client options: `dsn=None`, `transport=<local writer instance>`,
`default_integrations=False`, `auto_enabling_integrations=False` (keeps the SDK
from installing logging/excepthook hooks — W-2), `send_default_pii=False`,
`attach_stacktrace=False`, `server_name=None`, `max_breadcrumbs=100`,
`release=<package version>`, `environment="filler"`.

Report → envelope mapping: grouping key → `fingerprint`; signal / triage / origin /
build profile → tags; severity → `level`; the board-scoped trail → breadcrumbs;
guard state and workspace token → contexts.

**One stdout hazard to know about:** the SDK wraps the transport in
`_EnvelopePrinterTransport` when `SENTRY_PRINT_ENVELOPES` is set, which prints to
stdout. The Phase 0 fd-dup guard neutralises it, which is a concrete example of
why that guard is structural rather than advisory.

The identity of a file, used for ACKs and dedup (F-56, F-155), is
`f"{workspace_id}:{run_id}:{segment:04d}"`.

### delivery.py (F-52…F-57, F-111…F-118, F-134…F-136, F-159)

One daemon thread, one bounded `queue.Queue`. Never blocks a caller: producers use
`put_nowait` and drop on full (F-27, F-57).

Occasions (F-159, AC-97):

| Occasion | Sends |
|---|---|
| bootup | all sealed files of **prior** runs, after readiness, async (F-116) |
| periodic | sealed segments of the **current** run — never the live file |
| closeout | seals and attempts the final segment, inside the budget |

On ACK: `path.unlink()` the whole file (F-155). Progress in
`server_data/delivery_state.json` — per-file ACK marks, at-least-once (AC-25).
`simulated_remote/` is **never** drained (F-133).

### block.py (F-119…F-127)

Anchor at `server_data/delivery_anchor.json`:
`{"at": iso8601, "transport": name, "origin": "filler"|"real"}`.

```python
class BlockState(str, Enum):
    DORMANT = "dormant"; ARMED = "armed"
    TRIPPED = "tripped"; CLOCK_UNUSABLE = "clock_unusable"

def evaluate(anchor: Anchor | None, now: datetime | None) -> BlockState
```

Order matters and is the requirement, not a detail (F-122, N-9): **if there is no
anchor, return `DORMANT` before computing any elapsed time.** Reading a missing
anchor as infinitely stale inverts F-122 and bricks a fresh install on its first
operation. If `now` is unreadable or the elapsed value is negative/absurd, return
`CLOCK_UNUSABLE` and log — never trip (F-120, AC-66).

Threshold is exactly 14 days (F-120). `TRIPPED` raises a `PolicyRefusal` whose
message **names its remedy**, so §1.1 classifies it as correct behavior and the
monitor must not self-report it (F-121). Build no "N consecutive failures"
detector on top — the block is the enforcement (F-85).

**`check_block()` must do zero I/O.** It is called from
`_enforce_guarded_invocation`, which `dispatch` invokes as `before_execution` —
and in the sync path `run_synchronous` calls that *after* acquiring the reservation
lock, the board worker lock, and the execution lock. So `check_block()` executes
**inside a held board lock on every guarded call**. Reading `delivery_anchor.json`
from disk there would put file I/O on the hot path inside a lock, violating N-3.
Hold the anchor in memory: load it once at boot, and let the delivery thread update
the in-memory copy whenever it writes a new one. `check_block()` is then a
timestamp comparison against a cached value and nothing else.

This placement still satisfies F-125(b): the lock is held but no hardware
operation has started, so the refusal defers the *next* op rather than interrupting
one in flight. Note the distinction from recording — recording is provably outside
every lock (it happens after `dispatch` returns), whereas the block deliberately
sits inside one. That is why the block may compare a cached value and must do
nothing else.

---

**Verify step 7:** with `SimulatedRemoteTransport`, a sealed file is copied to
`simulated_remote/` and unlinked from `server_data/`, transport state reads
`filler_simulated` (never `sent`), and `evaluate(None, now)` returns `DORMANT`.

---

## Step 8 — `monitor/monitor.py`

```python
@dataclass(frozen=True, slots=True)
class MonitorContext:
    """Read-only callables injected by server.py. Never imports server.py."""
    run_id: str
    run_started_at: datetime
    server_version: str
    advertised_tools: Callable[[], tuple[str, ...]]
    list_revision: Callable[[], int]
    active_plan: Callable[[str, str], object | None]
    active_grant: Callable[[str, str], object | None]
    gate_snapshot: Callable[[str], object | None]
    live_identity: Callable[[str], object | None]
    connection_id: Callable[[str], str | None]

class IssueMonitor:
    def begin(self, tool, arguments, board) -> Observation | None
    def bind_workspace(self, path: Path | None) -> None
    def boot(self) -> None
    def closeout(self, reason: str) -> None
    def health(self) -> dict
    def check_block(self) -> None            # raises PolicyRefusal when TRIPPED
    def submit_report(self, form: Mapping) -> dict
    def submit_checkin(self, form: Mapping) -> dict

class Observation:
    def completed(self, result: object) -> None
    def failed(self, exc: BaseException) -> None

class NullMonitor:
    """Same surface, every method a no-op. Used when construction fails (§10.6)."""
```

`NullMonitor.begin()` returns `None`, `check_block()` returns without raising, and
`health()` reports monitoring as unavailable with the reason — never silently
pretends to be healthy.

**`action_batch` accounting.** A failing child raises inside its own `call_tool`
re-entry, so the *child* observation records the failure and files any report.
`build_batch_handlers` then catches it and returns a `"status": "batch_failed"`
payload, so the *parent* `action_batch` call classifies as a success. That is
correct, not a gap — the failure is already recorded once at the child, and
counting it twice would inflate counts and double-report (W-6). Do not add
special-case handling to "fix" it.

Every public method except `check_block` is wrapped internally in
`try/except BaseException` and swallows (N-6, W-8). A failure inside the monitor
**never** produces a report about itself and is counted only.

`Observation.completed/failed` do, in order: classify → trail append → counter
record → ledger append (which bumps `total_appended`) → thrash check → report if
warranted → enqueue delivery. Every one of these is non-blocking; the only
synchronous work is a dict build and two `deque`/dict updates (N-3, F-27).

`completed(result)` must run the result through `redaction.result_text()` before
classifying — the raw value is a `list[ContentBlock]`, not a string (§1.4).

**`begin()` must not retain the raw `arguments` mapping.** It computes
`fingerprint(arguments)` immediately and keeps only the fingerprint plus the tool
name and board. Holding a reference would park memory contents, UART bytes, and
absolute paths in a live buffer for the length of the call — exactly what F-3
bans, and W-11 says the exposure here is worse than in a generic server.

The 500-call tick (F-129) fires inside `record`: produce the summary, roll the
ledger segment (F-158), enqueue periodic delivery, and — personal builds only — set
the one-shot check-in prompt flag.

---

**Verify step 8:** `health()` called twice returns identical output apart from uptime,
and writes nothing.

---

## Step 9 — `monitor/narrative.py`, `monitor/tools.py`

### narrative.py — pydantic models, personal builds only

`IssueReportForm` per §5.2: `codebase_objective`, `hypothesis`, `goal`, `plan`,
`failure_point{action_taken, observed_result, named_step}`, optional
`signal_subcase` validated against the signal's allowed enum (required for S-6 and
S-7), `recent_actions: list[RecentAction]` with `max_length=5, min_length=1`
(each `{action, result, code_context}`), `earlier_phases: list[str]`,
`session_start: str`.

`CheckInForm` per §5.3: `codebase_summary`, `work_summary`,
`tools_used: list[{tool, purpose}]`, `effectiveness_observed`.

`model_config = ConfigDict(extra="forbid")` on both — F-19 wants malformed input
rejected, and `extra="forbid"` is how the repo already does this
(`tools/batch.py:16`). Every string field carries a `max_length`. Every string
field runs `check_narrative` — **not** `scrub_mechanical`; the narrative bar
deliberately allows real code names (F-153).

`effectiveness_observed` additionally rejects self-rating language (§5.3 prohibits
self-grading outright, so it is a rejection, not a warning).

### tools.py — the three agent-facing MCP tools (§5.6)

```python
def build_monitor_tools(monitor: IssueMonitor) -> dict[str, Callable[..., str]]
```

Returns `report_agent_issue`, `server_health_check`, and — **only when
`NARRATIVE_LOGGING`** — `submit_routine_checkin` (F-140: absent, not disabled).

Each tool's docstring is its MCP description (`server.py` passes
`description=_handler.__doc__`), so per the design charter §5 each must state what
it does, when to reach for it, its parameters, what it returns, and its common
failure modes with the recovery step. For `report_agent_issue` that includes the
§1.1 rule stated outright — *a refusal that named a workable remedy is not
reportable* — because the docstring is what an agent reads before deciding to call
it, and W-1 is the risk that decides this project.

- **No `board_id` parameter on any of them.** That is what gives F-18/F-49/F-72
  for free: `call_tool` resolves `board_id=None`, so `manager.worker_lock(None)`
  returns `nullcontext()` and no board lock is taken.
- Never pass them to `mcp.configure_layer2` or `mcp.configure_guarded_dispatch`,
  and never call `tool_registry.configure(...)` on them — that is what keeps them
  always-visible, unlocked, plan-free, and budget-free (F-17).
- Registered at import time, before any client connects, so their `list_revision`
  bumps cannot cause `tools/list_changed` churn (F-21, W-15).
- `server_health_check` is side-effect free (F-71, AC-29): it reads
  `counters.snapshot()`, ledger head/resident count/hardening, store and workspace
  binding state, transport state, anchor + origin, `narrative_logging`, and block
  state. It writes nothing and sends nothing.
- In a professional build `report_agent_issue` stays **registered and callable**
  and returns the F-146 message naming its remedy. It authors, stores, and sends
  nothing.

---

**Verify step 9:** with `NARRATIVE_LOGGING = False`, `build_monitor_tools` returns two
tools and `report_agent_issue` returns the F-146 message; with it `True`, three tools
and a malformed form is rejected by pydantic.

---

## Step 10 — wiring (the only edits to existing files)

### 10.1 `kernel/registry.py` — stdout containment (N-1, W-2, A-1, AC-10)

Replace the body of `run_stdio_async` (currently lines 406-417):

```python
async def run_stdio_async(self) -> None:
    protocol = None
    try:
        sys.stdout.flush()                      # nothing buffered may leak post-dup2
        protocol_fd = os.dup(sys.stdout.fileno())
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        protocol = anyio.wrap_file(
            io.TextIOWrapper(
                io.FileIO(protocol_fd, "w", closefd=True),   # FileIO has no "wb"
                encoding="utf-8",
            )
        )
    except (AttributeError, OSError, ValueError):
        protocol = None                         # no usable fds; fall back below
    try:
        async with stdio_server(stdout=protocol) as (read_stream, write_stream):
            await self._mcp_server.run(
                read_stream, write_stream, self.create_initialization_options()
            )
    finally:
        operation_manager.cancel_all("stdio client EOF or server shutdown")
```

Three details that are each a real defect if missed:

- **`io.FileIO` takes `"w"`, not `"wb"`.** `FileIO` is always binary and raises
  `ValueError` on a mode containing `b`.
- **`flush()` before `dup2`.** Anything already buffered in `sys.stdout` would
  otherwise be written to the *new* fd-1 target.
- **The whole redirect is guarded.** Under `pythonw`, an embedded host, or a
  launcher that hands us non-file stdio, `sys.stderr` may be `None` or lack
  `fileno()`. An unguarded `dup2` would kill the server during startup, which is
  precisely the failure class W-14 says this system cannot report. On failure
  `protocol` stays `None` and `stdio_server` falls back to its own
  `sys.stdout.buffer` — the pre-existing behavior, no worse than today.

After the `dup2`, nothing writing to fd 1 can corrupt framing — not a stray
`print`, not a logging handler, not the Sentry worker thread, and **not an owned
child process that inherits fd 1** (`kernel/processes.py:541`,
`adapters/swd_process.py:235`). That last clause is A-1 and no handler discipline
achieves it.

`stdio_server` already accepts an injected `stdout` and only falls back to
`sys.stdout.buffer` when none is given — verified in the pinned `mcp` package.

### 10.2 `kernel/registry.py` — the monitor hook

Add `self._monitor = None` in `__init__` and `def configure_monitor(self, m)`.

**Do not wrap the body in `try/except/else`.** The existing `call_tool` body
`return`s from inside its `try`, and in Python a `return` inside `try` skips the
`else:` clause entirely — `observation.completed()` would never fire and every
successful call would go unrecorded. **Rename the existing method and add a thin
wrapper instead:**

```python
async def call_tool(self, name: str, arguments: dict[str, Any]):
    board_value = arguments.get("board_id")
    board_id = board_value if isinstance(board_value, str) and board_value else None
    observation = None
    if self._monitor is not None:
        observation = self._monitor.begin(name, arguments, board_id)
    try:
        result = await self._call_tool_inner(name, arguments, board_id)
    except BaseException as exc:
        if observation is not None:
            observation.failed(exc)
        raise
    if observation is not None:
        observation.completed(result)
    return result
```

`_call_tool_inner` is the current `call_tool` body **verbatim**, with its first two
lines (the `board_value` / `board_id` derivation) removed because they move to the
wrapper and are passed in as a parameter. Everything else — `require_unlocked`, the
`revision_before` capture, the guarded-dispatch policy, both `dispatch` calls, the
`except` ladder, and the `finally:` that sends `tools/list_changed` — is unchanged
and stays inside `_call_tool_inner`.

Deriving `board_id` in the wrapper is what lets `begin` run **before**
`require_unlocked`, so a locked-handler refusal is still observed and recorded to
the trail (F-7).

Nested `action_batch` children re-enter `call_tool`, so they are counted
individually (F-38). Tag them via a `ContextVar` depth counter incremented in the
wrapper.

Nested `action_batch` children re-enter `call_tool`, so they are counted
individually (F-38). Tag them via a `ContextVar` depth counter.

### 10.3 `kernel/operations.py` — no change required

An earlier draft added a `ContextVar` publishing each operation's final state so
the funnel could tell a deadline termination from an ordinary failure. **Building
it proved it was not needed:** `dispatch` raises `OperationTimeoutError` and
`OperationCleanupError`, and the classifier already recovers both from the
exception's `__cause__` chain (§4). The `ContextVar` earned nothing and added a
context-propagation trap, so it was removed. Leave this file alone.

### 10.4 `tools/batch.py` — F-17 deny

In `_validate_children`, after the nested-batch check:

```python
if name in MONITOR_TOOL_NAMES:
    raise BatchValidationError(
        f"actions[{index}] names monitor tool '{name}'; monitor tools are not "
        "batchable and must be called directly"
    )
```

Import `MONITOR_TOOL_NAMES` from `monitor.tools`. Today these would be rejected
only incidentally, by the `board_id` requirement; F-17 wants it explicit (AC-22).

### 10.5 `tools/handshake.py` — workspace binding (F-34…F-36, F-164, AC-15)

`register_initialization_handshake` gains an optional `on_workspace` callback.
The tool signature becomes:

```python
def initialization_handshake(workspace_path: str | None = None) -> str:
```

Validate: absolute, exists, is a directory — else return the existing guidance
with a short note that the path was ignored and why. **Never raise**, since this
is the contractual first call.

The path is passed to `monitor.bind_workspace()`, which uses it **only** to compute
the workspace id and flush the F-164 buffer. It is **not** a store root and **not**
a write target (F-30).

### 10.6 `server.py` — composition

Five edits, all additive:

1. **Imports and construction**, after `server_run = create_server_run()`
   (line 287): build `MonitorContext` from existing read-only accessors —
   `tool_registry.advertised`, `tool_registry.list_revision`,
   `plan_engine.active_plan`, `permission_store.active_grant`,
   `gate_manager.snapshot`, `gate_manager.live_identity`, and
   `lambda b: _connection(b).connection_id`. Construct `IssueMonitor` and call
   `mcp.configure_monitor(...)`.

   **Construction must never be able to stop the server from importing:**

   ```python
   try:
       _monitor = IssueMonitor(_monitor_context)
   except BaseException:               # noqa: BLE001 - monitoring never fails closed
       _monitor = NullMonitor()
   ```

   `IssueMonitor` is built at module scope, so an exception here — unwritable
   store, a permissions quirk, a `platformdirs` edge case — would make `server.py`
   fail to import and the server refuse to start. That is monitoring failing
   **closed**, the exact inverse of N-6, and it lands in the startup window W-14
   says this system cannot report. `NullMonitor` implements the same surface with
   every method a no-op, `begin()` returning `None`, and `health()` reporting that
   monitoring is unavailable and why.
2. **Register the three tools** near the other `mcp.add_tool` loops (after line
   2357), with `structured_output=False` like every other tool here.
3. **Staleness gate** as the *first* statement of `_enforce_guarded_invocation`
   (line 647): `_monitor.check_block()`. This is exactly and only guarded hardware
   dispatch, and because `before_execution` runs at a dispatch boundary rather than
   mid-handler, F-125(b) holds structurally.
4. **`_error_code`** (line 327) delegates to `classify.error_code`.
5. **`main()`** (line 5732) — ordered closeout:

```python
def main() -> None:
    require_clean_startup()
    _monitor.boot()
    shutdown = threading.Event()

    def _handler(signum, frame):        # F-148: flag, then unblock. No real work.
        shutdown.set()
        raise KeyboardInterrupt         # MUST raise; see below

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, AttributeError):
            pass                        # not main thread, or signal unsupported
    try:
        mcp.run()
    except KeyboardInterrupt:
        pass                            # normal shutdown; drain runs in finally
    finally:
        for _board_id in connection_manager.assigned_board_ids():
            try:
                disconnect(_board_id)
            except Exception:
                pass
        plan_engine.close_run()
        tool_registry.reset()
        _monitor.closeout("signal" if shutdown.is_set() else "eof")
```

**The handler must raise, not merely return.** F-148's "flag-then-drain" assumes a
main loop that polls the flag; here the main loop is `mcp.run()`, which we do not
control. Installing a SIGINT handler that only sets a flag *replaces* Python's
default `KeyboardInterrupt`, so `mcp.run()` would never unblock and the server
would hang forever instead of shutting down — a worse regression than having no
closeout at all. Raising is still minimal handler work (no locks, no I/O, no
network), so F-148's actual constraint holds: the drain happens in `finally`, on
the main thread, not inside the handler.

Order is the requirement (F-112, F-80, AC-57): hardware released and children
terminated **first**, then the close record written, then the bounded send. The
close record is written inside `closeout()` before it attempts any delivery, so a
failed or slow send cannot cost the record (F-80, AC-33).

`_monitor.boot()` runs before `mcp.run()` and must not block: it writes the boot
record and *enqueues* bootup recovery, which the delivery thread performs after
readiness (F-116, AC-59).

**Verify step 10:** `uv run pytest tests -x` is green and
`uv run python -m pyocd_debug_mcp.server` still answers an MCP `initialize` handshake.

---

## Step 11 — `pyproject.toml`, `.gitignore`

Add `platformdirs>=4` and `sentry-sdk>=2` to `dependencies`. Add `.byo-monitor/`
to `.gitignore` (N-5 — reachable only via the `BYO_MCP_ARTIFACT_ROOT` fallback).

---

**Verify step 11:** `uv sync` resolves and `uv run python -c "import platformdirs, sentry_sdk"` succeeds.

---

## Step 12 — tests

`tests/test_monitor_*.py`, matching the existing `unittest` + in-process-fake style
of `tests/test_server_trust_model_round_*.py`. Assert through
`server_health_check` wherever possible (F-74, AC-34). No hardware.

| File | Covers |
|---|---|
| `test_monitor_stdout.py` | AC-10 |
| `test_monitor_classification.py` | **AC-2 (primary gate)**, AC-1, AC-6, AC-7, AC-8 |
| `test_monitor_thrash.py` | AC-3, AC-4 |
| `test_monitor_trail.py` | AC-12, AC-14, AC-84 |
| `test_monitor_ledger.py` | AC-18…AC-20, AC-35, AC-39…AC-47, AC-91…AC-96 |
| `test_monitor_workspace.py` | AC-98…AC-101, AC-13 placement, no plaintext path in store |
| `test_monitor_counters.py` | AC-29…AC-31, AC-36…AC-38, AC-73 |
| `test_monitor_delivery.py` | AC-23…AC-26, AC-54…AC-60, AC-71, AC-72 |
| `test_monitor_sentry_envelope.py` | fingerprint is restart-stable (F-24), tags carry signal/triage/origin/build, breadcrumbs carry only the failing board's trail, no payload survives into the envelope (AC-14, AC-27, AC-84) |
| `test_monitor_block.py` | AC-61…AC-66, AC-80 |
| `test_monitor_tools.py` | AC-22, AC-29, AC-34, AC-51…AC-53, AC-68, AC-69 |
| `test_monitor_narrative.py` | AC-76…AC-78, AC-81, AC-85…AC-88 |
| `test_monitor_passivity.py` | AC-11, AC-17 |

**Two tests decide whether this ships.** `AC-2`: replay a full correct-guarded
session (locked-tool refusal, all-NULL plan guide, closed gate, containment
rejection, `no board` sentinel) and assert **zero** server-defect reports. `AC-17`:
re-run `tests/baseline_capture.py` against `tests/baseline_transcript.json` with
the monitor enabled *and* with the sink unreachable and the store path invalid, and
assert the transcript is byte-for-byte unchanged.

**Test isolation:** every test points `BYO_MCP_ARTIFACT_ROOT` at a `tmp_path` and
must not touch the developer's real `%LOCALAPPDATA%\BYO`. `resolve_store_root()` is
cached, so expose a `_reset_cache()` for tests to call in `setUp`.
