# Windows analysis runtime mapping

Revision 1, 2026-10-08; contract candidate for ROOT's independent critique.
This supplies the physical mapping missing from the accepted
[analysis contract](code-analysis-contract.md) and
[ownership plan](code-analysis-implementation-plan.md). It changes neither
`byo-analysis.json` schema version 1 nor verify/MCP command syntax. Acceptance
for this run is Windows x86_64; preserve other platform implementations and run
no Linux, macOS or Docker checks. This document establishes no installed or
shipping acceptance and authorizes no publication.

## One mapping, one digest inventory

Use **`R/analysis/runtime.json`**, where `R` is the canonical active immutable
runtime. Its exact closed syntax is
[byo-analysis-runtime.schema.json](contracts/byo-analysis-runtime.schema.json).
Do not add a top-level analysis field to `release-manifest.json`.
Frozen Rust `ReleaseManifest` and its nested records deny unknown fields;
`ReleaseFile.kind` is an unrestricted string in both Rust and the existing
release schema. An inventoried metadata file therefore fits those interfaces
without a release-manifest extension or schema relaxation.

`R/release-manifest.json.files` remains the sole payload inventory: every leaf
has its existing `path`, SHA-256, byte `size`, `kind` and `executable` fields.
The mapping contains references, not another digest list. The existing
`R/sbom.cdx.json` owns component provenance, licenses and dependency relationships;
any SBOM file hashes are generated from `files`, never independent resolver input.
The existing builder must deliberately stage and classify analysis payloads before
SBOM/inventory generation; its current default classification is insufficient.
Compute payload digests once, emit their SBOM representation, then inventory the
SBOM itself; do not create a circular SBOM self-hash.

All mapping fields are required, with **no implicit defaults**. Reject duplicate
JSON keys, unknown fields, wrong types, unsupported versions and nulls. JSON
integer `schema_version` is exactly `1`; `platform` is `"windows"` and
`architecture` is `"x86_64"`, matching the enclosing release and actual host.
`programs` has exactly the IDs `cppcheck` and `clangd`. Each `version` is a
three-part numeric string, checked against the owned version probe.
Typed decoding must reject boolean/float coercion of `schema_version`; JSON
Schema's integer numeric equivalence alone cannot reject a `1.0` token.
An incompatible mapping change requires a new mapping schema version, leaving
project configuration version 1 and older runtime bytes intact.

| Field / payload | Exact runtime-relative location and meaning |
| --- | --- |
| `cppcheck.executable` | `analysis/cppcheck/cppcheck.exe` |
| `cppcheck.cfg_dir` | `analysis/cppcheck/cfg`; vendor library models, including `std.cfg` |
| `cppcheck.platforms_dir` | `analysis/cppcheck/platforms`; vendor platform XML data |
| `clangd.executable` | `analysis/clangd/bin/clangd.exe` |
| `clangd.resource_dir` | `analysis/clangd/lib/clang/<major>`; `<major>` equals the first component of `clangd.version`; required vendor headers are underneath `include/` |
| `licenses` | Explicit unique list of license file paths; Cppcheck's four recorded notices and clangd's `analysis/clangd/LICENSE.TXT` are mandatory; additional dependency notices stay in the program's license namespace |
| `dependencies` | Explicit unique list of bundled DLL paths adjacent to that program's executable; `[]` means no bundled non-OS DLLs, not absence of Windows imports |
| `sbom_ref` | Existing SBOM application component ID, exactly `byo-analysis:cppcheck` or `byo-analysis:clangd` |

Paths use `/`, are case-sensitive inventory spellings and resolve only from `R`.
Reject absolute/drive/UNC paths, backslashes, empty/dot/parent components, invalid
Windows names, case collisions and symlink/reparse traversal. Canonical resolved
files and data directories must remain beneath `R`. These checks catch a wrong
or escaped payload; schema validation alone cannot establish filesystem identity.

Complete mapping example for the recorded **development candidates**:

```json
{
  "schema_version": 1,
  "platform": "windows",
  "architecture": "x86_64",
  "programs": {
    "cppcheck": {
      "version": "2.22.0",
      "executable": "analysis/cppcheck/cppcheck.exe",
      "cfg_dir": "analysis/cppcheck/cfg",
      "platforms_dir": "analysis/cppcheck/platforms",
      "licenses": [
        "analysis/cppcheck/licenses/COPYING",
        "analysis/cppcheck/licenses/simplecpp-LICENSE",
        "analysis/cppcheck/licenses/tinyxml2-LICENSE",
        "analysis/cppcheck/licenses/picojson-LICENSE"
      ],
      "dependencies": [],
      "sbom_ref": "byo-analysis:cppcheck"
    },
    "clangd": {
      "version": "23.1.0",
      "executable": "analysis/clangd/bin/clangd.exe",
      "resource_dir": "analysis/clangd/lib/clang/23",
      "licenses": ["analysis/clangd/LICENSE.TXT"],
      "dependencies": [],
      "sbom_ref": "byo-analysis:clangd"
    }
  }
}
```

This is a complete valid mapping object, not a fabricated release inventory.
Runtime validity additionally requires the inventory/SBOM/files below. Known
pre-packaging executable identities binding this example are:

| Candidate | Observed executable SHA-256 |
| --- | --- |
| Cppcheck 2.22.0 | `bd85657e81f80597c63f4380c2e5f91f6a840e0b12cce159df9812e76bb147cb` |
| clangd 23.1.0 | `dbd52c13d21ef9d284f4f0627efe76c81adf2fe0998f230127f554a54fc9dde7` |

Their shipped `files` entries must contain the actual final-byte hashes/sizes.
Signing, stripping or rebuilding changes that identity and requires new affected
exact-byte tests. No unrecorded data or clangd archive digests are invented here.

## Payload and provenance handoff

Stage only accepted vendor data: Cppcheck's source `cfg/*.cfg` and
`platforms/*.xml` sets, preserving names; clangd's required vendor
`lib/clang/23/include/` tree, preserving relative layout. The packager compares
these sets and bytes with the exact accepted upstream inputs before generating
`files`; a nonempty directory alone does not prove completeness. Cppcheck's
`COPYING` and `externals/{simplecpp,tinyxml2,picojson}/LICENSE` become the four
license paths shown above. Preserve notice bytes; review additional attribution
and distribution/source obligations against the final artifacts. These observed
notices are not a legal-compliance conclusion.

The recorded Cppcheck source commit is
`a436ca35ed1887bee789765122b65ed2d7a7e045`; archive SHA-256 is
`68ed9efb7aad635b7f4c121662689b2377d1d745dc9e76227566516a9617f343`.
That hash was observed on an official codeload download, not independently
published. The recipe uses Visual Studio 17 2022/x64, Release, static MSVC
`MultiThreaded`, empty `FILESDIR`, no core/shared DLL, GUI, tests, Boost or
matchcompiler, and embeds UTF-8 `activeCodePage` at link. Its patch, options and
manifest digests and exact argv are in the verified ROOT recipe record below.
ROOT must transfer the actual matching patch/manifest bytes and recipe inputs;
the packager records compiler/CMake/linker versions and portable input paths.
Use deterministic input selection and staging; recipe identity alone does not
prove bit-reproducible output. Never copy build output addons or source wholesale.

For each application component, require matching name/version, license references
and SBOM relationships to every delivered executable, data file, DLL and notice.
Use SBOM `properties` string values `byo:analysis:upstream` (origin URL/reference,
artifact revision/hash and observed/published hash basis) and
`byo:analysis:recipe` (exact repack/build procedure, input references/digests,
patch/UTF-8 manifest, toolchain versions and byte transformations). File components
identify runtime paths and their upstream relative paths. Record static linked
third-party components as well as the ordinary/delay-import dependency closure
and tested Windows OS build. Missing exact clangd distribution digest, dependency
evidence or source/build inputs blocks packaging input acceptance. The input's
OS-only dependency observation does not establish every Windows version supported.

## Resolution, integrity and lifetime

1. The Rust launcher obtains `R` through existing `load_current`/`active_runtime`:
   current.json's manifest hash, version-directory containment, existing
   signature/development policy and full `verify_runtime` inventory checks remain
   authoritative. Retain the existing runtime lease. No installer-directory or
   cwd guessing. Load and validate the mapping from those already verified bytes;
   do not hash the whole runtime again in the Rust static step.
2. The private sidecar receives the launcher's canonical project/runtime roots.
   Its existing pre-server-import `AnalysisRuntimeContext(..., managed=True)`
   remains the authority boundary. Its current protocol checks are **not** payload
   digest verification. The Python resolver binds the release-manifest byte hash,
   validates mapping/SBOM relationships and hashes the mapping, SBOM and selected
   program's inventoried payload once before any executable probe/spawn. It need
   not rehash unrelated sidecar/workflow files already checked by the launcher.
   A standalone trusted development caller supplying `R` uses the same checks.
3. Validate closed syntax, target/version agreement, exact file references and
   all inventoried data members, nonempty required data directories, `cfg/std.cfg`
   and resource `include/stddef.h`; upstream set completeness belongs to the
   release validator, not a second runtime list. All referenced leaves must be
   readable regular files with matching inventory size/hash. The program's
   namespace must contain precisely its approved inventory payload. Record required directory
   and leaf identities/size/mtime with the validated manifest/mapping hashes.
   Cache per process and immutable runtime identity; compare those metadata
   identities on subsequent resolution, re-enumerating changed directories.
   Changed/missing files invalidate the receipt and version cache; revalidation
   happens only on a later independent call. No disk trust cache or repair loop.
4. Owned `--version` probes consume at most 5 seconds **within** the caller's
   remaining deadline, including spawn; cache only by validated program identity.
   Compare the reported numeric version with the mapping and retain raw output.
   A matching version does not prove analysis compatibility. Mapping work,
   hashing, lock waits, probe and query/static execution share the existing one
   deadline and cancellation; cleanup uses its existing bounded grace. Caller
   expiry is `incomplete` for MCP or `execution_error` for static verification,
   not permanent executable-failure evidence. Status never initializes LSP.
5. Cppcheck executes the mapped absolute executable with project cwd; this
   explicitly contracted adjacent cfg/platforms layout serves its empty-FILESDIR
   candidate. Library models are available, not automatically selected or proof
   of vendor support. Keep the explicit project target XML requirement. The
   clangd adapter passes `--resource-dir=<canonical mapped resource_dir>` alongside
   its existing database/driver arguments; installed proof must verify that data
   is actually used. No executable, runtime, command or flag override enters MCP.

Python source verification integrates at the existing workspace
`resolve_tool_path` seam, with focused analysis resolution before its development
fallback; it cannot retain that helper's unlabelled managed PATH behavior.
Rust packaged verification uses its active `Runtime`, with no hidden workspace
Python. Server binding stays in `code_analysis_runtime.py`, not tool handlers.
In managed mode, failure never falls back to PATH. Only an explicitly unmanaged
source/development launch may use PATH after an unusable supplied runtime, with
the runtime problem and fallback warning retained. Unrelated helper callers stay
unchanged; Windows source lookup must not implicitly search cwd or accept shell
wrappers as native analyzers.

Doctor, status and report evidence retain existing origin `managed_runtime` or
`path`, executable/version and configuration problems. Add `runtime_root`,
`runtime_manifest_sha256`, `analysis_mapping_sha256`, `executable_sha256` and
`data_paths` (canonical required directories); unresolved values are null.
Keep expected/observed identities distinct on failure. Updates choose a new
immutable runtime: running leased services keep their launch context; a fresh
launch selects the update. Detected in-place changes close old-session admission,
cancel/drain affected work with `analysis/configuration-changed`, and invalidate
diagnostics; never return an old successful result for new runtime bytes.

| Concrete invalid example | Outcome and recovery |
| --- | --- |
| Add `programs.clangd.command`, omit `cfg_dir`, or set `schema_version` to `true` | Syntax rejected with field name; regenerate a supported mapping. |
| Delete `analysis/runtime.json` or omit a program | Unavailable/blocked; restore the complete supported runtime. Older releases do not acquire support by PATH substitution. |
| Set `resource_dir` to `analysis/clangd/lib/clang/22` with version 23.1.0, report a different probe version, or use an ARM64 release/PE | Incompatible; select/package matching validated Windows x86_64 bytes. The resource mismatch needs semantic validation beyond schema. |
| Replace a referenced path with `../clangd.exe`, `C:/tools/clangd.exe`, or an escaping junction | Reject path/containment; restore the prescribed runtime layout. |
| Correct executable, but missing `cfg/std.cfg`, vendor platform XML, `include/stddef.h`, required notice, DLL, or its `files` entry | Incomplete payload; restore the accepted vendor set and inventory. A passing version probe cannot repair it. |
| Change executable/data bytes without matching the accepted inventory | Integrity failure; restore/reinstall the supported runtime, then relaunch. |

Runtime-resolution failures use existing `analysis/executable-unavailable` with
program, offending field/path, cause and remedy. Cppcheck is `blocked`, full verify
is FAIL; clangd is `unavailable`. Preserve discovery of all five tools. Existing
launcher integrity gates may refuse a corrupt runtime before server startup;
doctor must explain that refusal. Mapping syntax/target errors affect both
programs; payload/probe errors affect the selected program. No failure/skip
becomes full verify PASS.

## Ordered implementation and Windows acceptance

ROOT first accepts independent contract critique, exact upstream/build inputs,
parity corpus and native repository contexts. The existing packaging draft cannot
launch on this document alone; do not retarget the live harness.

| Slice / sole owner | Handoff and gate |
| --- | --- |
| Python Cppcheck owner, authored workspace | Parser/static step and visible resolution via `internal/verify_backend.py`, focused analysis helper and existing `internal/agent_workspace.py` seam; ROOT must allocate `internal/agent_workspace.py` and confirm the proposed helper path before editing. Deliver target/library setup and common fixture identities to Rust owner. |
| Rust Cppcheck owner, installer | `launcher/src/workflow.rs`, focused `code_analysis.rs`, parity corpus; owns `main.rs` module registration through stage acceptance. Same required static analysis after normal/local builds. |
| Server managed binding owner, MCP | After separate frozen-candidate review/testing, ROOT supplies an accepted integration base for `code_analysis_runtime.py` and necessary adapter/sidecar changes. Do not edit frozen review bytes in place. |
| Installer packaging owner | After accepted inputs and explicit `main.rs` transfer: existing builder, SBOM/inventory, doctor/status and archive validator. ROOT allocates `verify_build_output.py`, any installer schema copy, and any `manifest.rs` helper edits explicitly; no hidden ReleaseManifest changes. Packaging never takes `workflow.rs` ownership. |
| Independent installed tester and reviewer | Separate native Windows contexts, exact combined commit/archive/digest/fixture handoff; their existing reserved files and read-only reviewer roles remain. Findings return to each production owner. |

Use `kind=metadata` for the mapping; `analysis-executable`/true for both EXEs,
`analysis-data`/false for cfg/platforms, `analysis-resource`/false for resource
headers, `analysis-dependency`/false for DLLs, and `license`/false for notices.
Inventory and SBOM cover **every** delivered analysis leaf. Extend Windows PE
machine checks to both analysis EXEs and all bundled native dependencies:
AMD64 `0x8664`, PE32+; check ordinary/delay-import closure against the recorded
tested-OS dependencies and inventoried DLLs, with no PATH DLL dependency.

The archive validator's forbidden-source rule gets exactly one narrow exception:
selected vendor `.h` resource headers under the mapped `resource_dir/include/`,
with `analysis-resource` inventory entries, clangd SBOM ownership and byte/path
agreement against the accepted digest-pinned vendor distribution. A prefix or
suffix alone never grants admission. Keep arbitrary source, addons, build output,
project caches/reports (including `.firm` and `.cache`) and unlisted payloads rejected; inspect the archive's actual
bytes against the inventory. Preserve existing leak/signature checks. Any required
additional vendor resource format needs explicit input review, not a blanket source
exception. Preserve other-platform packaging without claiming validation.

Installed acceptance requires Windows x86_64 with clangd/Cppcheck absent from PATH
and a relocated runtime/project using spaces and Unicode:
target-aware, genuinely built ARM clean/defect/restore results **0/1/0**, Python/Rust
parity including local override/header scope, missing-data/corruption/version
blockers; real overloaded/template definitions, cross-file references/hover,
non-BMP/CRLF positions, fresh saved-file and explicit empty diagnostics; actual
MCP stdio EOF/idle shutdown leaving no owned child or waiter. Doctor and reports
must identify bundled data/bytes. Bind results to final archive/runtime hashes,
provenance, actual argv/exit/logs and tested OS. Update/uninstall preserves
project `byo-analysis.json`, `.clangd` and user files. Installed failures or skips
are incomplete gates; current source/development tests are not installed proof.
Required migration/release notes and independent review remain acceptance gates.

## Frozen evidence and documentation checks

Inputs: installer `258dd15a8d82705cae442566bce4db21064ea269`, authored workspace
`94fc8eca4a8900287ad2766c8cbcb37a13bce12d`, server/base
`e0f8f4a9bfd02a965f61bc4c5b79c4ac27b866d6`. The literal context manifest is
`C:\Users\Jason\Documents\Jason\FirmCLI_Sentry\harness\harness-single\local-config\cppcheck-clangd-20261008T174059Z\context\windows-runtime-contract-20261008T2247Z\manifest.json`,
SHA-256 `5fbbd86b82dff26a0295fa985df7d43dd6ea78670f003301cdc607bd0edf0d74`.
All 12 snapshot SHA-256 and Git blob hashes were checked before use. The literal
ROOT recipe is
`C:\Users\Jason\Documents\Jason\FirmCLI_Sentry\harness\harness-single\local-config\cppcheck-clangd-20261008T174059Z\cppcheck-source-build-candidate.json`,
SHA-256 `c02629e1ede8ed92eec7354a5326e58728566012b71dbb03558a3f8d3200bfcb`.
It records relocated Unicode/actually built ARM 0/1/0 development evidence;
this lane did not rerun it or inspect its referenced build directories.
Frozen installer `release/source-lock.json` still pins MCP
`595075fc8004e1d4f1282541888447e803ab07ee`. That pin is not acceptance of this run;
ROOT must supply accepted integrated source pins before packaging.

Lane-local `.agent-workspace/contract-evidence/` retains exact input paths/hashes,
documentation-check commands/results and before/after Git blobs. Checks are JSON
parsing, existing-tool schema/example validation and frozen-source compatibility
inspection only. No runtime test, build, download or installed proof is claimed.
`python .agent-workspace/contract-evidence/check_contract.py` exited 0 using
existing jsonschema 4.26.0: schema and one complete example validated, 31 negative
schema examples and two strict-JSON cases rejected, and frozen compatibility
assertions passed. Filesystem/probe/installed semantic gates remain unrun.
