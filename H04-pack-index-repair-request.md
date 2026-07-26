# H04 REQ-010 — public pack-index repair must honor zero retries and offline replay

Authorized local, host-only firmware-server validation. No physical board,
remote target, firmware mutation, commit, push, deployment, or flash is in
scope.

## Verified production behavior

The installed public `pyocd-pack-repair` entry point was exercised against a
disposable loopback Open-CMSIS-Pack fixture containing a valid master PIDX and
two valid PDSCs. The local cmsis-pack-manager data cache initially contained
only the correct flat, version-qualified Alpha descriptor.

With exact vendor `Acme`, pack names `Alpha_DeviceFamily` and
`Beta_DeviceFamily`, explicit test-local JSON/data paths, and `--retries 0`:

- the master `/index.pidx` was fetched;
- no descriptor request was made;
- the command exited `1`;
- an internal `AssertionError` escaped from `_download_descriptor`.

A positive control using the identical valid fixture and `--retries 1`
succeeded, requested only the master index and the absent Beta PDSC, reported
one descriptor download, and rebuilt a two-device index. This rules out the
corrected fixture, filters, cache layout, and cmsis-pack-manager parser as the
cause.

After that successful repair, the loopback server was stopped. A second
identical missing-only invocation exited `1` because
`repair_live_pack_index()` unconditionally fetched the master index. The
already-complete descriptor/index/alias bytes remained stable, and direct local
rebuild controls were deterministic, but the public command did not provide the
required offline no-op success.

## Expected behavior

1. `--retries N` means one initial descriptor request plus at most `N`
   additional retries. `--retries 0` is valid and makes exactly one initial
   attempt. A failed final attempt returns an actionable
   `PackIndexRepairError`; no assertion or traceback leaks.
2. A successful repair durably and atomically retains the exact validated
   master PIDX under the caller-selected cache root, bound to the exact
   `index_url`.
3. A later missing-only invocation against that same URL reuses the retained
   master PIDX, selects the same descriptors, observes that all selected
   version-qualified PDSCs are present, rebuilds/validates the local index
   deterministically, returns success, performs zero network requests, and
   leaves bytes unchanged.
4. `--refresh` explicitly bypasses cached master/PDSC reuse and performs current
   remote acquisition.
5. A missing or invalid retained master does not fabricate completeness. An
   invalid retained master fails honestly with actionable `--refresh` guidance;
   an absent retained master follows the existing online fetch path.
6. Cache identity must not cross `index_url` values. The design must work for
   arbitrary vendors, pack names, URLs, paths, hosts, and operating systems.

## Constraints and exclusions

- Keep the change in the one module that owns this public CLI unless an existing
  shared utility is clearly required.
- Preserve exact vendor/name/name-contains selection, missing-only versus
  refresh behavior, cmsis-pack-manager output format, atomic per-file descriptor
  replacement, and public success counters.
- Do not add hostile-input hardening, arbitrary PDSC/index size or member-count
  limits, board-specific cases, hardcoded paths/ports/vendors, or unrelated
  setup behavior.
- Add focused automated tests using loopback or fully controlled HTTP fakes,
  real valid synthetic PDSCs where end-to-end cmsis-pack-manager behavior is
  asserted, and isolated temporary cache roots.
- Preserve all existing H00–H04 repair behavior and repository tests.

## Design-charter interpretation

This is ordinary correctness and usability for a compliant but fallible
operator: `0` retries must not suppress the initial operation or leak an
assertion, and a completed durable repair must replay deterministically without
requiring a network that is no longer present. The simplest general solution
should cache validated source evidence by URL identity and use `--refresh` as
the explicit remote-refresh control. No adversarial-input hardening is wanted.
