# H04 datasheet-part binding — design-charter checks

## 2026-07-25 — main model, pre-request / post-specification

- **Contemplated diff:** establish positive, replayable applicability evidence between a local PDF
  and the exact requested MCU before the generic setup path promotes the PDF into a durable profile
  or reports readiness.
- **Correctness:** hashing proves byte identity but not document applicability. The repair must
  prove both, fail honestly when it cannot, and preserve stale-byte checks.
- **Simplicity:** use a maintained PDF text/metadata parser and a small normalization/matching
  policy; do not build a PDF parser or a general document-classification framework.
- **Generalizability/dynamism:** derive identity terms from the requested exact part and verified
  device-support authority at runtime. No board, vendor, hash, filename, OS, or host-path table.
- **Neatness/usability:** document-evidence logic stays in `setup_flow/datasheet_evidence.py`; the
  setup orchestrator consumes one explicit result and returns an actionable public remedy.
- **Trusted-but-fallible boundary:** the caller-selected PDF is trusted as non-hostile but may be
  the wrong document. Rejecting a verified mismatch/unproven association catches an ordinary agent
  mistake; it is not hostile-input hardening or paternalism.
- **Rejected alternatives:** STM32/nRF allowlists; known-document hash tables; filename matching;
  trusting caller/manifest strings; an external host `pdftotext` executable; arbitrary
  size/page/count limits; silent acceptance when applicability is unknown.
- **Scope exclusions:** no changes to firmware, hardware actions, permission gates, pack exact-leaf
  authority, unrelated setup behavior, or earlier accepted repairs.
- **Assumption/tie-breaker:** a datasheet becomes authority only after positive server-verifiable
  identity evidence. When evidence is unavailable, correctness beats convenience: return an
  actionable incomplete/refusal result instead of guessing.

## 2026-07-25 — main model, post-plan / pre-adversarial review

- **Plan reviewed:** SHA-256
  `e3d2bad59a92dda5bd41a0cab23081e01b0397d6e74ed410ffe0f37edab5aae6`.
- **Correctness:** the plan separates byte identity from document applicability, requires positive
  evidence before promotion, repeats the check at the commit boundary, and makes legacy replay
  fail honestly rather than grandfathering bad authority.
- **Simplicity:** one document-evidence module owns parsing/matching; orchestration consumes its
  typed result. The plan explicitly rejects a home-grown parser and a new framework.
- **Generalizability/dynamism:** identity terms come from the exact part and replayed support
  authority. Family datasheets remain possible through verified PDSC family/subfamily terms.
- **Neatness/usability:** one typed failure differentiates malformed, stale, and unproven
  applicability with a single actionable recovery.
- **Trusted-but-fallible boundary:** the guard catches a plausible wrong-document selection that
  would otherwise create false durable authority; no adversarial-input hardening is introduced.
- **Rejected plan alternatives:** exact-part-only matching for all family datasheets; arbitrary
  caller-derived prefix heuristics; catalog/hash special cases; permissive unknown-as-success.
- **Scope check:** the plan preserves previous uncommitted repairs and excludes firmware, hardware,
  unrelated schemas, and unrelated dependency upgrades.

## 2026-07-25 — main model, pre-implementation

- **Accepted review:** `/root/h04_datasheet_plan_adversarial` returned `PROCEED` for the exact plan
  SHA and supplied five execution/test risks now recorded in `plan-review.md`.
- **Implementation boundary:** only the change-loop doer may edit production. It must centralize
  document proof, derive terms from replayed authority, bind proof/capture to one byte snapshot, and
  preserve earlier accepted diffs.
- **Charter enforcement:** every doer/tester turn must perform the role checkpoints recorded in
  `plan-review.md` and append concrete acknowledgements here.
- **Rejected shortcuts:** caller prefixes, filename checks, known hashes, vendor cases, external
  host tools, unknown-as-success, or weakening replay/permission/live-identity gates.
- **Acceptance boundary:** role prose is not proof; only the neutral spec/regression gate plus main
  diff/charter review can accept the repair.

## 2026-07-25 — doer, pre-edit / post-risky implementation diff

- **Current diff:** adds maintained in-process PDF parsing, an exact-byte-bound applicability proof,
  generic setup gating/recheck, and generic-profile proof persistence/replay.
- **Correctness and trust boundary:** a user-selected PDF remains trusted but can be mistaken; the
  proof accepts only the requested part or an exact PDSC leaf emitted by replayed support, and fails
  honestly otherwise. It adds no arbitrary resource cap or hostile-input policy.
- **Generality and scope:** no board, vendor, filename, hash, OS, or caller-prefix rules were added.
  Built-in targets deliberately require exact part evidence because they have no PDSC family term.
  Existing catalog, permission, live-identity, pack-leaf, and byte-stability paths are unchanged.

## 2026-07-25 — doer, pre-verification / final verdict

- **Verification surface:** source compilation completed with the host interpreter and the locked
  parser dependency resolved. The intended focused `uv` checks were attempted but the pre-existing
  `.venv/lib64` access-denied cleanup failure stopped them before execution; the neutral harness
  remains authoritative.
- **Charter check:** the final diff keeps parsing/matching in one evidence module, persists only
  server-generated facts, replays offline from canonical captured bytes, and does not change hardware,
  permissions, packs, catalog policy, or any board-specific behavior.

## 2026-07-25 — doer, iteration 2 boundary repair

- **Production diff:** replaces normalized-substring matching with exact contiguous identity-token
  matching. This accepts separator variants of server-derived identities but rejects a longer,
  unrelated SKU with the requested SKU as a prefix.
- **Charter check:** this is a correctness guard for a fallible document selection, not a vendor or
  board rule. It keeps maintained parsing and the single evidence module, adds no limit, host
  assumption, or hostile-input defense, and leaves hardware, permissions, catalogs, and packs out
  of scope.

## 2026-07-25 â€” spec tester, pre-edit / CL-001 and CL-002 evidence tests

- **Test surface:** add an isolated H04 spec suite for parser-extracted exact-part and verified-family
  matches, wrong/textless and caller-derived-term refusals, normalization, and the digest-versus-current-
  bytes promotion boundary.
- **Correctness/trust boundary:** these checks reject a normal wrong-document or stale-byte mistake
  without treating the user-owned PDF as hostile. They require an actionable typed error rather than a
  guessed filename, vendor, or prefix match.
- **Simplicity/generalizability:** tests use generated generic identities and a minimal in-memory PDF;
  no board, vendor, host path, physical hardware, external tool, size cap, or allowlist is introduced.
- **Scope:** test and required checkpoint record only; no production, permission, pack, catalog, or
  hardware behavior is edited.

## 2026-07-25 â€” spec tester, between features / CL-003 replay tests

- **Test surface:** add generic-profile replay tests for a tampered persisted proof and for a legacy
  profile with no stored proof, requiring the latter to re-prove captured bytes against replayed
  server-owned support offline.
- **Correctness/trust boundary:** the checks fail closed on contradictory durable evidence while
  preserving a valid legacy path; no network fallback, caller authority, or silent profile mutation
  is accepted.
- **Scope/generalizability:** all identities and evidence are synthetic and local; no hardware, board,
  vendor, filename, host-specific path, resource limit, or hostile-input policy is introduced.

## 2026-07-25 â€” spec tester, final verification / verdict

- **Verification:** the recorded isolated focused command passed `8` tests with `2` subtests. The
  repository `.venv` remains untouched because its pre-existing `lib64` entry is access-denied.
- **Charter check:** the suite tests truthful association, stale-byte refusal, offline replay, and
  valid legacy re-proof with generic synthetic identities. It adds no production change, hardware
  action, vendor/board/filename allowlist, hostile-input defense, or arbitrary limit.

## 2026-07-25 â€” spec tester, iteration 2 pre-edit / identity boundary

- **Test surface:** add the neutral-gate regression for a longer unrelated SKU that contains an
  exact requested-part prefix, requiring a typed unproven-association refusal.
- **Correctness/trust boundary:** this catches an ordinary wrong-datasheet selection without
  inferring families from caller text or blocking verified PDSC family terms.
- **Scope/generalizability:** synthetic identities only; no production, hardware, board/vendor
  allowlist, path assumption, hostile-input rule, or arbitrary limit is added.

## 2026-07-25 â€” spec tester, iteration 2 final verification / verdict

- **Verification:** the recorded focused command passed `9` tests with `2` subtests, including the
  longer-SKU identity-boundary regression found by the preceding neutral report.
- **Charter check:** the added test preserves positive server-owned evidence and finite actionable
  refusals without altering production, persistence, permissions, hardware, supported families, or
  trusted-input policy.

## 2026-07-25 â€” spec tester, resumed iteration pre-edit / official H04 artifacts

- **Test surface:** add the supplied STM32L476RGT official PDF plus exact digest-verified Keil pack
  as a positive family-placeholder control, and the supplied nRF52840 PDF as the wrong-family
  control. The candidate and terms are derived through the real verified-pack path.
- **Correctness/trust boundary:** this proves that a correct family document is accepted only via
  server-derived PDSC ancestry while an unrelated official document remains an actionable refusal.
- **Scope/generalizability:** fixed artifacts are acceptance fixtures, not production policy; no
  board/vendor/hash/filename rule, hardware action, resource limit, or hostile-input guard is added.

## 2026-07-25 â€” spec tester, resumed iteration final verification / verdict

- **Verification:** the recorded focused command passed `11` tests with `2` subtests. It exercised
  the supplied SHA-256-pinned STM32 PDF/Keil pack positive control and nRF PDF negative control.
- **Charter check:** testing confirms runtime-derived PDSC ancestry rather than a production allowlist;
  no source, hardware, permissions, profile mutation, or trusted-input policy was changed.

## 2026-07-25 â€” spec tester, resumed iteration pre-edit / ancestry and provenance

- **Test surface:** add synthetic verified-pack coverage for direct-family devices and nested variant
  ancestry, plus identity-term provenance coverage distinguishing exact-only terms from verified
  family terms that may use the documented trailing-`xx` convention.
- **Correctness/trust boundary:** the tests reject unproved family inference and preserve ordinary
  PDSC XML layouts without adding caller-prefix matching, vendor cases, or any hostile-input policy.
- **Scope/generalizability:** in-memory generic pack bytes and synthetic PDFs only; no production,
  hardware, board/path assumption, resource cap, or external tool is added.

## 2026-07-25 â€” spec tester, resumed iteration final verification / verdict

- **Verification:** the recorded focused command passed `15` tests with `2` subtests, including the
  real H04 artifact controls and generic direct-family, variant-ancestry, and provenance boundaries.
- **Charter check:** the suite requires positive server-derived family authority while preserving
  exact-only built-in behavior; no production, hardware, permission, persistence, or policy change
  was made by this role.

## 2026-07-25 â€” spec tester, resumed iteration pre-edit / letter family placeholder

- **Test surface:** add a letter-ending synthetic family (`LPC55S`) to require the verified-family
  trailing-`xx` convention while rejecting that spelling for exact-only authority and rejecting a
  concrete longer token.
- **Correctness/trust boundary:** this removes an arbitrary family-name restriction without
  restoring caller-derived prefixes or allowing a built-in exact-only target to claim family scope.
- **Scope/generalizability:** only the existing test file changes; no production, hardware, vendor
  table, host assumption, arbitrary limit, or hostile-input policy is introduced.

## 2026-07-25 â€” spec tester, resumed iteration final verification / verdict

- **Verification:** the recorded focused command passed `16` tests with `4` subtests, including
  digit- and letter-ending verified-family placeholders, exact-only refusals, and concrete suffix
  refusals alongside the real H04 controls.
- **Charter check:** coverage enforces server-derived provenance and general family naming without
  adding production behavior, hardware action, permissions, persistence mutation, or policy scope.

## 2026-07-25 â€” spec tester, resumed iteration final static and behavioral verification

- **Verification:** focused Ruff and dependency-complete BasedPyright both reported zero tester-owned
  diagnostics; the recorded behavior command passed `16` tests with `4` subtests after full local
  artifact parsing.
- **Charter check:** cleanup retains every adversarial assertion and uses a concrete built-in support
  candidate rather than weakening the authority contract; no production or hardware behavior changed.

## 2026-07-25 â€” spec tester, resumed iteration pre-edit / focused static hygiene

- **Test surface:** sort the H04 test imports, combine nested assertion/patch contexts, and replace
  a structural fake with a real built-in support candidate so the stale-byte assertion satisfies the
  concrete authority type.
- **Correctness/simplicity:** assertions and exercised behavior are unchanged; these changes only
  remove focused lint/type noise without suppressions or looser contracts.
- **Scope:** tester-owned file only; no production, hardware, authority, policy, or environment
  assumption is changed.

## 2026-07-25 — doer, iteration 2 post-diff / pre-verification

- **Refinement:** preserves original server-derived token boundaries for matching while persisting
  only the normalized matched term. This makes family and separator-variant identifiers work without
  reintroducing substring/prefix acceptance.
- **Scope and boundary:** no caller-derived terms, hardware operations, tests, gate configuration,
  external executables, or environmental assumptions were introduced.

## 2026-07-25 — doer, iteration 2 final verdict

- **Verification:** the isolated regression command passed (`1 passed`), and the H04 spec suite
  passed (`8 passed, 2 subtests`). The recorded `uv run` regression command still cannot start
  because its existing `.venv/lib64` cleanup fails with access denied.
- **Final charter check:** exact identity evidence now rejects longer unrelated SKU prefixes while
  retaining exact server-derived family terms and separator variants. The repair is local,
  deterministic, parser-backed, and free of board/vendor/host rules, arbitrary limits, hardware
  action, or gate/test modification.

## 2026-07-25 — doer, acceptance ancestry repair

- **Production diff:** exact verified PDSC XML ancestry now supplies the family/subfamily terms and
  participates in the generic candidate identifier. PDF matching recognizes only the conventional
  trailing-`xx` placeholder form of a digit-ending identity; arbitrary concrete SKU prefixes remain
  rejected.
- **Correctness/scope:** ancestry comes from the digest-verified pack bytes, never caller text or a
  vendor table. No hardware action, test/fixture change, network replay, host executable, policy
  limit, or board-specific exception was introduced.

## 2026-07-25 — doer, legacy support-identity compatibility

- **Production diff:** new generic profiles bind PDSC ancestry into their support ID; a pre-change
  ID is accepted only when every immutable pack field replays from the same verified bytes. A stored
  applicability proof continues to re-prove against current server-derived terms.
- **Charter/scope:** this preserves valid offline legacy profiles without grandfathering changed
  pack authority or document evidence. It adds no caller authority, network fallback, hardware
  action, test alteration, board/vendor rule, or environmental assumption.

## 2026-07-25 — doer, acceptance repair final verdict

- **Verification:** H04 spec and regression suites passed in an isolated environment (`11 passed,
  2 subtests`); source compilation and whitespace checks passed. The real supplied STM32 PDF and
  verified Keil pack produced family term `STM32L476` and successful proof; the supplied nRF PDF
  was refused.
- **Final charter check:** support ancestry remains server-owned verified-pack evidence and exact
  identity matching never accepts a concrete SKU prefix. No hardware, network, test, permission,
  or environment-specific behavior changed. The neutral harness remains the acceptance authority.

## 2026-07-25 — doer, generic ancestry and provenance repair

- **Production diff:** PDSC ancestry now follows the actual XML tree so family-direct and nested
  variant leaves both yield verified family/subfamily terms. Typed evidence terms distinguish
  exact part/leaf authority from PDSC family/subfamily authority; only the latter permits `xx`.
- **Charter/scope:** these are generic verified-byte facts, not vendor cases. The guard prevents a
  fallible agent from promoting an unproven family document while retaining intended official family
  coverage; no hardware/network/test/gate changes, external parser, or arbitrary limit was added.

## 2026-07-25 — doer, provenance API compatibility refinement

- **Production diff:** identity terms retain string behavior for existing callers while carrying the
  server-generated family-placeholder provenance needed by the parser. Exact-only terms remain
  placeholder-ineligible.
- **Scope:** this is a representation-only compatibility repair; no authority, parser, hardware,
  test, network, or environment behavior is broadened.

## 2026-07-25 — doer, generic ancestry/provenance final verdict

- **Verification:** synthetic direct-family and nested-variant PDSC ancestry checks passed; exact
  versus family-placeholder boundaries passed. The real official-artifact regression passed in
  `100.68s`, and all remaining H04 tests passed (`13 passed, 1 deselected, 2 subtests`). Source
  compilation and whitespace checks passed.
- **Final charter check:** only server-owned verified PDSC family/subfamily terms can cover a
  conventional `xx` placeholder. Exact-only/built-in terms cannot, and concrete SKU prefixes still
  fail. No hardware, network, test/gate, vendor/board, or environment-specific behavior changed.

## 2026-07-25 — doer, letter-ending family placeholder repair

- **Production diff:** removes the context-free digit-ending condition from the already provenance-
  gated `xx` convention. Any verified PDSC family/subfamily term may use that convention; exact-only
  terms and concrete suffixes remain rejected.
- **Charter/scope:** this restores arbitrary hardware support with a narrowly server-owned rule,
  without a vendor table, caller inference, hardware/network action, test/gate modification, or
  arbitrary resource policy.

## 2026-07-25 — doer, focused H04 neatness repair

- **Production diff:** sorts the H04 support slot declaration and imports, corrects stale placeholder
  documentation, and reuses one verified PDSC-ancestry parse for both candidate ID and state.
- **Scope:** no behavior, authority, tests, test configuration, hardware, network, or environmental
  policy changed; this is a focused correctness/neatness cleanup only.

## 2026-07-25 — doer, focused static-format refinement

- **Production diff:** formats the local parser import and the H04-added device-support import
  placement only. Existing non-H04 server and tester-owned diagnostics remain out of this role's
  edit scope.
- **Scope:** no behavior or authority change; no tests, gates, hardware, network, or unrelated
  formatting sweep was performed.

## 2026-07-25 — doer, focused static acceptance final verdict

- **Verification:** targeted H04 production import checks passed; the new setup-support slot is no
  longer reported. Compilation and whitespace checks passed. Broader Ruff reports only two
  pre-existing server slots; dependency-complete BasedPyright reports the documented pre-H04 cast
  plus the tester-owned structural-fake error.
- **Final charter check:** the cleanup retained all evidence/authority behavior and avoided an
  unrelated formatting sweep or any test, hardware, network, vendor/board, or environment change.

## 2026-07-25 — doer, letter-ending family placeholder final verdict

- **Verification:** direct provenance-bound letter-ending checks passed; all isolated H04 suites
  passed (`20 passed, 2 subtests`) including the real official artifacts. Compilation and whitespace
  checks passed.
- **Final charter check:** PDSC family/subfamily provenance is the sole authority for `xx`; exact
  terms still fail that convention and concrete suffixes still fail. No hardware, network, test,
  gate, vendor/board, or environment-specific behavior was changed.

## 2026-07-25 — main model, post-neutral acceptance rejection

- **Observed gap:** both neutral suites passed, but the actual official STM32 H04 positive PDF was
  refused when evaluated with the exact terms the production support candidate currently exposes.
  Synthetic family-term tests did not establish the real end-to-end positive contract.
- **Correctness:** accepting the wrong PDF is fixed, but rejecting the correct official family
  datasheet is also a correctness failure. The repair cannot be accepted until both controls work.
- **Generalizability/dynamism:** the verified official PDSC already supplies exact-leaf ancestry
  (`DsubFamily`/`Dfamily`). Those server-owned facts, not a caller-prefix heuristic or a
  vendor/part allowlist, must drive family applicability and replay identity.
- **Simplicity:** retain the single parser-backed evidence module and extend the existing support
  candidate with the minimum typed provenance needed to distinguish exact identities from
  authoritative PDSC family terms.
- **Mistake guard boundary:** an explicit family-placeholder spelling such as the official
  `STM32L476xx` may satisfy verified `DsubFamily="STM32L476"`; an arbitrary longer concrete SKU
  must not. This catches wrong-document mistakes without widening to raw prefix acceptance.
- **Plan status:** the existing CL-001/CL-002 requirements already demand this behavior. This is an
  implementation/test-coverage shortfall, not a genuine plan mistake, so no amendment or renewed
  plan review is authorized.
- **Next gate:** resume the same doer/spec/regression identities sequentially, require real
  official-artifact coverage, rerun the neutral gate, then repeat main-model direct verification.

## 2026-07-25 — main model, second post-neutral acceptance rejection

- **Observed regressions:** the new ancestry reader rejects a valid exact PDSC device placed
  directly beneath `family`, and the PDF matcher makes the family-placeholder rule available to an
  exact-only built-in identity.
- **Correctness:** exact support must not be lost merely because a conforming PDSC omits
  `subFamily`; exact-only authority must not silently become family authority.
- **Generalizability/dynamism:** walk the exact matched leaf's actual XML ancestry rather than
  assuming one vendor pack shape. Preserve explicit provenance between exact terms and verified
  family/subfamily terms.
- **Simplicity/neatness:** one small typed term/provenance representation is preferable to
  context-free string heuristics. It keeps matching policy in the datasheet-evidence owner and PDSC
  derivation in device support.
- **Mistake-guard boundary:** permit `...xx` only for server-verified family/subfamily authority.
  Refuse it for an exact-only term, and continue refusing longer concrete SKU prefixes.
- **Plan status:** CL-001 already states both distinctions. No plan amendment or renewed plan review
  is authorized; resume the same persistent roles and neutral gate.

## 2026-07-25 — main model, third post-neutral acceptance rejection

- **Observed gap:** provenance is now explicit, but verified family placeholders still require the
  family string to end in a digit. Letter-ending PDSC families such as a synthetic `LPC55S` remain
  falsely rejected when the document uses `LPC55Sxx`.
- **Correctness/generalizability:** PDSC authority, not the last character class of a family name,
  determines whether placeholder matching is legitimate. The digit condition has no hardware or
  protocol basis and biases support toward one naming convention.
- **Simplicity:** remove the leftover condition. The already-implemented provenance flag plus a
  full-token `term + x+` match is the simpler and sufficient rule.
- **Boundary preserved:** exact-only terms and longer concrete suffixes remain refusals; only
  verified PDSC family/subfamily terms gain the placeholder convention.
- **Plan status:** unchanged CL-001 already requires arbitrary verified family/subfamily terms. No
  plan amendment or renewed review is authorized.

## Main checkpoint — 2026-07-25 rejection 004

The main model reread the complete design charter before focused static acceptance. It rejected
only repair-introduced neatness/simplicity/type-check findings, explicitly excluded unrelated
baseline diagnostics, and preserved correctness, arbitrary-hardware generality, trusted-input
scope, and all accepted behavioral controls.
