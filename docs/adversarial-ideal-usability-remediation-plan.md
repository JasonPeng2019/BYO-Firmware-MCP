# Adversarial ideal-usability remediation plan

1. Package distinct nRF52840 device-support and official-document evidence
   documents. Pin their hashes and runtime pyOCD target/SVD identities.
2. Add a catalog evidence loader which verifies assets and installed sources,
   parses strict schemas, invokes `reconcile_hardware_evidence`, and exposes
   only accepted reconciled regions and source manifests.
3. Make empty datasheet/evidence pins fail closed and advertise only complete
   reviewed automatic-setup entries.
4. Replace synthetic catalog provenance in automatic safety setup with the
   reconciliation result. Persist distinct evidence/source hashes in the
   fingerprint inputs and reconciled provenance in the safety map.
5. Align `Plan_Prompt_Contents_Spec.md` with the live setup schema,
   accepted-plan fallback response, setup-first routing, and
   `serial_exchange`; add exact conformance assertions.
6. Add non-authoritative `get_setup_status` Zephyr build guidance with the
   exact reviewed board target and installed scratch-path helper command.
7. Add UART write-log/close tests for readiness, first, and middle mismatch.
8. Resolve exact reviewed package-level parts to their catalog-owned built-in
   pyOCD target before research, with family-only/unreviewed-suffix adversarial
   coverage and no relaxation of the exact profile-commit requirement.
9. Define and validate a richer autonomous acceptance evidence record. Repeat
   non-destructive setup/safety/validation and the worker-thread application
   acceptance after the safety-source change, retaining exact MCP timeline,
   prompts, versions, report IDs/hashes, artifacts, and UART proof.
10. Expose non-authorizing Server Run ID/start time in the public handshake and
    require them in strict acceptance evidence.
11. Return the build helper as an exact Python-module command in the running
    server environment rather than relying on ambient PATH.
12. Clarify that `validation_passed_uart_not_configured` concerns an absent
    UART-content assertion, not attachment availability, and route agents to
    current readiness.
13. Add an immutable bounded readiness-probe delay to `serial_exchange`; first
    observe unsolicited boot/prompt output, then send the one planned probe
    only if still necessary. Add unit, plan-schema, and real-hardware coverage.
14. Run focused tests, Ruff, Pyright, the full suite, packaging/import checks,
   stdio smoke, and then submit the revised tree and evidence to a fresh
   adversarial re-audit. Repeat until green or only rejected criticisms remain.
15. Require current independently reconciled authority at every safety-map
    load, refresh, readiness, validation, and gate boundary. Fail legacy maps
    closed with a deterministic migration remedy and preserve them as evidence.
16. Extend both pinned evidence sources and strict reconciliation with exact
    erase origin/size, and remove catalog geometry from persisted authority.
17. Introduce one exact `serial_exchange` action validator shared by planning
    and execution, including bounded steps, exact nested keys, newline-only
    readiness probes, timing constraints, and atomic invalid-replacement tests.
18. Upgrade autonomous evidence validation from a manifest check to a causal
    proof linking Server Run, transports, MCP calls, operations, reports,
    committed artifacts, hardware observations, UART outcomes, and disconnect.
19. Return build guidance as structured argv plus shell-specific executable
    renderings, and exercise the Windows rendering in PowerShell.
20. Repeat the isolated no-board-YAML acceptance on the current authority
    schema, then submit implementation, tests, and evidence to the adversarial
    reviewer. Iterate on any new valid finding until the reviewer is green.
21. Replay persisted schema-v2 safety authority from hash-pinned server assets
    and installed runtime pins; compare exact documents, reconciliation,
    geometry, provenance, and regions before any gate or guarded operation.
22. Narrow the reviewed nRF peripheral map to volatile GPIO, prohibit NVMC/ACL,
    authorize breakpoints only in executable ELF segments, and prevent build
    RAM from creating authority.
23. Make UART conversation success depend on every planned step and process
    boundary bytes before sending the delayed readiness probe.
24. Reject destructive application/build path relationships before bootstrap,
    use versioned SDK caches with compatibility checks, and keep scratch copies
    free of generated/state trees.
25. Correct generated setup/serial plan examples and enforce runtime register
    width bounds for 32-, 64-, and 128-bit register classes.
26. Strengthen the acceptance validator and negative-test missing operations,
    product refusals, nonmonotonic timelines, duplicate artifacts, and file-time
    mismatches.
27. Ship the promised real-stdio fresh-workspace setup-only runner with a fixed
    identity/authorization input surface, fixed evidence output, and no
    downstream execution escape hatch.
28. Bind stable UART USB identity and current port through plan, preflight,
    cache, readiness, and runner evidence; redact all authority-bearing argument
    keys from transcripts.
29. Preserve empty results for wrong explicit probes, reject port-path-as-UART
    identity, disclose the resolved probe, and require exact live probe/UART
    identity at the pre-code barrier.
30. Repeat adversarial review until GREEN, then rerun current-tree isolated
    hardware acceptance and the complete software/quality/stdio matrix.
31. Make build bootstrap genuinely local-first: discover complete NCS/Zephyr
    workspace, SDK, compiler, and bundled Python before pip/network work; reject
    partial caches; provide a verified Python-owned managed fallback.
32. Make managed West state resumable and ownership-bound with an exact marker,
    repo/ref, manifest hash/path, early explicit-path validation, and compiler
    execution probes.
33. Add a universal bounded local-first dependency policy to handshake and all
    NULL plan responses, explicitly covering STM32CubeIDE/STM32Cube/ThreadX and
    equivalent heavy vendor packages; regression-test the guidance.
34. Repeat the fresh adversarial audit after these changes and accept only
    GREEN or demonstrably invalid criticism; then run the final full quality
    matrix without repeating destructive hardware work.
35. Make dependency caches process-safe and health-bound: stage SDK promotion,
    separate immutable workspace identity from post-update completion, use
    requirement-fingerprinted Python environments, validate vendor requirements
    offline, and repair damaged cache health markers.
36. Preflight and claim canonical build outputs before provisioning, refuse
    foreign contents, bind ownership to the exact app/output, and serialize
    cleanup, West execution, and artifact promotion across processes.
37. Record managed workspace global update completion separately from exact
    per-board validation so an already-present second board is admitted locally
    without another network-capable update; repeat adversarial review under the
    ordinary compliant-agent firmware workflow scope.
