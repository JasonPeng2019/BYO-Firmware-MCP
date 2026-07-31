# Autonomous Issue Monitor & Report System — BYO Firmware MCP Server

**Type:** Feature & risk specification (WHAT, not HOW)
**Target:** `MCP_Server/BYO-Firmware-MCP` — the `pyocd-debug-mcp` stdio server (BYO Server),
plus a client-side workspace skill, reporting into Sentry.
**Context:** Internal, team-only MCP server — not public-facing.

**About requirement IDs.** `F-n` (feature), `W-n` (risk), `A-n` (assumption), `S-n` (signal),
`AC-n` (acceptance criterion) are **stable identifiers, not an ordering**. They are grouped
by topic, so numbering within a section is deliberately non-contiguous. Every requirement
appears exactly once and states the current position; earlier drafts' superseded wording has
been removed, and §11 records which IDs were retired and why.

---

## 1. Purpose

Provide an autonomous system that detects when BYO Server or the agent interaction
goes wrong and records a structured, human-triageable issue in Sentry. Each report
describes **what was observed to go wrong** — it does not prescribe the fix.
Deciding what to change is the developer's job.

Alongside issue reports the system keeps a complete, code-content-free record of what the
server did — a durable activity ledger, live counters, and periodic health summaries —
which is delivered off-box and drains itself locally as it goes.

The system has **two independent detection origins** converging on **one sink**:

- The **server** detects problems it can see directly at managed dispatch (its own
  unexpected failures, worker/deadline faults, and crude repetition in inbound calls).
- The **model**, via a workspace skill, detects problems only visible in the
  conversation (plan-protocol confusion, unusable guidance, missing capability,
  frustration, abandonment) and hands a summary to the server to record.

### 1.1 The one thing that makes this server different

**BYO Server refuses on purpose, constantly, and correctly.** Locked handlers,
all-NULL plan guidance, closed validation gates, containment rejections, digest
drift, exhausted budgets, and `no board` sentinels are all *the product working as
designed* — the design charter treats naming the remedy as the feature. A monitor
built on the naive rule "error or refusal ⇒ report" will file hundreds of correct
refusals and bury the real signal on day one.

Therefore the central classification rule of this system is:

> **A refusal that names a remedy is not an issue. An issue is when the remedy is
> absent, wrong, unreachable, or when following it does not converge.**

Every signal below is written against that rule.

---

## 2. Boundary Assumptions (these drive every requirement)

- **A-1.** The server runs over stdio only. Stdout is MCP framing and is unavailable
  for any other output. This extends to owned child processes: per-session pyOCD
  provider workers, `native_build` children, and probe-inventory CLI children.
- **A-2.** The server sees inbound tool calls and their results/refusals/exceptions
  at managed dispatch. It does **not** see raw user turns, model reasoning, or the
  conversation.
- **A-3.** The model/client sees the full conversation but not the server's internal
  plan/gate/containment state except where it surfaces in a tool response.
- **A-4.** Reports go to Sentry. This is an internal team tool; captured sessions
  belong to the team itself.
- **A-5.** The team controls both the server and the client/skill layer.
- **A-6.** **Refusal is a first-class normal output** (§1.1). Refusals arrive both as
  `ToolError` and as structured non-error payloads (setup/validation statuses,
  `agent_prompt` prose, friendly choices). Neither form implies a defect.
- **A-7.** **Hardware is in the loop.** Probe disconnects, USB faults, J-Link DLL
  contention, locked or halted targets, and missing toolchains produce genuine
  failures that are **environment conditions, not server defects**.
- **A-8.** **Observation must never become authority.** The architecture is explicit
  that durable `.firm` evidence is evidence only and can never restore plans,
  permissions, assignments, or gates. Monitoring artifacts inherit this rule
  absolutely: nothing this system writes may be read back as authority, and nothing
  it does may alter dispatch order, deadlines, budget consumption, containment, or
  cleanup. The remote-logging staleness backstop of §4.18 is the sole, deliberate
  exception, and the only authority the monitoring layer holds.
- **A-9.** A Server Run identity already exists (`ServerRun.run_id`, `started_at`)
  and is the natural session identity. It does not survive restart, by design.
- **A-10.** A project artifact root already exists (`BYO_MCP_ARTIFACT_ROOT`, else the
  process cwd) and is resolved **once at module import**. It cannot be moved later
  in the process. This governs `.firm` only; the monitoring store is separate and
  late-bindable (§4.8, §4.9).
- **A-11.** **The OAuth-authenticated remote pipeline will not exist when this feature
  ships.** It is planned, not available. Every requirement below must therefore be
  satisfiable with remote delivery entirely absent, and no behavior may depend on a
  successful send. Remote delivery is an optional capability that is off by default and
  arrives later.
- **A-12.** The server is a child process of the client over stdio. When the client exits
  — including when it is killed — the server's stdin reaches EOF and it shuts down on its
  own. This was measured: EOF to process exit in 0.28 s with exit code 0, running both
  shutdown paths. Clean closeout is therefore the *normal* case, not the lucky one. It is
  still not guaranteed (see W-16), and observed clients also signal without closing stdin
  (F-115).

**Non-goals.** Time-based log rotation and retention policies are out of scope. Local
files are removed by exactly one mechanism: **a record is deleted once it has been
successfully pushed to its destination and acknowledged** (F-134). There is no manual
cleanup, no scheduled wipe, and no user-facing cleanup step — clearing local storage is
never the operator's responsibility. Full content redaction/PII scrubbing is also out of
scope on the internal-tool basis — **but see W-11, which is materially stronger for this
server than for a generic one**, and which is why the payload-exclusion rules in F-3 are
mandatory rather than advisory.

---

## 3. Detection Signals (taxonomy)

Each signal describes an **observed symptom**, not a diagnosis. Every report carries
its signal type so triage can group and route. Adding a signal type is adding an enum
value and a trigger description on the single skill path — not a new subsystem.

### Server-detected (deterministic, automatic — present in every build)

- **S-1 · Unexpected runtime error** — the server raised an exception that is not a
  policy refusal: an unhandled exception inside a handler, a provider-worker fault, a
  hard-deadline termination of an unreturned worker, an unconfirmable provider closure
  that retained a DLL/session reservation, or a failure inside managed cleanup.
  *Explicitly excluded:* locked-handler refusals, plan/permission/gate/budget refusals,
  containment refusals, and validation/setup refusal statuses.
- **S-2 · Thrashing** — the same tool with equivalent canonical arguments recurred
  beyond a threshold within a window with no change in outcome and no state
  transition. Primary detection is server-side; the skill may flag suspected thrashing
  as a best-effort backstop only. See F-10 for this server's mandatory exclusions.
- **S-3 · Environment/hardware fault** — a failure whose evidence points at the host or
  the board rather than the code: probe absent or changed, USB/driver fault, J-Link DLL
  contention, target unresponsive or locked, serial port vanished, toolchain or SDK
  missing during a native build. Reported so it is *visible*, tagged so triage does
  **not** treat it as a code defect.

### Model-detected (via the skill; require conversation-level judgment)

These exist **only in a personal build**. A professional build does not produce them
(F-140, F-141).

- **S-4 · Plan-protocol failure** — the model repeatedly failed to satisfy the
  two-step plan contract: submitting flattened action fields instead of the nested
  `action_parameters` object, adding prose/Markdown/wrapper keys, sending extra or
  missing fields, using placeholders or a partial-NULL request, or attaching
  `user_permission` to a non-permission plan. Two or more rejected submissions of the
  same plan tool is the trigger. *This is the highest-value model signal for this
  server* — it is the exact seam where a well-behaved agent still gets stuck.
- **S-5 · Discovery/binding failure** — the model called an unlisted, hidden, or
  relocked action; ignored `notifications/tools/list_changed`; failed to use the exact
  returned single-child `action_batch` fallback; edited that fallback's board, child
  name, or arguments; or combined a primary action with a paired repair.
- **S-6 · Guidance not followed / guidance not usable** — the server returned an exact
  next call (`load_call`, `next_call`, `plan_initialization_call`,
  `plan_action_parameters_template`, `accepted_response`, `preferred_call`) and the
  model did not or could not use it. Distinguish the two subcases in the summary:
  the model ignored usable guidance, or the guidance was ambiguous, incomplete, or
  contradicted actual tool behavior.
- **S-7 · Remedy dead-end** — a refusal arrived *without* an actionable remedy, or the
  named remedy was followed and produced the same refusal again, or setup/validation
  cycled (research request → candidate → refusal → research request) without
  converging. This is the correct home for most "it refused me" observations, and it
  is what separates a working guardrail from a broken one.
- **S-8 · Coverage gap** — the user asked for a reasonable board operation for which
  the server exposes no tool and no route at all. Distinct from S-7: a gap is *no path
  exists*, not *the path refused me*.
- **S-9 · Unusable output** — a tool returned without refusal but produced the wrong
  shape, empty or missing data where data was expected, truncated content, or output
  too large to use (large memory reads, long UART captures, oversized setup payloads,
  full tool-index renderings).
- **S-10 · Safety surprise** — a guarded action was correctly refused by containment
  in a way the user reasonably expected to succeed: an incomplete or missing memory
  map, an UNKNOWN span, a prohibited-region write, a write-only peripheral read,
  artifact digest drift, or a partition policy that grants no deployment authority.
  Usually **not a defect** — it is the highest-value product-feedback signal this
  server has, and must be tagged so it is triaged as product feedback, not as a bug.
- **S-11 · Abandonment / safety bypass** — the agent stopped using the server and
  routed around it: driving `pyocd`/`openocd`/vendor CLIs directly, hand-editing
  `.firm` contents, or completing the hardware task outside the guarded path. **Treat
  as the most severe model-detected signal** — it means the safety model was not
  merely inconvenient but was circumvented.
- **S-12 · Relay-boundary violation** — the model exposed structured payloads,
  continuation tokens, internal field names, board/connection IDs, or digests to the
  user, or asked the user to invent or repeat them, contrary to the client contract.
- **S-13 · User frustration** — the user expressed dissatisfaction (said something is
  broken, repeated or rephrased a request, contradicted the agent). Soft signal; may
  not correspond to a server defect and must be triaged as such.
- **S-14 · Explicit user report** — the user directly stated something is broken or a
  capability is missing.

---

## 4. Required Feature Areas

### 4.1 Recent-activity context capture

- **F-1.** Maintain a rolling record of recent tool activity — tool name, canonical
  argument fingerprint, outcome class, timing — bounded to a fixed maximum (~100
  events). This buffer size is independent of the periodic summary cadence (F-129) and
  keeps its own value.
- **F-2.** Every report, regardless of origin, must carry this trail.
- **F-3.** Trail entries store **identifiers, digests, and fingerprints only — never
  payloads**. Specifically excluded from the trail, the ledger, and every report body:
  memory read/write contents, UART capture bytes, datasheet bytes, full ELF/HEX/map
  contents, full native-build argv and environment, and full absolute host paths. Probe
  serials, device unique IDs, and MCU part numbers are recorded as digests or truncated
  identifiers. This is a hard content rule, not a size optimization (W-11). Fingerprints
  must be salted per F-149. The model-authored narrative of a personal build is governed
  by the separate, narrower bar of F-153.
- **F-4.** Trail entries must record this server's distinguishing facts:
  `board_id`, the live connection identity token, whether the outcome was a **success,
  a policy refusal, or an unexpected error**, the refusal's named remedy when present,
  and any observed transition in plan / permission / gate / tool-visibility state.
- **F-5.** The trail must be **board-scoped**. Same-board calls serialize but different
  boards run concurrently; a report must carry the trail for its own board and must not
  interleave another board's activity into it (see W-10).

### 4.2 Server-side runtime detection

- **F-6.** Unexpected server failures are captured automatically at the single managed
  dispatch funnel, without depending on the model to notice them.
- **F-7.** Capture must **classify before reporting**: policy refusals (locked handler,
  plan/permission/gate/budget/freshness, containment, digest drift, `no board`
  sentinel, terminal validation status) are recorded to the trail but do **not** raise
  an S-1 report. Only unexpected exceptions, worker faults, deadline terminations, and
  cleanup failures do.
- **F-8.** A captured runtime error must be enriched with the trail (F-2), the failing
  tool identity, and the board/connection scope.

### 4.3 Deterministic thrashing detection (server-side backstop)

- **F-9.** The server independently detects repetition — same tool plus equivalent
  canonical arguments recurring beyond a threshold within a window — and raises a
  report **without relying on the model's self-awareness**.
- **F-10.** The detector must not fire on this server's legitimate repetition patterns:
  - the all-NULL plan call followed by the populated plan submission (same tool, twice,
    by design);
  - polling loops using `get_state`, `read_execution_state`, `get_setup_status`, or
    bounded `wait`;
  - paginated or windowed memory/symbol reads while watching a value;
  - retries after a board-busy or timeout condition;
  - a validation retry that reuses the server-returned `accepted_response`;
  - the deliberate `board_safety_refresh` → `board_validate` sequence.

  Repetition alone is not thrashing; **repetition with an identical outcome and no
  state transition** is.

### 4.4 Model-side behavioral detection (the workspace skill)

Personal builds only (F-140).

- **F-11.** A skill exists that the model invokes when it observes any conversation-only
  signal (S-4 … S-14), and optionally suspected thrashing as a best-effort backstop
  to S-2.
- **F-12.** Invocation criteria must be **concrete and enumerated per signal**, with
  BYO-specific examples — a rejected plan envelope, an unlisted-tool call, a remedy
  that repeated itself, a containment refusal on an unmapped span — not an open-ended
  list.
- **F-13.** The skill must encode the §1.1 rule explicitly: a refusal that names a
  workable remedy is **not** reportable. The skill's negative examples matter as much
  as its positive ones.
- **F-14.** On invocation the skill produces a structured summary (§5) and passes it to
  the server's intake tool.
- **F-15.** All model-side signals share the one skill invocation path; the model
  supplies the signal type. No additional server code path per signal.
- **F-144.** The skill and its criteria are written **once, independent of any AI tool**
  — Codex, Claude Code, or any other agent. There is a single shared criteria file and
  no tool-specific skill variants.
- **F-104.** The skill must ship the **exact template(s) the model fills in**, enumerated
  per signal type — not a freeform prompt. This is what makes the untrusted-input
  validation of F-19 tractable (W-7): the server can size-bound and shape-check a fixed
  schema far more reliably than free prose. The two templates are specified in §5.2 and
  §5.3, and their shapes must be visibly different from each other so neither can be
  mistaken for the other.

### 4.5 Report intake (the MCP tool the skill calls)

- **F-16.** The server exposes an intake capability that accepts a model-produced
  summary and records it as an issue in the sink.
- **F-17.** Intake must be **structurally outside the safety surface**: always visible,
  never hidden or locked, requires no plan, consumes no plan or permission budget,
  requires no `board_id`, touches no hardware, opens no connection, and is not a
  guarded-dispatch tool. It must not be usable as an `action_batch` child.
- **F-18.** Intake must not participate in per-board serialization or hold a board
  execution lock, so filing a report can never stall or be stalled by hardware work.
- **F-19.** Intake must **validate and size-bound** the incoming summary. Model output
  is untrusted and may be malformed, oversized, or partly wrong. It must reject
  content that violates the applicable content bar of F-153.
- **F-20.** Intake attaches the same trail (F-2) and server-side metadata the automatic
  path uses, so model-origin and server-origin reports are consistent. The trail, guard
  state, board scope, grouping identity, mechanical anchors, and environment are always
  **server-supplied and never accepted from the model**.
- **F-21.** Registering intake must not perturb the dynamic discovery surface: it is a
  stable always-advertised tool and must not cause spurious `tools/list_changed`
  churn.

### 4.6 Classification, grouping & noise control

- **F-22.** Every report is tagged with its signal type (§3), a severity, and its origin
  (server-auto / server-thrash-detector / model-skill).
- **F-23.** Every report must additionally carry a **triage class** distinguishing
  *server defect* / *product feedback* (S-10, S-8) / *environment fault* (S-3) /
  *agent-behavior issue* (S-4, S-5, S-6, S-11, S-12) / *soft signal* (S-13). Without
  this the team will chase code changes for USB faults and correct guardrails.
- **F-24.** Reports carry a stable grouping identity so duplicates of the same
  underlying issue collapse rather than flooding the sink. Grouping must be stable
  across Server Runs — `run_id` changes every restart and must not enter the grouping
  key.
- **F-25.** Co-occurring signals collapse into a single report (e.g. a frustrated user
  reporting a plan-envelope failure that led to abandonment is one report, not three).
- **F-26.** Rate-limit / debounce so a single loop, a single recurring exception, or a
  single unplugged probe cannot generate a storm.

### 4.7 Delivery reliability

- **F-27.** Recording a report must not block or measurably delay tool execution.
  Delivery is best-effort and asynchronous, and must run **outside** the operation's
  hard deadline — never inside a board execution lock, never inside a non-interruptible
  flash transaction, and never in the critical path of managed cleanup.
- **F-28.** On clean shutdown — including stdio EOF, which is a normal end condition
  here — pending records flush before exit, without delaying connection teardown,
  owned process-group termination, or reset release.
- **F-29.** Delivery failure (no network, sink unreachable) degrades gracefully and
  never crashes, stalls, or fails-open the server.
- **F-106.** The latency bounds of F-27 and N-3 govern the **server's** recording of a
  report. The compute cost of the **model** authoring an intake form is client-side, is
  incurred in the agent loop, and is **outside** these bounds. This is a further reason
  the §1.1 classification rule and the collapse rule (F-25) matter: they prevent the
  model paying authoring cost on correct refusals.

### 4.8 Local persistence and storage layout

- **F-131.** The store lives in the platform's **per-user application-data directory**,
  under an app folder named **`BYO`**, with `server_data/` and `simulated_remote/`
  beneath it. Per OS:
  - **Windows** — `%LOCALAPPDATA%\BYO\` (i.e. `C:\Users\<user>\AppData\Local\BYO\`);
    Local, not Roaming, so the spool never syncs across machines.
  - **macOS** — `~/Library/Application Support/BYO/`.
  - **Linux** — `$XDG_DATA_HOME/BYO/`, defaulting to `~/.local/share/BYO/`.

  These must be resolved through a standard app-dirs mechanism (e.g. Python
  `platformdirs`, Rust `directories`, Node `env-paths`), never hardcoded. The store is
  **per-user** — each developer's activity under their own user account — not
  per-installation. If any part of it can land inside a repository it must be
  git-ignored.
- **F-161.** Within the per-user store, runs are filed **per workspace**:
  `<app-data>/BYO/server_data/<workspace_id>/<run_id>.<segment>.jsonl`, with
  `simulated_remote/` mirroring the same structure. Without this, every project a
  developer works on interleaves into one undifferentiated set of runs. Board-scoping of
  report *content* (F-5) is a separate concern and is unaffected.
- **F-162.** Workspace identity is a **stable random token, never derived from the
  path**. Two distinct identifiers exist and must not be conflated:
  - the **local directory name** is a salted digest of the workspace path (F-149). Its
    purpose is **auditable anonymization**, not secrecy from the owner: it guarantees no
    plaintext project path is ever written to disk or carried in anything that could be
    listed, shipped, or screen-shared, so the owner can open the store and confirm at a
    glance that each workspace is represented by an anonymized name. Per F-150 this hides
    nothing from the owner — who can of course map their own digests back — it guarantees the
    plaintext path is nowhere in the artifact.
  - the **pushed identifier** is an opaque random token generated once per workspace and
    persisted inside that workspace's folder. It carries **zero** path information, so the
    owner can see exactly what will represent the repo off-box and confirm it is fully
    anonymized, and it is safe to deliver under F-3.

  Deriving the pushed identifier from the path — even hashed — is forbidden: a workspace
  path is a small, guessable input, which is exactly the F-149 reconstruction channel.
- **F-163.** Cross-machine project correlation is a **receiver concern**. Because the
  pushed token is random, the same project on two machines produces two tokens, and no
  local mechanism may attempt to reconcile them by hashing paths, repository URLs, or any
  other guessable value. Grouping them into one project is done at the remote, by operator
  labelling or explicit registration.
- **F-30.** Every report is persisted locally as a file, one file per report, in addition
  to being delivered to Sentry. It is written in the run's per-workspace area of the per-user
  store — `<app-data>/BYO/server_data/<workspace_id>/` (F-131, F-161) — never inside the
  workspace project directory itself.
- **F-31.** Monitoring output must live **outside `.firm/`**. `.firm` is the trusted
  safety-evidence root with its own atomic-write, immutability, and no-persisted-authority
  contract; monitoring output is model-authored and untrusted (W-7), rate-limited, chatty,
  and self-deleting — none of which may touch the root that governs safety decisions (A-8).
- **F-32.** Nothing the server does may read a monitoring report back. Reports are
  write-only evidence and can never restore or influence plans, permissions,
  assignments, gates, or map authority (A-8).
- **F-33.** Local files require no time-based rotation or retention policy (§2
  Non-goals). They are removed only by successful push and acknowledgement (F-134),
  automatically and without any operator action.
- **F-132.** Application-data directories are **user-writable by definition** and created
  on demand, so the read-only-install problem largely disappears. The loud-fail path and
  the operator override (`BYO_MCP_ARTIFACT_ROOT`, §4.9) are retained as a **last-resort
  fallback** for locked-down or sandboxed machines — the rare case, not the expected one —
  and logging must still **never silently no-op** (F-37).
- **F-133.** Delivery destinations are named. The real transport delivers to the remote;
  the **filler delivers to `simulated_remote/`** under the same `BYO` app directory,
  which is **persistent, not temporary**. It is the stand-in for off-box storage during
  the filler era and must be **excluded from every deletion path** — it is a destination,
  not spool. Only `server_data/` is ever drained (F-134).

### 4.9 Session initialization and workspace binding

- **F-34.** The workspace path must be obtainable without adding a second "call this
  first" tool. `initialization_handshake` is already the contractual first call and is
  the natural place to accept an optional workspace path.
- **F-35.** The workspace path governs **logging only**. It must not move, reinterpret,
  or shadow the `.firm` artifact root, which is resolved at import and is not
  re-bindable (A-10).
- **F-36.** Any agent-supplied path is untrusted and must be validated before use —
  absolute, existing, and a directory.
- **F-37.** Log-writing must **never silently no-op** because a path is unset. If no
  valid destination is available, records must buffer or the write must fail loudly, and
  the system must be able to re-request the path rather than discard records.
- **F-164.** Binding is **late**, and records written before it must not be lost. The
  workspace path arrives on the handshake, but the boot record (F-76) is produced before
  it. Pre-binding records are **buffered in memory** and flushed into the workspace's
  file once the path is known. If no workspace is ever bound, the run's file is written
  under a literal `unbound/` workspace and delivered as such — honestly labelled, never
  discarded.

### 4.10 Activity ledger (the durable tool-call record)

Distinct from the bounded in-memory trail of §4.1. The trail is *context attached to a
report* and is capped at ~100 events; the ledger is the **complete record of what this
server did**, kept whether or not anything went wrong.

- **F-38.** The system must maintain a durable, append-only ledger of **usage snapshots
  (every 100 calls, §4.11), check-ins (every 500 calls in a personal build, §4.12), problem
  reports, and boot/close records** — **not** one record per tool call. Each usage snapshot
  carries the run's **cumulative** counts (F-165), so the ledger answers "how much was used,
  by which tools, with what outcomes" without a per-call entry. Per-call *sequence* context
  is kept only in the bounded in-memory trail (F-1) and is attached to problem reports (F-2);
  it is deliberately not durably logged, because problem-watching needs the run-up to a
  failure, not a permanent record of every individual call.
- **F-39.** The ledger must be **tamper-evident**: each record bound to its predecessor so
  that modifying or deleting any record is detectable after the fact. The guarantee is
  narrow and is stated honestly in §4.10.1. The requirement is detectability, not
  prevention.
- **F-40.** Ledger durability must come from the append itself, not from any shutdown
  path. A record is safe once written; nothing may depend on a later flush to make it so.
- **F-41.** The content rules of F-3 apply in full: identifiers, digests, and fingerprints
  only. The ledger is the largest and longest-lived body of data this system produces and
  is therefore where a payload leak would do the most damage.
- **F-42.** Each usage snapshot must record enough to answer, for the Server Run so far:
  which tools ran, how many times (cumulative), with what outcome distribution, and which
  advertised tools were never exercised. Because the counts are cumulative (F-165), the
  latest delivered snapshot alone answers this — no replay of earlier snapshots is needed.
- **F-81.** Every ledger record must carry the identity of the run that produced it, so
  it is comparable to the per-run counter (§4.15.1).
- **F-86.** The tamper-evidence chain must be scoped **per run**, not shared across runs.
  Multiple server instances can share one store, and a single store-wide chain would let
  concurrent writers break each other's chains and produce false tamper findings. F-154
  makes this literal: one run writes its own file, so a concurrent writer cannot touch
  another run's chain at all.

#### 4.10.0 File granularity — one sealed file per run segment

- **F-154. A run writes its own file and never rewrites it.** Records are appended for
  the life of the run (or segment, F-158); when the run or segment ends the file is
  **sealed** and never modified again. No record is ever removed from a file, and no file
  is ever rewritten in place. One file, one chain, genesis to close record.
- **F-155. Delivery, acknowledgement, and deletion are all whole-file.** The transport
  pushes a complete sealed file; the destination acknowledges it by identity; on ACK the
  local file is **unlinked**. The stable identity required by F-56 is the file's
  `(workspace_id, run_id, segment)` triple.
- **F-156. Chains never span a deletion boundary.** Because of F-154/F-155, a deleted
  file leaves **no dangling back-link**: whatever remains locally is a set of complete,
  independently verifiable chains, so local verification is **total for every file
  present**, not windowed. A verifier needs no special knowledge of deleted records — an
  absent file is simply absent, which F-93 treats as routine. Full-history integrity
  lives with the delivered copies, per the spool philosophy of §4.10.2.
- **F-157. The append-only hardening must remain applied for a file's entire life.**
  Record-level deletion would require rewriting the file, forcing the system to remove its
  own append-only protection, rewrite, and re-apply it — destroying the protection exactly
  when it was doing work. Under whole-file semantics the only operations are *append*
  (during the run) and *unlink* (after ACK), both of which an append-only ACL permits. No
  code path may weaken, strip, or re-apply the hardening in order to delete or compact
  records.
- **F-158. Long runs are segmented, and the segment cadence is the only-local window.**
  A file still being appended to cannot be pushed or deleted, so the only-local window
  would otherwise equal the entire run duration. A run therefore **rolls to a new
  segment** at a configurable boundary (default: the 100-call snapshot tick of F-129),
  sealing the current file and opening `<run_id>.<segment+1>`. Each seal aligns with a usage
  snapshot, so every ~100 calls a sealed file is pushed and its ACK refreshes the anchor
  (F-120). Each segment is a complete chain in its
  own right and carries the **head hash of its predecessor** in its genesis record, so the
  run's chain remains verifiable end to end wherever the full set exists. A segment whose
  predecessor was delivered and deleted is normal and is never a finding.

#### 4.10.1 What the chain does and does not detect

The chain of F-39 must not be over-claimed. Its guarantee is narrow, and the boundary is
exactly the offline case.

**A chain alone does not survive offline tampering.** The hash function is public and
there is no secret, so anyone with the file, the algorithm, and the server stopped can
edit a record and **recompute every subsequent link**, producing a chain that verifies
perfectly. They can equally truncate the tail and leave a valid shorter chain. The chain
detects *localized* modification — a line edited in a text editor, a partial write, disk
corruption — and nothing more.

- **F-87.** The specification, the implementation, and its documentation must state this
  limitation explicitly wherever the chain is described. The chain is an
  accident-and-corruption detector. It is not a defense against a person who wants to
  alter the record and can stop the server first.
- **F-88.** A secret key held on the same machine must not be treated as a fix. The
  server runs as the user, so a local key is readable by exactly the party a keyed chain
  would need to exclude (W-17). Do not add an HMAC and describe the result as secure.
- **F-166. What the snapshot chain does and does not do against under-reporting.** The
  motivating threat is a user — a personal user in particular, who owns their machine —
  pretending they used the tool less than they did. State the defense honestly, in three
  tiers: (1) **Casual under-reporting is defeated** by cumulative counts (F-165) plus the
  2-week block (§4.18): staying offline or dropping snapshots cannot lower the total, because
  the next snapshot the block forces out still carries the true running total, and the per-run
  chain (F-86) makes a decrease or a gap in the snapshot sequence detectable. (2)
  **Deliberate post-hoc editing is detection-not-prevention, and only once a real remote
  exists.** A machine owner can edit a snapshot's count down and recompute the chain (F-87,
  W-22); this is caught only by an off-box witness that recorded the delivered head (F-89,
  F-160), which does not exist in the filler era (F-103) — so the real under-report defense
  **turns on at OAuth cutover (F-124)**, not before. (3) **Source-level forgery is neither
  prevented nor detected, in any era:** a user who tampers with the counter or the server
  binary can emit a false-low count at the source, and no chain over the output can catch a
  lie told before the record was written (F-88). Any use of these counts that is adversarial
  — usage-based billing, for instance — must be documented with this ceiling; the number is
  **not** unforgeable by the machine's owner.
- **F-89.** **Only an external witness upgrades this.** Publishing the chain head off-box
  makes offline rewriting detectable, because a forged local chain will not match the head
  already recorded elsewhere. This is the actual integrity mechanism; the local chain is
  what makes it cheap to check.
- **F-160.** Whole-file push makes that witness **essentially free**. The destination
  receives the complete sealed file and can record its head hash on receipt, so the head is
  published off-box by construction and no separate checkpoint pipeline is required. Until
  a real remote exists this remains rehearsal only: a filler destination is not a witness,
  because it sits on the same machine as the thing it would be witnessing (F-103, W-18).
- **F-90.** The witness bounds the protection window. Everything appended **since the last
  published head is freely rewritable and undetectably so**. Under F-160 that window is
  exactly the set of un-pushed segments, which is why the F-158 roll cadence *is* the
  granularity of integrity — a tradeoff that must be stated rather than implied.
- **F-91.** On startup the system must verify the chains of prior runs before appending or
  recovering, and must record the verification outcome as an observable fact (F-70, F-44).
  An unverified chain must never be silently trusted or silently discarded.
- **F-92.** Verification must distinguish, and report differently: **chain invalid**
  (records modified), **chain valid but shorter than a published head** (truncated),
  **run absent entirely** (removed), and **verification impossible** (no published head to
  compare against). *Verification impossible* is the default until a real remote exists
  (A-11); once one does, F-160 collapses it to *verifiable* for every file already pushed.
- **F-93.** **Sanctioned deletion must not be reported as tampering.** Records are deleted
  automatically once pushed and acknowledged (F-134), so absence is the *expected* steady
  state — a fully drained store is a healthy store, not a wiped one. An integrity check
  that cries tamper every time delivery succeeds will be turned off within a month. Absence
  of an entire run's files is routine; *partial* inconsistency within a file that is still
  present is not. Only the latter is a finding.

**Best-effort local hardening.** Cheap, worth doing, and must not be mistaken for a
control:

- **F-94.** On creation the ledger file must be given the most restrictive access the
  platform allows while still permitting append. On Windows this means granting
  append-data while denying write-data, so existing bytes cannot be overwritten in place
  even though new records can be added.
- **F-95.** This is **hardening, not a boundary.** The file's owner can rewrite its
  permissions, and the server runs as the owner. It stops stray scripts, accidental
  clobbering, and any party that is not the owner; it does not stop the owner. It must be
  described in exactly those terms wherever it appears (F-87, F-88).
- **F-96.** Failure to apply the hardening must never prevent logging. The system degrades
  to an ordinary file and records that it did so; it does not fail closed on a
  permissions operation.
- **F-97.** Whether the hardening is in effect must be observable in the health check
  (F-70) and in summaries (F-44), so its silent absence is not mistaken for its presence.

#### 4.10.2 The local ledger is a spool, not the archive

The durable copy is the delivered one. Designing as though the local file must be
permanent is a fight against the machine's owner that cannot be won (W-17, W-22);
designing it as a spool that drains removes the need to win it.

- **F-98.** The local ledger must be treated as a **spool** whose contents are expected to
  be superseded by a delivered off-box copy. Permanence is a property of delivery, not of
  the local file.
- **F-99.** Once a file is confirmed delivered, the delivered copy is the authoritative
  one. The local copy remains useful for triage and verification but is no longer what the
  record's durability depends on.
- **F-100.** Nothing in the system may depend on the local ledger being permanent. The
  only path permitted to read back records from a previous run is the recovery path of
  F-54. This is what makes automatic delete-on-acknowledgement (F-134) safe rather than
  lossy: nothing downstream needs a file that has already been pushed.
- **F-101.** The interval during which the local copy is the **only** copy is exactly the
  window in which loss and undetectable tampering are possible (F-90). That window must be
  an explicit, documented, configurable value rather than an emergent property of the
  delivery cadence. Under F-158 it is the segment roll cadence.
- **F-102.** Shrinking that window must require **only** a change to that cadence — no
  change to the ledger format, the record contract, the recording occasions (F-75), or any
  caller. This is what allows the filler transport (F-59) to be replaced by the real
  OAuth-backed one (F-62) and immediately deliver the permanence property, with
  configuration as the only difference.
- **F-103.** Until a real transport exists (A-11), the system must report honestly that
  **no off-box copy exists** and that local permanence is therefore not provided.

#### 4.10.3 Self-draining spool

- **F-134. Delete-on-ACK.** When a delivery receives a positive **ACK** from its
  destination — the real remote, or `simulated_remote` in the filler era — the
  corresponding local file is **deleted**, so delivered data does not accumulate on local
  storage. At-least-once delivery with stable identity and receiver-side dedup (F-56)
  makes deletion safe against a redundant resend. **This is the system's only deletion
  mechanism** (§2, F-33): successful push is the sole trigger, it runs automatically in
  the background, and no operator action is involved at any point. The unit of push, ACK,
  and deletion is a whole sealed file (F-155).
- **F-135.** Deletion is gated on **confirmed delivery to a retaining destination** (the
  ACK), not on a *durable off-box copy* (§4.14 Definitions). In production the destination
  is off-box, so deletion coincides with real durability. In the filler era the destination
  is the local `simulated_remote` folder, so a filler ACK **relocates** the file there and
  frees the `server_data` copy — it is **not** a claim that a real off-box copy exists, and
  the health check and summaries must still report the delivery as **filler / simulated**
  (W-18, F-123). No net data is lost, because `simulated_remote` retains the copy (F-133)
  and is never drained.
- **F-136.** Because delivered files self-delete, `server_data` holds **only un-ACKed
  (undelivered) files** at any moment. Bootup recovery (F-54) therefore delivers exactly
  what remains. It follows that `server_data` is never a place anything should be deleted
  from by any other means: everything resident there is by definition not yet delivered, so
  any deletion that is not ACK-driven is data loss.
- **F-118.** **Deletion is a consequence of delivery, never a separate act.** There is no
  manual clear, no scheduled wipe, and no cleanup step anyone is asked to perform, which
  makes "delete something not yet delivered" unreachable by construction rather than
  forbidden by policy. Because delivery may lag until the next boot (F-117), un-acknowledged
  files simply remain resident for as long as that takes — that is correct, not a backlog to
  be tidied.

### 4.11 Usage snapshot (every 100 calls)

- **F-129.** There are two cadences: a **usage snapshot every 100 observed tool calls** (all
  builds), and the **check-in prompt every 500 calls** (personal builds, §4.12). Neither
  changes the trail buffer size (~100 events, F-1), which is a different quantity that keeps
  its own value.
- **F-43.** At each 100-call tick the system produces a **usage snapshot**: a summary record
  (§5.4) carrying the run's cumulative counts (F-165). This is the primary usefulness signal
  — problem-watching is the reports (§4.5), usefulness is the snapshot's counts and ratios.
- **F-165. Snapshot counts are cumulative and monotonic, never per-window deltas.** Every
  snapshot reports the run's running totals (calls, per-tool, per-outcome, per-error) as of
  that tick, not "what happened since the last snapshot." This is the anti-under-report
  property: because each delivered snapshot carries the true running total, dropping,
  withholding, or failing to deliver intermediate snapshots **cannot understate the total** —
  the next snapshot the 2-week block forces out (§4.18) still carries it. Combined with the
  per-run chain (F-86, F-39), a count that *decreases* or a snapshot missing from the chain's
  sequence is detectable. See F-166 for what this does and does not defend against.
- **F-44.** A summary must contain: run identity and uptime; **cumulative (run-to-date)**
  counts by tool, by outcome class, and by error class (F-165); the ledger's record count and
  chain head; the log-root and workspace binding state; hardening state (F-97); the build's
  `narrative_logging` capability (F-142); transport and delivery-anchor state including
  filler/simulated origin (F-123); the counter-vs-ledger delta (F-85); and the
  exercised-versus-advertised tool coverage for the run.
- **F-45.** The 100-call snapshot boundary and the 500-call check-in boundary are both
  counted per Server Run and must not reset on board change, disconnect, gate closure, or
  plan expiry.
- **F-46.** Summary production and delivery must be bounded and must not block or delay
  tool execution (same constraint as F-27).
- **F-47.** A summary is a *health* record, not an issue report. It must not be filed as a
  defect, must not participate in issue grouping or rate limiting, and must be
  distinguishable from reports at the sink.

### 4.12 Routine check-in (agent-authored, server-prompted)

Personal builds only (F-140). Companion to the report intake of §4.5, and deliberately a
separate, lower-stakes path.

- **F-48.** The server must expose a tool the agent calls to submit a conversation-level
  activity summary and trigger delivery of the current summary and any undelivered ledger
  content.
- **F-49.** It must carry the same structural properties required of report intake by
  F-17 and F-18: always visible, never hidden or locked, no plan, no permission, no board
  scoping or serialization, no hardware access, and not usable as an `action_batch` child.
- **F-50.** The agent-supplied narrative is untrusted input: validated, size-bounded, and
  subject to the content bar of F-153.
- **F-51.** Calling it must be safe and unremarkable at any point in a session, including
  when nothing has gone wrong. It is a routine action, not an error path, and must never
  be treated as evidence that a problem occurred.
- **F-128.** The check-in is **server-prompted** and **agent-authored**, and its narrative
  is **required**, not optional. On the **500-call check-in tick** (F-129) — distinct from
  the 100-call usage-snapshot tick — the server, in its **next
  tool response**, tells the agent it is time for a routine check-in and to write and
  submit one. The agent writes a narrative summary of the work it has done since the
  previous check-in and submits it via this action — never via the health check. Purpose:
  give the operator a running, human-readable ledger of what the agent is doing. The prompt
  is the one place monitoring writes *into* a tool response rather than observing passively
  (W-3): it is a bounded, sanctioned annotation that appends a check-in request to the
  server's own response without altering the tool's result, ordering, timing, locks, or any
  authority (A-8), and the agent's compliance is behavioral, not gate-enforced. It reads
  nothing from the conversation (A-2 untouched) and, like a refusal, is ordinary outbound
  content (A-6).
- **F-130.** The check-in remains a **health record, not an issue**: no severity, no
  signal type, no grouping identity (F-47), and its shape stays distinct from the
  error-report form (§5.2) so it is never read as "something went wrong" (F-51). A
  required, richer narrative does not make it an issue path. Server-side counts, coverage,
  and ledger state are attached by the server (F-64); the agent supplies only the narrative.

### 4.13 Delivery lifecycle — periodic, closeout, and bootup recovery

- **F-52.** There are exactly three delivery occasions: **periodic** (F-43), **closeout**
  (client exit or shutdown signal), and **bootup recovery**.
- **F-111.** They are deliberately unbalanced. **Bootup recovery is the occasion the
  system leans on** for reliable delivery: it is not capped by a kill grace, it is a
  deterministic point, and if readiness is signalled first it neither charges the startup
  deadline (W-14) nor blocks the new session's calls. **Closeout is a cheap best-effort
  attempt** retained for the coverage only it can provide (F-117). **Periodic is kept
  light** and must fail silently without disturbing tool work (F-57, F-29). The accepted
  tradeoff is a wider only-local window, bounded by F-158 and documented per F-101.
- **F-159.** Under whole-file semantics (F-154) each occasion pushes a different thing:
  **bootup** pushes all sealed files from prior runs; **periodic** pushes sealed segments
  of the current run and **never the live file**; **closeout** seals and attempts the final
  segment — the one thing only it can reach — and if it is killed, the next boot delivers
  that file instead.
- **F-116.** Bootup recovery runs **after readiness is signalled** to the client, drains
  the spool **asynchronously** so it never blocks the new session's early tool calls
  (F-57), and because it is not capped by a kill grace it may **retry within a bootup
  budget larger than closeout's**.
- **F-117.** Bootup's structural limit must be stated, not glossed: it can deliver **only a
  prior run's spool** — never the current run (not yet produced) and never the **final run**
  (no subsequent boot triggers it). Closeout is therefore **retained** as the best-effort
  catcher for the current run's tail and, critically, for the final or force-killed run that
  W-16 calls "the session worth having."
- **F-112.** The closeout budget must be spent **after** reset release, owned-process
  termination, and connection teardown — never before them (F-28). Ordered this way the
  closeout flush delays only the exit of an otherwise-idle process and never strands a
  board or a child process, whatever the budget.
- **F-113.** Closeout is **bounded, and the bound is dictated by the client, not chosen by
  the server.** Observed client kill grace (version-specific and drifting): **Claude Code**
  signals SIGINT → SIGTERM at +100 ms → SIGKILL at +400 ms more (~**500 ms** total) and does
  **not** close stdin first; **Codex** sends process-group SIGTERM → SIGKILL after ~**2 s**
  and does not await exit. Beyond that grace the process is force-killed mid-send regardless
  of any configured budget (W-16), so the closeout budget must fit inside the **tightest**
  client grace the deployment targets (~500 ms today) — a larger value buys nothing but the
  risk of being killed anyway.
- **F-114.** Because of F-113, durability must **not depend** on the closeout send
  succeeding. The append remains the durability source (F-40) and bootup recovery remains
  the backstop (F-54). The two are complementary, not competing.
- **F-115.** Closeout must be triggered by **SIGINT and SIGTERM**, not by stdin EOF alone.
  Observed clients initiate shutdown by signalling *without* closing stdin, so a server that
  waits only for EOF never runs its closeout and is SIGKILLed at the end of the grace window.
- **F-148.** The signal handling of F-115 must use a **flag-then-drain** shape, never inline
  work. The handler does the minimum — set a shutdown flag — and returns immediately; the
  **main loop** observes the flag and performs the ordered closeout: release reset and
  terminate owned children (F-112), write the close record (F-80), then attempt the bounded
  send (F-113), all inside the client kill grace. Doing real work — locks, I/O, network —
  *inside* the handler risks deadlocking against whatever the signal interrupted, burning the
  grace window and getting the process SIGKILLed with nothing saved. Anything the drain
  cannot finish is carried by bootup recovery (F-54). On Windows, where signals are
  unreliable and kill-on-close job objects may terminate with no notification, the handler is
  best-effort and append-plus-recovery remains the real safety net.
- **F-54.** At startup the system must detect ledger content not confirmed delivered and
  attempt to deliver it. This covers the cases where closeout did not run (W-16).
- **F-55.** Delivery progress must be tracked durably, so recovery neither loses content
  nor resends without bound.
- **F-56.** Duplicate delivery must be *tolerable*. Crash timing can produce a resend, so
  every delivered file must carry a stable identity (F-155) that permits the receiver to
  deduplicate. Exactly-once delivery is explicitly not a requirement; at-least-once with
  stable identity is.
- **F-57.** No delivery occasion may block tool execution, shutdown, or startup. A server
  whose remote endpoint is unreachable must start, run, and exit normally.
- **F-167. Delivery runs on a decoupled background sender.** All sending to the remote
  (OAuth or filler) happens in a **separate background worker**, never on the request path.
  The server's obligation for any record is the **local append** (F-40); once a file is
  sealed it is handed to the sender and the server moves on. Concretely:
  - the handoff is **non-blocking and bounded** — the server enqueues (or simply leaves the
    sealed file in `server_data/` for the sender to find) and returns immediately; it never
    `await`s a send, and never holds a board lock, the dispatch path, or a cleanup step while
    a send is in flight;
  - a **stuck or slow sender must be invisible** to the server — a hung socket, a wedged
    retry, or an unreachable endpoint may not back-pressure, stall, or crash request
    handling (F-29). If the queue fills, the server drops the *handoff*, not the record: the
    file stays on disk and the next boot's recovery (F-116) ships it.
  - The one place the sender's *outcome* legitimately reaches the server is the staleness
    backstop (§4.18), and even there it is a **local timestamp check** on the delivery anchor
    at dispatch time — never a network wait. The closeout attempt of F-113/F-159 is the only
    moment the server touches sending directly, and it is capped by the client kill grace
    (~500 ms) with the remainder left to background recovery. This requirement consolidates
    what F-27, F-29, F-57, and F-116 each require in part.
- **F-109.** "Disconnect" must be disambiguated wherever counting or delivery depends on
  it, because the two senses behave oppositely:
  - **Client process exit / stdin EOF / shutdown signal** is a **closeout** occasion:
    write the close record first (F-78, F-80), then attempt the bounded best-effort send.
  - **A board / connection disconnect mid-run** is **not** closeout. The Server Run
    continues, the live counter **survives** (F-66), and **no** closeout send is
    triggered. Treating every probe hiccup or USB reset as a closeout would manufacture
    exactly the S-3 environment-fault noise that A-7 and W-4 warn against.

### 4.14 Remote transport seam (OAuth pipeline pending)

Per A-11, the authenticated pipeline will not exist when this ships. These requirements
exist to make its absence safe and its later arrival cheap.

- **F-58.** Remote delivery must sit behind a single named seam with a **no-op default**.
  The system must be complete, correct, and useful with no remote transport present.
- **F-59.** A **filler implementation** of that seam must be provided for the interim. It
  must satisfy the same interface and record what it *would* have sent, so the periodic,
  closeout, and recovery paths are all exercised and testable before OAuth exists. Its
  destination is `simulated_remote/` (F-133).
- **F-60.** The filler must never masquerade as a working transport. Its state must be
  reported as **filler / simulated** — distinct from *sent*, from *failed*, and from *not
  configured* — and that state must be visible in the health check and every summary so
  nobody concludes data is leaving the machine when it is not. This is the single most
  important requirement in this section (W-18).
- **F-61.** No code path may treat a send as a **durable off-box copy** without positive
  confirmation from a real transport. Local durability remains the source of truth.
- **F-62.** The real OAuth-backed transport must be substitutable at the same seam without
  changing the ledger format, the summary contents, or the lifecycle of F-52.
- **F-63.** Credential acquisition, refresh, and failure are transport concerns behind the
  seam. No credential material may enter the ledger, a summary, a report, or any local
  file this system writes.
- **F-108.** Beyond the OAuth-backed **transport**, an OpenID-based **authentication**
  layer is also planned for later. It sits behind the same seam and, like the transport, is
  not required for the system to be complete (N-9). Per F-63, no credential, token, or
  identity material from either layer may enter any ledger record, summary, report, or
  local file.

**Definitions.**

- **Confirmed delivery** — the currently active delivery transport returned positive
  confirmation that it accepted the payload. The active transport is the **filler** today
  and the **real OAuth remote** after cutover (F-62), so "confirmed delivery" means
  *filler-confirmed* now and *remote-confirmed* later. This is what anchors the staleness
  interval (F-120) and what cutover swaps the source of (F-124).
- **Durable off-box copy** — a confirmed delivery to a **real remote**: a copy that exists
  on a machine other than this one. Strictly stronger than a confirmed delivery, and
  **never satisfied while the filler is active** (F-61, F-103).

Consequence for the filler era: because no durable off-box copy exists until OAuth ships,
the current build provides **no real off-box protection** — the delivery, anchor,
staleness, and block machinery are *rehearsed* in simulation (F-123), and real protection
begins at cutover (F-124). Deleting local files in the interim is therefore still lossy in
the off-box sense; that is the accepted pre-OAuth reality (F-103), not a guarantee of safe
deletion.

- **F-139. Cutover migration of the rehearsal backlog.** Everything the filler "delivered"
  lives in `simulated_remote/` and has **never actually gone off-box**. At OAuth cutover
  (F-124) that backlog must be **replayed through the real transport** rather than
  abandoned: each file is sent to the real remote and drained on its ACK exactly as a live
  file would be (F-134). Until a given file has been replayed and ACKed by the real remote
  it still has **no durable off-box copy** and must be reported as such — cutover does not
  retroactively make rehearsal deliveries real. Once the replay completes,
  `simulated_remote/` no longer holds the sole copy of anything and may be retired.

### 4.15 Live counter state (held in the server, not derived from logs)

The counts must exist as **live server state**, not as something reconstructed by reading
a file back. The log is a record *of* the counter; the counter is not a summary *of* the
log. That direction matters: it keeps counting correct when the store is unbound or
buffering (F-37), and it keeps the health check honest when delivery is broken.

- **F-64.** Call counts must be maintained in process-local live server state, and that
  state is the authoritative source. Ledger records and summaries are derived from it.
  Nothing may re-derive the counters by parsing written logs.
- **F-65.** The counter is expected **not** to survive a restart, and that is correct
  (A-9). Each Server Run counts its own activity from zero.
- **F-66.** The counter must **not** be authority-bearing, and must not be cleared by any
  operation that clears authority. Disconnect, gate closure, plan expiry, and run-scoped
  authority reset must all leave the counts intact — the activity still happened, and a
  report or summary produced after a disconnect must still reflect it.
- **F-67.** Minimum counted dimensions: total calls; per tool; per outcome class; per
  error class; first and last activity timestamps; a durable monotonic total-appended
  count (F-138); and which advertised tools have never been exercised this run.
- **F-68.** Counting must remain correct under this server's concurrency model, where
  same-board calls serialize but different boards run concurrently (W-10).
- **F-69.** Reading the counter must be side-effect free and must never block a tool call.

#### 4.15.1 Reconciliation — when the counter and the ledger disagree

They will disagree, routinely and legitimately, so the behavior must be defined rather
than discovered.

- **F-82.** **Neither one corrects the other**, because they answer different questions.
  The counter is authoritative for *what happened* (F-64); the ledger is authoritative for
  *what durably survived*. No reconciliation may overwrite either from the other, and
  there is no "higher value wins" rule.
- **F-83.** **Counter ahead of ledger is expected and is not an error.** The increment
  precedes the append, the store may be unbound or buffering (F-37), and a write may have
  failed. In this direction the counter is simply correct and the ledger is behind.
- **F-84.** **Ledger ahead of counter, within a single run, is never legitimate** and must
  be treated as an integrity anomaly — reported, never reconciled away. Within a run the
  ledger cannot lead the counter, so this means injected records, a second writer, or a
  lost counter. A rule that took the larger value would trust exactly the records that
  should be distrusted.
- **F-138.** Because delivered files self-delete (F-134), the resident local record count
  is **not** a proxy for "records appended this run". Reconciliation must key off the
  durable monotonic total-appended count (F-67), never off resident file size. A large gap
  between the live counter and the resident record count is **expected** and must not be
  read as degraded logging.
- **F-85. Report the numbers; do not build a detector.** Three quantities must be
  **observable, not merely internal** — exposed in the health check (F-70) and in every
  summary (F-44): the live counter, the durable total-appended count (F-67), and the
  un-ACKed backlog size. Alongside them, if the last local write failed, the failure and
  its error class must be shown. That is the whole obligation, and it is what makes F-37
  ("log-writing must never silently no-op") measurable rather than aspirational.

  There must be **no threshold, trend analysis, or issue-raising detector** built on these
  numbers, for three reasons: the staleness backstop (§4.18) already enforces the
  consequence — broken recording produces no ACKs, so the anchor goes stale and dispatch is
  refused; a report about an unwritable store generally cannot be written either; and W-8
  forbids the monitoring path from filing reports about its own failures. A human or a test
  reads the health check. The block is the enforcement.

  Note when reading these numbers that counter-ahead-of-total-appended and a
  counter-vs-**resident-file-count** gap mean different things: the first is a real
  shortfall, the second is expected and normal, because delivered files delete themselves
  (F-138).

### 4.16 Health check action

Distinct from §4.12. That one *submits and sends*; this one *reports back* and changes
nothing.

- **F-70.** The server must expose a read-only tool that returns the current live counter
  state (F-67), run identity and uptime, ledger record count and chain head, store and
  workspace binding state, hardening state, build capability, transport state, and
  delivery-anchor state.
- **F-71.** It must be **side-effect free**: no send, no write, no counter mutation, no
  state transition. Calling it twice in a row must produce the same answer apart from
  elapsed time.
- **F-72.** It must carry the same structural properties required by F-49: always visible,
  no plan, no permission, no board scoping or serialization, no hardware access, and not
  usable as an `action_batch` child.
- **F-73.** It must report transport state as *sent*, *failed*, *not configured*, or
  *filler / simulated* (F-60, F-123), so a caller can tell whether anything is actually
  leaving the machine, and must state plainly when no off-box copy exists (F-103).
- **F-74.** It must be usable as a **test oracle**: assertions about which tools ran, with
  what outcomes, must be expressible through this tool's response rather than by reaching
  into server internals. This is what makes the system verifiable through the protocol
  with no hardware attached.

### 4.17 Recording occasions — boot, snapshot every 100 calls, close

Recording is local and must always succeed; delivery is remote and may not be available
at all (A-11). These are separate concerns and must not be conflated.

- **F-75.** The always-present recording occasions are **boot**, a **usage snapshot every
  100 tool calls** (F-43), and **close**. A personal build adds a **check-in record every 500
  calls** (§4.12), and a **problem report** produces a record whenever one is filed. None of
  these is a per-call record (F-38).
- **F-76.** The **boot record** must capture run identity, start time, server version,
  resolved configuration state (store root, workspace binding, transport configured or
  not), and whether undelivered content from a previous run was found (F-54).
- **F-77.** The **periodic record** is the usage snapshot of §4.11, produced every 100 calls.
- **F-78.** The **close record** must capture final counters, uptime, and the shutdown
  reason where known (stdio EOF versus signal versus other).
- **F-79.** Every recording occasion must complete **locally** regardless of transport
  state. A run with no transport configured still produces a complete boot record, its
  periodic records, and a close record.
- **F-80.** The close record must be written **before** the closeout send is attempted, so
  that a failed, slow, or unconfigured send cannot cost the record. Recording is the
  durable act; sending is best-effort (F-40, F-113).

### 4.18 Remote-logging staleness backstop

Self-monitoring is the system's identity, not an opt-in: this backstop is **default-on**.
Its premise is that invisible hardware work is the core harm the reporter exists to prevent
— a board worked on without any record throws the whole team off, hiding exactly the issues
the work may have caused. So once remote logging has gone unconfirmed for too long, the
system stops doing more unauditable hardware work. This is the one place monitoring holds
authority, granted deliberately as the sole exception to A-8, N-6, and N-9 (F-126).

- **F-119.** Bootup auth runs **after readiness is signalled** and is **non-blocking**
  (extends F-116): offline or failed auth leaves the server fully functional, and
  transport/auth state is reported honestly (F-73). Nothing in this section is opt-in.
- **F-120.** A durable **delivery anchor** records the most recent confirmed delivery
  across runs (builds on F-55). Staleness = local wall-clock now − anchor timestamp; the
  threshold is **exactly 2 weeks (14 days)**. The local clock is trusted (internal tool, no
  adversary — §2, operator's own machines). If the clock is unreadable or unset so no sane
  elapsed value can be computed, the block does **not** trip (fail-open on a dead RTC is
  preferred to bricking a bench machine); the condition is logged.
- **F-121.** Past the threshold the system applies a **clean hard block**: guarded hardware
  dispatch is refused until a fresh confirmed delivery re-anchors the interval. **No
  override, no escape hatch** — the only exit is to connect and deliver. The refusal
  **names its remedy** (restore network / deliver), so §1.1 classifies it as correct
  behavior, not a defect — it is neither an S-1 nor an S-7.
- **F-122.** The block **requires an anchor to measure against.** With no anchor yet — no
  delivery has ever succeeded on this install — the block is **dormant**. This is the
  bootstrap condition that prevents a first-operation brick before any delivery has had a
  chance to occur.
- **F-123.** **The filler transport anchors the interval.** A filler delivery produces a
  delivery anchor exactly as a real transport will, so the entire bootup-auth / anchor /
  staleness / block machinery runs for real before OAuth exists. The anchor and the
  transport state are tagged **filler / simulated** and reported distinctly from a real
  *sent* (F-60): the health check (F-73) and summaries (F-44) must show the current anchor
  is filler-origin, so no one concludes a real off-box copy exists (W-18). Filler anchoring
  is **not** confirmation of real delivery (F-61) and makes no file's durability depend on
  it (F-40). **Consequence:** in the current filler build the block is **armed but
  self-clearing** — every filler delivery re-anchors, so it never trips in normal use while
  the machinery is fully exercised.
- **F-124. Cutover.** Replacing the filler with the real OAuth transport (F-62) changes
  only which component stamps the anchor and flips the reported state from filler/simulated
  to real *sent* / *failed*. The block logic, threshold, and dispatch gate are unchanged.
  From that point the anchor reflects genuine off-box delivery and the block protects real
  audit coverage — it "goes live" at cutover with no code change.
- **F-125. Block edges.** (a) The always-on monitor tools — report intake (F-17), routine
  check-in (F-49), health check (F-72) — remain callable while blocked; they are how the
  state is observed and the reconnect is driven, so only *guarded hardware dispatch* is
  refused. (b) The block gates at the **next dispatch boundary** and never interrupts an
  in-flight flash transaction or board lock — a half-written board is worse than deferring
  the next op.
- **F-126. Principle scope.** This backstop is the sole, deliberate exception to:
  - **N-9** — starts/runs/exits normally offline **except** that guarded hardware dispatch
    is refused once remote logging is stale past the threshold (and only once an anchor
    exists, F-122);
  - **N-6** — fail-open for reporting **except** the staleness block, the one sanctioned
    case where monitoring state refuses dispatch;
  - **A-8** — observation never carries authority **except** the staleness block, which is
    the only authority the monitoring layer holds.
- **F-127. Test hook.** A test transport that (a) reports failure-always and (b) accepts
  injected anchor timestamps must be able to drive the block through **dormant → armed →
  tripped → cleared** without waiting real days or standing up a remote backend. This is how
  the block is validated while the filler otherwise keeps it self-clearing.
- **F-145. "Delivery off" is an observed state, not a toggle.** There is **no monitoring
  kill switch.** "Off" means the observed condition *"the server could not reach the remote
  to send its tool-usage reports."* The server always wants to connect and deliver; "off"
  describes the *failure* to deliver, not anyone disabling delivery. Two consequences of
  this being delivery-driven:
  - **Every build keeps the delivery signal alive by itself.** The 100-call usage snapshots
    (F-43) and the server-generated mechanical reports (S-1/S-2/S-3) exist in *every* build,
    carry **no codebase content** (F-3, F-141), and are delivered and ACKed continuously;
    those ACKs refresh the anchor. A snapshot every 100 calls is a steady heartbeat on its
    own, so a professional build refreshes the anchor perfectly well — there is no
    chicken-and-egg and no brick-in-two-weeks.
  - **Narrative on/off is orthogonal and has zero bearing on the block**, because narrative
    was never what kept the signal alive.

### 4.19 Build profiles — personal and professional

Some downloads go to users who must not have their project or codebase described in any log.
This is a **build-time feature configuration** baked when the download is cut — one codebase,
a flag set at package time — not a runtime toggle and not a per-workspace setting.

- **F-140.** A build-time feature flag, **`narrative_logging`**, gates all **model-authored
  narrative** output. When it is disabled in a build:
  - the **routine check-in** (§4.12) is not present — the server never prompts for one and
    the submission action is absent;
  - the **error-report narrative** is not present — the model-authored prose fields of the
    issue-intake form (§5.2) are not offered, and any narrative content that arrives anyway
    is discarded, never stored;
  - consequently the **conversation-level, model-detected signals S-4 … S-14** are not
    produced, since they are inherently prose descriptions of what the agent was doing.
- **F-141.** The flag gates **only** model-authored narrative. The entire **server-only**
  layer runs unchanged in a sensitive build — ledger, trail, counter, periodic records,
  health check, and the full delivery lifecycle — because that layer is already
  code-content-free by construction (F-3). The **server-detected** signals **S-1**, **S-2**,
  and **S-3** still fire, carrying their mechanical anchor only — failing tool, normalized
  error signature, argument fingerprints — with no narrative. The explicit tradeoff: a
  sensitive build keeps *"tool X failed with signature Y,"* and loses *"here is the story of
  what the agent was attempting in the codebase."*
- **F-142.** A sensitive build must **declare itself**. The health check (F-70) and
  summaries (F-44) must report the capability as **`narrative_logging: enabled`** or
  **`narrative_logging: not_built`**, so that an absence of check-ins and of model-detected
  reports is legible as a **build property**, not as the agent going silent or logging being
  broken. The absence is a configured capability, never a fault, and must not be reported as
  one (contrast F-85 degraded logging).
- **F-143.** Because the gate is compiled/packaged in, there is **no fail-safe direction to
  reason about** and **no code path that can re-enable narrative** in a build where it is
  off. This is the point of choosing a build-time feature over a runtime toggle: a
  misconfigured env var, a bug in a flag check, or a misbehaving agent cannot cause a
  sensitive build to emit narrative it was built without.
- **F-143a. What a professional build does and does not guarantee.** The guarantee is
  precise, and is stated as a bound rather than an absolute: a professional build emits **no
  code-specific content and nothing from which this codebase can be reconstructed** —
  narrative is absent (F-140), only the mechanical layer is emitted (F-141, F-153), and that
  layer is structurally code-content-free and non-reconstructable (F-3, F-149/F-150). It
  **does not** guarantee "nothing about the activity leaves at all": **coarse operational
  metadata** — which tool ran, in what order, with what outcome and timing — still leaves.
  That metadata is the generic "it is firmware doing firmware things" kind that is true of
  all code and cannot rebuild this one, so it is allowed. This bound is deliberate: the
  stricter "literally nothing leaves" reading would also disable the mechanical reports that
  keep the staleness anchor fresh (F-145), which would brick the tool after two weeks.
  Documentation and any user-facing description of professional mode must state this bound
  accurately and must **not** imply zero-egress.
- **F-146. Professional vs personal; report tool present-and-refusing.** A build with
  `narrative_logging` disabled is the **professional** build; a build with it enabled is
  **personal**. In a professional build the agent bug-report action remains **registered and
  visible** — it is *not* removed — and when invoked returns a clear, remedy-naming message:
  *this is a professional license; remote bug reporting is disabled to avoid leaking company
  or codebase information; the bug-report feature is available in personal mode.* No
  narrative is authored, stored, or sent. Keeping the tool present-and-explaining rather than
  absent stops the agent from hunting a missing tool and misfiling an **S-5** (this is a
  correct refusal under §1.1, not a defect). The routine check-in stays fully absent: it is
  server-*prompted*, so with no prompt the agent never expects it and no S-5 arises.
- **F-147. Build identification.** Professional and personal downloads are distinguished by
  **filename convention** at distribution time, alongside the runtime self-declaration of
  F-142. Who cuts the builds is a distribution process, not a design concern.

---

## 5. Contracts

### 5.1 Report contract

Every report, whichever origin produced it, must express the following. This defines
content, not serialization.

- **Identity:** schema version, unique report id, timestamp, session id
  (`ServerRun.run_id`) and run start time.
- **Classification:** signal type (§3), severity, origin, and triage class (F-23).
- **Observed symptom:** a short title plus a description of what was observed to go
  wrong and what the agent was attempting at the time.
- **Technical anchor:** the failing/relevant tool name; a normalized error signature
  for unexpected errors; the refusal category and named remedy for refusal-adjacent
  signals; canonical argument fingerprints (salted, F-149).
- **Usage snapshot:** the run's cumulative counts at the moment of the report (F-165), so
  a report is self-describing about how much activity surrounded the failure — the "usage
  count on every problem report" companion to the periodic snapshot.
- **Board scope:** `board_id`, connection identity token, and whether that identity is
  hardware-stable (provider UID) or session-local — the two are not interchangeable
  for triage.
- **Workspace scope:** the pushed workspace token (F-162).
- **Guard state at the time:** validation gate open/closed, active plan and remaining
  budget, permission mode in effect, and current tool-visibility revision. Recorded as
  observed facts only; never re-readable as authority (A-8, F-32).
- **Context:** the board-scoped recent-activity trail (F-2, F-5).
- **Conversation narrative:** present for model-detected signals (S-4 … S-14) in a
  personal build only; absent by construction in a professional build (F-140).
- **Grouping identity:** the stable, restart-independent fingerprint used for
  deduplication (F-24).
- **Environment:** `pyocd-debug-mcp` package version, Python version, host platform,
  build profile (F-142), and the relevant provider/backend identity when a hardware fault
  is implicated.

### 5.2 Issue-report narrative form (personal builds only)

Model-supplied fields **only**; everything else is server-attached (F-20). Recency-biased,
three ordered bands, most detail at the failure and fading with distance. All fields
required unless noted. An *action* means one tool-call-level step.

- **Band 1 — failure focus** (maximum detail):
  - `codebase_objective` — what this codebase is for and the objective being pursued in it:
    the "why are we in this code at all" context that frames the whole report.
  - `hypothesis` — the agent's current best belief about what went wrong.
  - `goal` — the problem or task it was trying to accomplish when the failure occurred.
  - `plan` — the approach it was executing toward that goal.
  - `failure_point` — structured: `action_taken` (what it did at the moment of failure),
    `observed_result` (what happened instead of what it expected), `named_step` (which
    tool/step it believes tripped it; the authoritative mechanical anchor is server-attached
    per F-20, so this is the agent's account, not the source of truth).
  - `signal_subcase` — required **only** where the signal demands one: S-6 → *ignored usable
    guidance* vs *guidance was unusable*; S-7 → *no remedy* / *remedy repeated / same
    refusal* / *setup–validate cycle never converged*.
- **Band 2 — immediate run-up** (detailed):
  - `recent_actions` — an ordered (oldest→newest) list of **exactly the last 5 actions**
    before the failure (fewer only if the session was shorter). Each entry has: `action`
    (what the agent did), `result` (what happened), and `code_context` (the goal of that
    action in the codebase and what it did for the codebase — what part of the code it was
    serving, changing, reading, or verifying). 1–2 sentences per part. **No compression in
    this band** — five individual entries.
- **Band 3 — broader lead-up** (compressing, governed by the fade rule):
  - `earlier_phases` — an ordered list of one-line phase summaries covering everything
    before the last 5 actions. A *phase* is a contiguous run of related actions toward one
    sub-goal.
  - `session_start` — exactly one line describing how the session began.
- **The fade rule** (explicit algorithm for Band 3): (1) the 5 actions immediately before
  the failure get one entry each (Band 2, no compression); (2) everything earlier is grouped
  into phases where phases **nearer** the failure are **narrower** (fewer actions each, more
  of them) and phases **further** back are **broader** (more actions each, fewer of them) —
  detail decreases monotonically with distance from the failure; (3) the session start
  collapses to the single `session_start` line.

### 5.3 Check-in narrative form (personal builds only)

No failure anchor; three flat required sections plus a required outcomes section, covering
the work since the previous check-in (or as far back as the agent can recall on the first).
Its shape must be visibly distinct from §5.2 (F-104, F-130).

- `codebase_summary` — what the codebase is, what it is for, and the current state and
  objective of the work in it.
- `work_summary` — broad, phase-level narrative of what the agent worked on this window and
  roughly what happened. Not step-by-step.
- `tools_used` — a list; for each tool exercised this window, the tool (generically named)
  and what it was used *for*, in the agent's words (e.g. "memory-read — repeatedly polled a
  status value"; "flash — wrote firmware twice"). This is the human-readable *why* companion
  to the server's own tool counts (F-64).
- `effectiveness_observed` — **observable outcomes only**: what was accomplished, what it got
  stuck on, where it needed retries or could not find a path. Concrete facts of what
  happened. **No self-rating, self-grade, or self-assessment of its own quality or
  competence** — prohibited, both by request and because the model must not psychoanalyze
  itself.

### 5.4 Summary record contract

A summary is a health record, not an issue (F-47). It must express:

- **Identity:** schema version, summary id, timestamp, run identity and uptime, and the
  call ordinal that triggered it (or "agent-invoked").
- **Activity:** counts by tool, by outcome class, and by error class for the run.
- **Coverage:** which advertised tools were exercised and which were not.
- **Ledger state:** resident record count, durable total-appended count, and chain head
  digest; verification outcome (F-92); hardening state (F-97).
- **Delivery state:** store and workspace binding state; transport state — *sent*,
  *failed*, *not configured*, or *filler / simulated* (F-60, F-123) — the delivery anchor
  and its origin; and the un-ACKed backlog size (F-85).
- **Build capability:** `narrative_logging: enabled | not_built` (F-142).
- **Agent narrative:** present only for agent-submitted check-ins in a personal build (§5.3).
- **Environment:** server version and run identity.

A summary carries no severity, no signal type, and no grouping identity, because it is
not an issue. Anything a summary reveals that *is* an issue must be filed separately
through the report path.

### 5.5 Two-layer redaction policy

Redaction is split by layer, because the two layers have different audiences.

- **Mechanical layer** — trail, ledger, fingerprints, server-detected reports. Universal in
  every build and the **only** thing a professional build emits. Hard bar: no codebase
  content and no reconstruction signal (F-3, F-149, F-150), salted fingerprints. This is
  what protects professional users.
- **Narrative layer** — personal builds only. This is the **opt-in codebase-describing
  layer** (exactly what a professional build strips). Because personal mode means "log
  everything," the narrative **may describe the codebase and may name real code elements** —
  objectives, per-action code context, and summaries are the point of it, not a leak. The one
  bar that remains: the narrative is a *description*, not a raw dump — it must **not embed
  verbatim payloads** (memory contents, firmware binaries, UART byte streams, or full command
  lines), which are payloads (F-3), not summary, and add nothing as prose.

- **F-153.** Both bars above are mandatory and must be enforced at intake (F-19) against the
  layer the field belongs to. A field is validated against the narrative bar only if it is
  model-authored prose in a personal build; everything else takes the mechanical bar.
- **F-149. Salted fingerprints.** F-3 keeps the content-bearing material out, but one channel
  could still recover *this-codebase* specifics: the argument **fingerprint**. An unsalted
  hash of a small or guessable input (a register address, a short enum, a filename from a
  known set, a workspace path) can be brute-forced back to its real value, and that recovered
  value *is* codebase content. Fingerprints must therefore use a **per-deployment secret
  salt**, so that (a) a fingerprint cannot be brute-forced back to a small input, and (b) the
  same value does not produce a matching fingerprint across reports, closing cross-report
  correlation. With this in place the only signal left is **coarse operational metadata** —
  tool identity, call order, outcome, timing — which characterizes firmware generically and
  cannot reconstruct this codebase's content; that metadata is allowed.
- **F-150. State the bound honestly.** The guarantee is "no verbatim code content and no
  indirect channel that recovers this codebase's specifics," **not** "no metadata at all."
  Coarse "it is firmware doing firmware things" metadata remains and is intended. The salt is
  a **privacy** secret, not an integrity one: like the chain-key discussion of F-88 it runs as
  the user and does not defend against the machine's own owner — it defends against readers of
  the *reports*, which is the correct threat model, since reports leave for the team's remote.

### 5.6 The three agent-facing actions

There are exactly **three**, and no two of them may be conflated:

1. **Submit error report** — the agent authors an issue report (§5.2) and submits it
   (§4.5). Personal builds author a narrative; professional builds return the F-146
   refusal.
2. **Submit routine check-in** — the agent authors a routine activity summary (§5.3) and
   submits it (§4.12). This is the summary submission path, *not* the health check. Absent
   in a professional build.
3. **Health check** — the agent calls it and gets back the server's health readout (§4.16).
   Read-only, on demand, returns data *to* the agent. It is not a submission.

---

## 6. Non-Functional Requirements & Constraints

- **N-1.** Nothing may write to the server's stdout except MCP protocol framing. This
  covers the Sentry SDK, any logging handler it installs, its worker threads, and any
  handler inherited by owned child processes.
- **N-2.** Report payloads are size-bounded end to end (trail, summary, arguments) and
  content-bounded per §5.5.
- **N-3.** Negligible added latency on normal tool calls; zero added latency inside
  board locks, flash transactions, and cleanup.
- **N-4.** The system must function whether the server process is short-lived or
  long-lived, and must not assume in-memory state survives restart.
- **N-5.** No part of the monitoring store may be committed to version control. The store
  normally lives outside any repository (F-131); where the last-resort fallback of F-132 can
  place it inside one, that path must be git-ignored.
- **N-6.** The monitor must be fail-open with respect to reporting and fail-closed with
  respect to safety: if monitoring cannot run, tool execution proceeds unchanged; if
  monitoring is running, it must never suppress, soften, or alter a refusal or an
  exception that dispatch needs to propagate. The staleness backstop (§4.18) is the one
  sanctioned exception (F-126).
- **N-7.** The monitor adds no new authority-bearing state to `ServerRun` and no new
  persisted keys to `.firm`.
- **N-8.** The ledger grows by one record per 100-call **usage snapshot** (plus check-ins
  and problem reports), **not per call**, so it is far smaller than a per-call log would be.
  It must still be size-bounded per record. Its growth is bounded in practice by
  delete-on-acknowledgement (F-134): local storage holds only what has not yet been pushed,
  so a store that keeps growing means delivery has stopped draining — visible in the health
  check (F-85) and enforced, if it persists, by the staleness backstop (§4.18).
- **N-9.** The system must start, run, and exit normally with no remote transport
  configured, no network available, and no credentials present. This is the default
  operating state until the OAuth pipeline exists (A-11), not an error condition. The
  staleness backstop (§4.18) is the one sanctioned exception (F-126), and it **narrows this
  guarantee rather than removing it**: it can refuse guarded dispatch only once a delivery
  anchor exists (F-122) and remote logging has been unconfirmed for 2 weeks (F-120). Until
  then — and throughout the filler era, where the filler self-anchors (F-123) — "no network"
  still starts, runs, and exits normally.
- **N-10.** The ledger, summaries, and any delivery bookkeeping are evidence only. Like
  every other artifact here, they can never restore or influence plans, permissions,
  assignments, gates, or map authority (A-8).
- **N-11. Tunable cadences, thresholds, and bounds are single named constants, not inlined
  literals.** Every recalibratable number this spec fixes — the usage-snapshot cadence (100,
  F-129), the check-in cadence (500, F-129/F-128), the trail buffer size (~100, F-1), the
  staleness threshold (2 weeks, F-120), the segment-roll boundary (F-158), the
  rate-limit/debounce windows (F-26), and per-record size bounds (N-2) — must be defined once
  as a named constant and referenced everywhere it is used, never written as a bare literal
  scattered through the code. Recalibrating any of them is then a one-place edit. Two things
  this must get right:
  - **Distinct quantities stay distinct constants even when their values coincide.** The
    trail buffer (~100, F-1) and the snapshot cadence (100, F-129) share the value 100 today
    but are different knobs (F-1 and F-129 say so explicitly): they must be two separate
    constants, so raising the snapshot cadence never silently resizes the trail. Two meanings
    are never collapsed into one literal because the numbers happen to match now.
  - **Observed-external values are named too, but marked as measured, not tunable.** The
    client kill grace (~500 ms, F-113) is dictated by the client, not chosen by us; it is
    named as a constant for clarity, but the closeout budget is fit *within* it (F-113), not
    set equal to a knob we turn.

---

## 7. What To Watch Out For (implementation risks)

- **W-1 · The refusal storm — the defining risk of this project.** This server refuses
  by design and does so often. Any mapping of "refusal ⇒ issue" produces a flood of
  correct-behavior reports on the first real session and destroys the signal. F-7 and
  F-13 exist solely to prevent this; get the classification right before anything else.
- **W-2 · stdout is the wire.** Any print, logging handler, or SDK debug output on
  stdout corrupts MCP framing and breaks the server intermittently. The exposure here
  is wider than a generic server: pyOCD, pylink, and pyserial are all chatty, the
  Sentry SDK installs its own handlers and threads, and owned child processes
  (provider workers, native-build children, probe-inventory CLI) must not inherit
  anything that reaches the protocol pipe.
- **W-3 · Monitoring must not become authority — or weaken it.** The architecture is
  strict that evidence is never authority. Two failure modes: a report file being read
  back as state, and — more insidious — an instrumentation wrapper that catches an
  exception, delays a deadline, holds a lock, or reorders the guarded dispatch sequence.
  Observation must be strictly passive.
- **W-4 · Hardware faults are not code defects.** Unplugged probes, USB resets, locked
  targets, and J-Link DLL contention will be the highest-volume real failures. Without
  the S-3 / F-23 separation, triage spends its time on the host's USB stack.
- **W-5 · A thrashing model can't reliably notice it's thrashing.** Behavioral
  self-detection misses loops exactly when one is happening. The deterministic
  server-side detector (F-9) is the reliable path for S-2 — and it must know this
  server's legitimate repetition patterns (F-10) or it becomes a false-positive engine.
- **W-6 · Signal overlap causes double-reporting.** One situation routinely satisfies
  several model signals here: a rejected plan envelope (S-4) that the model works
  around by calling the hidden action directly (S-5), fails, and abandons (S-11),
  while the user complains (S-13). Without the collapse rule (F-25) that is four
  reports for one event.
- **W-7 · Untrusted model output.** The narrative is model-generated: validate it, bound
  it, and never assume it is well-formed or accurate.
- **W-8 · Report-path recursion.** A failure inside the reporting or intake path must
  not itself trigger a report, and must not trip startup hygiene or clean-startup
  checks on the next run.
- **W-9 · Frustration and safety surprise are not defects.** S-13 usually reflects a
  prompt/UX issue; S-10 usually reflects a correctly closed guardrail. If these are not
  classified distinctly (F-23), the team will be sent chasing phantom server changes —
  and, worse, may loosen a guardrail in response to a report that was really product
  feedback.
- **W-10 · Concurrency bleed.** Different boards execute concurrently by design. A trail
  that is global rather than board-scoped will attach board B's activity to board A's
  report and make both untriageable.
- **W-11 · The redaction assumption is weaker here than in a generic server.** This
  server can read arbitrary device memory — including provisioning and security regions
  it deliberately permits reading — plus device unique IDs, probe serials, UART traffic,
  datasheet paths, and absolute local host paths. Sending that to a hosted sink
  unscrubbed is a materially different exposure from a generic internal tool. F-3's
  payload exclusions are the mitigation and are mandatory. Revisit the whole assumption
  if sink access is ever wider than the team.
- **W-12 · Local growth is now purely a function of delivery health.** A firmware session
  is call-dense — polling loops, symbol reads, setup retries — so the store fills fast.
  While delivery drains it, delete-on-ACK (F-134) keeps it small on its own; when delivery
  stalls, it grows unbounded and **there is no cleanup step left to relieve it**. The
  un-ACKed backlog size is therefore worth surfacing in the health check and summaries
  (F-85) — but it needs no detector on top: sustained delivery failure is what the
  staleness backstop already acts on (§4.18). Rate-limiting (F-26) bounds the sink side.
- **W-13 · The workspace path arrives through a soft contract.** A stdio server does not
  reliably inherit the workspace as its cwd, and the client may not advertise it.
  `initialization_handshake` is the ergonomic source but depends on the model calling
  it first — the same "don't rely on the model at the right moment" risk as W-5, now
  applied to the logging subsystem's own setup. Note also that the `.firm` artifact
  root is resolved at import time and cannot be moved by a later handshake — the
  monitoring store must be independently late-bindable (F-35, F-164).
- **W-14 · Startup failures are invisible to this system.** The two real observed
  failures of this deployment — a stale `.venv\lib64` junction aborting `uv`, and a
  34-second cold start exceeding a 30-second client startup deadline — both occur
  *before* the server process is monitoring anything. This system cannot report them.
  That is a known and accepted limitation; do not design around it, and do not let its
  absence be read as "no startup problems occurred."
- **W-15 · Tool-surface churn.** Discovery here is dynamic: visibility changes as plans
  are accepted, and the registry emits `tools/list_changed`. An intake tool that
  perturbs the advertised set, or instrumentation that changes list revision as a side
  effect, will cause spurious client refreshes and can itself look like a defect.
- **W-16 · Closeout is the normal path, but not a guarantee.** Measured behavior is
  favorable: client death closes the pipe, the server hits EOF and exits cleanly in 0.28 s
  running both shutdown paths (A-12). It still does not run when the client places the
  server in a Windows job object with kill-on-close, on a process-tree kill, on power loss,
  or when a wedged non-interruptible operation causes someone to force-kill the server —
  and that last case is precisely the session worth having. This is why durability must come
  from the append (F-40) and why bootup recovery exists (F-54). Startup failures are outside
  all of it (W-14).
- **W-17 · "Untamperable" is not achievable locally, and calling it that is dangerous.**
  There is no privilege boundary: the server runs as the user, so any protection the
  server can apply, the user can remove. File permissions and ACLs set by a same-user
  process are not a control. The honest properties are: the chain makes edits *detectable*
  (F-39), and shipping a head off-box makes them detectable *by someone other than
  whoever made them* (F-89, F-160). Only the second is integrity in any meaningful sense,
  and it depends on a pipeline that does not exist yet (A-11). Do not let the word
  "untamperable" appear in the implementation or its documentation.
- **W-18 · A filler transport that pretends to work is worse than none.** The interim stub
  (F-59) sits exactly where a real transport will later sit, and the failure mode is
  silent false confidence: months of sessions believed to be archived remotely while
  nothing ever left the machine. The *filler / simulated* state must be distinct from
  *sent* and *failed*, and must surface in every summary (F-60). Assume this will be
  forgotten unless it is visible by default.
- **W-19 · Remote delivery amplifies the content exposure that reports carefully avoid.**
  Reports are curated, redacted, and rate-limited. A ledger is the opposite: complete,
  continuous, and about every operation. The server's existing event records already carry
  a probe serial field and a board-config path, so a naive full-record send would put
  hardware identifiers and local filesystem layout on the wire every session. The
  monitoring ledger must not reuse those records: send counts, digests, and salted
  fingerprints, and send fuller detail only where a specific need justifies it.
- **W-20 · Delivery bookkeeping is a correctness problem, not a detail.** Tracking what
  has been sent is the seam where recovery either duplicates or loses. Since crash timing
  can always produce a resend, design for at-least-once with stable identity (F-56); an
  implementation that tries for exactly-once will lose content instead.
- **W-21 · The periodic boundary will fire mid-operation.** A firmware session is
  call-dense, so the **100-call snapshot boundary** (and, every fifth one, the 500-call
  check-in) will regularly land inside a plan sequence, a batch, or a flash. Snapshot
  production, segment rolling (F-158), and the check-in prompt must all be safe at any point
  and must never interleave with, or delay, the operation that happened to trip the counter.
- **W-22 · The chain is defeated completely by stopping the server first.** This is the
  most likely way the integrity story gets over-trusted. With the process down, the file
  in hand, and a public hash function, an editor can change any record and recompute every
  link after it, or truncate the tail — and the result verifies. The chain catches
  accidents and corruption, not intent (F-87). Only the external witness closes this, and
  only for files already published (F-89, F-90, F-160). Until the pipeline of A-11 exists
  **there is no published head at all**, so the current honest guarantee is corruption
  detection only.
- **W-23 · Tamper detection will collide with routine automatic deletion.** Delete-on-ACK
  (F-134) removes files constantly and by design, and the result looks identical to
  truncation. An integrity check that reports tampering every time delivery succeeds trains
  the team to ignore it, and it will be ignored on the occasion that matters. Absence — of a
  whole run, or of the earlier segments of a run whose later ones are still resident — must
  be routine, and only partial inconsistency *within a file still present* is a finding
  (F-93, F-156).

---

## 8. Acceptance Criteria (definition of done)

### Classification and reporting

- **AC-1.** An unexpected server exception produces a Sentry issue tagged as a runtime
  error with triage class *server defect*, carrying the board-scoped trail, with no
  manual action.
- **AC-2.** A full session of correct guarded behavior — locked-tool refusals, all-NULL
  plan guidance, a closed gate, a containment rejection, a `no board` sentinel —
  produces **zero** server-defect reports. This is the primary gate; nothing else
  matters if this fails.
- **AC-3.** A tight retry loop produces exactly one grouped issue, raised by the
  server-side detector even if the model never invokes the skill.
- **AC-4.** The all-NULL-then-populated plan sequence, a `get_state` polling loop, and a
  validation retry using the returned `accepted_response` each produce **no** thrashing
  report.
- **AC-5.** Two or more rejected submissions of the same plan tool produce one
  plan-protocol issue (S-4) tagged *agent-behavior*, carrying which envelope rule was
  violated — without leaking the plan's payload contents.
- **AC-6.** A user request for a board operation with no route at all produces a
  coverage-gap issue (S-8) via the skill, with no exception raised — and a refusal that
  named a workable remedy produces no issue at all.
- **AC-7.** A containment refusal on an unmapped span produces an S-10 issue tagged
  *product feedback*, clearly distinguishable in the sink from a server defect.
- **AC-8.** An unplugged probe mid-session produces an S-3 issue tagged *environment
  fault*, not a server-defect issue, and is rate-limited to one grouped report.
- **AC-9.** When multiple model signals apply to one situation, a single collapsed
  report is filed.
- **AC-12.** Two concurrent boards, one failing, produce a report whose trail contains
  only the failing board's activity.

### Passivity and stdout

- **AC-10.** Under normal operation and under a report storm, stdout carries only MCP
  protocol traffic and the server remains stable; owned child processes emit nothing to
  the protocol pipe.
- **AC-11.** Report recording never blocks tool execution: measured tool latency with
  monitoring enabled is indistinguishable from baseline, no report is emitted from
  inside a board lock or a flash transaction, and pending records flush on clean
  shutdown and on stdio EOF without delaying teardown.
- **AC-17.** With the sink unreachable and the store path invalid, the server's tool
  behavior, refusals, and safety decisions are byte-for-byte unchanged from baseline.

### Content and redaction

- **AC-14.** No report contains memory contents, UART bytes, datasheet bytes, artifact
  contents, or full host paths.
- **AC-27.** No ledger record, summary, or delivery artifact contains credential material,
  memory contents, UART bytes, artifact contents, or full host paths.
- **AC-83.** Argument fingerprints are salted with a per-deployment secret: a fingerprint of
  a small/guessable input cannot be brute-forced back to its value, and the same value does
  not produce a matching fingerprint across two reports (F-149).
- **AC-84.** No mechanical-layer field carries this-codebase content or anything from which
  it could be reconstructed (symbol names, string literals, memory layout, logic); coarse
  operational metadata — tool identity, call order, outcome, timing — is present and is not
  treated as a leak (F-149, F-150). The personal-build narrative is exempt (F-153).
- **AC-87.** The personal-build narrative may name real code elements and describe the
  codebase (objective, per-action code context, summaries); it must **not** embed verbatim
  payloads — memory contents, firmware binaries, UART byte streams, or full command lines
  (F-153, F-3).

### Storage, workspace, and persistence

- **AC-13.** Each triggered report appears both in Sentry and as a local file in the
  per-workspace area of the per-user store (`<app-data>/BYO/server_data/<workspace_id>/`,
  F-131/F-161), and no report file is written inside the workspace project directory or under
  `.firm/`.
- **AC-15.** `initialization_handshake` accepts and validates a workspace path; it governs
  logging only and does not relocate `.firm`.
- **AC-16.** With no workspace path yet provided, a report still reaches Sentry and the
  local write buffers or fails loudly — it is never silently discarded.
- **AC-74.** The store resolves to the per-OS user application-data directory under a `BYO`
  app folder (`%LOCALAPPDATA%\BYO\`, `~/Library/Application Support/BYO/`,
  `~/.local/share/BYO/`) via a standard app-dirs mechanism; it is per-user and user-writable,
  and the `BYO_MCP_ARTIFACT_ROOT` override applies only as a last-resort fallback (F-131,
  F-132).
- **AC-70.** With the application-data directory unavailable, the store falls back to the
  operator root and never silently no-ops (F-132, F-37).
- **AC-98.** Runs are filed under a workspace level, and two workspaces on one machine
  produce disjoint sets of run files (F-161).
- **AC-99.** The pushed workspace identifier is a random token that is stable across runs
  for the same workspace and contains no path-derived material; no delivered artifact
  permits recovery of a workspace path (F-162, F-3, F-149).
- **AC-100.** The same project opened on two machines yields two unrelated workspace
  tokens, and no local code path attempts to reconcile them (F-163).
- **AC-101.** Records produced before the handshake binds a workspace are buffered and
  flushed into the bound workspace's file; with no workspace ever bound they are written
  under `unbound/` and delivered, never discarded (F-164, F-37).

### Ledger, chain, and file granularity

- **AC-18.** The ledger records usage snapshots (every 100 calls), check-ins, problem
  reports, and boot/close records — **not** one record per tool call; per-call sequence
  context appears only in the trail attached to problem reports (F-38, F-1, F-2).
- **AC-19.** Modifying or deleting any ledger record is detectable by chain verification;
  appending a valid new record is not falsely flagged.
- **AC-103.** Each usage snapshot carries the run's cumulative counts, not a per-window delta:
  withholding or dropping intermediate snapshots does not lower the total, because the next
  delivered snapshot still carries the true running total, and a count that decreases or a gap
  in the snapshot sequence is flagged by chain verification (F-165, F-86). Every problem report
  likewise carries the run's cumulative counts (§5.1).
- **AC-104.** The under-report defense is documented as three tiers with an honest ceiling:
  casual under-reporting defeated by cumulative counts plus the block; deliberate post-hoc
  editing detectable only via an off-box witness and only after cutover; source-level forgery
  by the machine owner neither prevented nor detected. No document claims a personal user's
  usage count is unforgeable (F-166, F-87, F-88).
- **AC-20.** After an abrupt process kill, every call completed before the kill is present
  in the ledger — durability came from the append, not from shutdown.
- **AC-35.** Every ledger record is attributable to the run that produced it, and a store
  containing files from several runs still yields a correct per-run comparison.
- **AC-39.** Two server instances sharing one store do not invalidate each other's chains,
  and neither produces a false tamper finding.
- **AC-40.** A record edited offline without recomputing the chain is detected at the next
  startup and reported as *chain invalid*.
- **AC-41.** A record edited offline **with** the chain recomputed is **not** detected
  locally — and the system does not claim it was. With a published head available, the same
  edit is detected as a head mismatch.
- **AC-42.** Startup verification distinguishes and reports differently: chain invalid,
  truncated against a published head, run absent, and verification impossible.
- **AC-43.** A store fully drained by successful delivery — and equally a log folder
  removed wholesale — produces no tamper finding on the next run.
- **AC-44.** With no published head available, startup reports verification state as
  *impossible* rather than *passed*.
- **AC-45.** The ledger file is created with append permitted and in-place overwrite
  denied; an attempt to rewrite existing bytes without first changing permissions fails,
  while appends continue to succeed.
- **AC-46.** On a platform or filesystem where the hardening cannot be applied, logging
  continues normally and both the health check and summaries report the hardening as
  absent.
- **AC-47.** No feature reads a ledger record from a previous run except the recovery path
  of F-54; a store drained by delivery between runs breaks nothing.
- **AC-91.** A run writes its own file, appends to it, and never rewrites it; after the run
  or segment ends the file's bytes do not change (F-154, F-157).
- **AC-93.** Every file present locally verifies as a complete chain from genesis to its
  last record; no resident file contains a back-link to a record outside itself except a
  segment's recorded predecessor head (F-156, F-158).
- **AC-95.** The append-only hardening remains applied for a file's entire life; no code
  path removes, weakens, or re-applies it in order to delete or compact records (F-157,
  F-94).
- **AC-96.** A long run rolls segments at the configured boundary; changing that boundary
  changes only the only-local window and leaves the ledger format, record contract,
  recording occasions, and callers unchanged (F-158, F-101, F-102).

### Delivery, deletion, and recovery

- **AC-24.** A run whose closeout send did not execute is detected at the next startup and
  its undelivered content is sent then; a run whose closeout succeeded is not resent
  without bound.
- **AC-25.** A resend caused by crash timing is deduplicable at the receiver by stable
  identity; nothing is lost in preference to avoiding a duplicate.
- **AC-26.** The closeout send does not extend shutdown beyond its fixed budget, and a
  failed or unconfigured send leaves exit status, teardown, and reset release unchanged.
- **AC-55.** A board/connection disconnect mid-run triggers no closeout send and does not
  reset the counter; a client exit does trigger a bounded closeout send after the close
  record is written (F-109, F-66).
- **AC-57.** The closeout send runs **after** reset release and owned-process termination:
  a slow or hung closeout never delays reset release and never strands a board or child
  process, a closeout killed mid-send loses nothing the append had already made durable,
  and undelivered content left by a killed closeout is delivered by bootup recovery on the
  next run (F-111…F-114).
- **AC-58.** Closeout is triggered by SIGINT and SIGTERM as well as stdin EOF; a client that
  signals without closing stdin still gets a close record and a closeout attempt, and the
  whole closeout completes inside the tightest targeted client kill grace so it is never
  truncated by SIGKILL (F-113, F-115).
- **AC-82.** A signalling client causes the flag-then-drain closeout to run — hardware
  released, close record written, send attempted — within the kill grace; no real work is
  done inline in the handler; and an unfinished send is delivered by bootup recovery at next
  start (F-148, F-115).
- **AC-59.** A run's undelivered spool is delivered at the next boot, after readiness is
  signalled, without the bootup send delaying the startup handshake or blocking the new
  run's early tool calls (F-116).
- **AC-105.** With the sender stuck on an unreachable or hanging endpoint, tool-call latency
  is indistinguishable from baseline, no request `await`s a send, and the server keeps
  handling calls, shutting down, and starting up normally; sealed files simply remain on disk
  for later background delivery (F-167, F-29, F-57).
- **AC-92.** Delivery, ACK, and deletion all operate on a whole file: an ACKed file is
  unlinked in full, and a file with no ACK is never partially modified (F-155).
- **AC-94.** Deleting an ACKed file produces no tamper finding, and a segment whose
  predecessor was delivered and deleted verifies normally (F-156, F-93).
- **AC-97.** Periodic delivery never pushes the file currently being appended to; bootup
  delivers prior runs' files; closeout seals and attempts the final segment, and a killed
  closeout leaves that file for the next boot (F-159, F-114).
- **AC-60.** Successful push is the only thing that removes a local record: an acknowledged
  file is deleted automatically with no operator action, an un-acknowledged file is never
  deleted by any code path, and no cleanup, rotation, or wipe mechanism exists to invoke
  (F-118, F-134).
- **AC-67.** Deletion is gated on the **ACK** (F-135) — a file leaves local storage when and
  only when its destination confirmed it — but no output ever equates that with a **durable
  off-box copy**: while the filler is active, a deleted file is reported as relocated to
  `simulated_remote`, not archived off-box.
- **AC-71.** On ACK the corresponding local `server_data` file is deleted; a fresh install's
  `server_data` holds only un-ACKed files; `simulated_remote` retains delivered files and is
  never drained (F-134, F-133, F-136).
- **AC-72.** A filler ACK deletes the `server_data` copy, yet the health check and summaries
  still report the delivery as filler / simulated and make no durable-off-box claim (F-135,
  W-18).
- **AC-75.** At cutover the `simulated_remote/` backlog is replayed through the real
  transport and drained on real ACKs; any file not yet replayed-and-ACKed is still reported
  as having no durable off-box copy (F-139, W-18).

### Transport seam

- **AC-23.** With no transport configured, every summary reports transport state **not
  configured** — never *sent* — and the server starts, runs, and exits normally.
- **AC-49.** Replacing the filler transport with a real one delivers the off-box copy with
  no change beyond configuration, and the reported state moves to *sent* without any other
  code path behaving differently.
- **AC-50.** While no real transport exists, the health check and summaries state plainly
  that no off-box copy exists and local permanence is not provided.
- **AC-54.** With the filler transport active and writing to `simulated_remote/`, every
  summary and the health check report transport state as **filler / simulated** — never
  *sent* (F-60, F-123).
- **AC-56.** No credential, token, or identity material from the OAuth or OpenID layers
  appears in any ledger record, summary, report, or local file (F-108, F-63).

### Counters, summaries, and health check

- **AC-21.** A usage snapshot is produced at the 100th call and every 100 thereafter, carrying
  cumulative per-tool and per-outcome counts, exercised-versus-advertised coverage, ledger chain head,
  store binding state, and transport state.
- **AC-106.** The snapshot cadence, check-in cadence, trail buffer size, and staleness
  threshold each resolve to a single named constant. Changing the snapshot cadence in that
  one place changes snapshot production (F-43), the segment roll (F-158), and the periodic
  recording occasion (F-77) together, with no other edit — and leaves the trail buffer size,
  a separate constant, unchanged (N-11, F-1).
- **AC-28.** A periodic boundary that falls inside a plan sequence, a batch, or a flash
  neither delays nor interleaves with that operation — including the segment roll and the
  check-in prompt (W-21).
- **AC-29.** The health check tool returns live counts that match the calls actually made
  in the run; calling it twice consecutively changes nothing but elapsed time, and it
  emits no record and triggers no send.
- **AC-30.** Counts survive a disconnect and a run-scoped authority reset within the same
  Server Run, and start from zero after a restart.
- **AC-31.** With the store unbound and no transport configured, the counter is still
  accurate and the health check still answers correctly — proving counts come from live
  state and not from reading logs back.
- **AC-34.** A test can assert which tools ran and with what outcomes entirely through the
  health check tool's response, with no hardware and no access to server internals.
- **AC-36.** With the store unbound, the counter leads the ledger, the health check reports
  the delta, and no integrity anomaly is raised — this direction is expected.
- **AC-37.** A ledger record injected for the current run is detected as an integrity
  anomaly and reported; it is never absorbed by taking the larger count.
- **AC-38.** With local writing broken, the health check and summaries show the counter
  ahead of total-appended and name the write failure; **no issue is filed about it**, and
  the condition is left to the staleness backstop to enforce (F-85, W-8).
- **AC-73.** A verifier does not flag ACK-deleted files as tamper, and a large
  counter-vs-resident-file-count gap is not treated as a fault — delivered files are
  supposed to be gone (F-138).

### Recording occasions

- **AC-32.** A boot record, one usage snapshot per 100 calls, and a close record are all
  produced locally in a run where no transport is configured.
- **AC-33.** When a closeout send fails or is unconfigured, the close record is still
  present locally — it was written before the send was attempted.

### Staleness backstop

- **AC-61.** In the current filler build, guarded hardware dispatch is never blocked: every
  filler delivery re-anchors the interval, and a fresh install with no anchor yet is dormant
  (F-122, F-123).
- **AC-62.** The health check and summaries report the current anchor and transport as
  filler / simulated, never as a real *sent*; no output implies a real off-box copy exists
  (F-123, F-60, W-18).
- **AC-63.** With a test transport reporting failure-always and injected dates, crossing the
  threshold refuses guarded hardware dispatch with a remedy-naming refusal, and the report,
  check-in, and health-check tools all remain callable throughout (F-121, F-125, F-127).
- **AC-64.** The staleness block never interrupts an in-flight flash or board lock; it applies
  only at the next guarded dispatch boundary (F-125).
- **AC-65.** A fresh confirmed delivery clears the block and re-anchors the interval; the
  block logic is unchanged when the real OAuth transport replaces the filler, differing only
  in anchor source and reported transport state (F-124, F-121).
- **AC-66.** With the local clock unreadable or unset, the block does not trip and the
  condition is logged; a bench machine with a dead RTC is not bricked (F-120).
- **AC-80.** When the server cannot reach the remote to deliver its tool-usage reports for
  **2 weeks**, it blocks further guarded hardware dispatch until a delivery succeeds; in
  every build the continuously-delivered mechanical reports (S-1/S-2/S-3) refresh the anchor
  on their own, so a professional build never bricks for lack of narrative, and narrative
  on/off does not affect the block (F-145).

### Agent-facing actions, forms, and builds

- **AC-22.** The routine check-in can be called at any point, including a session where
  nothing went wrong; it acquires no board lock, is refused as an `action_batch` child, and
  produces a record distinguishable from an issue report.
- **AC-51.** The skill ships enumerated per-signal intake templates; a freeform or malformed
  submission that does not conform to a template is rejected by intake (F-104, F-19).
- **AC-52.** The issue-report form and the check-in form are structurally distinct; a
  check-in can never be filed as, or mistaken for, an issue report (§5.2, §5.3, F-47, F-51).
- **AC-53.** An intake form carrying content that violates its layer's bar, or an internal
  identifier, is rejected and never recorded (F-153, F-3, S-12).
- **AC-68.** There are exactly three agent-facing actions; a routine check-in can never be
  filed as, mistaken for, or routed to the health-check action, or vice versa (§5.6).
- **AC-69.** The routine check-in is server-prompted on the **500-call** tick, its narrative is
  required, and it carries no severity / signal type / grouping; the usage-snapshot cadence is
  **100**, the check-in cadence is **500**, and the trail buffer stays ~100 (F-128, F-129, F-130).
- **AC-79.** The skill criteria are a single tool-agnostic file; no Codex- or Claude-Code-
  specific skill variants exist (F-144).
- **AC-85.** A personal-build issue report carries all Band-1 fields (`codebase_objective`,
  `hypothesis`, `goal`, `plan`, `failure_point`, and `signal_subcase` where required),
  exactly the last 5 actions in `recent_actions`, and a compressing `earlier_phases` plus a
  one-line `session_start`; detail decreases monotonically with distance from the failure.
- **AC-86.** A personal-build check-in carries `codebase_summary`, `work_summary`,
  `tools_used`, and `effectiveness_observed`, the last stated as observable outcomes only
  with no self-rating of any kind.
- **AC-88.** Each of the (up to) 5 `recent_actions` carries `code_context` — the goal of the
  action in the codebase and what it did for the codebase.
- **AC-76.** In a build with `narrative_logging` disabled, no routine check-in is ever
  prompted or accepted, the error-report narrative fields are absent, and no S-4…S-14 report
  is produced; narrative content that arrives anyway is discarded, never stored (F-140).
- **AC-77.** The same sensitive build still records the full server-only layer and still
  fires S-1/S-2/S-3 with mechanical anchors only; a runtime error produces a report carrying
  the failing tool and error signature but no narrative (F-141).
- **AC-102.** The server-only mechanical output — ledger records, trail, counters, and
  server-detected S-1/S-2/S-3 reports — is **byte-identical** whether `narrative_logging` is
  enabled or disabled: a test drives the same tool sequence through a personal and a
  professional build and diffs the mechanical output, asserting no difference. This is what
  makes "professional is a pure subtraction" (F-141, §5.5) verifiable rather than assumed.
- **AC-78.** The health check and summaries report `narrative_logging: enabled | not_built`;
  in a sensitive build the absence of check-ins and model-detected reports is reported as a
  build capability, never as degraded logging or a fault (F-142).
- **AC-81.** In a professional build the report tool is present and callable and returns the
  professional-license message without authoring, storing, or sending a report; its presence
  produces no S-5; and server-detected S-1/S-2/S-3 still fire with mechanical anchors (F-146,
  F-141).
- **AC-90.** A professional build emits no code-specific or reconstructable content, but does
  emit coarse operational metadata (tool identity, order, outcome, timing); no user-facing
  description of professional mode claims zero-egress (F-143a, F-150).

---

## 9. Retired identifiers

These IDs existed in earlier drafts and have been removed. They are listed so a reference
in older material can be resolved, and so the numbering gaps are not mistaken for omissions.

| Retired | Was | Replaced by |
|---|---|---|
| F-107 | Filler writes to a temporary folder | F-133 — `simulated_remote/` is persistent |
| F-110 | Monitoring output stays under `.agent-workspace/logs` | F-131 / F-161 — per-user app-data store, per-workspace subdivision |
| F-105 | Two intake templates, check-in narrative optional | §5.2 / §5.3 — both narratives specified and required (F-128) |
| F-137 | Chain verification covers only a trailing resident window | F-156 — whole-file deletion means every resident file verifies completely |
| F-151, F-152 | Narrative field lists stated as amendments | §5.2 / §5.3 — folded into the contracts section |
| F-53 | Closeout bounded by a "small fixed budget" | F-111 / F-113 — budget dictated by the client kill grace |
| F-43 (early wording) | Summary every 100, then every 500 | F-43 / F-129 — usage snapshot every **100**; separate check-in every **500** (personal); trail buffer keeps ~100 (F-1) |
| AC-48 | Only-local window is a configurable documented value | AC-96 — same property, now expressed as the segment roll cadence |
| AC-89 | Check-in carries `codebase_summary` | AC-86 — merged, since the field is required by §5.3 |

Superseded wording that was folded into a surviving requirement rather than retired is
noted inline at that requirement.
