# Adversarial plan review record

Plan SHA-256: a183fa3ec85b888d76b96d6bdac6fceb17b0843c7d81656244ae236f3d47044b

- Current validated plan SHA-256:
  `a183fa3ec85b888d76b96d6bdac6fceb17b0843c7d81656244ae236f3d47044b`
- Reviewer session: `019f9d3d-c36c-7493-b53e-9ea95eecb75d`
- Original plan SHA-256:
  `852f427df6c33852a4a680543f0db598f9e85d596a7a5cdda8d1e8410f7fbab5`
- Original verdict: `BLOCK` because CL-001 did not define the Python 3.10 exception graph,
  cancellation identity, exact text, or traversal oracle.
- Targeted amendment verdict: `AMENDMENT_READY`
- Amendment record: `plan-amendments.md` PA-001.

## Numbered execution risks and test targets

1. Preserve the same active normalized primary or cancellation object and original traceback
   through the close attempt. Do not replace it with a newly raised post-cleanup principal object.
2. Clear the raw close exception's automatically-created implicit context before installing the
   explicit graph, so primary -> close -> primary cannot cycle.
3. Assert the exact cause-before-context identity sequences, strings, suppression flags, and cycle
   absence for primary-only, close-only, primary-plus-close, and cancellation-plus-close.
4. Cover capture expected-text and `max_bytes` early returns, exactly-one close, and absence of
   implicit retry/reopen.
5. Run existing UART evidence, H05 wait-cancellation, and H05 marker-unlink suites plus one serial
   delegate boundary.
