H04 adversarial suite is recorded and passes:

```text
./.h01-venv-batchstrict/Scripts/python.exe -m unittest tests/test_h04_attachment_cache_spec.py
Ran 8 tests — OK
```

Added [test_h04_attachment_cache_spec.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\tests\test_h04_attachment_cache_spec.py).

Coverage:
- CL-001: direct UART persistence callback, no-UART/research-stop boundary, and unconfirmed external-adapter stop.
- CL-002: portable missing/valid/corrupt diagnostics plus unavailable-service fallback.
- CL-003: corrupt/authority-shaped cache cannot override direct identity or resolve absent/ambiguous ports.
- CL-004: byte-identical repeated confirmation.

Recorded command and manifest are Bash-compatible for the neutral harness. No remaining untestable ambiguity identified.

Charter checkpoints: before analysis; before each edit; before verification; immediately before final verdict.
