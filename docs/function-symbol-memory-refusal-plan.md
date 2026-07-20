# Plan: function-symbol memory access refusal

1. Add one shared preflight helper for resolved symbol type and scalar alignment.
2. Apply it to symbol reads and symbol writes before any containment or backend call.
3. Add focused tests proving function and unaligned symbols are refused with zero target I/O.
4. Run the focused memory suite, then the complete locked pytest suite, Ruff, and Pyright.
5. Resume the same hardware session and re-run the function-symbol misuse plus the intended
   `find_symbol`/breakpoint path.

