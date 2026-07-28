Added one nonduplicative regression: the registered `read_memory_symbol` handler retains its public parameter order and defaults.

Evidence:

- Exact Bash regression command: 3 passed
- Adjacent A20 spec suite: 10 passed, 6 subtests
- Ruff and `git diff --check`: passed
- Charter SHA verified: `03347b…5411bdb`

Blast radius covered: server registry/public interface, constructor wiring, scalar lifecycle isolation, and raw address-read compatibility. No actionable remaining defect found.

Charter checkpoints recorded: analysis, pre-edit, pre-verification, final verdict.
