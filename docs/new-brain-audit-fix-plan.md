# New Brain adversarial audit fix plan

1. Fix operation lifecycle first: flash transition, finalizer binding, serial gate, and batch board reservation. These share dispatch semantics; verify cancellation/authorization/lock tests together.
2. Fix small containment/generalization items: symbol-size bounds, custom probe compatibility, and portable ELF extraction. Verify zero backend calls on refusal and existing flash containment.
3. Fix setup surface as one contract slice: optional UART, strict startup assignments, and parseable incomplete-profile repair. Regenerate plan/tool contracts only after schemas settle.
4. Make Zephyr fallback local-only and remove MCU-only board-target advice; verify native-first guidance and helper CLI behavior.
5. Run focused suites, Ruff, Pyright, full pytest, package/import, and stdio smoke.
6. Run a fresh hostile audit against the complete design. Validate and loop only on new valid major findings; stop on zero findings or all-invalid/repeated findings.

Dependencies: setup changes share `board_setup` schemas and must land together. Batch locking must preserve child dispatch rather than bypass it. ELF changes must not weaken stable-map, entry, vector, segment, HEX-companion, or erase-sector checks. No fix may add persisted authorization or caller-provided ranges/policy.
