# PLAN: Run-scoped Current Symbol Artifact

1. Add a small in-memory board-to-ELF binding in the server with path and SHA-256 verification.
2. Bind it only after successful application flash; derive the already-required ELF companion for
   HEX and leave failed/refused/bootloader paths unchanged.
3. Make symbol memory handlers translate artifact resolution failures into a stable refusal before
   symbol parsing or backend memory access.
4. Add focused tests for success-only binding and refusal-before-backend behavior.
5. Run focused tests, Ruff, Pyright, full locked pytest, then hostile diff review.
