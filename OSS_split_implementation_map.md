# MCP Server OSS / Commercial Implementation Map

Status: normative migration specification.

This document maps the live implementation under
`MCP_Server/BYO-Firmware-MCP` to the feature policy in `OSS_structure.md`. It is
written so an implementation agent can perform the split without making new
product-placement decisions. If this document and an old architecture or plan
document disagree, this document controls the code migration and
`OSS_structure.md` controls the strategy.

## 1. Fixed product boundary

There will be two installable products:

| Product | Repository/package | Purpose |
|---|---|---|
| Open hardware server | Existing `pyocd-debug-mcp` distribution and `pyocd_debug_mcp` Python package | A complete, local, independently useful MCP server and Python HAL for one process operating explicitly selected targets. |
| Commercial infrastructure OS | New private `firmcli-sentry` distribution and `firmcli_sentry` Python package | Persistent policy, safety, coordination, approvals, organizational state, managed inventory, polished workflows, and multi-agent/multi-user/multi-rig operation. |

The only permitted product dependency is:

```text
firmcli_sentry -> pyocd_debug_mcp
```

`pyocd_debug_mcp` must never import `firmcli_sentry`, conditionally discover it,
or change its semantics when the commercial package is installed. The open
server must start and expose its complete static tool set with only the open
distribution installed.

The commercial product must call public HAL contracts. Its production
deployment must run an isolated open HAL worker over stdio for each claimed
board. It must not reach through the public boundary into pyOCD adapter
internals.

## 2. Meaning of the four migration actions

| Action | Required implementation |
|---|---|
| `OSS` | Keep the feature in `pyocd_debug_mcp`; remove imports of commercial concepts. |
| `CLOSED` | Move the feature to `firmcli_sentry`; it is absent from the open wheel and open repository. |
| `SPLIT` | Extract the named open feature into the stated open destination and move the named remainder to the stated private destination. Do not copy the whole old module into both products. |
| `REPLACE` | Do not migrate the current wrapper. Author the specified open wrapper over public HAL services; keep the existing guarded wrapper private. |

File layout does not decide feature placement. Shared source must be split even
when that requires moving functions or types. There are no approved
entanglement exceptions in the present codebase.

## 3. Target package layout

The open package must converge on this layout:

```text
pyocd_debug_mcp/
  contracts/       # board config, identity, geometry, requests, results, errors
  adapters/        # SWD/JTAG/UART providers and isolated provider workers
  runtime/         # operation, process, session, timeout, and cleanup mechanics
  discovery/       # local probes, UARTs, hooks, explicit remote endpoints
  artifacts/       # ELF/HEX/BIN inspection and artifact collection
  packs/           # pack bytes, validation, PDSC/SVD parsing, index repair
  onboarding/      # stateless target-support inspection and config generation
  services/        # connection, target, symbol, flash, memory, register, UART APIs
  mcp_tools/       # static direct wrappers over the services above
  server.py        # thin static MCP composition
```

The private package must converge on this layout:

```text
firmcli_sentry/
  hal_client/       # public HAL client and worker supervision
  firmstore/        # durable profiles, artifacts, reports, bindings, caches
  policy/           # safety maps, policy evaluation, enforcement, permissions
  guardrails/       # plans, gates, approvals, guarded action composition
  inventory/        # persistent fleet, assignment, remote endpoint registry
  setup/            # reviewed evidence, managed setup, validation, refresh
  monitor/          # monitoring, audit, telemetry, narratives, health
  mcp_tools/        # commercial workflow tools and guarded compatibility tools
  server.py         # dynamic commercial composition
```

Open code may operate several explicitly named connections in one process for
ordinary scripting and testing. It must not implement persistent pools, leases,
fair scheduling, cross-user ownership, cross-board workflow state, or durable
remote endpoint inventory.

## 4. Open MCP server contract

The open server exposes exactly this static tool surface after migration:

| Tool | Contract and boundary |
|---|---|
| `initialization_handshake` | Return open server version, static capabilities, and schema versions. No dynamic visibility or entitlement state. |
| `discover_hardware` | Return local probes, UARTs, hook results, current connections, and caller-supplied remote endpoints. Do not remember results across runs. |
| `get_discovery_hook_contract` | Return the open discovery-hook schema. |
| `refresh_discovery_hooks` | Execute explicitly configured open hooks within mechanical time and process limits. |
| `inspect_target_support` | Accept `mcu_part_number`, optional `pack_path`, and optional `pyocd_target`; return candidates and physical geometry without persistence or authority claims. |
| `onboard_target` | Accept display name, MCU part, optional pack/target/probe/UART inputs; return a portable `BoardConfig` plus observed identity and geometry. Do not save it. |
| `connect` | Accept `board_id`, exactly one inline `board_config` or `board_config_path`, optional `probe_selector`, and optional `uart_port`. No profile lookup or ambient override. |
| `connect_under_reset` | Same explicit configuration and selection contract as `connect`, using the physical under-reset mechanism. |
| `disconnect` | Release the named open connection and all owned local resources. |
| `get_board_info` | Return direct target, probe, connection, and physical geometry facts. |
| `get_state` | Return current physical execution state. |
| `halt`, `resume`, `step`, `reset_and_run`, `reset_and_halt` | Direct target-control primitives. |
| `read_cpu_register`, `read_execution_state` | Direct read primitives. |
| `write_cpu_register`, `set_execution_state`, `register_write` | Direct writes with type, width, alignment, and backend-capability validation only. |
| `find_symbol`, `read_memory_symbol`, `read_memory_address`, `write_memory` | Direct symbol and memory operations with mechanical validation only. |
| `set_breakpoint`, `remove_breakpoint` | Direct backend breakpoint operations with canonical address handling. |
| `flash_firmware` | Accept an explicit local artifact; validate file structure and physical compatibility, program it, and report/read back mechanical verification. No app/boot role or organizational allowlist. |
| `read_serial`, `write_serial`, `serial_exchange` | Direct explicit-port or connection-scoped UART operations. |
| `recover_target` | Execute an explicitly named recovery mechanism after `confirm_destructive=true`; it does not decide organizational permission. |
| `action_batch` | Execute a non-recursive batch against one named board with ordinary operation cancellation and timeout mechanics. |
| `wait` | Cancellable bounded wait. |
| `run_native_build` | Accept exact `argv`, `cwd`, environment additions, declared outputs, and timeout; execute without a shell. |
| `collect_build_artifacts` | Collect caller-selected build outputs without FirmStore persistence. |

There are no plan tools, approvals, policy refreshes, safety validation workflows,
managed setup workflows, dynamic hidden tools, monitor tools, persistent remote
probe tools, or FirmStore tools in the open server.

The paid product retains the existing guarded names and semantics during the
migration. Its guarded actions call the HAL only after the private evaluator
returns an allow decision.

## 5. Correctness versus policy

The open HAL owns mechanical correctness:

- request and schema validation;
- file existence and artifact structural parsing;
- address width, integer width, alignment, and backend capability checks;
- physical memory geometry, erase-sector mechanics, and programming algorithms;
- faithful execution of the exact requested operation;
- transport errors and direct read-back verification.

The commercial layer owns policy and organizational meaning:

- permitted and prohibited address ranges or partitions;
- whether an artifact is suitable for an asset, role, environment, or workflow;
- approvals, roles, authority, budgets, freshness, and state-dependent rules;
- persistent evidence and the organizational result of verification;
- cross-agent, cross-user, cross-board, rig, and time coordination.

Public policy request and decision schemas belong in the open contracts package
so customers can supply inputs and consume outputs. The evaluator, enforcement
mechanism, rule storage, and orchestration remain private.

## 6. Live source migration map

Every path below is relative to `MCP_Server/BYO-Firmware-MCP`.

### 6.1 Package root and adapters

| Current path | Action | Exact implementation |
|---|---|---|
| `src/pyocd_debug_mcp/__init__.py` | SPLIT | Rewrite as open package metadata and public HAL exports. Move commercial exports into `firmcli_sentry/__init__.py`. |
| `src/pyocd_debug_mcp/adapters/__init__.py` | OSS | Keep and export only public provider interfaces and implementations. |
| `src/pyocd_debug_mcp/adapters/provider_worker.py` | OSS | Keep isolated provider process client. |
| `src/pyocd_debug_mcp/adapters/provider_worker_runtime.py` | OSS | Keep isolated provider worker runtime. |
| `src/pyocd_debug_mcp/adapters/swd_interface.py` | OSS | Keep SWD/JTAG provider contract. |
| `src/pyocd_debug_mcp/adapters/swd_process.py` | OSS | Keep process-isolated SWD provider. |
| `src/pyocd_debug_mcp/adapters/swd_pyocd.py` | OSS | Keep pyOCD implementation. |
| `src/pyocd_debug_mcp/adapters/uart_interface.py` | OSS | Keep UART provider contract. |
| `src/pyocd_debug_mcp/adapters/uart_pyserial.py` | OSS | Keep pySerial implementation. |
| `src/pyocd_debug_mcp/artifact_collector.py` | OSS | Move to `artifacts/collector.py`. Remove `FIRMSTORE_DIRNAME` and the `.firm` destination prohibition; accept an explicit output root. |
| `src/pyocd_debug_mcp/board_config.py` | OSS | Move to `contracts/board_config.py`; keep the portable, serializable configuration format. |
| `src/pyocd_debug_mcp/discovery_failures.py` | OSS | Move to `discovery/failures.py`. |
| `src/pyocd_debug_mcp/discovery_hooks.py` | OSS | Move to `discovery/hooks.py`; hook roots are explicit configuration, not FirmStore paths. |
| `src/pyocd_debug_mcp/local_env.py` | OSS | Keep local environment loading used by open hardware adapters; it must not create paid authority. |
| `src/pyocd_debug_mcp/native_build.py` | OSS | Move to `artifacts/native_build.py`; require argv execution without a shell and explicit output declarations. |
| `src/pyocd_debug_mcp/pack_index_repair.py` | OSS | Move to `packs/index_repair.py`; retain the open CLI entry point. |
| `src/pyocd_debug_mcp/pack_provision.py` | SPLIT | Extract download/byte validation/index mechanics into `packs/byte_store.py` with an explicit root. Move FirmStore defaults, admission records, and managed retention to `firmcli_sentry/firmstore/packs.py`. |
| `src/pyocd_debug_mcp/probe_families.json` | OSS | Keep as open discovery data. |
| `src/pyocd_debug_mcp/probe_families.py` | OSS | Move to `discovery/probe_families.py`. |
| `src/pyocd_debug_mcp/probe_inventory.py` | OSS | Move to `discovery/probe_inventory.py`. |
| `src/pyocd_debug_mcp/serial_resolver.py` | OSS | Move to `discovery/serial_resolver.py`. |
| `src/pyocd_debug_mcp/target_errors.py` | OSS | Move to `contracts/errors.py` and retain physical/backend error translation only. |
| `src/pyocd_debug_mcp/timeouts.py` | SPLIT | Put physical execution defaults and timeout types in `runtime/timeouts.py`. Move plan-, setup-, finalizer-, approval-, and monitor-specific budgets to private owners. |

### 6.2 FirmStore and guardrails

| Current path | Action | Exact implementation |
|---|---|---|
| `src/pyocd_debug_mcp/firmstore/__init__.py` | CLOSED | Move to `firmcli_sentry/firmstore/__init__.py`. |
| `src/pyocd_debug_mcp/firmstore/cache.py` | CLOSED | Move unchanged in responsibility to private FirmStore. |
| `src/pyocd_debug_mcp/firmstore/profiles.py` | CLOSED | Move profiles and persistent board configuration to private FirmStore. |
| `src/pyocd_debug_mcp/firmstore/reports.py` | CLOSED | Move durable reports/evidence to private FirmStore. |
| `src/pyocd_debug_mcp/firmstore/store.py` | CLOSED | Move store layout, locks, persistence, and admission to private FirmStore. |
| `src/pyocd_debug_mcp/guardrails/__init__.py` | CLOSED | Create private guardrail exports; open request validators are exported from open contracts/runtime packages instead. |
| `src/pyocd_debug_mcp/guardrails/flash_gate.py` | SPLIT | Extract artifact path/type/structure and physical target compatibility checks into `artifacts/validation.py`. Move artifact-role, allowed-range, assignment, approval, and refusal logic to `firmcli_sentry/policy/flash.py`. Replace `PolicyRefusal` in open code with `HalRequestError`. |
| `src/pyocd_debug_mcp/guardrails/gate.py` | CLOSED | Move gates, approval state, authority, and guarded action decisions to `firmcli_sentry/guardrails/gate.py`. |
| `src/pyocd_debug_mcp/guardrails/permissions.py` | CLOSED | Move permission models/evaluation to `firmcli_sentry/policy/permissions.py`. |
| `src/pyocd_debug_mcp/guardrails/plan_defs.py` | CLOSED | Move all plan definitions to private guardrails. |
| `src/pyocd_debug_mcp/guardrails/plan_engine.py` | CLOSED | Move plan issuance, continuation, expiry, and consumption to private guardrails. |
| `src/pyocd_debug_mcp/guardrails/recover_gate.py` | SPLIT | Put explicit recovery-mechanism schema and destructive-confirmation validation in `contracts/recovery.py`; move permission, approval, assignment, and policy checks to `firmcli_sentry/policy/recovery.py`. |

### 6.3 Inventory, remote endpoints, and kernel

| Current path | Action | Exact implementation |
|---|---|---|
| `src/pyocd_debug_mcp/hardware_inventory.py` | SPLIT | Move identity comparison, `ProbeRow`, `UartRow`, `ActiveConnectionRow`, `VendorUartRow`, vendor parsing, snapshot merging/deduplication, `HardwareInventoryService`, and run-scoped probe/UART selection stores to `discovery/inventory.py`. Inject explicit remote endpoint rows. Move `snapshot_from_validation_inventory`, `validation_inventory_from`, durable assignments, and validation adapters to `firmcli_sentry/inventory/compat.py`. |
| `src/pyocd_debug_mcp/remote_probes.py` | SPLIT | Move `RemoteProbeError`, endpoint data type, `normalize_host`, `normalize_port`, and `check_endpoint` to `discovery/remote_endpoint.py`. Move load/save/upsert/remove, timestamps, locking, default paths, and the persistent registry to `firmcli_sentry/inventory/remote_probes.py`. |
| `src/pyocd_debug_mcp/kernel/__init__.py` | SPLIT | Rewrite open exports for runtime lifecycle only; create separate private kernel/guardrail exports. |
| `src/pyocd_debug_mcp/kernel/finalizers.py` | CLOSED | Move structured workflow finalizers and commercial response completion to private guardrails. |
| `src/pyocd_debug_mcp/kernel/hygiene.py` | OSS | Move process/filesystem hygiene primitives to `runtime/hygiene.py`. |
| `src/pyocd_debug_mcp/kernel/operations.py` | SPLIT | Move cancellation, operation/resource lifecycle, per-board locks, `ManagedOperation`, `OperationManager`, dispatch, and physical timeout mechanics to `runtime/operations.py`. Move `SAFE_EXIT_REMINDER`, tool-name policy, `wrap_layer2_response`, and commercial finalizer semantics to private guardrails. |
| `src/pyocd_debug_mcp/kernel/processes.py` | OSS | Move child-process lifecycle and cleanup to `runtime/processes.py`. |
| `src/pyocd_debug_mcp/kernel/registry.py` | CLOSED | Move dynamic tool visibility, locks, monitor wiring, guarded dispatch, and paid composition to the private server. The open server uses a static registry authored in `mcp_tools/__init__.py`. |
| `src/pyocd_debug_mcp/kernel/run_state.py` | CLOSED | Move commercial authority and workflow run state to private guardrails. Open runtime state is limited to live sessions and operations. |

### 6.4 Monitor

All monitor features are commercial. They provide persistent observation,
classification, audit, reliability, and organizational feedback rather than
physical I/O.

| Current path | Action | Exact implementation |
|---|---|---|
| `src/pyocd_debug_mcp/monitor/__init__.py` | CLOSED | Move to `firmcli_sentry/monitor/__init__.py`. |
| `src/pyocd_debug_mcp/monitor/block.py` | CLOSED | Move blocking/constraint behavior to private monitor. |
| `src/pyocd_debug_mcp/monitor/build_profile.py` | CLOSED | Move monitored build profiling to private monitor. |
| `src/pyocd_debug_mcp/monitor/classify.py` | CLOSED | Move classification to private monitor. |
| `src/pyocd_debug_mcp/monitor/counters.py` | CLOSED | Move persistent counters to private monitor. |
| `src/pyocd_debug_mcp/monitor/delivery.py` | CLOSED | Move delivery to private monitor. |
| `src/pyocd_debug_mcp/monitor/ledger.py` | CLOSED | Move ledger/audit persistence to private monitor. |
| `src/pyocd_debug_mcp/monitor/monitor.py` | CLOSED | Move monitoring coordinator to private monitor. |
| `src/pyocd_debug_mcp/monitor/narrative.py` | CLOSED | Move narrative generation to private monitor. |
| `src/pyocd_debug_mcp/monitor/paths.py` | CLOSED | Move monitor path ownership to private monitor. |
| `src/pyocd_debug_mcp/monitor/redaction.py` | CLOSED | Move monitor redaction policy to private monitor. |
| `src/pyocd_debug_mcp/monitor/reports.py` | CLOSED | Move monitor reports to private monitor. |
| `src/pyocd_debug_mcp/monitor/thrash.py` | CLOSED | Move cross-operation thrash analysis to private monitor. |
| `src/pyocd_debug_mcp/monitor/tools.py` | CLOSED | Move monitor MCP tools to the private tool package. |
| `src/pyocd_debug_mcp/monitor/trail.py` | CLOSED | Move durable operation trail to private monitor. |
| `src/pyocd_debug_mcp/monitor/transport.py` | CLOSED | Move telemetry transport to private monitor. |

### 6.5 Safety

| Current path | Action | Exact implementation |
|---|---|---|
| `src/pyocd_debug_mcp/safety/__init__.py` | CLOSED | Move safety-policy exports private. Export open geometry from `contracts/geometry.py`, not an open `safety` namespace. |
| `src/pyocd_debug_mcp/safety/enforce.py` | CLOSED | Move policy evaluation and enforcement private. |
| `src/pyocd_debug_mcp/safety/linker.py` | SPLIT | Extract ELF/HEX/BIN segments, symbols, and address allocation inspection into `artifacts/inspection.py`. Move allowed-region interpretation and safety conclusions to `firmcli_sentry/policy/artifact_linker.py`. |
| `src/pyocd_debug_mcp/safety/map_build.py` | SPLIT | Move `EraseSector`, `MapGeometry`, and `GenericMapGeometry` physical facts to `contracts/geometry.py`. Move identities, digests, partitions, safety contributions/regions, reviewed documents, allocation ownership, repositories, and safety-map builders to `firmcli_sentry/policy/map_build.py`. `MapPartitions` is private. |
| `src/pyocd_debug_mcp/safety/refresh.py` | CLOSED | Move managed refresh, authority, persistence, and freshness behavior private. |
| `src/pyocd_debug_mcp/safety/regions.py` | SPLIT | Move generic address ranges and physical geometry errors to `contracts/geometry.py`. Move `RegionKind`, `ActionCategory`, `SourceAuthority`, `Provenance`, `SafetyRegion`, `Allowed`, `Refusal`, `SafetyMap`, policy containment, and recovery disclosure private. |
| `src/pyocd_debug_mcp/safety/verify2.py` | CLOSED | Move safety verification and organizational result private. |

### 6.6 Services and setup flow

| Current path | Action | Exact implementation |
|---|---|---|
| `src/pyocd_debug_mcp/services/__init__.py` | SPLIT | Rewrite to export public HAL services only. Private workflows export from their own packages. |
| `src/pyocd_debug_mcp/services/connections.py` | OSS | Keep connection mechanics after replacing the current commercial `SessionRecord` dependency with open `HalSession`. |
| `src/pyocd_debug_mcp/services/session_runtime.py` | SPLIT | Move session ID/time helpers and minimal `HalSession(board_id, connection_id, created_at, recovery_used)` to `runtime/sessions.py`. Move `ToolOutcome`, `ToolEvent`, JSONL event/session paths, persistent summaries, policy refusal context, and action context to private monitor/guardrails. |
| `src/pyocd_debug_mcp/services/symbols.py` | OSS | Keep symbol and executable parsing in open services/artifacts. |
| `src/pyocd_debug_mcp/services/target_control.py` | OSS | Keep direct physical target control service. |
| `src/pyocd_debug_mcp/services/uart_capture.py` | OSS | Keep direct UART capture mechanics; remove durable evidence ownership. |
| `src/pyocd_debug_mcp/services/uart_exchange_schema.py` | OSS | Keep transport request/result schema. |
| `src/pyocd_debug_mcp/setup_flow/__init__.py` | SPLIT | Rewrite open exports for stateless onboarding; move managed setup/validation exports private. |
| `src/pyocd_debug_mcp/setup_flow/board_catalog.py` | SPLIT | Extract generic file/PDF byte reading and hashing into `artifacts/evidence.py`. Move reviewed catalog records, admission, authority, and catalog persistence to `firmcli_sentry/setup/board_catalog.py`. |
| `src/pyocd_debug_mcp/setup_flow/datasheet_evidence.py` | SPLIT | Move stateless PDF input validation/hash reading to `artifacts/evidence.py`. Move capture, replay, retention, and evidence-store records to private setup/FirmStore. |
| `src/pyocd_debug_mcp/setup_flow/device_support.py` | SPLIT | Move part normalization, builtin target geometry/support, PDSC/SVD parsing, candidate/geometry models, candidate binding, explicit pack validation, and stateless `DeviceSupportResolver` to `onboarding/device_support.py`. Move project/FirmStore replay and persisted organizational binding resolution private. Registry resolution is open only when the registry object/root is explicitly supplied by the caller. |
| `src/pyocd_debug_mcp/setup_flow/packs.py` | SPLIT | Move candidate models, fingerprinting, `validate`, and `validate_device` into open `packs/validation.py`. Move `promote`, reports, research records, admission, and FirmStore persistence to private setup. |
| `src/pyocd_debug_mcp/setup_flow/preflight.py` | SPLIT | Move stateless request/candidate models and explicit input validation to `onboarding/preflight.py`. Move attachment caching, polished choice/continuation behavior, stored profiles, and managed guidance to private setup. |
| `src/pyocd_debug_mcp/setup_flow/research.py` | CLOSED | Move research workflow, provenance authority, and persistence private. |
| `src/pyocd_debug_mcp/setup_flow/reviewed_evidence.py` | CLOSED | Move reviewed evidence and authority private. |
| `src/pyocd_debug_mcp/setup_flow/setup.py` | CLOSED | Keep the present managed setup workflow private. Open onboarding is a new stateless service. |
| `src/pyocd_debug_mcp/setup_flow/targets.py` | CLOSED | Move reviewed target catalog/admission and managed target state private. Open target support comes from explicit inputs and backend facts. |
| `src/pyocd_debug_mcp/setup_flow/validate.py` | CLOSED | Move managed validation, policy truth, persistence, and setup status private. Physical transport failures continue to originate in the open HAL. |

### 6.7 Current MCP tool modules

| Current path | Action | Exact implementation |
|---|---|---|
| `src/pyocd_debug_mcp/tools/__init__.py` | SPLIT | Replace the open file with static open-tool exports. Create private guarded-tool exports separately. |
| `src/pyocd_debug_mcp/tools/artifacts.py` | SPLIT | Put a direct `collect_build_artifacts` wrapper over the open collector in `mcp_tools/artifacts.py`. Move plan/gate/paid-response wording and persistence integration private. |
| `src/pyocd_debug_mcp/tools/batch.py` | SPLIT | Put non-recursive, one-board validation/execution in `mcp_tools/batch.py` without monitor imports. Move authorization, plan-generated compatibility behavior, fleet batching, and fallback policy private. |
| `src/pyocd_debug_mcp/tools/breakpoints.py` | SPLIT | Move address canonicalization and direct set/remove handlers open. Move executable-containment policy, event recording, and guarded responses private. |
| `src/pyocd_debug_mcp/tools/discovery.py` | OSS | Move to `mcp_tools/discovery.py`; retain open hook contract, explicit refresh, and run-scoped retry state. Remove FirmStore paths and commercial response language. |
| `src/pyocd_debug_mcp/tools/execution.py` | SPLIT | Put direct halt/resume/step/reset wrappers in `mcp_tools/execution.py`. Move plan wording, event creation, and `wrap_layer2_response` behavior private. |
| `src/pyocd_debug_mcp/tools/flash.py` | REPLACE | Keep current guarded application/bootloader handlers private. Author open `flash_firmware` over artifact inspection, connection, and target services with mechanical validation only. |
| `src/pyocd_debug_mcp/tools/handshake.py` | REPLACE | Keep current dynamic handshake private. Author a static open handshake that reports only open version, capabilities, and schemas. |
| `src/pyocd_debug_mcp/tools/memory.py` | SPLIT | Move parsing, symbol lookup, and direct memory handlers open. Move plan artifact binding, event recording, policy callbacks, refusal formatting, and allowed-region checks private. |
| `src/pyocd_debug_mcp/tools/misc.py` | SPLIT | Put a thin cancellable `wait` handler open; move event/finalizer wrapping private. |
| `src/pyocd_debug_mcp/tools/plans.py` | CLOSED | Move all `*-plan` tool registration and plan lifecycle private. |
| `src/pyocd_debug_mcp/tools/registers.py` | SPLIT | Move raw parsing/read/write plus basic width/alignment validation open. Move prohibited-register policy, guarded-call validation, safety callbacks, events, and guarded responses private. |
| `src/pyocd_debug_mcp/tools/remote_probes.py` | CLOSED | Move register/unregister tools private. The open `connect` accepts an explicit remote endpoint selector and never persists it. |
| `src/pyocd_debug_mcp/tools/serial.py` | SPLIT | Move codecs and explicit-port/direct UART handlers open. Move event/finalizer/plan wrappers, durable selection, and policy behavior private. |
| `src/pyocd_debug_mcp/tools/session.py` | REPLACE | Keep current profile-driven, override-plan, polished connect flow private. Author open explicit-config `connect`, `connect_under_reset`, `disconnect`, `get_board_info`, and `get_state` handlers. |
| `src/pyocd_debug_mcp/tools/setup.py` | CLOSED | Move managed overview/load/plan/setup/fix/continue/refresh/validate/status tools private. Open support inspection and onboarding use new stateless handlers. |
| `src/pyocd_debug_mcp/tools/unlock.py` | REPLACE | Keep `target_unlock-plan` and guarded `target_unlock` private. Expose only explicit-mechanism `recover_target` in the open server. |

### 6.8 Server composition

| Current path | Action | Exact implementation |
|---|---|---|
| `src/pyocd_debug_mcp/server.py` | REPLACE | Move the current commercial composition to `firmcli_sentry/server.py`. Author a thin open static server. Extract only `_parse_int`, `_word_size_is_valid`, `_run_cmd`, `resolve_board_config`, `format_board_info`, `_built_in_target_names`, `_target_names`, `_mcu_family`, `_normalized_target_identity`, `_stable_identity_equal`, `_connection_matches_probe`, `_enumerate_pack_targets`, and `_live_test_builtin_setup_target` into the relevant open contracts/services. Do not copy existing decorated handlers. The private server is then refactored to call `hal_client`, not adapter internals. |

## 7. Existing tool-by-tool disposition

This table covers the current registered MCP feature surface. “Private” means
the present behavior and name stay in the commercial product. “Open
replacement” means the physical capability remains open under the stated
direct contract, without the old guard machinery.

| Current tool | Destination | Migration instruction |
|---|---|---|
| `connect` | SPLIT | Present profile/assignment flow private; open name uses explicit `BoardConfig` and selector. |
| `disconnect` | OSS | Direct resource cleanup open; paid layer delegates to it. |
| `get_board_info` | OSS | Direct physical facts open. |
| `get_state` | OSS | Direct execution state open. |
| `connect_override` | CLOSED | Keep plan/override/authority workflow private. |
| `halt` | OSS | Direct physical primitive. |
| `resume` | OSS | Direct physical primitive. |
| `step` | OSS | Direct physical primitive. |
| `reset_and_run` | OSS | Direct physical primitive. |
| `reset_and_halt` | SPLIT | Direct mechanism open; present plan/policy wrapper private. |
| `connect_under_reset` | SPLIT | Direct mechanism open; present plan/policy wrapper private. |
| `read_cpu_register` | OSS | Direct read open. |
| `read_execution_state` | OSS | Direct read open. |
| `write_cpu_register` | SPLIT | Direct mechanically valid write open; guarded wrapper private. |
| `set_execution_state` | SPLIT | Direct mechanically valid write open; guarded wrapper private. |
| `register_write` | SPLIT | Direct mechanically valid write open; prohibited-register and plan policy private. |
| `find_symbol` | OSS | Open artifact/symbol primitive. |
| `read_memory_symbol` | OSS | Direct open read. |
| `read_memory_address` | SPLIT | Direct open read; region and plan restrictions private. |
| `write_memory` | SPLIT | Direct mechanically valid write open; guarded region policy private. |
| `flash_application` | CLOSED | Keep named role, artifact binding, safety map, and policy workflow private. Open equivalent physical access is `flash_firmware`. |
| `flash_bootloader` | CLOSED | Keep named role, approval, recovery, safety map, and policy workflow private. Open equivalent physical access is `flash_firmware`. |
| `read_serial` | SPLIT | Direct open UART read; workflow/event semantics private. |
| `write_serial` | SPLIT | Direct open UART write; guarded workflow semantics private. |
| `serial_exchange` | SPLIT | Direct open exchange; plan/event semantics private. |
| `set_breakpoint` | SPLIT | Direct open breakpoint; executable/safety policy private. |
| `remove_breakpoint` | OSS | Direct physical primitive. |
| `wait` | SPLIT | Cancellable wait open; workflow event/finalizer behavior private. |
| `collect_build_artifacts` | SPLIT | Explicit caller-directed collection open; FirmStore admission and workflow binding private. |
| `setup_overview` | CLOSED | Managed, persisted setup overview. |
| `load_setup_tool` | CLOSED | Dynamic/polished setup machinery. |
| `board_setup-plan` | CLOSED | Plan workflow. |
| `board_setup` | CLOSED | Managed setup. Open replacement is stateless `onboard_target`. |
| `board_fix_setup` | CLOSED | Persistent remediation workflow. |
| `continue_setup` | CLOSED | Stateful continuation workflow. |
| `board_safety_refresh` | CLOSED | Safety policy refresh and authority. |
| `board_validate` | CLOSED | Managed validation and organizational truth. |
| `get_setup_status` | CLOSED | Persistent setup state. |
| `get_discovery_hook_contract` | OSS | Open extensibility contract. |
| `refresh_discovery_hooks` | OSS | Open explicitly configured hook execution. |
| `register_remote_probe` | CLOSED | Durable remote resource inventory. |
| `unregister_remote_probe` | CLOSED | Durable remote resource inventory. |
| `target_unlock-plan` | CLOSED | Plan/approval workflow. |
| `target_unlock` | CLOSED | Present guarded workflow. Open replacement is explicit `recover_target`. |
| `action_batch` | SPLIT | One-board non-recursive execution open; authorized/fleet/workflow batching private. |
| `initialization_handshake` | REPLACE | Current dynamic/guarded handshake private; static capability handshake open. |
| `report_agent_issue` | CLOSED | Persistent monitor/organizational feedback. |
| `server_health_check` | CLOSED | Commercial monitor and reliability view. Open server relies on MCP initialization and ordinary operation errors. |
| `submit_routine_checkin` | CLOSED | Persistent monitor behavior. |

All sixteen current generated plan tools are private:
`board_setup-plan`, `connect_override-plan`, `write_cpu_register-plan`,
`set_execution_state-plan`, `read_memory_address-plan`, `write_memory-plan`,
`set_breakpoint-plan`, `flash_application-plan`, `flash_bootloader-plan`,
`register_write-plan`, `reset_and_halt-plan`, `connect_under_reset-plan`,
`target_unlock-plan`, `read_serial-plan`, `serial_exchange-plan`, and
`write_serial-plan`.

## 8. Public contracts required before moving implementations

Create and freeze these contracts in the open package first:

1. `BoardConfig`: portable target, probe, transport, clock, reset, UART, pack,
   and optional physical geometry configuration. It contains no organization,
   assignment, approval, or FirmStore identity.
2. `ProbeSelector` and `RemoteEndpoint`: explicit connection inputs.
3. `PhysicalIdentity`: normalized target/probe facts used to detect accidental
   mismatches.
4. `AddressRange`, `EraseSector`, and `PhysicalGeometry`: physical facts only.
5. `HalSession`, `OperationId`, `HalRequestError`, `HalTransportError`, and
   `HalCapabilityError`.
6. Typed request/result objects for connect, target control, memory, registers,
   breakpoint, flash, UART, discovery, build, artifact inspection, and recovery.
7. `PolicyEvaluationRequest` and `PolicyDecision`: public integration schemas.
   The decision contains allow/refuse, machine-readable reason, constraints,
   expiry, and opaque evidence references. No evaluator is included.
8. Provider extension protocols for debug transports, probe discovery, UART,
   reset/recovery, and generic request/response buses. Put future CAN, new
   probes, vendor transports, and new physical buses behind these open
   contracts. Provider code can be supplied by the project, a customer, or a
   third party without changing the commercial policy/orchestration layer.

Every public service must be callable as Python, independent of MCP. MCP tools
perform only schema translation and service invocation.

## 9. State and storage rules

The open product can retain only process-scoped operational state and
caller-directed files:

- live connections, operation cancellation, locks, and cleanup ownership;
- run-scoped probe/UART selections and discovery retries;
- caller-supplied board configuration and remote endpoints;
- caller-selected build/artifact output directories;
- an explicitly selected pack-byte cache/root.

The open product must not create `.firm`, infer FirmStore, or persist profiles,
assignments, approved artifacts, safety maps, validation truth, plans,
approvals, remote probe registries, event ledgers, monitor state, or user/org
ownership.

The commercial product owns all of those durable records. It passes explicit
configurations, paths, decisions, and endpoints across the HAL boundary.

## 10. Dependency and packaging changes

The open `pyproject.toml` must directly declare the libraries used by the open
implementation: `mcp`, `anyio`, `httpx`, `pydantic`, `psutil`, `pyocd`,
`pyelftools`, `pyserial`, `python-dotenv`, and `pyyaml`. The open pack-byte store
requires an explicit caller-selected root, so remove `platformdirs`. Remove
`sentry-sdk` from the open distribution.

The private distribution depends on `pyocd-debug-mcp`, `sentry-sdk`, and its
private persistence/transport dependencies. It owns the existing monitor and
commercial CLI entry points. The open distribution retains:

- `pyocd-debug-mcp` for its static MCP server;
- `pyocd-pack-repair` for mechanical pack index repair;
- `pyocd-collect-artifacts` for caller-directed artifact collection;
- `pyocd-native-build` for exact-argv native builds.

## 11. Test and support-file migration map

Every path below is relative to `MCP_Server/BYO-Firmware-MCP`. For `SPLIT`
tests, move individual test functions alongside the feature they assert; do
not leave a cross-product test importing both package internals.

### 11.1 Harness and fixture files

| Current path | Destination |
|---|---|
| `tests/baseline_capture.py` | CLOSED: commercial baseline/compatibility capture. |
| `tests/baseline_transcript.json` | CLOSED: commercial baseline fixture. |
| `tests/codex_harness.py` | CLOSED: current full commercial-agent harness. Add a separate open smoke client for the static HAL tools. |
| `tests/discovery_hook_fixtures.py` | OSS: open hook contract fixtures; remove FirmStore ownership assumptions. |
| `tests/fake_discovery_hook.py` | OSS. |
| `tests/fake_provider_worker.py` | OSS. |
| `tests/manual/manual_remote_probe_hardware_check.py` | SPLIT: explicit endpoint/connect hardware check open; registry setup private. |
| `tests/monitor_support.py` | CLOSED. |
| `tests/store_cleanup.py` | CLOSED. |

### 11.2 Open, closed, and split test files

| Current path | Destination and exact split |
|---|---|
| `tests/test_breakpoint_tools.py` | SPLIT: canonicalization/direct backend tests OSS; containment, events, and policy tests closed. |
| `tests/test_change_loop_spec.py` | OSS: physical edit/build/flash/debug loop contract. |
| `tests/test_change_loop_stale_setup_allowance.py` | CLOSED: persisted setup allowance behavior. |
| `tests/test_connection_promotion_transaction.py` | SPLIT: provider connection/rollback cleanup OSS; summary, gate, promotion, and assignment tests closed. |
| `tests/test_disconnect_cleanup.py` | OSS. |
| `tests/test_discovery_hook_process.py` | OSS. |
| `tests/test_discovery_hook_registry.py` | SPLIT: hook loading with explicit temporary root OSS; FirmStore registry ownership closed. |
| `tests/test_discovery_hook_safety.py` | SPLIT: hook schemas, rejection of authority payloads, and native precedence OSS; FirmStore, gates, plans, and dynamic visibility closed. |
| `tests/test_discovery_hook_workflow.py` | OSS after removal of commercial persistence assertions. |
| `tests/test_discovery_retry_store.py` | OSS: retain run-scoped retry behavior only. |
| `tests/test_discovery_tool_contract.py` | OSS. |
| `tests/test_hook_gating_and_budget.py` | SPLIT: hook/provider timeout and discovery mechanics OSS; finalizer, setup, plan, and authority gates closed. |
| `tests/test_inventory_snapshot_concurrency.py` | OSS: process-local snapshot correctness. |
| `tests/test_monitor_behaviour.py` | CLOSED. |
| `tests/test_monitor_classification.py` | CLOSED. |
| `tests/test_monitor_codex_e2e.py` | CLOSED. |
| `tests/test_monitor_counters_trail.py` | CLOSED. |
| `tests/test_monitor_delivery.py` | CLOSED. |
| `tests/test_monitor_ledger.py` | CLOSED. |
| `tests/test_monitor_narrative_tools.py` | CLOSED. |
| `tests/test_monitor_passivity.py` | CLOSED. |
| `tests/test_monitor_redaction.py` | CLOSED. |
| `tests/test_monitor_stdio_lifecycle.py` | CLOSED. |
| `tests/test_monitor_thrash_block.py` | CLOSED. |
| `tests/test_monitor_wiring.py` | CLOSED. |
| `tests/test_phase2_uncovered.py` | SPLIT: direct discovery/serial primitives OSS; guarded responses, events, and commercial surface assertions closed. |
| `tests/test_preflight_probe_guidance.py` | SPLIT: stateless input/candidate validation OSS; cached guidance and continuation closed. |
| `tests/test_probe_cli_command.py` | OSS. |
| `tests/test_probe_inventory.py` | OSS. |
| `tests/test_probe_selection_records.py` | OSS for run-scoped records; move any durable assignment assertion closed. |
| `tests/test_process_cleanup.py` | OSS. |
| `tests/test_regression_change_loop.py` | SPLIT: artifact discovery, adapter reset truth, physical recovery, and direct operations OSS; event logs, assignment, gates, and revocation closed. |
| `tests/test_regression_stale_setup_allowance.py` | CLOSED. |
| `tests/test_remote_probe_smoke.py` | CLOSED for present registry integration; create an open explicit-endpoint smoke test. |
| `tests/test_remote_probes.py` | SPLIT: host/port normalization, boundary ports, reachability, and explicit selector behavior OSS; load/save/upsert/remove, registry paths, tools, and persistence closed. |
| `tests/test_server_assignment_connect.py` | CLOSED. |
| `tests/test_server_trust_model_round_1.py` | SPLIT: unbounded serial schema, direct mapped-memory validation, wait/native build, and same-board batch OSS; research, pack promotion, setup plans/budgets, and prohibited safety ranges closed. |
| `tests/test_server_trust_model_round_2.py` | SPLIT: symbol search, malformed ELF, long config/provider identifiers, and artifact/build path hygiene OSS; finalizer timeout and reviewed target admission closed. |
| `tests/test_server_trust_model_round_3.py` | SPLIT: symbol/serial direct validation, batch/artifact production, collector, hygiene, PDSC parser, and explicit pack-binding primitives OSS; setup names, guarded flash wording, retention gates, and safety allocation closed. |
| `tests/test_server_trust_model_round_4.py` | SPLIT: symbol/serial boundaries, batch/artifact production, collector, hygiene, PDSC parsing, and pack stability/parser tests OSS; setup routes, commercial diagnostics, and safety gates closed. |
| `tests/test_server_trust_model_round_5.py` | SPLIT: proof that repeated direct flash/UART calls reach the backend OSS; event-ledger assertion for every attempt closed. |
| `tests/test_setup_overview_no_probe.py` | SPLIT: no-probe discovery/support facts OSS; managed setup-overview response closed. |
| `tests/test_swd_process_isolation.py` | OSS. |
| `tests/test_swd_pyocd_breakpoints.py` | OSS for adapter breakpoint behavior. |
| `tests/test_swd_pyocd_jlink_multisession.py` | OSS: explicit local multi-connection mechanics, not fleet coordination. |
| `tests/test_trusted_input_admission.py` | SPLIT: stateless pack/device validation OSS; FirmStore admission/persistence closed. |
| `tests/test_uart_capture_evidence.py` | SPLIT: byte capture mechanics OSS; persistent evidence/admission semantics closed. |
| `tests/test_unified_inventory.py` | SPLIT: inventory rows, merging, provider facts, selection, and snapshots OSS; `ValidationInventory` adapters and persistent assignment views closed. |
| `tests/test_validation_honesty.py` | CLOSED for current managed validation workflow; port worker-deadline and physical transport-loss cases into new OSS runtime tests. |

## 12. Required migration sequence

Perform these stages in order. A stage is complete only when its listed tests
and boundary checks pass.

### Stage 1 — establish the open contracts

1. Create `contracts`, `runtime`, `artifacts`, `discovery`, `packs`,
   `onboarding`, `services`, and `mcp_tools` packages.
2. Extract the public data/error/request/result contracts listed in section 8.
3. Move adapters and physical services behind those contracts without changing
   behavior.
4. Add import-boundary tests that fail on any reference from open code to
   `firmcli_sentry`, `firmstore`, `guardrails`, `monitor`, commercial safety
   policy, plans, approvals, or durable assignments.

### Stage 2 — make the open Python HAL complete

1. Extract runtime lifecycle, discovery, explicit remote endpoint validation,
   artifact inspection, pack validation, stateless onboarding, build, and
   physical-operation services.
2. Replace current commercial session/event objects with `HalSession` and
   ordinary HAL results.
3. Test a complete Python-only loop: discover, onboard explicit config,
   connect, build, collect artifact, flash, halt, inspect registers/memory,
   use a breakpoint/UART, reset, recover, and disconnect.
4. Run that loop with no private package installed and with no `.firm`
   directory present.

### Stage 3 — author the open static MCP server

1. Implement exactly the tool surface in section 4 as thin wrappers over the
   Python HAL.
2. Register tools statically; remove plans, dynamic visibility, monitor hooks,
   FirmStore discovery, and commercial finalizers.
3. Add ordinary-script and MCP-client end-to-end tests proving independent
   operation on a fake provider and the existing hardware-marked fixtures.

### Stage 4 — create the private product

1. Create `firmcli_sentry` and move FirmStore, safety policy, guardrails,
   monitor, managed inventory, setup/validation, and existing commercial MCP
   composition into it.
2. Add `hal_client` using public open contracts. First support in-process calls
   for migration tests, then make isolated stdio HAL workers the production
   path.
3. Replace every direct adapter/service import in private code with `hal_client`.
4. Preserve current commercial tool names and guarded behavior with contract
   tests against the baseline transcript.

### Stage 5 — remove the old mixed namespaces

1. Delete the open package's `firmstore`, `guardrails`, `monitor`, and `safety`
   policy namespaces after their private imports are migrated.
2. Delete compatibility re-exports that allow private features through the
   open namespace.
3. Split test files as specified in section 11 and ensure each distribution's
   tests install only its declared product dependencies.
4. Update README, server guide, architecture docs, examples, and client
   configuration to describe the two distinct servers.

## 13. Non-negotiable boundary checks

Add these checks to CI:

1. Build an open wheel, inspect its archive, and fail if it contains
   `firmstore`, `guardrails`, `monitor`, policy safety maps, plans, approvals,
   assignments, private package strings, or `sentry_sdk`.
2. Parse the open source import graph and fail on any import of
   `firmcli_sentry` or a private namespace.
3. Install the open wheel into an empty environment and run its static MCP
   handshake plus the complete fake-provider edit/flash/debug/UART loop.
4. Assert the open server exposes exactly the section 4 tools, independent of
   environment variables and whether the private wheel is installed.
5. Assert open operations do not create `.firm` and do not write outside
   caller-declared paths except documented temporary/process files.
6. Assert no open result claims policy permission, organizational safety,
   approval, validation authority, or persistent ownership.
7. Assert the private import graph depends only on public `pyocd_debug_mcp`
   contracts/services and never imports `pyocd_debug_mcp.adapters.*`.
8. Run the private compatibility suite and prove every guarded write/flash/
   recovery action obtains a private allow decision before the HAL request.

## 14. Completion criteria

The split is complete only when all statements below are true:

- An engineer can install only `pyocd-debug-mcp` and use Claude, Codex,
  Cursor, another MCP client, a custom Python agent, or ordinary scripts to
  build, flash, debug, inspect, use UART, recover, and iterate on real firmware.
- The engineer supplies explicit configuration; no commercial profile,
  FirmStore, account, service, license, or hosted backend is required.
- Custom probes, transports, discovery hooks, pack sources, target support,
  and physical buses can be implemented against open contracts.
- The open server does not provide the commercial persistent safety evaluator,
  plans, approvals, managed setup, organizational artifact control, inventory
  registry, monitor, audit system, fleet scheduler, or shared-resource OS.
- The commercial product works with any conforming open HAL provider and can
  receive customer policy/configuration inputs through public schemas without
  revealing its evaluator or enforcement implementation.
- No live source file, test file, or current MCP tool remains in the old mixed
  package without the destination specified by this document.

## 15. Deliberately excluded material

`sentry-evidence`, `fallback-support-evidence`, and sibling archive/snapshot
directories are historical evidence, not live implementation inputs. Do not
migrate them as product code. Preserve them according to the existing evidence
retention policy and update generated path references only after the live split
is complete.
