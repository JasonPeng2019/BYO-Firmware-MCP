AMENDMENT_READY — `plan.md` matches SHA-256 `a183fa3e…47044b`.

1. Implementation risk: preserve the active primary/cancellation exception through cleanup; do not re-raise it from a new post-cleanup site. That is necessary to retain the original object and traceback while attaching the specified close graph.

2. Implementation risk: on a close failure while another exception is active, explicitly clear the close exception’s implicit context before linking it. This prevents the primary → close → primary cycle PA-001 identifies.

3. Exact neutral test targets:
   - New focused UART-close spec: assert all four specified traversal sequences, object identities, strings, cause/context edges, suppression flags, cycle absence, and original primary/cancellation traceback identity for capture, write, and exchange.
   - The same spec: close-only, ordinary-primary-plus-close, cancellation-only, and cancellation-plus-close using unique fake-adapter sentinels.
   - The same spec: capture expected-text and `max_bytes` early returns; assert one close, no retry, and no implicit reopen.
   - Existing regressions: [test_uart_capture_evidence.py](C:/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/tests/test_uart_capture_evidence.py), [test_h05_wait_cancellation_spec.py](C:/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/tests/test_h05_wait_cancellation_spec.py), and [test_h05_marker_unlink_spec.py](C:/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/tests/test_h05_marker_unlink_spec.py).