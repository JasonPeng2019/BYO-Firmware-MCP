# Review of Claude's four design-flaw reports

Review target: the requested `MCP_Server_Dev/BYO-Firmware-MCP` checkout,
HEAD `0bfe9ee`, rather than the report's release commit `fd387e3`.
The four pre-existing deletions under `fallback-support-evidence/results/`
are unrelated and remain untouched. No release worktree is being edited.

## Findings and repair decisions

1. **Flaw 1: valid.** `_svd_peripheral_regions` preserves hardware access
   permissions, but `_derive_generic_safety_map` turns every writable register
   into an allowed peripheral. Hardware writability does not establish permission
   to alter flash/protection state. Preserve a separate prohibited classification
   through SVD overlap/alias segmentation and emit prohibited safety regions.
   Retain ordinary peripheral controls and flash status/access-control registers.
   Do not reuse the CPU substring matcher blindly: `lock` also occurs in `clock`,
   and an unqualified `CR` is an ordinary control register on many peripherals.
   Change the generic generator digest so existing maps require refresh.
   Name-derived policy is limited by SVD metadata; it is not a proof of the
   semantics of arbitrary vendor registers or of firmware executed on the chip.

2. **Flaw 3: valid.** The compiled sidecar's parser cannot interpret its own
   executable as `python -m pyocd`, or execute a hook by accepting a `.py` path.
   Add bounded child entry routes for native probe listing and Python hooks;
   retain source-mode commands and explicit probe CLI overrides. Correct the
   managed native-build template and exercise discovery in the sidecar self-test.
   Prefer the launcher shipped beside the sidecar bundle: client configuration
   can use an absolute launcher path without adding `byo` to PATH. State the
   helper invocation cwd separately from the build child's `--cwd`.
   Statically import pyOCD's CLI and explicitly include its subcommand package in
   the installer build recipe. Compiled routes require a future Actions-built
   artifact test; source-mode simulation is not compiled-product verification.
   The report overstates the consequence: explicit raw UART needs no probe
   discovery, and remote probes/hooks are separate routes. Native probe discovery
   is nevertheless broken by this command construction.
   The optional launcher `PYOCD_CLI` allowlist change is not needed for the
   repaired default and is not included. Direct/source overrides still work;
   the managed launcher's existing environment filter remains unchanged.

3. **Flaw 4: valid.** `_resolve_recorded_uart` conflates absent selections with
   disappeared selections; it also skips the cache when zero stable ports remain.
   Refuse saved-adapter failures with its identity, current ports, and new-plan
   recovery guidance. Keep the failed session selection until explicit setup or
   re-selection so a second call cannot resume guessing. Preserve explicit port
   overrides and first-resolution behavior. Raw UART already requires its port
   argument and bypasses saved-adapter lookup, so adding this refusal to raw
   tools would be inappropriate. Improve planned UART field documentation.

4. **Flaw 5: valid for empty groups; proposed signalling is not justified.**
   An absent leader with an empty POSIX group must permit stale-marker removal.
   Probe with signal zero; clear only after `ESRCH`. Retain markers on permission
   errors, identity uncertainty, or a surviving leaderless group. POSIX prevents
   PGID reuse *during a group's lifetime*, but an old group can finish and its
   number be reused before the next server starts. A nonempty group with no
   leader cannot by itself prove it is the original group. Do not signal it
   merely because its numeric PGID matches an old marker.
   See [POSIX process/group lifetime and ID reuse](https://pubs.opengroup.org/onlinepubs/009696699/basedefs/xbd_chap04.html).

## Execution and validation scope

Use direct scoped edits as authorized. Preserve unrelated ROOT and nested-repo
changes. Add regression tests for these four failures, first record their
baseline results, then repair the candidate as a batch and run only the focused
tests and relevant existing contracts. No full-suite rerun, local compiler,
product build, commit, push, or publication. Prepare explicit real-hardware tests
without performing dangerous flash-controller unlock or protection writes.

## Implementation evidence

| Report | Relevant implementation after repair |
|---|---|
| 1 | `safety/register_policy.py::prohibited_svd_register`; `setup_flow/device_support.py::_svd_peripheral_regions` and `_intersect_access_regions`; `server.py::_derive_generic_safety_map`; `safety/map_build.py::require_reconciled_authority`; `safety/regions.py::SafetyMap.check` |
| 3 | `runtime_commands.py`; `probe_families.py::configured_probe_cli_commands`; `discovery_hooks.py::DiscoveryHookSpec.command`; `sidecar.py::_probe_list`, `_run_hook`, `_check_probe_discovery`; `native_build.py::command_template`; installer `release/scripts/build_release.py` |
| 4 | `server.py::_resolve_recorded_uart` and `_saved_uart_unavailable`; `hardware_inventory.py::SessionUartSelectionStore.resolve`; the three UART `port` fields in `guardrails/plan_defs.py` |
| 5 | `kernel/processes.py::terminate_marked_group`; unchanged `kernel/hygiene.py` consumes its successful empty-group result and removes the marker |

The prohibited-write overlay also preserves write-only MMIO read restrictions.
Old generic maps are rejected by the *authority verifier*, not merely assigned a
different digest at generation time, so a deployed old map cannot keep allowing
the removed writes. Normal `board_safety_refresh` is the migration route.

## Observed validation

Evidence is in workspace `.harness-runtime/design-flaws-review/`.
Every fixed-candidate pass retained separate stdout/stderr, exact argv/cwd, exit
codes, SHA-256 log receipts, and a candidate stability receipt.

- Baseline: 15 tests; 13 assertion failures (including subtests), 3 errors; exit 1.
  These reproduced the four reports and the missing sidecar subcommands.
- First candidate: all seven independent checks completed. The new-regression
  group had one Windows test-fixture error (`signal.SIGKILL` is unavailable),
  and the sidecar self-test exposed pyOCD Unicode output failing with the
  Windows legacy codec. The remaining five checks passed; one native POSIX
  case was skipped. All original failure logs remain in `pass-1/`.
- Batched correction: the fixture now asserts that every group probe uses
  signal zero, without referencing a POSIX-only signal. Listing explicitly
  uses UTF-8 streams; the self-test passes the same UTF-8 environment and
  byte-oriented capture already used by the server's real inventory runner.
  A legacy-codepage regression was added.
- Final `pass-2/`: **50 tests ran: 49 passed and 1 skipped**. All five test groups
  exited 0; focused lint of all changed Python/test files and the installer
  recipe exited 0; formatting of the six selected files exited 0. Candidate
  file hashes were unchanged during the pass. Git whitespace checks passed
  in both repositories. No full-suite run occurred.

The exact unittest selectors are retained in `pass-2/results.json` and the
coordination-only `run_focused_checks.py`. They cover the new reproductions,
existing source/sidecar command contracts, session-UART resolution/workflow,
generic map replay, and the existing startup-hygiene contract.

The skipped case needs native POSIX process groups. Compiled-mode command tests
simulate runtime selection; no repaired standalone binary was built locally or
claimed verified. The installed-product and board acceptance procedures in
[design-flaws-hardware-tests.md](design-flaws-hardware-tests.md) are prepared,
not executed. Source-mode self-test did exercise real native probe enumeration,
but did not establish a hardware acceptance result for a particular board.

Changes remain local and uncommitted. Before a future packaged candidate is
built in Actions, the reviewed server commit must be selected in the installer's
source lock through the normal authorized release process. Existing published
releases and their pinned source locks are unchanged.
