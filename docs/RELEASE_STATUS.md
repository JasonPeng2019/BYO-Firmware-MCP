# 0.1.8rc1 release status

`0.1.8rc1` is a **mixed-product, pre-split, local-delivery preview**. It is a
software evaluation candidate, not a claim that the planned open-source and
commercial product split is complete and not a public production release.

The CAN integration is currently proposed through draft, testing-only
[BYO-Firmware-MCP pull request #2](https://github.com/JasonPeng2019/BYO-Firmware-MCP/pull/2),
from `feature/canbus-integration` into `release/0.1.8-preview`. The pull request
does not target or publish to a BYO Releases repository.

The preview is intended to make the current source tree and its optional
Classical CAN integration reproducible on software-only hosts. The default
installation excludes CAN backend libraries. Install the `can` extra and stage
an operator-controlled profile before enabling CAN; off mode retains the same
seven-name MCP registration without importing `python-can`.

## Software-preview scope

- deterministic package metadata and archive contents;
- base installation, import, and static off-mode CAN inventory;
- an explicit source/raw-sdist client-workspace handoff (not an installed-wheel
  feature and never attached implicitly by `pip install`);
- the process-local `python-can` virtual adapter/worker contract, including
  exact RX/TX through a child-hosted peer (not a cross-process transport);
- strict startup profiles, plan binding, allowlists, quotas, cleanup, and
  canonical CAN results; and
- local simulated monitor delivery, which never claims off-box durability.

## Software simulation verification (2026-10-14)

The test branch adds native `tcp_sim` and `virtual` profile loading,
bounded caller startup with late-child ownership, macOS directory-relative release
evidence I/O, and immutable quota-bound `read_can` capture files. Dedicated tests
exercise native sockets and the public MCP stdio path with simulated SWD and a
fixture-seeded generic setup-full policy. Physical profiles remain disabled.

The native CAN jobs passed on hosted Windows, macOS, and Ubuntu with Python 3.12
at `16db6e3` ([run and retained simulation artifacts](https://github.com/JasonPeng2019/BYO-Firmware-MCP/actions/runs/38648726195)).
Each ran 98 tests: Windows skipped three POSIX-only cases; macOS and Linux skipped
one Windows-only handle-ownership case. Windows also passed the two existing
registry/junction regressions. Linux passed all 15 privileged kernel-vcan tests.
macOS and Linux each passed four release-evidence I/O cases, deferring the one
self-referential historical-attestation check. The MCP test uses real stdio,
real CAN worker processes, independent TCP traffic, public validation/plans,
verified capture bytes, and a final cleanup receipt; SWD and setup policy remain
explicitly simulated. Independent review found no remaining blocker in this scope.

Local macOS clean wheel/sdist installs and all 28 package-regression tests passed.
The Linux VM ran 1,430 tests; three fixture/inventory failures were corrected and
all 72 targeted follow-ups passed. A broader local macOS run still exposed
separate workspace-attachment `/dev/fd` and SWD-cleanup failures outside this CAN
change; this is not whole-product native platform qualification.

The older retained release logs below describe the pre-change candidate; their source
manifest intentionally becomes stale after implementation edits. Regenerate the
complete release evidence before a release, including its opt-in live-agent lane.
Focused and regression logs for this change are separate evidence. The aggregate
release CI remains red at its stale-attestation gate; native simulation job success
must not be presented as release approval.

## Retained baseline verification matrix (before these changes)

| Environment | Evidence in this candidate | Status |
| --- | --- | --- |
| Ubuntu 24.04 x86_64, Python 3.12 | Full default 1,397-test software suite (14 platform/class-level opt-in skips in a normal run; evidence generation defers its one self-referential retained-log case and runs all 4 release-evidence cases against the immutable staged set before publication; 1,415 discovered cases), Ruff, Pyright, package inventory, privacy/evidence consistency, and clean-install verifier | Locally verified |
| Opt-in Codex/MCP monitor module, pinned `gpt-5.6-luna` | 17 live agent/MCP cases passed against isolated explicit monitor roots, plus 5 harness/unit cases (22 module tests total) | Locally verified |
| CPython 3.10.20 | All 222 source files parsed locally with the 3.10 interpreter; the full 1,397-test runtime suite passed in the hosted Ubuntu 3.10 lane | Local parse and hosted runtime verified |
| GitHub-hosted Ubuntu, Python 3.10 and 3.12 | Locked lint, format, type, privacy, evidence, test, clean-install, and artifact workflow completed on the feature branch; both runtime lanes passed | Hosted verification passed ([branch runs](https://github.com/JasonPeng2019/BYO-Firmware-MCP/actions/workflows/software-can.yml?query=branch%3Afeature%2Fcanbus-integration)) |
| GitHub-hosted Windows, Python 3.12 | Six focused off-mode, configured-mode fail-closed, registry-concurrency, and junction-containment checks passed | Hosted verification passed ([branch runs](https://github.com/JasonPeng2019/BYO-Firmware-MCP/actions/workflows/software-can.yml?query=branch%3Afeature%2Fcanbus-integration)) |
| macOS, any supported Python | Configured CAN fails closed before profile loading; no native runtime lane | Disabled pending qualification |
| Linux `vcan`, physical CAN, or pyOCD/J-Link HIL | Requires privileged interfaces or attached hardware | External qualification gate |

The virtual interface is a python-can process-local queue. The production CAN
adapter remains isolated in a spawned worker, so a virtual bus opened by an
unrelated process is not its peer. Software tests put an independent peer in
that worker solely to prove exact send/receive behavior. Native `tcp_sim` and Linux
`vcan` now provide cross-process simulation evidence; neither proves a physical bus.

CAN startup has a five-second caller budget covering OS spawn and readiness.
`multiprocessing.Process.start()` itself cannot be interrupted portably; a daemon
launch owner retains a stalled call and retires any late child. Cancellation and
startup deadlines no longer wait on a spawn-held lifecycle lock. Until retirement
is proven, status reports `startup-pending`/`unknown`, never a clean close. No
backend command is submitted after startup authority is revoked. This bounds the
application's startup wait, not OS process creation, interpreter exit, or stalled
filesystem I/O during evidence persistence.
## Regenerating the retained evidence

The release logs are generated by checked-in scripts rather than an ad hoc
shell transcript. On a fresh host, provision the pinned parse-only interpreter,
run the default and opt-in suites, and then generate the manifest-bound logs:

```bash
UV_PYTHON_INSTALL_DIR=/tmp/byo-uv-python uv python install --no-bin 3.10.20
UV_PYTHON_INSTALL_DIR=/tmp/byo-uv-python uv run --locked --extra can python scripts/generate_release_evidence.py --python-install-dir /tmp/byo-uv-python
uv run --locked --extra can python scripts/verify_release_evidence.py
```

The generator captures the candidate manifest first, then itself runs the static
checks, full software suite, live Codex/MCP suite, and executable
`scripts/verify_python310_parse.py` command under CPython 3.10.20. It rejects any
source drift before writing the four logs and their attestation. The one
self-referential retained-log test is deferred during the full suite. The generator
then builds an immutable staged set, runs the complete release-evidence module and a
fresh privacy scan against those exact bytes, and publishes only after both pass. A
post-publication consistency and privacy gate rolls back the prior set on failure.

## External release gates

The following work is deliberately not represented as complete by this
candidate:

- a maintainer-approved project-wide license and public redistribution decision;
- a final repository-history privacy remediation before public repository
  publication: use a history-free clean export or rewrite and rescan every
  reachable ref, then address any already-published clone/cache separately;
- the documented open-source/commercial repository and package split;
- real OAuth/OpenID identity, authenticated remote upload, and off-box ACKs;
- physical CAN adapter/bus qualification and hardware-in-the-loop evidence;
- whole-product native platform regression closure and platform-specific physical
  backend qualification, plus physical pyOCD/J-Link regression coverage.

The machine requirements, execution sequence, evidence rules, and completion
criteria for these gates are documented in the
[external qualification runbook](EXTERNAL_QUALIFICATION_RUNBOOK.md). That
runbook is testing-only and does not authorize publication to an official BYO
Releases repository.

The Apache-2.0 text bundled under `LICENSES/` and `THIRD_PARTY_NOTICES.md`
records provenance for CAN code adapted from Firmware-CLI. It does **not**
declare a license for the project as a whole. Public publication remains
blocked until the maintainers resolve that legal gate.
