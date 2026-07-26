# Independent adversarial plan review

Reviewer session identity: 019f9c2e-6feb-7b61-a730-37320001bf49
Model: gpt-5.6-terra, medium reasoning

Plan SHA-256: 8bb7371fd01f2bc751a78a18fffd390f638ec434b9dbd99656ab448df4504da1

Verdict: Proceed. The plan is implementable and addresses the verified production defects. No concrete contradiction makes it unimplementable.

## Design-charter checkpoints

1. Before review/tracing: read `../.codex/design_charter.md`; confirmed the requested bounded retry semantics, honest error reporting, exact-URL cache identity, and exclusion of adversarial-input hardening are charter-aligned.
2. After tracing relevant production source: reread `../.codex/design_charter.md` after tracing `src/pyocd_debug_mcp/pack_index_repair.py`; confirmed the plan keeps the change in the owning module and preserves the existing general, flat version-qualified PDSC contract.
3. Before finalizing: reread `../.codex/design_charter.md`; confirmed the review introduces no hardware operations, vendor/board/OS cases, arbitrary limits, or hostile-input controls.

## Numbered execution risks and test targets

1. Cache-root ownership must be concrete. The current module receives independent `json_path` and `data_path` values, while the plan says retained PIDX lives under the caller-selected cache root. Choose and document one deterministic location available for every supported explicit-path combination, without silently using a global/default cache. Test data-only override, json-only override, and both overrides; prove the retained PIDX is found on the corresponding second offline call.

2. The retained master must be the exact response evidence that parsed successfully. `fetch_master_index()` currently returns only parsed references, and `parse_master_index()` deliberately skips incomplete nodes. Retain the raw successful response while preserving existing parsing and selection; do not reserialize parsed references into a new PIDX. Test byte-for-byte retained content and that malformed retained XML fails before descriptor requests with actionable `--refresh` guidance.

3. `--refresh` must bypass both cached-master selection and missing-only descriptor planning. Today refresh is represented solely as `missing_only=False` at the call site. Ensure the new branch fetches the remote master and redownloads every selected descriptor even when valid retained evidence exists. Test request ordering/counts and public `download_count` for a populated cache.

4. Required offline byte stability is stronger than merely succeeding. `rebuild_cached_index()` always calls the cmsis-pack-manager dump path, so the end-to-end test must recursively hash descriptor, index, alias, and retained-master bytes before and after the offline call, not only compare result counters. If the dependency cannot produce stable bytes for unchanged input, avoid rewriting unchanged outputs while still performing requested validation; do not weaken the acceptance requirement.

5. Descriptor failure cleanup and negative-retry validation need public-boundary coverage. `_download_descriptor()` currently reaches an assertion when retries is zero. Validate negative retries before any descriptor request and cache mutation; for zero require exactly one attempt, no sleep before it, cleanup of the `.part` file on every failure, preservation of an existing destination, and `PackIndexRepairError` reaching `main()` as `[FAIL]`/exit 1 without traceback. Cover transport and non-2xx HTTP final failures, plus early success with positive retries.

6. Master-cache publication is intentionally after successful downloads and rebuild, but tests must prove that a failed descriptor download or failed rebuild does not create or replace retained evidence. This prevents a later offline run from claiming a completed repair based on an unsuccessful run. This is a correctness test, not crash-injection machinery.

7. Exact URL identity must use the exact supplied string, not a normalized URL or a filename-safe truncation that can merge sources. Test two distinct URLs serving different valid PIDXs under the same cache root; prove distinct retained paths/content and that each subsequent offline invocation selects only its own descriptors. Preserve arbitrary URL and OS support without special cases.

8. Preserve caller-visible contracts while expanding help text. Test all existing option names/defaults/repeatability, exact case-insensitive vendor/name filtering, all-token `name_contains`, no-match failure, flat version-qualified paths, and unchanged success fields. Help tests should explicitly state that retries are additional attempts, missing-only can use retained evidence for the exact URL, and `--refresh` is the recovery path for invalid retained evidence.
