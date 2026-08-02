# Fallback-support evidence

This is the final evidence bundle for the MCP server's fallback paths: discovery hooks,
provider-qualified selectors, and registered remote pyOCD probe endpoints.  It is kept
separate from the runnable server code so historical test material is available without
turning the repository root back into a development workspace.

## Contents

- `tests/` contains the complete final automated test suite, including the fallback
  regression tests and the manual remote-probe hardware check.
- `specs/` contains the implemented remote-probe plan and the discovery-hook plan and
  implementation guide.
- `test-plans/` contains the final test rationale and the practical HIL test plan.
- `results/` contains the current automated-check result and practical-test results,
  including retained HIL artifacts.

## Recorded verification status

The final green result is documented in the preserved Opus records under `results/`:
730 automated tests passed with 7 skipped, lint was clean, and source type checking
was clean. This bundle does not rerun or alter those tests; it preserves their sources,
plans, practical checks, and recorded outcomes together.
The remote-probe practical check requires an attached ST-LINK and a free TCP port. Run
`uv run --locked python fallback-support-evidence/tests/manual/manual_remote_probe_hardware_check.py`
only on a machine with that hardware available. Its recorded successful result is in
`results/remote-probe-hardware-review.md`.
