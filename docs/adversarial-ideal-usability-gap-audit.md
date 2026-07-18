# Adversarial ideal-usability audit

Audit date: 2026-07-17

The first adversarial pass returned non-green after inspecting code, live MCP
contracts, tests, and both autonomous nRF52840 acceptance artifacts.

## Valid gaps

### AI-G01 — automatic safety setup did not perform two-source reconciliation

`_build_automatic_catalog_safety` assigned official-document and
device-support provenance to the same catalog constants and wrote them without
calling `reconcile_hardware_evidence`. This preserved caller-range exclusion
but did not prove independent source agreement.

Required result: load two pinned, hashed evidence documents with distinct
roles; verify the installed device-support source identity; parse both through
the strict `HardwareEvidence` schema; require accepted reconciliation; and
persist only reconciled regions plus both source documents/hashes.

### AI-G02 — empty datasheet allowlists failed open

Catalog entries for nRF52833 DK and Nucleo-L476RG had no reviewed document
hashes. The validator treated an empty allowlist as “accept any digest.” Empty
must instead mean unavailable/unreviewed and first-time automatic setup must
remain closed. Only boards with complete reviewed document and device-support
evidence may be advertised as automatically reviewed.

### AI-G03 — acceptance evidence was insufficiently linked

Autonomous evidence recorded results and hashes but omitted the initial
supervisor prompt, exact MCP request/response timeline, server/report IDs,
immutable report hashes, versions, and an explicit cross-check against server
artifacts. A repeat acceptance must retain those records and a validator must
reject missing or inconsistent links.

### AI-G04 — canonical prompt specification drifted from the live surface

`Plan_Prompt_Contents_Spec.md` retained an obsolete setup action schema and did
not describe `serial_exchange`. The canonical document, declarative definitions,
live schema, and tests must agree exactly.

### AI-G05 — Windows-safe Zephyr build guidance was not discoverable in MCP

The installed `pyocd-zephyr-build` helper already uses short scratch paths, but
an isolated agent was not directed to it after setup readiness. Readiness must
return non-authoritative board-target/build guidance and explicitly recommend
the helper on Windows/long paths. This guidance must never become safety
authority.

### AI-G06 — UART stop-on-failure lacked a direct regression assertion

The implementation stops correctly, but tests must prove no later payload is
written after readiness, first-step, or middle-step mismatch and that the UART
handle closes.

### AI-G07 — package-level Nordic parts did not auto-resolve built-in targets

The fresh isolated acceptance supplied the required exact `nRF52840-QIAA`, but
target discovery compared that package spelling to `nrf52840` only by exact
normalized equality before a profile existed. Setup therefore requested manual
target research even though the reviewed board catalog and built-in target
already established one unambiguous route. Fresh setup must use the exact
reviewed catalog board/package mapping before falling back to research, while
rejecting unreviewed suffixes.

### AI-G08 — public acceptance evidence could not identify a Server Run

The in-memory `ServerRun` had a unique ID and absolute start time, but neither
was exposed through the public MCP surface. An autonomous client therefore
could not bind plans, reports, hardware observations, and transport restarts to
the server process without inspecting internals or inventing evidence.

Required result: the initialization handshake exposes the non-authorizing
`run_id` and `started_at`, tests prove restart uniqueness and side-effect-free
disclosure, and strict evidence validation rejects a missing run identity.

### AI-G09 — returned Zephyr build command depended on ambient PATH

Readiness recommended the console-script spelling `pyocd-zephyr-build`, but a
fresh agent process did not have the server virtual environment's Scripts
directory on `PATH`. The helper existed and worked only after the agent found
its implementation environment itself.

Required result: return an exact command through the running server Python
(`python -m pyocd_debug_mcp.zephyr_build`) so the guidance is directly
executable without PATH discovery, while retaining its advisory-only boundary.

### AI-G10 — UART validation status and readiness guidance appeared contradictory

`board_validate` returned `validation_passed_uart_not_configured` even when
`get_setup_status` immediately resolved a stable UART and reported
`ready_for_uart_work=true`. The seven-result vocabulary uses “not configured”
to mean that no expected UART content assertion is in the profile, not that no
UART attachment exists, but the response did not explain that distinction.

Required result: preserve the specified vocabulary while explicitly explaining
that content validation was skipped and direct the client to the authoritative
current UART readiness field.

### AI-G11 — readiness probes could be sent before post-flash firmware was ready

The state-preserving UART exchange sent its one planned readiness probe
immediately after opening the port. On the real nRF52840 bridge, opening during
post-flash boot caused that probe to be lost even though the boot marker then
arrived; correctly, no command steps ran, but the workflow required avoidable
retry reasoning.

Required result: support an exact bounded pre-probe observation delay. During
that window unsolicited boot/prompt output may satisfy readiness; only if it
does not should the single planned probe be sent. Preserve stop-on-failure,
single-handle, exact-plan, and bounded-time behavior.

### AI-G12 — legacy synthetic safety maps could retain write authority

Maps written before independent reconciliation used provenance labels that
looked authoritative, and restart/refresh paths did not require the current
evidence schema. A stale workspace could therefore appear configuration-ready
without meeting the reviewed two-source boundary.

Required result: every authority-bearing load, refresh, setup-status, and gate
path must require the current reconciled source schema and provenance. Legacy
maps remain readable evidence but are closed for guarded work and return the
exact setup, safety-setup, and validation remedy.

### AI-G13 — erase geometry was not independently reconciled

The reviewed catalog supplied erase origin and size directly after memory
regions were reconciled. Because flash containment depends on erase sectors,
that geometry must have the same two-source agreement as the address map.

Required result: make erase geometry part of the strict evidence schema,
compare both independent sources, reject disagreement or omission, and persist
only the reconciled value.

### AI-G14 — serial-exchange nested plans were not exact-schema validated

The top-level plan was bound exactly, but malformed or oversized step objects
could be accepted and fail later. At the same time, the documented newline-only
readiness probe used an empty text field that generic validation rejected.

Required result: use one shared exact validator for both plan replacement and
execution, cap steps, enforce cross-field timing/probe constraints, accept the
documented empty-text plus line-ending probe, and preserve the active plan on
invalid replacement.

### AI-G15 — acceptance evidence relied too heavily on self-assertion

The first strict validator checked filenames and hashes but did not reconstruct
the causal links among handshake, transports, MCP requests, operations,
reports, committed profile/map sources, build artifacts, UART exchange, and
disconnect. A fabricated record could satisfy the shape without proving the
workflow.

Required result: parse and cross-link every timeline record, canonical argument
hash, run/transport/operation/report identity, hardware identity, current
authority schema, artifact hash, terminal UART result, and disconnect event.

### AI-G16 — Windows build guidance was not valid PowerShell syntax

An exact quoted Python path is not invoked by PowerShell unless prefixed with
the call operator. The returned command therefore still required shell-specific
repair by the agent.

Required result: return both a shell-neutral argv array and an executable
PowerShell rendering using `&`, and prove the returned help command executes in
a real PowerShell process.

### AI-G17 — persisted safety evidence was structurally checked, not replayed

A schema-v2-shaped manifest could claim agreement while changing its embedded
source documents or reconciled regions. Current safety authority must be
reproducible from server-owned, hash-pinned evidence and installed runtime
pins, not merely resemble the expected schema.

Required result: revalidate both pinned source files and runtime target/SVD
identities, rerun deterministic reconciliation, and require exact source,
geometry, provenance, and hardware-region equality at every authority load.

### AI-G18 — broad hardware/build regions granted excess authority

The reviewed nRF evidence described the entire peripheral aperture as writable,
including nonvolatile-control blocks, and broad application partitions were
marked executable. Build-derived RAM could also widen unknown space into
writable RAM.

Required result: expose only reviewed volatile peripheral subranges, mark
nonvolatile-control ranges prohibited, authorize breakpoints only in executable
loadable segments, and never let build artifacts create RAM authority.

### AI-G19 — UART success could be inferred from readiness output alone

An expected command marker appearing in readiness capture could make a failed
or zero-step conversation look matched. The readiness-delay boundary could also
send a probe before processing bytes already available at that instant.

Required result: success requires readiness plus every nonempty planned step,
and the implementation must read before deciding to probe at the delay
boundary.

### AI-G20 — build isolation could damage sources or reuse an incompatible SDK

The build helper did not reject a build directory equal to, or above, the
application directory. Its managed SDK cache was not version-keyed, and scratch
copies could include build/state trees.

Required result: reject destructive path relationships before any runtime
bootstrap or cleanup, key managed SDKs by required version, validate candidate
versions, and exclude generated/state trees from scratch copies.

### AI-G21 — plan examples and CPU register bounds drifted from contracts

The serial example used the wrong budget and too few actions, the setup example
used an unreviewed board, and CPU register writes had no width-specific upper
bound.

Required result: generate examples from task-accurate guidance and enforce
32/64/128-bit values according to the runtime register class.

### AI-G22 — acceptance evidence did not fully bind causality and product success

Transport-level success was insufficient: required operations could be absent
or return a product refusal, timestamps could be reordered, artifact aliases
could be duplicated, and recorded build times/paths were not fully bound to the
actual safety refresh and flash.

Required result: require the ordered setup barrier, safety rebuild/validation,
flash, five-step UART result, and final disconnect; bind exact semantic status,
run/board, arguments, file identity/times, and reject nonmonotonic or duplicate
evidence.

### AI-G23 — the promised checkout-local setup-only runner was absent

The automation specification required a bounded real-stdio setup barrier, but
the only complete orchestration lived in external acceptance workspaces. A new
user could not reproduce the setup-first milestone from this checkout alone.

Required result: ship a fixed setup-only runner with explicit identities,
one-attempt authorization, strict terminal-status handling, fixed-path evidence,
and no command, callback, build, flash, UART-write, or code-generation surface.

### AI-G24 — runner identity and evidence boundaries were incomplete

The first runner version recorded requested UART facts without binding them to
setup/readiness, and its transcript retained the structured permission field.

Required result: bind stable UART USB identity plus current port in the immutable
setup plan, expose the live resolved tuple at readiness, compare it exactly, and
recursively omit permission/approval/authorization/grant fields from evidence.

### AI-G25 — zero-match hardware identity fell back to another device

An explicit probe with no inventory match restored the full probe list, while a
mutable COM port could satisfy the initial UART identity check.

Required result: preserve empty filtered probe results, require UART identity to
match a nonempty USB serial rather than a port label, expose the resolved live
probe, and bind both live identities in the runner's final barrier.

### AI-G26 — build bootstrap ignored complete local vendor tooling

The helper created a network-dependent private Python environment before it
looked for an installed NCS toolchain, preferred a partial managed cache, and
delegated managed setup to host `wget`/7-Zip. A fresh offline user could fail
despite having everything needed under a normal vendor install location.

Required result: discover and execute-validate compatible local
workspace/SDK/Python tooling first, reject incomplete caches, and keep a
checksum-verified Python-owned managed fallback with no ambient archive tools.

### AI-G27 — managed dependency cache ownership and retries were ambiguous

An interrupted West initialization could not resume, foreign same-name cache
directories were not strongly distinguished from helper-owned state, explicit
bad paths could cause bootstrap side effects before refusal, and a downloaded
compiler was not executed before promotion.

Required result: bind managed workspaces to an owner marker, repo/ref, canonical
manifest hash, and exact in-root West manifest; resume only that owned state;
validate explicit paths before side effects; execute-probe compilers before use.

### AI-G28 — local-first guidance was Zephyr-specific and implicit

Agents were not explicitly directed to reuse other large installed vendor
dependencies such as STM32CubeIDE-provided STM32Cube/ThreadX before downloading
them. That creates avoidable latency, bandwidth, and brittle setup behavior.

Required result: teach bounded, validated local-first discovery in the
initialization handshake and every NULL plan guide for SDKs, RTOS trees,
toolchains, device packs, and large libraries, with network fetches only as a
disclosed fallback and no whole-drive crawl.

### AI-G29 — dependency caches could be mistaken for complete or shared unsafely

Interrupted workspace updates, partial SDK extraction, mutable shared Python
environments, and unvalidated vendor Python requirements could make a cache
look ready when it was not. Concurrent helpers could also mutate the same cache
without a process boundary.

Required result: use OS-released bounded locks, immutable requirement-
fingerprinted environments, staged SDK promotion, immutable workspace identity
plus post-update completion, exact board validation, and an offline full-
requirements health probe before reusing vendor Python.

### AI-G30 — canonical build outputs were not safely owned or serialized

A caller-selected nonempty output directory could be cleaned during stale-cache
or scratch promotion, and two helper processes could write the same output at
once. Invalid output requests were discovered only after runtime provisioning.

Required result: preflight before provisioning, adopt only empty output roots,
bind an atomic owner marker to the exact app and resolved output, preserve
foreign contents, serialize the full build/promotion boundary, and keep cleanup
inside that verified ownership boundary.

### AI-G31 — switching managed-workspace board targets repeated global update

A workspace globally updated for one board could rerun network-capable `west
update` merely because another already-present exact board target had not yet
been recorded in the completion document.

Required result: distinguish global update completion from per-board local
validation, and append an already-supported board target atomically without a
second update.

## Rejected criticism

Removing the datasheet question from setup is not accepted. The user explicitly
required the agent to ask for the board/MCU and authoritative datasheet during
fresh setup. The flow will retain that question while independently verifying
the exact reviewed bytes and failing closed for unreviewed boards.

The audit also correctly rejected claims that `action_batch` bypasses
authorization, that matching profiles should skip validation, or that
`serial_exchange` currently continues after a mismatch.

The audit does not require hostile-host defenses for a symlink or junction
manually planted inside an already helper-owned build tree. That is outside the
compliant-agent and ordinary firmware-development workflow, while rejecting all
toolchain-created links would reduce portability. Root links and junctions,
foreign output roots, and every recursive cleanup boundary remain protected.
