# External Qualification and Release-Gate Runbook

## Purpose and scope

This runbook covers the work that cannot be completed on the original software-only
development host. It is an execution guide for a separate privileged test host, hardware lab,
native non-Linux runner, or authenticated staging environment. It covers:

1. privileged Linux `vcan` qualification;
2. physical CAN and pyOCD/J-Link hardware-in-the-loop (HIL) qualification;
3. native Windows and macOS qualification;
4. OAuth/OpenID authenticated off-box monitor delivery;
5. repository-history privacy remediation; and
6. the maintainer-owned license and open-source/commercial split decisions.

This document does **not** authorize a production release. The CAN changes are currently under
the draft, testing-only BYO-Firmware-MCP pull request. Qualification changes and evidence must
remain in BYO-Firmware-MCP test branches or an isolated test fork. Do not open, retarget, merge,
publish, or upload artifacts to an official BYO Releases repository as part of this runbook.

## Current boundary

The test branch has passed native CAN simulation on hosted Linux, macOS, and Windows,
plus privileged Linux vcan. See the dated evidence in `RELEASE_STATUS.md` for the
exact tested scope and remaining platform and hardware qualification gaps. Its boundaries include:

- `socketcan_vcan` passed 15 tests against privileged Linux kernel interfaces;
  this provides software-bus evidence, not physical adapter qualification.
- Physical `socketcan` profiles are parsed but are rejected during startup. The generic Linux
  inspector also leaves physical bounded-operation and transmit-quiescence capabilities
  unproven. Do not bypass either control merely to make a laboratory test pass.
- Physical profiles remain disabled. Software `virtual` and `tcp_sim` profiles
  passed native Linux/macOS/Windows communication, timeout, cancellation, cleanup,
  and simulated MCP acceptance checks.
- Monitor delivery uses the local `simulated_remote/` filler. It has no OAuth/OpenID identity,
  authenticated upload, or durable off-box acknowledgement.
- The Apache-2.0 material under `LICENSES/` records the provenance of CAN code adapted from
  Firmware-CLI. It is not a project-wide license declaration.

Passing the ordinary unit suite again does not close any of these gates.

## Rules for every external run

### Repository containment

- Use a fresh clone or disposable runner.
- Check out the exact reviewed BYO-Firmware-MCP commit by object ID, not a moving branch name.
- Record the commit ID with `git rev-parse HEAD` before testing.
- Keep all work on a testing branch or isolated test fork.
- Never target an official BYO Releases repository.
- Never publish wheels, source archives, evidence bundles, or container images as production
  releases from these procedures.

### Evidence handling

Record enough information to reproduce the run without publishing sensitive values:

- UTC start and end times;
- exact Git commit and `uv.lock` SHA-256;
- operating-system, kernel, architecture, Python, and `uv` versions;
- test command lines and exit statuses;
- CAN interface type, driver version, configured bitrate, and state;
- pass/fail result for every acceptance case; and
- cleanup result, including whether any worker, network interface, probe session, or bus traffic
  remained after the test.

Redact OAuth tokens, cookies, authorization codes, DSNs containing credentials, private URLs,
user identity claims, hostnames, usernames, local absolute paths, USB serial numbers, probe
unique IDs, and device serial numbers. Store an opaque salted identifier when correlation is
needed. Run the repository privacy verifier before attaching any evidence to a pull request.

### Baseline checkout and software gate

On a Linux test machine, replace `<approved-commit>` with the reviewed pull-request head:

```bash
git clone https://github.com/JasonPeng2019/BYO-Firmware-MCP.git
cd BYO-Firmware-MCP
git fetch origin feature/canbus-integration
git switch --detach <approved-commit>
git status --porcelain=v1
git rev-parse HEAD
sha256sum uv.lock

uv sync --locked --extra can --group dev
uv run --locked --extra can ruff check \
  src tests scripts client-workspace client-workspace/bin/attach-project
uv run --locked --extra can pyright \
  src scripts client-workspace client-workspace/bin/attach-project
uv run --locked --extra can python -m unittest discover -s tests -p 'test_*.py' -v
uv run --locked --extra can python scripts/verify_release_artifacts.py
uv run --locked --extra can python scripts/verify_release_evidence.py
uv run --locked --extra can python scripts/verify_evidence_privacy.py
```

On macOS, use `shasum -a 256 uv.lock` in place of `sha256sum uv.lock`. On native Windows,
run the same Git and `uv` commands from PowerShell without the shell line continuations, and use:

```powershell
(Get-FileHash uv.lock -Algorithm SHA256).Hash.ToLowerInvariant()
```

The repository test commands are otherwise platform-neutral unless a test explicitly reports a
platform skip. Record every skip and confirm that it matches the documented qualification
boundary rather than treating it as a pass.

Stop if the checkout is unexpectedly dirty or any baseline gate fails. External qualification
must not be used to excuse a software regression.

## Track A: privileged Linux `vcan`

### Required machine

Use a disposable or administrator-approved Linux host with:

- a kernel providing `can`, `can_raw`, and `vcan`;
- permission to create and remove network interfaces (`CAP_NET_ADMIN` or controlled `sudo`);
- `iproute2`;
- `can-utils` for an independent `candump`/`cansend` peer; and
- the locked project environment with the `can` extra.

A different physical computer is not mandatory. The original host would be sufficient if its
administrator supplied these privileges and created the interface. Do not attempt to evade local
privilege policy with user namespaces or containers.

### Interface setup

Choose a test-only interface name and first verify that it is unused. The following example uses
`byovcan0`:

```bash
ip link show byovcan0 2>/dev/null && {
  echo 'Refusing to reuse an existing interface' >&2
  exit 1
}

sudo modprobe can
sudo modprobe can_raw
sudo modprobe vcan
sudo ip link add dev byovcan0 type vcan
sudo ip link set dev byovcan0 up

ip -details -statistics link show dev byovcan0
```

The details must identify the link as `vcan`, show it as up, and provide a positive interface
index. Save this output after removing host-identifying data.

### Test profile

Copy `fixtures/can/virtual_tx_allowed.yaml` into a private test directory and make only these
authority changes:

```yaml
profile_id: vcan_qualification
board_ids: [qualification_board]
adapter:
  kind: socketcan_vcan
  channel: byovcan0
```

Keep the exact receive and transmit allowlists narrow. Do not add an `identity` or
`debug_attachment` block to a `socketcan_vcan` profile. Calculate and pin the exact profile bytes:

```bash
export PYOCD_CAN_MODE=available
export PYOCD_CAN_CONFIG=/absolute/private/path/vcan_qualification.yaml
export PYOCD_CAN_CONFIG_SHA256="$(sha256sum "$PYOCD_CAN_CONFIG" | awk '{print $1}')"
unset PYOCD_CAN_RUNTIME_SHA256 PYOCD_CAN_READ_SECONDS
```

Do not commit the machine-specific profile or its absolute path.

### Required test cases

Use an automated privileged integration test checked into the testing branch. An ad hoc
`candump` transcript alone is not sufficient. The test may use `can-utils` as an independent
process, but the system under test must use the BYO `PythonCanAdapter`, isolated worker, session
manager, and MCP tool boundary where applicable.

1. **Capability identity:** inspection reports a nonphysical `vcan` link, the exact channel,
   positive ifindex, active state, and supported bounded open/send/receive/close/quiescence.
2. **Open continuity:** the interface identity is the same before and after backend open.
3. **Cross-process receive:** an independent process sends allowed standard ID `0x321`; BYO
   returns the exact payload and metadata within the configured bound.
4. **Receive filtering:** a disallowed ID is not returned as an allowed frame.
5. **Cross-process transmit:** BYO sends allowed standard ID `0x123`; an independent process
   receives the exact ID, frame format, DLC, and payload once.
6. **Transmit policy:** disabled transmit, an ID outside the allowlist, malformed payload, and a
   call without an exact plan are refused without putting a frame on the interface.
7. **Timeouts:** an empty read and an absent peer finish inside the documented operation bound.
8. **Endpoint drift:** deleting and recreating the interface, changing its ifindex, or taking it
   down causes a typed refusal rather than continued use.
9. **Cleanup:** normal disconnect, caller cancellation, worker failure, and process shutdown
   close the socket and retire the worker. A later bind succeeds only after proven cleanup.
10. **Budgets:** process quotas and rate limits still apply over the real SocketCAN transport.
11. **Privacy:** provider output remains metadata-only when selected by the profile, and retained
    artifacts follow the configured evidence policy.
12. **Regression:** the full software suite remains green while the privileged tests are opt-in
    and skip clearly when their declared interface is absent.

The full MCP route requires a setup-full, currently validated debug session. A direct adapter
test can qualify SocketCAN transport behavior, but it must not be reported as full MCP/HIL
evidence. Full-route evidence belongs in Track B with an attached test board and probe.

### Teardown

Remove only the interface created by this run:

```bash
sudo ip link set dev byovcan0 down
sudo ip link delete dev byovcan0
ip link show byovcan0 2>&1 || true
```

Confirm that the BYO server, its CAN worker, `candump`, and all peer processes have exited. A
passing run must not leave a network interface or background process behind.

### Completion criteria

This gate closes only when the privileged automated test, the baseline suite, cleanup checks,
and a fresh independent review pass on the same commit. Record Linux distribution, kernel,
python-can, pyroute2, iproute2, and can-utils versions. Update `docs/RELEASE_STATUS.md` with a
narrow claim describing exactly what was tested; do not generalize `vcan` evidence to a physical
bus.

## Track B: physical CAN and pyOCD/J-Link HIL

### Important implementation prerequisite

The current release candidate intentionally rejects physical `socketcan` startup in
`src/pyocd_debug_mcp/can_startup.py`. In addition,
`src/pyocd_debug_mcp/adapters/can_socketcan.py` does not claim generic physical bounded close,
send, receive, or transmit-quiescence support. Therefore **the current build cannot be physically
qualified simply by attaching an adapter**.

Before entering the lab, a testing branch must:

1. select one exact adapter/driver/firmware combination;
2. implement a driver-specific, reviewable capability inspector and close/TX-quiescence oracle;
3. preserve exact USB VID, PID, serial, driver, channel, bitrate, bus-state, and ifindex binding;
4. retain the startup rejection for every unqualified combination;
5. add failure, timeout, cancellation, hot-unplug, endpoint-drift, and cleanup tests; and
6. receive maintainer and independent adversarial approval for the narrow experiment.

Do not delete the startup guard, replace `UNKNOWN` capabilities with unconditional `SUPPORTED`,
or use a test mock as physical-bus evidence.

### Safe bench requirements

Use an isolated bench network, never a vehicle, production machine, safety system, or shared
facility bus. The bench needs:

- the exact candidate CAN adapter and driver;
- a second independent CAN node or analyzer;
- two correct termination resistors and verified bus resistance with power removed;
- common ground and appropriate transceivers;
- a current-limited supply and an accessible emergency power disconnect;
- the exact target board, firmware image, pyOCD-compatible probe, and—when applicable—J-Link;
- a known bitrate and a written ID/payload allowlist; and
- operator approval before any transmit-enabled test.

Start RX-only. Do not enable transmit until wiring, bitrate, identity, listen-only behavior, and
the independent analyzer have all been verified.

### Host and device inventory

Capture and sanitize:

```bash
uname -a
uv run --locked --extra can pyocd list
ip -details -statistics link show dev can0
readlink -f /sys/class/net/can0/device/driver
```

Use the platform's device inventory to obtain the exact adapter VID, PID, serial, driver, and
firmware revision. Do not publish raw serials or probe UIDs. Confirm that the values match the
private immutable test profile before starting BYO.

### Bus setup

Use the adapter vendor's documented commands. A typical SocketCAN RX-only setup resembles:

```bash
sudo ip link set dev can0 down
sudo ip link set dev can0 type can bitrate 500000 listen-only on
sudo ip link set dev can0 up
ip -details -statistics link show dev can0
```

Transmit qualification may proceed only if the selected driver can positively report the
required state. If the profile requires one-shot transmit, configure and verify one-shot mode;
if that state is unavailable, stop and leave transmit disabled.

### Profile requirements

A physical profile must contain:

- `adapter.kind: socketcan`;
- the exact channel and adapter identity (`driver`, `usb_vid`, `usb_pid`, and `serial`);
- a nonempty exact probe UID allowlist;
- the exact pyOCD version and CMSIS-Pack name, version, and SHA-256;
- `pack_debug_sequences: disabled` until a separate exact manifest is qualified;
- a verified nominal bitrate;
- RX-only transmit policy for the first phase;
- narrow exact arbitration-ID allowlists; and
- finite time, frame, rate, attempt, artifact, and byte budgets.

Store this profile privately, use restrictive permissions, and pin both the source and canonical
runtime hashes. Never substitute wildcard device identities.

### Qualification sequence

Run each phase independently and stop on uncertainty:

1. **RX-only idle:** open, bounded empty read, status, disconnect, and worker cleanup.
2. **RX-only traffic:** inject allowed and disallowed frames from the independent node; verify
   exact filtering, metadata, payload policy, ordering, and bounded return.
3. **Debug binding:** connect the exact probe and board, complete setup-full, run
   `board_validate`, then prove that `connect_can` accepts only that current board/connection
   identity and validation stamp.
4. **Lifecycle:** debug `disconnect` closes CAN first; CAN cleanup failure retains ownership and
   debug authority instead of claiming success.
5. **Hot unplug:** remove the CAN adapter and probe separately during idle, receive, send, and
   close. Every outcome must be typed, bounded after worker creation, and conservative.
6. **Bus faults:** exercise bus-off/error states with vendor-approved equipment. Verify no
   automatic widening, unsafe retry, or false active status.
7. **RX soak:** run a bounded load/soak test at the approved frame rate and confirm quotas,
   memory bounds, artifact caps, and clean shutdown.
8. **Transmit dry run:** review the exact ID and payload with the operator while transmit remains
   disabled. Prove refusal and zero observed bus traffic.
9. **Transmit-enabled run:** on an isolated harmless responder, enable only the reviewed IDs,
   require an exact plan, send bounded test frames, and verify exact reception with the
   independent analyzer.
10. **Rate/quota enforcement:** attempt one frame beyond each approved limit and prove refusal
    without an extra bus frame.
11. **Shutdown and power cycle:** terminate the MCP process during operations, power-cycle the
    target, and confirm no orphan worker, stale authority, or continuing transmit.
12. **Regression:** rerun the complete software, packaging, privacy, `vcan`, and release-evidence
    gates on the exact qualified commit.

### Physical/HIL completion criteria

Close only the specific adapter/driver/firmware/OS/board/probe combination that was tested.
Evidence must include an independent bus capture, sanitized BYO results, bounded timing results,
fault and hot-unplug outcomes, cleanup proof, and an independent review. Do not claim generic
physical CAN support from one adapter.

## Track C: native Windows and macOS qualification

For a native Windows checkout of the consolidated test branch, fetch that branch
alone. The remote has historical branch names differing only by capitalization,
which collide in Git's files-based ref storage on a case-insensitive filesystem:

```bash
git clone --single-branch --branch test/canbus https://github.com/JasonPeng2019/BYO-Firmware-MCP.git
```

This clone retains the selected branch's history. Native Windows CI uses a
single-revision checkout because its selected tests do not inspect ancestry;
Linux/macOS and release-evidence CI retain full history. The artifact uploader
uses the pinned Node 24 action rather than relying on runner migration of Node 20.

The software update enables `virtual` and `tcp_sim` on native macOS/Windows.
Run `tests.test_can_native_sim` and `tests.test_can_mcp_sim_e2e` on each native
runner and retain the results; adding CI configuration is not execution evidence.
The dated native jobs linked in `RELEASE_STATUS.md` qualify the tested software
backends; the earlier Windows disabled-mode results alone do not.
Physical native backends remain separate work.

Use separate native Windows and macOS machines or hosted runners. A compatibility layer, Linux
container, Wine environment, or virtual machine with a Linux kernel is not native evidence.

For each platform:

1. run the baseline install, off-mode inventory, complete software suite, package build, and
   clean wheel/sdist install;
2. confirm private-file, no-symlink/reparse-point, snapshot, process-spawn, cancellation,
   timeout, signal/termination, and cleanup semantics on the native filesystem and process model;
3. select and implement a platform-native CAN backend rather than mapping a Linux SocketCAN
   name onto an unrelated driver;
4. add a backend-specific identity and capability contract;
5. run virtual/native-adapter RX, TX, endpoint-drift, cancellation, and cleanup cases;
6. repeat physical/HIL testing for any platform/adapter combination proposed for support; and
7. keep the current fail-closed guard for every platform and backend not explicitly qualified.

An independent reviewer must confirm that a passing mocked backend is not being represented as
native driver or physical-bus evidence.

## Track D: OAuth/OpenID off-box monitor delivery

### Decisions required before implementation

This track does not inherently require a different operating system, but it requires an external
staging service and credentials not present on the development host. Before writing the
production transport, maintainers must approve:

- the remote service and ownership boundary;
- OAuth grant type and OpenID provider;
- issuer, audience, scopes, redirect/device-flow behavior, and token storage mechanism;
- upload API, object identity/idempotency key, maximum request size, and timeout policy;
- the exact durable-ACK contract;
- retention, deletion, residency, and incident-response policy; and
- which fields are permitted to leave the machine.

Use a dedicated test tenant and synthetic records. Never use a production tenant or real user
records for initial qualification.

### Implementation constraints

- Replace the transport only at the existing monitor transport seam.
- Keep request handling nonblocking; uploads remain in the background worker.
- Treat an item as delivered only after the approved remote durability acknowledgement.
- Do not delete or retire the local unacknowledged record on a network write alone.
- Preserve ordering, idempotency, restart recovery, staleness, closeout, and backpressure
  behavior.
- Keep access tokens, refresh tokens, client secrets, authorization codes, and identity claims
  out of logs, tool responses, artifacts, exceptions, and Sentry envelopes.
- Validate TLS certificates and OpenID issuer, audience, expiry, nonce/state, and signature as
  required by the selected flow.
- Use least-privilege scopes and an approved secret store; never commit `.env` credentials.

### Required staging tests

1. first authorization and identity binding;
2. token expiry, refresh, revocation, and invalid issuer/audience/signature;
3. denied consent and insufficient scope;
4. TLS failure, DNS failure, connection timeout, interrupted upload, and process restart;
5. HTTP 401/403, 408, 409/idempotent replay, 429 with bounded retry, and 5xx behavior;
6. duplicate submission and lost-response replay without duplicate durable records;
7. remote ACK validation before local deletion;
8. replay of the `simulated_remote/` backlog at cutover without loss or reordering;
9. 100- and 200-call snapshot boundaries, final closeout, and recovery;
10. offline buffering and later delivery without blocking MCP requests;
11. maximum-size, malformed, and privacy-disallowed payload rejection;
12. concurrent workspaces and identities without cross-tenant delivery;
13. local clock skew and expired-token behavior;
14. proof that credentials and prohibited fields are absent from all retained artifacts; and
15. staging-side confirmation that the acknowledged object survives client shutdown and local
    deletion.

Run these tests against an intentionally nonproduction endpoint. Sanitized evidence may record
opaque remote object IDs and timestamps, but not tokens, identities, private URLs, or raw
payloads. A service-side durable record is required; `FILLER_SIMULATED` is not an off-box ACK.

### OAuth completion criteria

The gate closes only after code review, security/privacy review, staging end-to-end tests,
credential-leak scanning, recovery testing, and a documented rollback plan. Production rollout
and credential issuance are separate approvals.

## Track E: repository history, licensing, and product split

### History/privacy remediation

Perform the history review in an isolated mirror, not the working development clone:

```bash
git clone --mirror https://github.com/JasonPeng2019/BYO-Firmware-MCP.git byo-history-audit.git
git -C byo-history-audit.git fetch --all --tags --prune
git -C byo-history-audit.git fsck --full
git -C byo-history-audit.git for-each-ref --format='%(refname) %(objectname)' \
  > reviewed-refs.txt
```

Use a maintainer-approved history and secret scanner over every reachable ref, tag, blob, tree,
commit message, and relevant Git hosting attachment. Do not paste detected secret values into an
issue or evidence log. Classify each finding as credential, personal data, private path/host,
licensed material, generated artifact, or false positive.

Maintainers must then choose one of these release inputs:

- a history-free clean export of an approved tree; or
- an approved history rewrite followed by a fresh mirror clone and complete rescan.

A rewrite requires explicit maintainer authorization, credential rotation before publication,
coordination with collaborators, and a plan for tags, pull requests, forks, caches, artifacts,
and existing clones. A local rewrite alone does not remove already distributed data.

### Project-wide license decision

Prepare a source/dependency/provenance inventory and identify files with missing, conflicting,
or third-party terms. Preserve `THIRD_PARTY_NOTICES.md` and the Firmware-CLI derivation record.
Project maintainers or counsel must choose and approve the project-wide license and public
redistribution terms. Do not infer that Apache-2.0 applies to the entire project merely because
an Apache license text is bundled for adapted CAN files.

### Open-source/commercial split

The maintainers must approve the product boundary before files are moved. In isolated testing
repositories:

1. map each module, dependency, document, skill, and artifact to open-source, commercial, shared,
   or excluded;
2. identify APIs and package metadata that cross the proposed boundary;
3. ensure the open package works without importing, documenting, or silently requiring private
   modules;
4. ensure private packages do not accidentally redistribute restricted source or evidence;
5. build and test both products from clean clones;
6. scan both complete histories and artifacts for privacy, secrets, and license compatibility;
7. verify names, URLs, issue links, update channels, and release destinations; and
8. obtain maintainer/legal approval before creating or publishing final repositories.

This work can be prepared on any suitable development machine, but its final boundary and
publication decision are human approvals, not test results.

## Final sign-off checklist

Do not change the release status from preview until every applicable item is checked:

- [ ] Exact reviewed BYO-Firmware-MCP commit recorded.
- [ ] Work remained on a testing branch/fork and did not target BYO Releases.
- [ ] Baseline software, type, lint, package, privacy, and evidence checks passed.
- [ ] Privileged Linux `vcan` cases passed and cleaned up.
- [ ] Any claimed physical adapter combination passed isolated-bench and HIL qualification.
- [ ] Any claimed Windows/macOS backend passed native qualification.
- [ ] OAuth/OpenID staging delivery produced real durable off-box ACKs without credential leaks.
- [ ] Every reachable repository ref and proposed release artifact passed privacy review.
- [ ] Project-wide licensing and redistribution were approved by the responsible maintainers.
- [ ] Open-source/commercial boundaries were approved and both clean builds passed.
- [ ] Evidence was sanitized and independently reviewed.
- [ ] `docs/RELEASE_STATUS.md` was updated with narrow, evidence-backed claims.
- [ ] Any production release or official release-repository action received separate explicit
      authorization.

If an item is not applicable, record who made that decision and why. Never convert a missing
environment, unavailable capability, skipped test, simulated response, or human approval into a
pass.
