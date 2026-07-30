# Phase 0 implementation notes

Deviations from the guide, and defects I noticed while implementing but deliberately
did **not** fix in Phase 0. Carried into the Phase 1 adversarial passes so they get
adjudicated on the record rather than silently absorbed.

## Deliberate deviations (to adjudicate in Phase 1)

### D1. `tests/discovery_hook_fixtures.py` is not in the guide's file list
The guide lists 12 new test files and one fixture script. I added a 14th module, a
shared test helper, so the eight hook test modules do not each re-implement manifest
construction. Every helper builds a real manifest on disk and a real child process;
nothing about the execution path under test is mocked.

### D2. Legacy vendor helpers were **not** made a snapshot provenance source
Step 4 says `SERIAL_FALLBACKS` and its two parsers "move behind the unified layer as a
third provenance source, `("vendor:nrfjprog",)` etc."

This is not implementable as written. `_resolve_nordic_serial` and
`_resolve_stlink_serial` are **probe-scoped resolvers**, not enumerators: they take a
`board` and a `probe` and match vendor output against that probe's UID
(`serial_resolver.py:583-594`). A snapshot has no board or probe context, so there is
nothing to match against and no way to synthesize board-independent vendor rows.

What I did instead:
- `HardwareInventoryService` carries a `vendor_uarts` callable seam and merges
  `VendorUartRow`s with `vendor:<provider_id>` provenance when native UART is empty, so
  the layer and its merge rules exist and are tested.
- Server wiring leaves that callable at its empty default.
- `resolve_serial_port` keeps consuming `SERIAL_FALLBACKS` exactly as today, so
  `PYOCD_SERIAL_FALLBACK_REGISTRY` behavior is preserved byte-for-byte -- which is what
  the guide's compatibility requirement and the safety test actually assert.

A future change can supply board-scoped vendor rows through the existing seam.

### D3. `ProbeRow`/`UartRow` carry `snapshot_id`; `UartRow` also carries `row_id`
The guide's dataclasses omit these, but `test_inventory_snapshot_concurrency.py` is
specified as asserting "every row inside one returned snapshot carries the same
`snapshot_id`". That is not checkable without the field. `ProbeRow` also keeps a
`probe_id` distinct from `unique_id`, because the value existing callers use as a
pyOCD selector is the *session connection ID* for a UID-less live session, and
collapsing the two would change `ValidationProbe` behavior.

## Known defects left in place for the CODE ADVERSARY to catch

### K1. `_active_connection_rows` TOCTOU on a disconnecting board
`connection_manager.assigned_board_ids()` releases its lock before
`connection_for(board_id)` re-acquires it, so a board that disconnects in between makes
`connection_for` raise `BoardNotConnectedError` mid-iteration. Pre-existing in
`_validation_inventory`; the guide says preserve the injection *verbatim*, so it is
preserved. In `_setup_overview` a broad `except Exception` turns it into
`inventory_error`, but other call sites do not catch it.

I wrote the `maybe_connection`-and-skip fix, then reverted it to stay faithful to
Phase 0's "nothing more, nothing less". Expect this as an iteration-1 finding.
