# Native analysis runtime mapping

Revision 2, 2026-10-10. The accepted portability contract broadens mapping
schema **1** to the four existing product targets. It supersedes the previous
Windows-only implementation boundary and preserves the
[analysis behavior contract](code-analysis-contract.md), project configuration
version 1, public tool parameters and result formats. This source document
establishes no native build, installed, baseline, signing or publication acceptance.

The accepted contract is `accepted-portability-contract.md`, committed as
`c5d048f`, SHA-256
`e648242e4032b251de922c2cc3f86241315833d28e15aea59ac21004590f219a`.
Installer source reference is `a5d8cf575207f619f71e04c48e1a3498ddd8854f`;
ROOT owns matched final source pins and integration. No consumer selects inputs
from a mutable installer checkout or copies its dependency implementation.

## Targets and closed mapping

`R/analysis/runtime.json` resolves only from the explicitly supplied canonical
immutable runtime root `R`. Its closed syntax is
[byo-analysis-runtime.schema.json](contracts/byo-analysis-runtime.schema.json).

| Target | Mapping platform / architecture | Native executable format |
| --- | --- | --- |
| `windows-x86_64` | `windows` / `x86_64` | AMD64 PE32+ |
| `macos-aarch64` | `macos` / `aarch64` | Thin little-endian Mach-O 64, CPU `0x0100000c`, executable type 2 |
| `macos-x86_64` | `macos` / `x86_64` | Thin little-endian Mach-O 64, CPU `0x01000007`, executable type 2 |
| `linux-x86_64` | `linux` / `x86_64` | ELF64 little-endian, machine 62, executable/PIE type 2 or 3 |

Host OS `Windows/Darwin/Linux` normalizes to `windows/macos/linux`; machine
`AMD64/x86_64` to `x86_64`, and `arm64/aarch64` to `aarch64`. Require the
host, release manifest and analysis mapping to agree. Mapping tokens remain
canonical. `linux-x86_64-glibc-2.28` is a release verifier/CI target alias,
not a mapping platform or architecture value. macOS 12+ and Linux glibc 2.28+
remain the accepted baselines; translated processes do not establish native
build-host architecture evidence. No additional product target is introduced.

All fields are required, without defaults: exact integer-token
`schema_version: 1`, `platform`, `architecture`, exactly two programs
`cppcheck` and `clangd`, and each program's three-part numeric `version`,
`executable`, `licenses`, `dependencies`, `sbom_ref` and data directories.
Reject duplicate JSON keys, unknown fields, nulls and type coercion, including
`true` and `1.0` as schema version. JSON Schema numeric equivalence alone does
not enforce the integer-token rule; the runtime's strict decoder does.

| Field | Exact runtime-relative layout |
| --- | --- |
| Cppcheck executable | Windows: `analysis/cppcheck/cppcheck.exe`; POSIX: `analysis/cppcheck/cppcheck` |
| Cppcheck data | `cfg_dir: analysis/cppcheck/cfg`; `platforms_dir: analysis/cppcheck/platforms` |
| clangd executable | Windows: `analysis/clangd/bin/clangd.exe`; POSIX: `analysis/clangd/bin/clangd` |
| clangd resource directory | `analysis/clangd/lib/clang/<major>`, matching the first version component |
| Windows dependencies | Adjacent regular basename `*.dll` files: Cppcheck root or clangd `bin/` |
| macOS dependencies | Regular basename `*.dylib` files under `analysis/<program>/lib/`, native CPU and Mach-O dylib type 6 |
| Linux dependencies | Regular basename `*.so` or `*.so.<numeric-components>` files under `analysis/<program>/lib/`, ELF64 LE machine 62 and shared-library type 3 |
| SBOM application IDs | Exactly `byo-analysis:cppcheck` and `byo-analysis:clangd` |

License/dependency arrays are explicit and unique, including under case folding.
`[]` means no bundled non-OS shared libraries, not absence of native imports.
Cppcheck notices `analysis/cppcheck/licenses/{COPYING,simplecpp-LICENSE,
tinyxml2-LICENSE,picojson-LICENSE}` and clangd's `analysis/clangd/LICENSE.TXT`
remain mandatory. Additional notices stay under `analysis/<program>/licenses/`.
The accepted Linux static GCC runtime recipe also requires Cppcheck's
`GCC-COPYING3` and `GCC-COPYING.RUNTIME` there. Their actual compiler input
notice bytes and provenance must be recorded; no digest is manufactured.

Paths use `/`, exact case-sensitive inventory spelling and the shared portable
Windows-safe name subset on every host. Reject absolute/drive/UNC paths,
backslashes, empty/dot/parent components, reserved names, trailing dots/spaces,
case-fold collisions, symlinks/reparse points, hard-linked POSIX leaves and
special files. Inspect every ancestor and leaf without following links;
resolved paths stay within `R`. Unreferenced files and empty directories in a
selected program's namespace fail validation.

## Inventory, SBOM and native identity

`R/release-manifest.json.files` remains the sole digest inventory: each closed
entry has `path`, SHA-256, integer byte `size`, `kind` and boolean `executable`.
The release manifest's existing closed syntax and protocol versions are retained.
The mapping contains references, never a second executable/data digest list.
Hash final payloads, emit their SBOM components/relationships, then inventory
`sbom.cdx.json` itself. No SBOM self-hash.

| Payload | Kind / executable |
| --- | --- |
| `analysis/runtime.json` | `metadata` / false |
| `sbom.cdx.json` | `metadata` / false |
| Analyzer executable | `analysis-executable` / true |
| Cppcheck cfg/platform XML | `analysis-data` / false |
| clangd resources | `analysis-resource` / false |
| Bundled native private library | `analysis-dependency` / false |
| Notices | `license` / false |

Require exact selected namespace membership, classifications, sizes, hashes,
mapping references and SBOM ownership. `cfg/std.cfg`, platform XML and resource
`include/stddef.h` are required. A nonempty data directory or a version probe
does not prove upstream completeness. Release validation owns the locked
vendor selection, including clangd's native include/share resources.

Application name/version, license references and string provenance properties
`byo:analysis:upstream` and `byo:analysis:recipe` are required. Preserve other
explicitly recorded properties and static third-party/GCC runtime identities.
Reachable file components must cover every selected executable, resource, private
library and notice exactly; dangling/duplicate relationships or another
analyzer's file namespace fail. Any recorded SHA-256 must agree with the sole
release inventory. Missing compiler/static-input provenance blocks packaging
acceptance; the runtime does not synthesize or replace it.

Before probes, check native headers of the selected executable and all its
mapped private dependencies. Preserve Windows PE signature, AMD64 machine and
PE32+ checks. POSIX checks require complete Mach-O/ELF headers, matching CPU,
class/endian/type and bounded header/load-command tables; reject scripts,
Windows wrappers, wrong architecture, truncation and fat runtime Mach-O.
Release validation separately proves ordinary/delay imports, Mach-O load
commands/minimum OS/install names/private resolution, and ELF interpreter,
DT_NEEDED, RPATH/RUNPATH, imported symbol versions and recursive closure.
Only baseline OS libraries or inventoried private dependencies are allowed.

POSIX staging modes are 0755 for executables/directories and 0644 for data,
notices/private libraries, without set-ID bits. Installation normalizes to
owner-only 0700 executables/directories and 0600 data. Executables must retain
execute permission and be executable by the effective caller. Receipts bind
device/inode, full mode, owner/group, link count, size, nanosecond mtime/ctime,
and effective readable/executable access. Windows file identities/attributes
retain their existing semantics. Cache reuse rechecks those observations;
matching hashes do not repair lost permissions.

## Resolution and lifetime

Launcher verification/leases select `R`. The private pre-server-import
`AnalysisRuntimeContext` is the authority boundary; cwd, environment overrides
and handshake labels cannot select a managed runtime. Managed failures never
fall back to PATH. Explicit unmanaged source use retains its development PATH
behavior, runtime failure evidence and fallback warning.

Process-local receipts bind release-manifest, mapping, SBOM and the selected
namespace before execution. Recheck metadata, required directories and leaves
on reuse; directory changes invalidate the receipt and probes/sessions.
Expected and observed identities remain separate. No disk trust cache,
automatic repair or hidden retry is added. Payload/probe failures affect the
selected analyzer; mapping/target failures affect both.

Managed version probes and clangd sessions remove inherited `LD_*` and `DYLD_*`
loader injection/search overrides. `analysis_child_environment` applies only
to actual `origin: managed_runtime`; development environments remain inherited.
Explicit project/runtime context and query-driver arguments retain their meaning.
clangd always receives the absolute mapped `--resource-dir`. Cppcheck retains
empty FILESDIR, adjacent data and explicit firmware target-platform XML; the
host architecture is never the firmware ABI.

Hash/probe/spawn/query work shares the existing deadline/cancellation. Owned
version probes have a maximum five-second phase within that budget. Status
does not initialize LSP or indexing. All five tools remain board-independent
and available at every existing tier/profile, with unchanged public results.

Only analysis calls opt into `popen_owned(..., analysis_ownership=True)` on
POSIX. `kernel/_analysis_posix.py` is their single private ownership home;
Windows Jobs and hardware subprocess callers retain their existing behavior.
It observes leader exit with `waitid(WNOWAIT)`, preserving an unreaped exact
child/PGID anchor through normal exit, deadline, cancellation and setup failure.
Successful `Popen(start_new_session=True)` construction establishes the actual
direct child's session before `begin_ownership`; child setup failures raise through
Popen's error pipe. Fresh retained-child waitid observations establish leader
liveness/exit independently of live image or birth metadata. Missing OS birth
metadata remains missing in the receipt and never grants detached PID authority.
An exit before initial descendant observation is recorded as coverage information
(`early_direct_child_exit`), and alone does not invalidate scoped proof.
Darwin's narrow libc bridge follows the public
[wait definitions](https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/bsd/sys/wait.h)
and [siginfo layout](https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/bsd/sys/signal.h);
its actual ABI execution remains a matching-host Actions gate.
An observer captures group members and descendants with PID/birth identities;
children-list results are only candidate PIDs. Descendant acquisition requires
a fresh birth/PPID chain to an exact already-owned parent, with every edge and
birth identity rechecked before capture; stale parent maps grant no ownership.
detached children are signaled only after fresh identity checks. Group signals
require the retained exact anchor. Live/unknown children prevent confirmation;
zombies are distinguished from live children. A bounded cleanup receipt records
captured states, leader exit/reap, uncertainties and the existing three-grace
reserve. Unconfirmed cleanup retains its marker and evidence.
An internal observation phase that runs out of time records an incomplete
snapshot and can retry within that same reserve. Fully validated identities
acquired before interruption remain captured. The receipt records an incomplete
observation count and the last incomplete operation; these interim deadlines
do not replace final proof. A complete final snapshot and known terminal states
are required before confirmation. Actual inaccessible or unknown identities,
observation errors and signal permission failures remain persistent uncertainties.
Darwin's `getpgid`/`getsid` use a live-only kernel lookup. The successful new-session
spawn and fresh exact retained-child proof keep the leader anchor usable after
exit without querying its unavailable live image. Unknown retained-child
observation remains unconfirmed. Darwin group discovery uses kernel-filtered
`proc_listpids(PROC_PGRP_ONLY)` membership, including zombies, with bounded
buffer growth and rejection of errors/truncation. It avoids unnecessary group
signals only after retained exit proof and two equal complete native membership
snapshots with fresh matching zombie birth identities for other members and
authoritative retained-child exit proof for the leader. Captured detached children
still require their own cleanup. Signal permission failures, live/unknown native
membership and lost observation coverage keep cleanup unconfirmed. The receipt
records the terminal native membership proof. Failed cleanup logs its private
receipt, including the first observation failure, for matching-host Actions diagnosis.
It also records the last eight group signal operations, selected signals, results
and errno values; no signal error alone establishes terminal group membership.
If Darwin's group signal returns `EPERM` after the pre-signal check, cleanup
retries exact retained-child exit and complete terminal membership within the
existing signal phase and total three-grace deadline. Only positive proof can
classify the race between the check and signal as terminal. The diagnostic keeps
the original failed `killpg`/errno
and a separate terminal-proof operation. Prior observation failures remain
failures; Linux/Windows permission errors receive no such classification.
An unsuccessful immediate check can wait for the healthy child to become
terminal within that phase. A reaped terminal member can shrink the group:
reject the unstable snapshot, freshly prove each removed exact identity nonlive
or absent, and require a new complete stable membership pair within the same
deadline. The proof snapshots acquired identities under the bounded ownership
guard and reuses an unambiguous exact captured token if a listed terminal member
disappears before its birth metadata can be read. Fresh absence and the stable
remaining pair are still required; a first-time missing birth is not accepted.
Unknown or ambiguous identities, unsafe additions or reuse, denied or unavailable
membership, prior errors and exhaustion remain failures. No unrelated uncertainty
is cleared, and the phase is not extended by either retry.
The receipt names `coverage: retained_group_and_captured_descendants` and
`scope_confirmed`. Its `confirmed` compatibility alias means only that same scope.
Probe and clangd consumers require the named scope and successful retirement/reap;
the compatibility boolean alone is insufficient. Unobserved detached descendants
remain `not_proven`, including after scoped success. Version probes retain a
private copy of their cleanup receipt on success/failure and in cached results;
clangd retains its private last receipt before clearing ownership, including EOF,
shutdown and failed cleanup. Transport threads must still complete before clangd
clears its process. These receipts add no public tool result fields.

A polling observer cannot establish ownership of an unobserved child that
detaches/reparents before observation. The committed ROOT decision in installer
`docs/code-analysis-posix-lifecycle-scope.md` at
`65a0ef8777bd6e2ab799b2e43685d2ba7d0b76f6`
(accepted addendum SHA-256
`539ee677c0a3249ab5ada75db43bd96faeb99c76a985c7c98b416beda721f222`)
defines this scope for trusted analyzers and explicitly configured query drivers
under the caller's existing permissions. It does not promise universal containment.
Unknown observed ownership, a failure preventing proof of the original group or
captured set, failed exact wait/reap, and inaccessible/truncated terminal membership
remain unconfirmed. Independent native escape controls must retain `not_proven`;
they cannot turn scoped retirement into proof that every possible descendant is gone.

Recovery markers retain their schema-2 native birth identities. Missing birth
metadata grants no detached PID or recovery signal authority. Analysis spawn first
attempts the normal marker; a live/unknown child without recoverable identity fails
setup honestly. An already-terminal exact direct child may return without a marker
only after the covered group and captured set are proven retired and the child is
reaped within the existing cleanup reserve. Its receipt remains available. Generic
hardware marker behavior and Windows Jobs/records are unchanged. For unconfirmed
cleanup, retain the process/evidence and any valid marker, recover only identities
that still match, then restart the server before another analysis child is started.
Timely and abandoned POSIX analysis setup exceptions also inspect their actual
cleanup receipts before releasing spawn admission. Unconfirmed scoped cleanup
latches the existing restart/refusal state and retains a private receipt copy.
Pre-spawn failures without an actual receipt and positively retired setup failures
remain retryable; Windows behavior is unchanged.

Resolution failures keep `analysis/executable-unavailable`, offending path/cause
and restore/select-matching-runtime remedy. Cppcheck is blocked and full verify
fails; clangd is unavailable. Missing required data/notices/dependencies,
corruption, wrong target/type, links, lost access/modes and in-place changes
cannot become success or managed PATH substitution.

## Pending independent GitHub Actions gates

ROOT owns remote execution on exact integrated commits. Local source-only
lint/format/AST inspection establishes no native acceptance. Tester-owned
portable fixtures must execute on all four hosts; Windows-only skips are not
macOS/Linux proof. Required gates include:

1. Closed JSON/target/header/dependency/namespace/SBOM/mode negatives and Windows
   process/hardware regressions, using the independent server tests.
2. Matching native build and pinned Cppcheck 2.22.0 / clangd 23.1.0 staging,
   final dependency/OS-baseline checks, real version/data probes, archive modes
   and installed permissions. GCC compiler/static library/notices provenance,
   Linux clangd RUNPATH removal and macOS native thinning remain build gates.
3. Both runners' authoritative ARM clean/defect/restored cases and all five MCP
   tools, saved-file freshness, query-driver/resource consumption, poisoned PATH
   and loader environment, no-board/tier/profile behavior and natural EOF.
4. Descendants surviving leader exit, observed detachment, startup/fast-detach
   races, PID reuse, timeout/cancellation/setup failure/idle shutdown, bounded
   honest cleanup receipts and an unaffected unrelated peer; update/uninstall
   and prior runtime lease preservation.

In ROOT's provisioned matching GitHub checkout/environment, execute
`uv run --locked python -m unittest discover -s tests -p 'test_code_analysis_*.py'`
and focused clangd/process cleanup tests through the reserved portability
workflow. Native analyzer fixtures require the tester's measured inputs.
Integrated build/archive/install/core-smoke commands and required hash-bound
ARM compiler fixture are defined in the accepted portability contract. Missing
prerequisites, required skips and unexecuted checks remain pending.
