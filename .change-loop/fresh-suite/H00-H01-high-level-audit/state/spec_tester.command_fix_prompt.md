# Spec-tester neutral-command correction

Resume the same spec-tester role. Do not edit any source or test file. The recorded
`spec_test_cmd` is consumed by the neutral Bash harness, so PowerShell `$env:` assignment is not
valid there. Replace only the recorded command with this exact Bash-neutral, self-preparing
command:

`uv sync --locked && uv run --locked --no-sync pytest -q tests/test_h00_repository_contract.py`

Keep the manifest unchanged. Do not rerun the suite in this turn. Confirm the state file contains
exactly that one line.
