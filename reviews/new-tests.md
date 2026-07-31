# Phase 2: New Test Coverage

## File: `tests/test_phase2_uncovered.py`

### NormalizePortNameTests (6 tests)

**Function**: `pyocd_debug_mcp.serial_resolver.normalize_port_name`

Why these cases mattered:
- Port normalization is used throughout the serial discovery stack. Incorrect handling of the Windows device namespace prefix could silently produce wrong matching.
- Case-folding must be consistent for port deduplication.
- Whitespace handling affects parsing and comparison of user-supplied or system-detected ports.

Test cases:
1. `test_basic_port_name_lowercased` — Port names in uppercase are correctly lowercased (e.g., COM1 → com1)
2. `test_windows_device_namespace_prefix_stripped` — Windows `\\.` prefix is stripped before lowercasing
3. `test_whitespace_stripped` — Leading/trailing whitespace is removed
4. `test_empty_string_becomes_empty` — Empty strings remain empty after normalization
5. `test_mixed_case_and_numbers` — Alphanumeric strings with underscores are handled correctly
6. `test_prefix_with_backslash_escape_only_removes_exact_prefix` — Only the exact `\\.` prefix is stripped; partial matches pass through

---

### DecodeHookStdoutTests (6 tests)

**Function**: `pyocd_debug_mcp.discovery_hooks.decode_hook_stdout`

Why these cases mattered:
- Hook output decoding is the first line of defense against malformed discovery hook responses.
- Invalid UTF-8 from a hook could crash the server or silently corrupt discovery results if not caught early.
- Invalid JSON similarly signals a broken hook that should fail fast with clear diagnostics.
- The function's "no replacement" policy (rejects with UnicodeDecodeError, never .decode('utf-8', 'replace')) must be tested to ensure diagnostics are accurate.

Test cases:
1. `test_valid_utf8_json_output` — Valid UTF-8 JSON is parsed and routed to schema validation
2. `test_invalid_utf8_raises_error` — Invalid UTF-8 bytes are rejected with a descriptive error
3. `test_utf8_with_replacement_chars_raises_error` — UTF-8 that would require replacement characters is rejected
4. `test_invalid_json_raises_error` — Valid UTF-8 with invalid JSON syntax is rejected
5. `test_valid_json_but_wrong_schema_raises_error` — Valid JSON is forwarded to schema validation, which may reject it
6. `test_empty_payload_raises_error` — Empty output is rejected as invalid JSON

---

### ParseHookDeclarationTests (8 tests)

**Function**: `pyocd_debug_mcp.discovery_hooks.parse_hook_declaration`

Why these cases mattered:
- Hook declarations are user-supplied (via YAML manifests or hook_registry.json) and are the attack surface for misconfiguration.
- The manifest loader reuses this function for each entry; bugs here cascade across all hooks.
- Kind/runner validation gates whether a hook can run at all, and must reject unknowns to prevent silent failures.
- Extra/wrong-typed fields often signal manifest corruption or agent-written mistakes that should fail early.

Test cases:
1. `test_not_a_dict_raises_error` — Non-dict input is rejected (list, string, None)
2. `test_missing_required_field_raises_error` — Missing 'kind' or 'runner' is rejected
3. `test_unknown_kind_raises_error` — kind values outside SUPPORTED_KINDS are rejected
4. `test_unknown_runner_raises_error` — runner values outside SUPPORTED_RUNNERS are rejected
5. `test_extra_fields_raise_error` — Unknown fields in the declaration are rejected
6. `test_wrong_type_fields_raise_error` — Fields with wrong types (e.g., kind as int instead of str) are rejected
7. `test_hook_id_validation` — hook_id with invalid characters (e.g., spaces) is rejected
8. (Implicitly tested) — Valid declarations with all required fields pass through

---

### ResolveDeclarationTests (4 tests)

**Function**: `pyocd_debug_mcp.discovery_hooks.resolve_declaration`

Why these cases mattered:
- Resolution bridges declarations (untrusted user/agent input) to executable specs. This is where symlink escapes and path traversal attacks are caught.
- The containment checks (`is_relative_to`) must be verified to prevent an agent from writing a hook that points outside the root and runs arbitrary code.
- NUL bytes in paths can cause silent truncation on POSIX systems; rejection here is critical.
- File existence is checked only here, so missing-file errors must be caught.

Test cases:
1. `test_executable_runner_requires_absolute_path` — Executable runner rejects relative paths
2. `test_project_runner_requires_containment` — Project runner rejects paths outside the hook root
3. `test_project_runner_with_null_bytes_raises_error` — NUL bytes in entrypoint are rejected
4. `test_file_not_found_raises_error` — Non-existent entrypoint files are rejected

---

### RetryContextTests (4 tests)

**Class**: `pyocd_debug_mcp.tools.discovery.RetryContext`

Why these cases mattered:
- Retry contexts preserve the exact call that failed, allowing users to re-run it after fixing the root cause (e.g., writing a hook).
- Deep-copy is used to avoid aliasing, so the stored call cannot drift if the caller modifies their local dict.
- Handling of None/empty arguments must be correct to avoid spurious KeyErrors when replaying.

Test cases:
1. `test_retry_call_with_no_tool_returns_none` — retry_call() returns None when retry_tool is None
2. `test_retry_call_deep_copies_arguments` — Arguments are deep-copied, not aliased
3. `test_retry_call_with_empty_arguments` — None and {} arguments both become {} in the call
4. `test_retry_context_fields` — All context fields are stored and accessible

---

### DiscoveryFailureTests (6 tests)

**Class**: `pyocd_debug_mcp.discovery_failures.DiscoveryFailure`

Why these cases mattered:
- Failure payloads are sent to agents as structured data; incorrect serialization breaks agent logic.
- Optional fields (hook_contract_call, diagnostics, etc.) must be included iff set, not always.
- Payload must be JSON-serializable to transit through MCP.
- Failure factories (no_native_probe_failure, etc.) encapsulate domain knowledge; their output must match the contract.

Test cases:
1. `test_minimal_failure_payload` — Minimal failure includes only required fields
2. `test_failure_with_all_optional_fields` — All optional fields are serialized when present
3. `test_no_native_probe_failure_structure` — Probe failure always includes hook_contract_call
4. `test_no_native_uart_failure_structure` — UART failure structure is correct
5. `test_failure_payload_is_serializable_to_json` — Payload round-trips through JSON

---

### VendorUartRowsTests (8 tests)

**Function**: `pyocd_debug_mcp.hardware_inventory.vendor_uart_rows` and adapters

Why these cases mattered:
- This function runs inside `snapshot()` where an exception breaks all discovery, not just this layer.
- Empty/missing executables, nonzero exits, and unparseable output must all be handled gracefully without raising.
- Identity fields (usb_serial/vid/pid) must be None (not fabricated) to ensure rows are classified as session-local, not stable — a row wrongly marked stable would claim a durable identity it cannot actually support.
- The function gates on parser type and exit codes, so both success and failure paths must be covered.

Test cases:
1. `test_empty_serial_fallbacks_returns_no_rows` — Empty fallbacks tuple returns no rows or subprocess invocations
2. `test_executable_absent_skips_spec` — resolve_command_path returns None → spec skipped, run_cmd never called
3. `test_nonzero_exit_code_124_timeout_skips_spec` — Exit 124 (timeout) → no rows, continues to next spec
4. `test_nonzero_exit_code_127_not_found_skips_spec` — Exit 127 (not found) → no rows, continues
5. `test_garbage_output_no_exception_empty_rows` — Unparseable output → no exception, no rows
6. `test_valid_nrfjprog_output_produces_rows` — Valid nrfjprog output → rows with vendor:provider_id provenance
7. `test_valid_stm32_programmer_output_produces_rows` — Valid STM32 output → rows with vendor:provider_id provenance
8. `test_vendor_rows_classified_session_not_stable` — Rows with None identity fields → classified session-local, not stable

---

## Summary

- **Total new tests**: 40 (32 Phase 2A + 8 vendor_uart_rows)
- **Functions covered**: 7 (normalize_port_name, decode_hook_stdout, parse_hook_declaration, resolve_declaration, RetryContext, DiscoveryFailure, vendor_uart_rows)
- **Edge cases targeted**: UTF-8/JSON validation, path traversal, type checking, deep-copy semantics, optional field serialization, Windows path handling, subprocess failure modes, identity field classification
- **All tests passing**: Yes
