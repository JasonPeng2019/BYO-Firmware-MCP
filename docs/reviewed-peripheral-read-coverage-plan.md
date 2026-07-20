# Reviewed peripheral read coverage — implementation plan

1. Add non-overlapping, read-only nRF52840 APB/AHB peripheral spans to both pinned evidence
   authorities and the matching catalog hardware-region declaration. Split around the existing
   NVMC/ACL prohibited window and writable GPIO window.
2. Recompute and pin the two evidence-file SHA-256 digests in the reviewed catalog.
3. Extend reviewed-evidence tests to prove UARTE read access, unchanged GPIO write access, and
   unchanged NVMC prohibition.
4. Run the focused reviewed evidence, safety refresh, safety enforcement, and server safety-tool
   suites; then Ruff, Pyright, and the complete locked pytest suite.
5. Run a fresh GPT 5.6 Terra high/fast read-only diff audit, vet its findings, fix valid issues, and
   repeat until clean.
6. Resume the blocked Luna session, refresh its map, prove the live UARTE register read now works,
   and continue the firmware failure loop.

