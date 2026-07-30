# Iteration 4 — Diff Adversary

Scope: `git diff 0fff3f1..HEAD` — commits 9f8f6bc (C12/D11 structural legacy-token fix),
9a11c83 (C13 confirmation test + C14/D13 fix), ced6231 (review artifacts only).
`reviews/ledger.md` (27 rows) and iterations 1-3 read in full first. This is the
termination-gate pass: four directed attack targets plus an independent sweep.

## Summary

| Severity | Count |
| --- | --- |
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 0 |
| LOW / PROCESS | 1 (D14) |

Three of the four directed targets closed with **no new defect** after genuine attempts
to break them (details below, reported as negative results rather than omitted, per the
instruction not to manufacture findings). The fourth (legacy-token reachability) is
answered as requested — the code's current behavior is correct, and the dead-code
question is answered "no, keep it," with the reasoning laid out since the ledger's own
docstrings assert a narrower reachability claim than what actually holds. One new,
genuinely out-of-diff process finding (D14) surfaced during the target-4 sweep and is
reported because it affects whether this review's own "suite is green" verification is
trustworthy.

**Suite state as actually reproduced:** ruff clean (confirmed, both with and without the
uncommitted `probe_families.py` change — see D14). Test count is 617 with 7 skips when
green, but the suite is **not reliably green under repeated runs on this machine** — see
D14 for the reproducible cause (unrelated to this diff) and the code-review file for a
second, also out-of-diff, reproducible flake in `test_swd_process_isolation.py`.

---

## Directed target 1 — `probeid:`/`probe:` prefix-length and lexical-ordering sweep: no defect found

Grepped every slice/split/partition touching a connection token
(`services/connections.py`, `hardware_inventory.py`, `server.py`) for a hardcoded
offset (`[6:]`, `len("probe:")`, etc.) instead of `len(PROBE_CONNECTION_PREFIX)` /
`len(LEGACY_PROBE_CONNECTION_PREFIX)`. Every site uses the named constant or
`.split(":", 1)` / `.partition(":")` (which locate the delimiter dynamically, not by a
fixed offset):

- `services/connections.py:85` — `rest = connection_id[len(PROBE_CONNECTION_PREFIX):]`.
- `hardware_inventory.py:806-807`, `server.py:3691-3692, 4863-4864, 4885, 4893` — all
  gate on `startswith(LEGACY_PROBE_CONNECTION_PREFIX)` first, then
  `.split(":", 1)[1]`, which finds the first colon wherever it actually is rather than
  assuming a fixed length. The one unrelated `token.split(":", 1)[1]` at
  `server.py:1038` operates on a `"hook:{hook_id}"` provenance tag, not a connection
  token, and is similarly delimiter-relative, not offset-relative.

No repo-wide grep for `[6:]`, `[7:]`, `[8:]`, `len("probe`, or similar turned up a
literal-length slice anywhere touching either prefix. **No off-by-N exists.**

On sorting/grouping: grepped every `.sort(` / `sorted(` call in `src/`. The only one
touching connection state is `ConnectionManager.assigned_board_ids()`
(`services/connections.py:228`, `sorted(self._connections)`), which sorts **board IDs**,
never `connection_id` tokens. `discovery_hooks.py:675` sorts hook specs by
`(source, kind, hook_id)` — unrelated to the probe-token prefix. Nothing lexically
compares or groups `probeid:...` / `probe:...` / `session:...` strings by anything
sensitive to the prefix change (e.g. no code assumes `probe:` sorts before or after
`session:`, or relies on a shared prefix length for a dict/set key derivation outside
the functions already covered by C12/D11's fix and its tests). **No finding.**

---

## Directed target 2 — legacy `probe:` reachability: answered "yes, genuinely reachable, keep it"

The ledger's C12/D11 resolution states the exposure is "specifically a token minted by
a pre-FIX-8 server that an agent still holds... being replayed against the post-FIX-8
comparison/resolution helpers" and separately that gate/authority state "lives on
`ServerRun` and never survives a process restart, so no persisted tokens exist." Both
of those statements are individually true (verified independently below) but they do
not add up to "legacy tokens are unreachable in the code as it stands today." They
answer a narrower question (does *state* persist across a restart) than the one that
determines reachability (can a client supply a legacy-shaped string as an ordinary
argument, right now, in a single continuously-running process).

**Verified independently, no persistence exists:**
- `kernel/run_state.py::ServerRun` is a plain in-memory dataclass; `clear_authority()`
  clears four dicts and nothing else. No `__reduce__`, no file I/O, grepped clean.
- `guardrails/gate.py` is explicitly docstringed "Run-scoped... live-identity and
  safety-map gate state" and its backing dict is one of `ServerRun`'s four fields.
- `services/connections.py` (`ConnectionManager`) and `hardware_inventory.py`
  (`ProbeSelectionStore`, `SessionUartSelectionStore`) are all plain in-memory
  structures; grepped for `json.dump`, `pickle`, `write_text`, `open(...,"w")` — no
  hits in any of the three files.
- `firmstore/cache.py`'s `AttachmentCache` and `firmstore/reports.py` never reference
  `connection_id` at all.

**But the legacy path does not need any of that state to survive anything, because it
is stateless re-derivation from the token text itself:**
`hardware_inventory.derive_selection_from_token(connection_id, snapshot)`
(`hardware_inventory.py:765-823`) is called by `ProbeSelectionStore.resolve()`
(`:646-667`) **whenever `recorded is None`** — i.e. any time the presented
`connection_id` was never recorded (first use, evicted past `MAX_PROBE_SELECTIONS=256`,
or the store was cleared by a `refresh_discovery_hooks` call). It works purely by
parsing the string and matching it against the *current* snapshot's rows; it consults
no stored assignment. And the string it parses is not always server-minted: `probe_id`
is a plain, optional, client-supplied argument on the public `board_validate` tool
(`tools/setup.py:535`, `def board_validate(board_id: str, probe_id: str | None = None)`),
which flows into `_select_probe` / `_connection_matches_probe`
(`server.py:3669-3695`) — another function that explicitly branches on
`candidate.casefold().startswith(LEGACY_PROBE_CONNECTION_PREFIX)`.

So concretely, **today, in one continuously-running server process, with no restart
involved at all**, an agent that types (or remembers from an earlier turn, or copies
from stale documentation/examples, or simply guesses) a `probe:{uid}` string into the
`probe_id` argument of `board_validate` exercises the legacy path exactly as designed.
This is a normal, expected client interaction pattern for an MCP tool with a
free-text optional argument, not an exotic replay-across-upgrade scenario.

**Conclusion on the "harder question":** the legacy path is not dead code and must not
be deleted. It is live, client-reachable, ordinary-path code, correctly and safely
implemented (per C8/C12/D11's fixes: refuses on cross-provider ambiguity rather than
guessing, and can never collide with a canonical token since the prefixes are now
structurally distinct). The only correction needed is to the *stated* justification in
the ledger/docstrings — "an agent holding a token from before this upgrade" undersells
how easily this path is hit; it is any client supplying `probe:`-shaped text to
`probe_id`, on every server run, indefinitely. This is not a code defect (the behavior
is correct either way), so it is not filed as a numbered finding — it is the answer the
coordinator asked for.

---

## Directed target 3 — C13 "fails closed" claim: re-attacked, still holds

Attacked the specific angle requested: does anything write one direction of
`RunAssignmentStore`'s two-way mapping without the other, or clear one side without the
other?

Grepped every mutation site of `RunAssignmentStore._assignments`
(`setup_flow/setup.py`): `assign` (:878-895), `replace` (:897-909), `clear_connection`
(:969-974), `clear_board` (:976-981) are the **only four** methods that touch the dict,
and every one writes or pops both `("connection", x)` and `("board", y)` keys inside
the same `with self._guard:` block — none partial. `run_if_current` (:949-967) and
`require` (:911-924) only read. The sole external touch is
`ServerRun.clear_authority()` (`kernel/run_state.py:38`,
`self.assignments.clear()`), which empties the whole dict at once (symmetric by
construction, since it's the same dict backing both key namespaces). Grepped the whole
tree for any other direct `.assignments[...]` write — `server.py:330` is the only
instantiation site (`assignment_store = RunAssignmentStore(server_run.assignments)`),
and nothing else reaches into the dict directly. **No asymmetric-write path exists.**

Went one level deeper than iteration 3's C13/D12 test, which only exercised the
*profile-divergence* half of the risk. The other half — a **TOCTOU between the two
separate `connection_manager.maybe_connection(board_id)` reads** inside
`_stamp_validation_session` (`server.py:3532`, direct) and
`_known_provider_for_board` (`server.py:3483`, called indirectly via
`_provisional_setup_connection_id` at `server.py:3541`) — looked like a plausible way
to construct a canonical-*looking* provisional key from a stale probe_uid paired with a
*different*, freshly-reconnected probe's provider. Traced it to ground: both
`_stamp_validation_session` and `_record_validation_mismatch` are only ever invoked as
`ValidationHooks` callbacks from `BoardValidator._validate_locked`
(`setup_flow/validate.py:242`), which itself only runs inside
`with self._lock_for_board(request.board_id):` (`validate.py:239`), and
`_board_validator` is constructed with `lock_for_board=connection_manager.lock_for`
(`server.py:3651`). Every other mutator of `connection_manager`'s per-board state in
`server.py` (`connect`, `disconnect`, and 15+ other sites) is itself wrapped in
`with connection_manager.lock_for(board_id):`. So the two `maybe_connection(board_id)`
reads inside one validation call are provably serialized against any concurrent
reconnect/disconnect for that same board — the TOCTOU I set out to find is closed by a
lock discipline that already exists for an unrelated reason (per-board operation
serialization), not by anything added for C13. **No new defect found; the fails-closed
conclusion stands, on a broader basis than iteration 3 established.**

---

## Directed target 4 — skip audit and holistic test-suite integrity

**All 7 skips, enumerated and independently verified justified** (ran
`unittest discover -s tests -v` and confirmed the skip reason against the actual
runtime condition, not just the decorator text):

| # | Test | Reason | Verified still valid? |
| --- | --- | --- | --- |
| 1-2 | `test_discovery_hook_process.py::PlatformSpecificTests` (executable-runner, execute-bit tests) | `@unittest.skipIf(os.name == "nt", ...)` — POSIX execute-permission semantics don't exist on Windows | Yes — this machine is Windows; the equivalent Windows path is covered elsewhere in the same file without the skip |
| 3-4 | `test_discovery_hook_registry.py::ContainmentTests` (symlink escape / symlink-inside-root) | `self.skipTest(...)` on `OSError` from `os.symlink` | Yes — reproduced the actual `WinError 1314` ("required privilege not held") on this account; this is the standard non-elevated/non-Developer-Mode Windows symlink restriction, not a stale excuse |
| 5 | `test_server_trust_model_round_2.py` (stable directory link test) | same symlink-privilege skip | Yes — same `WinError 1314`, unrelated to this feature |
| 6 | `test_server_trust_model_round_3.py` (r3_04 empty selected directory link) | same symlink-privilege skip | Yes — same cause |
| 7 | `test_trusted_input_admission.py` (official STM32U5 pack integration) | requires `BYO_MCP_OFFICIAL_U5_PACK` env var pointing at a real pinned pack | Yes — deliberate opt-in integration test, not something that should run by default in CI without the real artifact |

None of the 7 are stale, none are discovery-hook-feature skips masking untested new
behavior, and none would flip to a false pass if the underlying behavior were deleted
(they are unconditional `skipTest`/`skipIf`, not conditional assertions).

**Same-fixture-both-sides recurrence check (the C13 anti-pattern):** read every test
added or touched by iterations 2-4 in `test_setup_overview_no_probe.py`,
`test_probe_selection_records.py`, `test_server_assignment_connect.py`. The two C12/D11
regression tests (`test_a_legacy_uid_containing_a_colon_still_resolves_to_its_own_probe`
/ `..._never_resolves_to_a_different_probe`) assert against hardcoded literal values
(`"cmsisdap"`, the literal uid string), not values re-derived from the function under
test. `StructuralLegacyDiscriminationTests::test_a_provider_containing_a_colon_still_round_trips`
does mint-then-parse through the same two production functions, but that is a
legitimate encode/decode round-trip invariant (checking the two functions are inverses
of each other), not the C13 pattern of deriving the *expected* value from a fixture
that can't diverge from the *actual* value under test. The one test purpose-built to
close the exact gap C13 identified,
`test_a_stale_divergent_profile_provider_fails_closed_not_wrong`
(`test_server_assignment_connect.py`), correctly constructs its two sides from
genuinely independent fixtures (a `BoardConfig` declaring `probe_family="jlink"` versus
a stored assignment minted for `"cmsisdap"`) — confirmed by reading it end to end, not
assumed from its name. **No recurrence of the pattern found.**

---

## D14 — LOW / PROCESS — the "617 passed / 7 skipped, ruff clean" baseline is not reproducible from a clean checkout of the commits under review; it silently depends on an uncommitted, out-of-scope working-tree change

**Not a defect in the reviewed diff** (`0fff3f1..HEAD` never touches
`probe_families.py`), and not something to fix per this task's explicit instruction to
leave the three leftover files alone. Reported because it bears directly on whether
this and prior iterations' "suite is green" claims are trustworthy, and the task
brief's own CURRENT STATE line asserts exactly that baseline.

**Reproduction:**
```
git status --short
 M src/pyocd_debug_mcp/probe_families.py       # uncommitted, unstaged
?? tests/test_preflight_probe_guidance.py       # untracked
?? tests/test_probe_cli_command.py              # untracked
```
`unittest discover -s tests` picks up the two untracked test files (they are ordinary
`.py` files under `tests/`, discovered regardless of git tracking status). With the
working tree exactly as it sits (dirty `probe_families.py` present), the full suite
passes: 617/7. Stashing *only* `probe_families.py` (leaving the two untracked test
files in place, i.e. reproducing what a `git clone` of `ced6231` plus those two
leftover files would actually contain) and re-running just those two test files
reproduces one failure:

```
FAIL: test_default_inventory_uses_the_server_interpreter
  (tests.test_probe_cli_command.ProbeCliCommandTests)
AssertionError: Tuples differ:
  ('...\\.venv\\Scripts\\pyocd.EXE', 'list', '--probes')
!= ('...\\.venv\\Scripts\\python.exe', '-m', 'pyocd', 'list', '--probes')
```
i.e. `tests/test_probe_cli_command.py` asserts on behavior (`configured_probe_cli_commands()`
routing pyOCD invocation through `sys.executable -m pyocd`) that only the *uncommitted*
version of `probe_families.py` implements; the version actually committed at `ced6231`
does not have this change, and the test fails against it.
`tests/test_preflight_probe_guidance.py`'s one test is unaffected either way (ruff also
stays clean either way — this is purely a test-suite issue, not a lint issue).

**Consequence:** anyone verifying this work from a fresh `git clone` (or after
`git clean -fd` / `git stash` in this checkout) gets a suite failure that has nothing to
do with the discovery-hook feature, sourced entirely from another session's in-progress,
uncommitted edit sitting in the same working tree. This iteration's own verification
runs (see the code-review file for the second, independent flake found during this
sweep) were performed against the dirty tree as instructed, so the feature-under-review
itself is unaffected — but the "suite is green" framing in the task brief is only true
of this specific working directory's current, uncommitted state, not of the commits it
names. Flagging for the coordinator's awareness; no action taken on the leftover files
per instruction.
