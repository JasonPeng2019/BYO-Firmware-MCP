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

The macOS workspace-attachment and SWD-cleanup regressions are corrected. Attachment
uses directory-relative operations instead of `/dev/fd` traversal and preserves
user files and directory modes through atomic replacement. Process cleanup reaps
an exited worker before confirming process-group disappearance, including Darwin's
EPERM response for an unreaped zombie. Independent review exercised preservation,
rollback, descriptor cleanup, and process retirement.

The release generator runs against fresh process-recovery state. The live Codex
harness explicitly gives each MCP server its own runs directory, so the operator's
unresolved process markers cannot disable the test server. CAN fixtures canonicalize
their newly allocated temporary roots before passing them to no-symlink profile
reads; supplied-path checks remain intact.

## Retained candidate verification matrix

The four retained logs and their attestation describe one complete source manifest.
Changing any candidate source invalidates that manifest and requires regeneration.
The consistency and privacy gates remain mandatory in CI.

| Environment | Evidence in this candidate | Status |
| --- | --- | --- |
| macOS 26.6.2 x86_64, Python 3.12.13 | Full default 1,438-test software suite (30 skips during generation: platform/opt-in cases plus one self-referential retained-log case; all 5 release-evidence cases then run against the immutable staged logs), Ruff, Pyright, lock and privacy checks | Retained generator logs |
| Opt-in Codex/MCP monitor module, pinned `gpt-5.6-luna` | 17 live agent/MCP cases plus 5 harness/unit cases, 22 module tests total; private monitor and process roots | Retained generator log |
| CPython 3.10.20 | All 228 source files parsed with the 3.10 interpreter | Retained parse log |
| GitHub-hosted Ubuntu, Python 3.10 and 3.12 | Complete runtime suites, clean installs, evidence checks, and exact wheel/sdist handoff verification | Required CI gates; consult the current branch run |
| GitHub-hosted Linux/macOS/Windows, Python 3.12 | Native software CAN, public simulated MCP, retained captures, and cleanup; macOS attachment/SWD regressions; Linux kernel vcan | Prior CAN/vcan success linked above; added macOS regression lane must pass on the current revision |
| Physical CAN and real SWD/probe onboarding | Requires attached hardware | External qualification gate |

[Current test-branch CI runs](https://github.com/JasonPeng2019/BYO-Firmware-MCP/actions/workflows/software-can.yml?query=branch%3Atest%2Fcan-macos-vcan-20261014)
retain the exact revision, platform outcomes, simulation evidence, and package bytes.
The test branch is not merged or published as a production release. Native Windows
workspace attachment remains unsupported (use WSL for that source-handoff command);
this does not prevent native Windows MCP or CAN simulation testing.

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
then generate the manifest-bound logs (the generator runs both suites itself):

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
- whole-product native Windows regression qualification and platform-specific physical
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
