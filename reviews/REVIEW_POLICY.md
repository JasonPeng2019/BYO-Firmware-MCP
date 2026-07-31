# Review policy — materiality and test efficiency

Binding on all review, adjudication, and fix work in this repo from 2026-07-30.
Supersedes the looser conventions used in iterations 1–5.

**The goal is a working product delivered efficiently, not a perfect one.** The
loop exists to find defects that matter, not to grind indefinitely on conditions
that never occur.

## 1. Safety cap

Raised from 5 to **10 iterations**. The loop runs until it clears. If iteration
10 is reached without clearing, stop and summarize.

## 2. Clearing condition (changed — read this)

Previously: an iteration cleared when it produced zero VALID findings.

**Now: an iteration clears when it produces zero findings adjudicated MUST-FIX.**

Findings ruled EXTRANEOUS are recorded in the ledger with that verdict and do
**not** block clearing. This is a deliberate lowering of the bar, made explicit
rather than quietly applied. It is what makes the loop terminable: without it, a
sufficiently motivated adversary can always find *something*, and the loop never
ends even when the product works.

## 3. Division of labour

- **The reviewer still reports everything it finds.** Do not self-censor, do not
  pre-filter, do not soften a finding because it might be ruled extraneous.
  Under-reporting hides real defects; the filter belongs downstream where it can
  be applied consistently and recorded.
- **The orchestrator adjudicates materiality.** Every finding gets MUST-FIX or
  EXTRANEOUS, with a one-line reason, in `reviews/ledger.md`.

## 4. Materiality rubric

### MUST-FIX

- Wrong behaviour on an **ordinary path with realistic inputs** — the wrong
  hardware selected, wrong data returned, corrupted state, crash, hang, deadlock.
- Data loss, resource leaks, or unbounded growth reachable in normal operation.
- Security or isolation failures.
- **A test that passes when the behaviour it names is broken.** This is
  always MUST-FIX regardless of how narrow the underlying case is. A false green
  is worse than no test, and this task produced three of them (C15's, D16's,
  D18's).
- A guide requirement not actually implemented end to end (cf. D15 — scaffolded,
  unit-tested, never wired to production).
- Anything that makes an operation exceed or misreport its timeout budget in a
  configuration a user can actually reach.

### EXTRANEOUS — record, do not fix

- Failure modes requiring inputs no caller can produce, or that the type system,
  validation layer, or call-site contract already prevents.
- Hardening beyond the surrounding code's existing norms. **If the adjacent,
  already-reviewed code has the identical exposure, it is not a new defect** —
  it is a pre-existing property of that layer. (Precedent: `_run_cmd` not
  catching `PermissionError`/`OSError`, ruled out of scope because
  `native_probes` on the line above has the same exposure.)
- Style, naming, comment density, structural preference.
- Defensive checks against conditions that cannot occur given actual call sites.
- Pre-existing defects outside the current diff. Record them for separate triage;
  do not fold them in. (Precedent: the `continue_setup` inventory-budget gap.)
- Speculative "a future caller might" reasoning with no current caller.

### The test to apply

**Does a realistic user, in a configuration they can actually reach, get a wrong
or broken result?** If no — record it and move on. If the answer is "only if
someone passes a value nothing produces," it is EXTRANEOUS.

When genuinely uncertain, prefer MUST-FIX for anything touching hardware
selection, data integrity, or test validity; prefer EXTRANEOUS for everything
else. Do not fix something merely because fixing it is easy — every change is a
chance to introduce the next defect, and in this task a fix has done exactly that
four consecutive times.

## 5. Test execution — do not rerun the world

Running the full suite takes ~3–4 minutes, and
`tests/test_swd_process_isolation.py` spawns real subprocesses. Rerunning
everything after each one-line fix is waste.

**During a fix/verify cycle, rerun only what is affected:**

```bash
python -m unittest tests.test_module.ClassName.test_name   # one test
python -m unittest tests.test_module.ClassName             # one class
python -m unittest tests.test_module                       # one module
```

**Run the full suite only at these points:**

1. Before reporting a round complete.
2. Before a commit.

Not after every intermediate edit, and never "just to be sure" mid-iteration.

The same rule applies to breaking-a-test-to-prove-it-fails experiments: run the
single affected test, not the suite.

Lint and types can also be scoped while iterating — `ruff check <file>`,
`pyright <file>` — with the full `ruff check src/ tests/` and `pyright src/` at
the same two checkpoints as the suite.
