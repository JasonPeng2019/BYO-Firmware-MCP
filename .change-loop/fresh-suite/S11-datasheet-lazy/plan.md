# Change implementation plan

## Source change list

- Source: `.change-loop/fresh-suite/S11-datasheet-lazy/changes.md`
- Goal summary: Make generic datasheet applicability evaluation stop after the first valid
  metadata/page identity proof instead of extracting every PDF page eagerly, while preserving
  exact PDF hashing, server-derived identity authority, deterministic evidence order, complete
  late/no-match inspection, typed failures, and all prior repairs.

## Repository context and assumptions

- Verified architecture and relevant entry points:
  - `src/pyocd_debug_mcp/setup_flow/datasheet_evidence.py` is the single owner of
    `prove_datasheet_applicability()`. It hashes the exact payload, checks that the requested part
    is among server-derived terms, constructs `pypdf.PdfReader`, searches metadata and page text,
    and returns `DatasheetApplicabilityProof`.
  - The current implementation builds one list containing metadata plus `extract_text()` for
    every page before entering the matcher loop. It therefore performs all page extraction even
    when metadata or page 1 proves the requested authority.
  - Production callers are limited to the generic setup/replay paths in `server.py`. Those paths
    call the function repeatedly during fresh setup; no caller needs all-page text after a proof
    has been established.
  - Existing H04 datasheet spec/regression suites assert exact term boundaries, placeholder
    provenance, real official-document controls, proof fields, and typed failures. No existing
    test asserts extraction call order or early termination.
  - Main-model S11 evidence independently reproduced a public paired setup request with no reply
    at 300 seconds. A direct benchmark of the 18,277,247-byte official nRF52840 PDF took 92.421
    seconds and returned `evidence_locus="metadata"`, proving the all-page work was unnecessary.
- Existing test/build commands relevant to the change:
  - `.h01-venv-batchstrict/Scripts/python.exe -m unittest -q
    tests.test_h04_datasheet_binding_spec tests.test_h04_datasheet_regressions`
  - `.venv/Scripts/ruff.exe check src tests`
  - `.venv/Scripts/ruff.exe format --check src tests`
  - `.venv/Scripts/pyright.exe src/pyocd_debug_mcp/setup_flow/datasheet_evidence.py`
- <!-- Assumption: once parser-extracted metadata or an earlier page establishes an exact
  server-derived authority term, extracting unrelated later pages adds no correctness authority.
  A later malformed page therefore cannot invalidate an already established proof. If no earlier
  proof exists, every page remains required and any parser failure retains the current typed
  unreadable-PDF result. This resolves the behavior toward the charter's simplest correct
  evidence path without caching or weakening identity checks. -->

## Plan items

### CL-001 — Evaluate datasheet identity evidence lazily in deterministic order

- **What to change:** Replace the eager metadata-plus-all-pages list construction inside
  `prove_datasheet_applicability()` with the smallest obvious control flow that evaluates one
  evidence source at a time. Check metadata first, then call `extract_text()` on pages in document
  order, and return immediately on the first valid server-derived term. Keep parsing and matching
  inside the existing typed error boundary. Do not change callers, public dataclasses, authority
  derivation, matching rules, or add a cache/timeout/configuration surface.
- **Where:** Production change only in
  `src/pyocd_debug_mcp/setup_flow/datasheet_evidence.py`. Tester roles add separate focused files
  under `tests/` and do not modify production or the existing H04 suites.
- **Exact intended behavior:**
  1. Compute `pdf_sha256` from the exact supplied bytes exactly as today and reject a requested
     identity absent from `authority_terms` before accepting any parser evidence.
  2. Construct the same `pypdf.PdfReader`, normalize metadata by joining its values exactly as
     today, and test every normalized server-derived term in its existing tuple order. A match
     returns the same proof schema with `evidence_locus="metadata"` and the same
     `pypdf-{version}` parser version without calling any page's `extract_text()`.
  3. If metadata does not match, enumerate pages from index zero. Extract and evaluate one page
     before advancing to the next. A page-N match returns `evidence_locus="page:N"` and no later
     page is extracted.
  4. A match on the final page extracts every page exactly once. A no-match document extracts
     every page exactly once and raises the current `DatasheetApplicabilityError` text naming the
     requested part and verifiable official evidence.
  5. Metadata absence/`None` and page `extract_text()` returning `None` retain the current empty
     text behavior. Parser construction, metadata access, page iteration, or any required page
     extraction failure is still normalized to the current typed
     `datasheet PDF could not be read for MCU applicability` error with the parser exception as
     its cause.
  6. `_contains_identity_term()`, normalization, exact token boundaries, verified-family `xx`
     convention, term priority, payload digest, proof field values, and all real official
     datasheet accept/reject outcomes are byte-for-byte compatible apart from skipping
     unnecessary later-page parsing after a valid earlier proof.
- **Must remain intact:** No fabricated identity; exact payload hashing; server-derived authority
  only; deterministic metadata-before-pages and term ordering; complete late/no-match scanning;
  current exception types/text/causes for required parsing; public APIs and dataclasses; all H04
  and H05 behavior; provider/OS/board/part neutrality. Add no cache, persisted authority, timeout,
  retry, dependency, public knob, board/part/path special case, server caller edit, or unrelated
  refactor.
- **Objective verification:**
  - Spec tests patch `pypdf.PdfReader` with instrumented metadata and fake pages. They prove a
    metadata match performs zero page extractions, a page-1 match extracts only page 1, and proof
    digest/locus/parser fields remain exact.
  - Spec tests prove a later page match extracts pages 1..N once in order and skips N+1 onward;
    a no-match document extracts every page once and retains the actionable typed failure.
  - Spec tests inject a later-page extraction failure after an earlier proof and prove it is not
    touched; they inject a failure on a required page and prove the current typed parser failure
    and cause are retained.
  - Regression tests exercise real small PDFs plus the supplied official nRF/STM controls to
    preserve exact matching, family-placeholder, digest, and late/no-match behavior.
  - The neutral gate runs only tester-owned files. After green, the main manager runs the existing
    H04 suites, lint/type checks, a direct official-nRF metadata benchmark, and the targeted S11
    public setup retest from a fresh isolated MCP process.

## Out of scope / must not change

- `server.py` setup phase structure, repeated authority replay, plan/action timeouts, operation
  dispatch, pyOCD/J-Link behavior, pack promotion, profile/safety-map persistence, setup schemas,
  public tool descriptions, or live-hardware policy.
- Authority-term derivation, token matching, PDF digest/reference capture, evidence replay,
  dataclasses, dependencies, or parser selection/version.
- Existing H04/H05 production and test files, firmware, experiments, fixtures, SDK/toolchains,
  hardware mappings, permissions, and unrelated dirty work.
- No cache of parser results or live authority, timeout inflation, board/part/OS/path/provider
  branch, speculative abstraction, formatting sweep, commit, push, deploy, flash, or hardware
  action.

## Acceptance gate

- Every CL-NNN item has automated spec assertions.
- Regression coverage exercises normal real-PDF matching and preserved official controls.
- Both tester-recorded commands exit zero in the same neutral harness iteration.
- The doer modifies only the named production file and does not touch tests, manifests, commands,
  prior H05 repairs, or unrelated dirty work.
- Every active role rereads `../.codex/design_charter.md` before acting and before completion. The
  doer records concrete pre-edit, production/test-handoff, and post-implementation checks in
  `.change-loop/fresh-suite/S11-datasheet-lazy/DESIGN_CHARTER_CHECKS.md`.
- Main verification confirms the diff is narrow, no arbitrary limit or environment special case
  was added, existing H04 tests/lint/type checks pass, the official metadata-match benchmark no
  longer extracts all pages, and targeted S11 setup returns within its declared live bound.
