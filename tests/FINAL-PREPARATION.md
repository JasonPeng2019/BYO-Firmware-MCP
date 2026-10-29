# Final preparation execution paths

Deterministic quality, build, source and artifact checks execute in GitHub
Actions with the real locked dependencies. Real model requests, native-client/U1
integration and physical board acceptance execute LOCALLY only, never in GitHub
Actions or on a self-hosted runner. These paths prepare source for the SAME
independent V/R final roles. Build-owner runs do not close the original
complete source/native/installed/model/U1/board scope.

The existing native code-analysis workflow now executes
`tests.test_final_policy_fingerprint_migration` on all four targets. Its controlled
fingerprint test retains current geometry. Its separate genuine producer test
executes all deciding imports from the exact unmodified original source archive
in `fixtures/policy-one-producer`, with the real registered Nordic pack and
datasheet. It persists/refreshes that producer's output, rather than changing a
current digest and calling it an old-produced map. The child uses an empty prior
compatibility-map repository; no device I/O is necessary. Exact archive/source,
child stdout/stderr/exit, produced map and public response are retained in the
existing FR15 evidence artifact, including failures before an expected response.

The same workflow runs the unchanged original deterministic model-pin body and
deciding assertion without the real-agent authentication/store fixtures:

```sh
uv run python tests/prepared_model_method.py --firmware-source . --pin-oracle --evidence "$RUNNER_TEMP/pin-oracle"
```

This is one original method and two synthetic header checks, zero model requests.
It needs no authentication and may also run locally.

The other 17 original methods make real pinned `gpt-5.6-luna` requests and run
LOCALLY only; the driver refuses GitHub Actions and self-hosted runners. They
use the operator's existing supported codex sign-in. `codex exec
--ignore-user-config --ignore-rules --ephemeral` keeps the operator's own
config, its MCP servers and execpolicy rules out of the run and writes no
session files; authentication still comes from the existing codex home. The
suite's single MCP server, model, effort and cadences are `-c` overrides frozen
in `tests/codex_harness.py`. Nothing reads, copies or moves credentials or
opens a codex config, and no second sign-in, new OS account, CI model secret or
runner is required. `fixtures/model-boundary.template.json` is deliberately
nonlaunchable. Once its actual source hashes, KnownFolder proof and OS
profile/app-data paths are supplied, each original method runs once:

```sh
uv run python tests/prepared_model_method.py --firmware-source . --qualification QUALIFIED.json --method ORIGINAL_SELECTOR --evidence CASE_DIRECTORY --execute
```

Set `BYO_MCP_MONITOR_ROOT` to a fresh dedicated directory below the qualified
private root and bind that exact path as `monitor_root` in the qualification.
The existing supported production launch setting is forwarded explicitly to
the source MCP child; assertions and cleanup select that same effective store.
Native default application-data selection is still qualified independently;
real model requests do not snapshot, rewrite or restore unrelated shared stores.
Each method refuses an existing monitor root and restores its owned store at
teardown. The historical `uv run python -m unittest tests.test_monitor_codex_e2e -v`
entrypoint supports the same setting, and skips (never passes) when codex is absent
or not signed in. The product pin, effort and cadence are unchanged. The
complete source runner in Actions records the 17 real requests as blocked
before import and runs the deterministic body in its own observed child.
Historical all-model blocks remain raw.

The Installer's existing native build workflow executes
`release/scripts/final_hosted_forwarding.py` against each newly built native
bundle. Before install/store effects, it records OS token/profile/KnownFolder
and directory owner/DACL/token access on Windows or uid/passwd-home and ownership on POSIX,
joined to the exact ephemeral hosted job. It reuses the compiled read-only
selector, installs privately, observes the actual installed launcher's unique
credential-free sidecar PID/birth/image/environment, performs the real handshake
and health call, and retains new workspace records, preservation and EOF cleanup.
Unknown/denied observations stay nonpassing. The original strict observation
environment and its refusal outside its single root are unchanged.

The Windows hosted profile may be SYSTEM-owned: its native account/profile and
KnownFolder join, trusted owner and actual enabled-token DACL Modify rights must
all qualify before product effects. EOF can deliver sealed records to the local
`simulated_remote` directory; the observation retains both permitted locations
and requires the current actual run's bound workspace and durable close counts.
This local simulated delivery does not establish off-box permanence. Read-only
selector denial includes exact path/mode/callers and stays fatal until explained.

No hosted check supplies native client U1 trust enforcement, real model calls,
hardware authority, compiled-positive board/ELF/setup/flash-plan acceptance or
either safe physical board. Those original prerequisites remain explicit and
are LOCAL acceptance: use the existing supported native client sign-in and the
client's own project/hook review, and rediscover both boards' actual probe/UART
identities before any safe read-only check. No erase, program, unlock,
protection, reset, halt or resume operation is part of this acceptance.
