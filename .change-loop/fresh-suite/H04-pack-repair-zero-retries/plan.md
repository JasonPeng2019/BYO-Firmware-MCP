# Change implementation plan

## Source change list

- Source: `/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/.change-loop/fresh-suite/H04-pack-repair-zero-retries/changes.md`
- Goal summary: Make the public pack-index repair command perform one initial descriptor request when `--retries 0`, report exhausted requests honestly, and retain validated master-index evidence by exact URL so a completed missing-only repair replays offline with no byte changes while `--refresh` remains the explicit online refresh path.

## Repository context and assumptions

- Verified architecture and relevant entry points: `pyproject.toml` exposes `pyocd-pack-repair = pyocd_debug_mcp.pack_index_repair:main`. The entire behavior is owned by `src/pyocd_debug_mcp/pack_index_repair.py`: `main()` parses CLI arguments and calls the sole `repair_live_pack_index()` implementation; that function solely calls `fetch_master_index()`, `select_refs()`, `plan_downloads()`, `_download_descriptor()`, and `rebuild_cached_index()`. Deterministic `query-codebase` searches found no other callers and no existing pack-index-repair tests.
- Verified cache contract: `PdscRef.cache_filename` and `descriptor_path()` use the cmsis-pack-manager flat, version-qualified `{vendor}.{pack}.{version}.pdsc` layout; `rebuild_cached_index()` rebuilds JSON/aliases from every top-level `*.pdsc` in `Cache.data_path`.
- Existing test/build commands relevant to the change: focused tests will use the repository's locked environment (`uv run --locked pytest ...`); repository-wide static/runtime checks are `uv run --locked ruff check src tests`, `uv run --locked pyright`, and `uv run --locked pytest`.
- Charter decision: cache the exact validated raw master PIDX under the caller-selected data cache, with a deterministic filename containing a SHA-256 identity of the exact UTF-8 `index_url`. This is the smallest general mechanism that supports arbitrary URLs and avoids cross-source reuse; no vendor, board, host, port, OS, or environment special case is permitted.
- Accepted interpretation: `retries` means additional attempts after the mandatory initial request, matching conventional CLI meaning and making the specified value zero useful; therefore the existing default of three permits at most four total descriptor requests.
- Accepted interpretation: a normal missing-only invocation prefers a valid retained master PIDX for the exact `index_url`; `--refresh` is the explicit request to reacquire both the master index and selected descriptors from the network.

## Plan items

### CL-001 — Give descriptor retries correct, bounded, honest semantics

- **What to change:** Refactor `_download_descriptor()` so it always makes one initial request and then at most `retries` additional requests, preserves bounded backoff only between remaining attempts, atomically replaces the destination only after a complete successful response, removes its `.part` file after every failed attempt, and raises `PackIndexRepairError` with the descriptor URL and final cause after exhaustion. Reject negative retry counts as an ordinary actionable `PackIndexRepairError`/CLI failure before any descriptor request; do not use an assertion for operator input. Update the `--retries` help text to state “additional retries after the initial request.”
- **Where:** `src/pyocd_debug_mcp/pack_index_repair.py`, specifically `_download_descriptor()`, `repair_live_pack_index()` input handling, and `build_parser()`.
- **Exact intended behavior:** `retries=0` performs exactly one descriptor request; `retries=2` performs no more than three and stops immediately on success; an exhausted final attempt returns public exit code `1` through the existing `[FAIL] ...` path with no traceback or `AssertionError`; negative values return an actionable failure without network I/O or cache mutation.
- **Must remain intact:** The current request timeout, redirects, user-agent, streaming chunk size, bounded backoff, per-file temporary replacement, descriptor URL construction, successful counters, and `PackIndexRepairError` CLI handling remain intact. No retry cap beyond the operator-provided nonnegative count is introduced.
- **Objective verification:** Automated controlled-HTTP/fake-stream tests count attempts for zero and positive retry values, prove early success stops retrying, prove final transport/HTTP failure has the exact public error type/message and no `.part`/destination corruption, prove negative input makes zero requests, and invoke `main()` or the installed-style CLI boundary to prove exit `1` without traceback.

### CL-002 — Retain validated master PIDX by exact URL and replay missing-only repairs offline

- **What to change:** Add small module-local helpers to (a) derive a portable master-cache path from the exact `index_url` using SHA-256 rather than filesystem-unsafe URL text, (b) fetch raw master PIDX bytes/text and parse them before use, (c) load and parse the exact-URL retained PIDX for ordinary missing-only operation, and (d) write newly fetched validated master bytes through a sibling temporary file and atomic replace only after descriptor downloads and index rebuild have all succeeded. Restructure `repair_live_pack_index()` so non-refresh/missing-only calls use a valid exact-URL retained master without touching the network; an absent retained master follows the existing online fetch path; and refresh always fetches current master data and redownloads selected descriptors. Keep selection and descriptor planning driven by parsed PIDX references, never by client strings or stale JSON aliases alone.
- **Where:** `src/pyocd_debug_mcp/pack_index_repair.py`, beside `fetch_master_index()`/`parse_master_index()` and in `repair_live_pack_index()`.
- **Exact intended behavior:** On a fresh partial cache, the command fetches the master PIDX, downloads only selected version-qualified PDSCs absent from `Cache.data_path`, rebuilds a coherent index/aliases, then retains the validated master for that exact URL. A second identical missing-only call with the server unavailable reads that retained PIDX, plans zero descriptor downloads, deterministically rebuilds/validates the local JSON/aliases, exits `0`, makes zero network requests, reports `download_count=0`, and leaves all preexisting cache bytes byte-for-byte stable. Deleting one selected PDSC after a successful run causes only that descriptor to be fetched from the retained PIDX without refetching the master. `--refresh` bypasses retained reuse, fetches the master, and redownloads matching descriptors. A corrupt retained PIDX fails before descriptor downloads with a message naming the retained evidence and instructing the operator to rerun with `--refresh`; it never fabricates completeness or silently crosses to another URL. Distinct `index_url` values map to distinct retained evidence.
- **Must remain intact:** Exact case-insensitive vendor and pack-name filters, all-token `name_contains` filtering, the “no descriptors matched” failure, default online behavior when no retained master exists, flat version-qualified descriptor paths, rebuild from all cached top-level PDSCs, current `RepairResult` fields/counters, explicit caller JSON/data paths, and arbitrary legitimate PIDX/PDSC sizes remain intact. Do not add hostile-input hardening, arbitrary limits, a new dependency, a global cache, or environment-specific paths.
- **Objective verification:** A temporary-root end-to-end test with a standards-conforming loopback PIDX and two valid PDSCs preloads exactly one flat version-qualified descriptor, runs with `retries=0`, and proves request sequence `[master, missing descriptor]`, two cached PDSCs, two indexed devices, coherent JSON/aliases, and exact-URL retained master. After server shutdown, the identical second call must exit successfully, report zero downloads, perform no network request, and have identical recursive hashes. Additional tests prove missing-descriptor repair from retained PIDX, refresh network behavior, corrupt-cache actionable refusal/recovery, and non-collision across distinct URLs.

### CL-003 — Make the public CLI teach the new durable/offline contract

- **What to change:** Update only the module docstring and argparse help strings needed to explain that ordinary missing-only repair reuses retained master evidence for the exact index URL, `--refresh` forces remote master and descriptor refresh, `--retries` counts additional attempts after the initial request, and corrupt retained evidence is recovered with `--refresh`. Keep wording concise and operator-facing.
- **Where:** `src/pyocd_debug_mcp/pack_index_repair.py` (`__doc__` and `build_parser()`).
- **Exact intended behavior:** `pyocd-pack-repair --help` gives a compliant operator enough information to predict offline replay, force a live refresh intentionally, and choose `--retries 0` without trial-and-error.
- **Must remain intact:** All existing option names, defaults, repeatability, entry-point name, success output fields, and exit-code conventions remain compatible. No new mandatory option or outside knowledge is introduced.
- **Objective verification:** Parser/help tests assert the retry, retained-master, exact-URL, offline-reuse, and `--refresh` recovery semantics while also asserting all prior option names/defaults remain available.

## Out of scope / must not change

- Do not edit MCP setup, board profiles, pack provisioning, datasheet evidence, hardware providers, plans/permissions, firmware, fixture/test-run evidence, or any physical-board behavior.
- Do not special-case Acme, STM32, a particular URL/port/path, Windows, WSL, or the H04 fixture.
- Do not accept the test agent's original nested unversioned descriptor layout; preserve the verified cmsis-pack-manager flat version-qualified contract.
- Do not broaden into crash-injection transaction machinery, hostile-input hardening, arbitrary PIDX/PDSC limits, dependency upgrades, or unrelated refactors.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, or generated artifacts.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, and adjacent behavior touched by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, or gate commands.
- The same final production diff passes the corrected public-loopback H04 oracle: first run with `--retries 0` fetches only the master and absent descriptor; second offline run succeeds with zero requests and byte-stable cache state.
