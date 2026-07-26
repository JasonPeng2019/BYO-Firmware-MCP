Added adversarial suite: [test_h04_pack_index_repair_spec.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\tests\test_h04_pack_index_repair_spec.py).

- CL-001: zero/positive/negative retries, cleanup, destination preservation, CLI failure.
- CL-002: exact raw retained evidence, offline replay and stability, refresh, corrupt evidence refusal, URL isolation.
- CL-003: help text and legacy options/defaults.

Recorded command and manifest in the required state directory. Verified:

```text
uv run --isolated --with pytest python -m pytest -q tests/test_h04_pack_index_repair_spec.py
6 passed
```

Remaining ambiguity: this isolated suite uses a minimal cache double, so actual cmsis-pack-manager index byte stability and independent json-only cache-root behavior remain best covered by the neutral loopback oracle.

Design-charter checkpoints completed: initial review; immediately before each test edit; before verification; final verdict.
