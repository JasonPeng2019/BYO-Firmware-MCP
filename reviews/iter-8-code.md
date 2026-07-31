# Iteration 8 — Code Adversary

Scope: the codebase at HEAD (`a8eb383`), attacked fresh as a hostile reviewer with no diff
context, per `reviews/REVIEW_POLICY.md` and the coordinator's breadth-over-depth steer.
Deliberately spent this round outside `hardware_inventory.py`/`kernel/operations.py`
(swept clean four times running) and inside the failure-code family
(`discovery_failures.py`) and its callers, which no prior round examined for call-site
completeness — every prior pass checked whether the *functions* build correct payloads,
never whether production code actually *calls* them under their real trigger condition.
`reviews/ledger.md` (C1–C18, D1–D24, M1–M6) and all prior iteration reports read first.

**Verification performed this round:** `uv run --locked ruff check src tests` → all
checks passed. `uv run --locked pyright src` → 0 errors. `PYTHONPATH=src python -m
unittest discover -s tests` → 666 passed, 7 skipped, OK.

## Summary

| ID | Severity | One-line |
| --- | --- | --- |
| C19 | HIGH (cross-reference to D25–D28) | Reading `discovery_failures.py` cold, then grepping for who actually calls its exported functions, surfaces the same four-part gap independently of any diff history: `unsupported_provider_failure` and `selection_disappeared_failure` have zero production callers, `UART_OPEN_FAILED` is a dead branch of `open_failure_payload`, and `_no_native_probe_overview`'s kind-blind `hook_failures[0]` selection is visible on inspection once you know `hook_failures` isn't kind-filtered. |

No further findings from a fresh, diff-blind pass beyond what's detailed in
`reviews/iter-8-diff.md` (D25–D28) — this file adds the cold-read confirmation and a few
additional angles checked from that lens, plus a short independent sweep.

---

## Cold read: `discovery_failures.py`, as a reviewer who has never seen `hardware_inventory.py`'s history would encounter it

Read the whole 319-line module top to bottom with the question a diff-blind reviewer would
naturally ask of any small, self-contained module that exports ten named failure
constructors: **is every one of these actually reachable?**

- The module's own docstring states two rules as "structural, not documentary" — the
  open-failure/no-hook-contract guarantee, and "nothing here stamps a gate." Both are true
  and enforced (confirmed independently: `open_failure_payload`'s fixed key set plus its
  `assert`, and no `gate_manager`/`assignment_store` reference anywhere in the file).
  Reading a module whose own docstring makes such precise, checkable claims is what
  prompted checking every *other* implicit claim the module makes — namely, that each
  failure code it defines corresponds to a real, reachable condition.
- `ALL_FAILURE_CODES = DISCOVERY_CODES | OPEN_FAILURE_CODES` — ten string constants. Wrote
  out, from the module alone, what event each one is supposed to correspond to (the
  docstrings and the guide's own step-8 table agree closely), then searched the rest of
  `src/` for a call site that could plausibly produce each one under its stated condition:

  | Code | Constructor | Called from `src/` for its real condition? |
  | --- | --- | --- |
  | `discovery/no-native-probe` | `no_native_probe_failure` | Yes — `server.py:4963`. |
  | `discovery/no-native-uart` | `no_native_uart_failure` | Yes — `server.py:4629`. |
  | `discovery/hook-failed` / `-timeout` / `-output-invalid` / `-source-changed` | `hook_failure` | Yes, dynamically, via `HookExecution.failure_code` — `server.py:4956` — **but see D28: the kind passed is hardcoded, not derived from the failing execution.** |
  | `discovery/unsupported-provider` | `unsupported_provider_failure` | **No.** Zero callers outside its own unit test. |
  | `discovery/selection-disappeared` | `selection_disappeared_failure` | **No.** Zero callers outside its own unit test; both real `SelectionDisappeared` catch sites bypass it. |
  | `probe/open-failed` | `open_failure_payload(PROBE_OPEN_FAILED, ...)` | Yes — `server.py:1261`. |
  | `uart/open-failed` | `open_failure_payload(UART_OPEN_FAILED, ...)` | **No.** The one call site to this function never passes this code. |

  Three of ten codes (and one dynamically-selected one, imprecisely) never fire as
  intended. Detailed in `reviews/iter-8-diff.md` as D25 (unsupported-provider, HIGH),
  D26 (selection-disappeared, MEDIUM), D27 (uart/open-failed, MEDIUM), D28 (kind
  mislabeling, MEDIUM-HIGH). Not duplicating the full analysis here — recorded as C19 to
  mark that the same finding is independently reachable without any diff-history context,
  purely from reading one module and grepping its call graph.

- **Why `pyright` cannot catch this class of bug.** `hook_failure(code: str, kind:
  FailureKind, ...)` — passing the string literal `"probe"` where a *correctly-typed but
  semantically wrong* value belongs type-checks perfectly; `FailureKind = Literal["probe",
  "uart"]` only constrains the value to one of the two valid strings, not to the *correct*
  one for the data being described. This is a purely semantic/logical defect, invisible to
  both `ruff` and `pyright` (both confirmed clean at HEAD) — the kind of bug static
  analysis structurally cannot find, which is exactly why a human, source-reading pass
  matters here.

---

## Independent sweep, diff-blind

Beyond the failure-code cluster, spent remaining budget checking a few areas neither this
round's diff report nor any prior round examined closely, per the coordinator's explicit
"go where the sweeps have not been":

- **`_get_setup_status`'s probe-readiness branch** (`server.py:4523-4614`) — a different
  question from "is any probe visible" (it's "is *this connected* board's live session
  ready for flash planning"), so it has no analogous unsupported-provider/selection-
  disappeared exposure of its own; its one discovery-failure-shaped call
  (`no_native_uart_failure` at `:4629`) is the same call already covered under D28's
  "related" note in the diff report. No separate finding.
- **`carries_hook_contract`'s recursive walk** (`discovery_failures.py:311-318`) — checked
  for a depth or cycle problem (a payload containing a self-referential or deeply nested
  structure). `DiscoveryFailure.to_payload()` (the only real payload producer) builds a
  shallow, fixed-depth dict from dataclass fields — no path to unbounded recursion in
  practice. Not filed; purely defensive code with no reachable pathological input.
- **`hook_failure`'s `code not in _HOOK_FAILURE_MESSAGES` guard** (`:221-222`) — raises
  `ValueError` for any code outside the four hook-failure constants, which would fire if
  `HookExecution.failure_code` ever returned something outside its own four-way
  `if`/`elif` chain (`discovery_hooks.py:284-296`). Read that chain: it's exhaustive over
  the four non-`ok` outcomes plus a `return "discovery/hook-failed"` catch-all — no path
  returns an unrecognized string. Consistent, no defect.

No further findings.
