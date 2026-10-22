# Focused real-hardware checks for the four design-flaw repairs

These are procedures to run against the repaired candidate, not recorded passes.
Do not run the full suite, reflash firmware, mass erase, write protection values,
or execute a flash-controller unlock sequence as part of these checks.

## Candidate and evidence

Use an Actions-built candidate for installed-product checks. Record the server
source SHA, installer source SHA, archive SHA-256, installed sidecar SHA-256,
host OS/architecture, probe provider/UID, target part, and adapter USB identities.
For source-mode checks, also record the exact Python environment and source path.
Do not treat source-mode compiled-flag simulation as an installed-product result.

Use a disposable firmware project with an existing known-good image. For UART
misrouting, use board A with a debug probe and a separate serial adapter A, plus
a second adapter B connected to an independent console. Use a harmless known
echo command supported by the existing firmware and a unique nonce per attempt.
Capture B's receive traffic independently. Record actual identities during
setup; a port number or a board's product name is not its stable USB identity.

Follow the live MCP schemas and normal plan/permission workflow. Save every
request, result, exception, exit code, port inventory, relevant map, and observer
log. Finish all independent cases before triage. If a prerequisite fails, mark
its dependent cases BLOCKED with that prerequisite; continue other cases and the
other board. Keep the code/artifacts fixed throughout the pass.

## H3: installed native discovery, hooks, and helper guidance

1. On each release host that is available, start the installed product through
   its real launcher with no `PYOCD_CLI` override and without relying on a
   separately installed Python/pyOCD. First unplug probes and run the selected
   runtime sidecar's `self-test --runtime-root <runtime> --launcher-version
   <version>`. Expect exit 0 and `probe_discovery_verified: true`; zero probes
   is valid. Save stdout/stderr. A driver/backend failure is a failure, not proof
   that no probes exist. On Windows, capture the Unicode listing without a
   `charmap` error.
2. Attach one known probe and call `setup_overview`. Expect its real UID and
   provider, successful native-list diagnostics, and an argv containing
   `probe-list list --probes`, never `<sidecar> -m pyocd`. Select the explicit
   discovered connection and perform `connect` plus a read-only board query.
   Repeat for each available CMSIS-DAP/ST-Link/J-Link family; record unavailable
   families/hosts as UNRUN, rather than extrapolating from one probe.
3. Exercise the discovery-hook contract in a second disposable project. Trigger
   the normal native-unavailable condition (unplug the native probe, or use the
   UART hook path on a host with no native serial row). Install a `server-python`
   hook using the documented manifest. Have it import a sibling helper and emit
   one contract-valid row for the known hardware with a spaced argument. Refresh
   hooks and request discovery. Expect valid JSON/row provenance, script-style
   argv, and the sidecar `run-hook` route. A reported hook row alone is not proof
   the hardware was opened; subsequently connect to the actual endpoint.
4. Request setup-status/build guidance. Expect the installed launcher's
   `internal native-build` route and an invocation cwd of the firmware project,
   distinct from the build child's `--cwd`. The bundled absolute launcher path
   must work even when `byo` is absent from PATH. Do not compile on the coordination
   PC. A harmless invocation with an intentionally missing required helper input
   may verify parsing: expect the native-build parameter error, not a sidecar
   `invalid choice: pyocd_debug_mcp.native_build` error. Actual build verification
   belongs in Actions.

## H1: prohibited SVD registers on a live board

Use a setup-full board whose verified pack contains an SVD, preferably the
STM32L476RG from the report. Check its *actual* pack/reference manual before using
these illustrative addresses:

| Register | STM32L476RG address | Expected map result |
|---|---:|---|
| FLASH.KEYR | `0x40022008` | prohibited; still write-only |
| FLASH.OPTKEYR | `0x4002200C` | prohibited; still write-only |
| FLASH.CR | `0x40022014` | prohibited |
| FLASH.OPTR | `0x40022020` | prohibited |
| FLASH.WRP1AR | `0x4002202C` | prohibited |
| FLASH.ACR / FLASH.SR | `0x40022000` / `0x40022010` | retain ordinary SVD access |

1. Preserve a copy of the pre-repair project/map. Start the candidate on that
   project. A protected operation must report `safety/map-refresh-required` for
   the old generator. Use the advertised `board_safety_refresh` route, then
   validate again. Existing application ownership must not widen or disappear.
2. Inspect the newly committed immutable full-policy map as well as its project
   projection. Check every listed dangerous register, including aliases, is
   effectively prohibited. For unlock registers, confirm a write-only access
   span is retained so prohibition does not grant read permission. Confirm the
   benign status/access registers and a normal GPIO/USART control remain usable.
3. Test negative actions through a **source-mode test launch with a write-blocking
   backend observer**: allow real discovery, connection, and reads; record and
   refuse every backend `write_memory` call before forwarding. Use the existing
   `target_control.configure_backend_for_tests` seam before server import, with a
   wrapper around the real process backend. Submit normal `register_write` plans
   and actions for the prohibited spans. Expect `safety/prohibited` and **zero
   backend write calls**. A backend-observer refusal means the product guard
   failed. Do not send unlock keys, RDP encodings, or erase-start values even with
   this observer. In the installed binary, limit these dangerous-register checks
   to map inspection and planning unless equivalent write interception is
   available; explicitly record action-level rejection as UNRUN there.
4. As a positive hardware control, use a documented harmless GPIO output bit
   selected by the operator. Save its original value, perform a planned masked
   write, verify the physical/readback effect, and restore that bit with another
   planned write. Do not use flash latency or flash status bits as this positive
   control. Confirm ordinary clock configuration was not prohibited merely
   because its name contains `clock`.

## H4: saved UART disappearance, repeated refusal, and explicit recovery

1. Confirm and save board A's stable adapter A. Record its USB serial/VID/PID and
   current path. Demonstrate the existing harmless echo operation on A.
2. Leave A's debug probe connected, unplug **only adapter A**, and leave B visible.
   Run `read_serial`, `write_serial`, and `serial_exchange`, each using its own
   normally accepted plan and a null `port`. Expect
   `uart/saved-adapter-unavailable` before opening B. The error must identify A,
   list the current ports, require a **new matching plan** for an explicit port,
   explain the multiple-port human choice, and name `board_setup` for permanent
   changes. B's capture must contain none of A's nonce traffic.
3. Repeat each failed action twice without setup/reselection. Expect the same
   refusal each time. Repeat with zero visible adapters and with multiple
   unrelated adapters; no case may resume guessing. Repeat for a session-only
   adapter on a host/adapter that lacks a stable USB identity, within the same
   server run. A missing session choice must stay unavailable until explicitly
   replaced or the run ends.
4. Reconnect A through a different USB port so its device path changes. With its
   stable identity unchanged, the null-port operation must recover automatically
   and exchange the nonce with A, not B. Restore normal hardware connections.
5. Deliberately choose an alternate test console with the operator, submit a
   **new** plan containing that explicit port, and run the matching action. Expect
   exactly the deliberate nonce on that console. Verify the original saved USB
   attachment remains unchanged. A changed port supplied against the old plan
   must still get a plan mismatch.
6. As an independent control in a new no-setup project, call a raw UART tool with
   an explicit test-console port. It should retain its normal bounded behavior.
   Raw UART must not acquire a saved-adapter lookup or a new-plan requirement.

## H5: POSIX restart after a connected server is killed

Run on native Linux and macOS where available; Windows mocks do not satisfy this
case. Repeat source and installed modes, recording unavailable combinations.

1. Launch the test server as a tracked child of the test driver, with an isolated
   project/marker root. Record its PID/start token and its provider worker's
   PID/start token/PGID. Connect to a board and obtain a read-only board query.
   Keep an independently tracked unrelated control process alive throughout.
2. Kill **only the exact server process launched by the driver**, using its live
   process handle. Do not use a name-wide kill command. Let its worker observe
   EOF and exit. Verify the recorded worker birth is gone and `killpg(pgid, 0)`
   reports `ESRCH`; retain the server's stale marker on disk before restart.
3. Restart the same project normally. Expect the empty-group marker to be removed
   automatically, no startup-hygiene refusal, and successful rediscovery and
   reconnection/read-only query. Verify the unrelated control process remains
   alive and unrelated marker files are unchanged.
4. In source mode, reproduce an empty stale marker under an isolated shared
   source-mode runs root and start a second disposable project. The old empty
   marker must not block that project either.
5. Keep the nonempty/permission-denied/recycled-identity controls from the focused
   regression tests: they must retain evidence and avoid destructive signals.
   A surviving leaderless group is deliberately not automatically killed based
   solely on an old numeric PGID. Report such a case separately from the fixed
   empty-group restart case.

## Acceptance record

For each case record PASS, FAIL, BLOCKED (with dependency/reason), or UNRUN (with
host/hardware reason), and link its request transcript and evidence hashes.
All available independent cases must finish before any product repairs. Keep
the original failure logs and start a separately identified pass after repairs.
