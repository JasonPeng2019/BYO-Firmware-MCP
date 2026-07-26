Regression suite now passes: 3 tests, no hardware access.

The prior callback regression is fixed: cache persistence runs only when a UART is selected. I also made the suite import workspace source reliably under the neutral Bash runner.

Covered edges:

- External confirmation stop → no cache callback.
- Ready setup without UART → no cache callback.
- Valid cache status → read-only, byte-preserving diagnostic.

Recorded command and manifest are updated in the required state files.

Design charter checkpoints completed: before analysis, before editing, before verification, and before final verdict.
