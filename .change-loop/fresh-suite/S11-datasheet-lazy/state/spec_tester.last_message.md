Tightened [test_s11_datasheet_lazy_spec.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\tests\test_s11_datasheet_lazy_spec.py) for CL-001:

- Final-page proof extracts every required page once, in order.
- `PdfReader` construction failures retain typed error and cause.
- Existing coverage attacks metadata/early/later proof, no-match, `None`, iteration, and extraction failures.

Recorded command is now Bash-safe and PowerShell-safe:

```sh
./.venv/Scripts/python.exe -m unittest -q tests.test_s11_datasheet_lazy_spec
```

It passes: 8 tests under both PowerShell and Bash simulation. The manifest remains limited to the S11 test file.

Design-charter checkpoints logged this iteration: pre-edit, pre-verification, final verdict. No hardware action occurred. Remaining ambiguity: none in CL-001’s focused behavior; official-PDF controls remain covered by existing H04 suites.
