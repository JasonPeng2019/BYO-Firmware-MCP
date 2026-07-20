# Function-symbol memory access refusal

## Problem

`read_memory_symbol` and the symbol form of `write_memory` accept ELF function symbols as if they
were data objects. On Thumb targets a function symbol can carry an odd address. Passing that address
to a scalar backend read can raise an assertion, turn a normal input mistake into a connection
failure, and obscure the recovery path.

## Required behavior

- Symbol memory reads and writes operate on data objects, not executable function symbols.
- A resolved `STT_FUNC` is refused before containment checks or target I/O with a stable policy code
  and guidance to use `find_symbol`, breakpoint tools, or the guarded mapped-address read when code
  bytes are intentionally needed.
- Scalar symbol accesses whose address is not aligned to their requested width are likewise refused
  before target I/O instead of leaking a backend assertion.
- Ordinary data-symbol behavior and the breakpoint function-symbol path remain unchanged.

