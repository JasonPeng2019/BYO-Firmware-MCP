# S11 validated production repair request

Repair the verified generic-setup timeout caused by
`prove_datasheet_applicability()` eagerly extracting text from every PDF page before checking
metadata or earlier pages.

The current S11 official nRF52840 PDF is 18,277,247 bytes. A direct benchmark takes 92.421 seconds
even though the accepted identity is in PDF metadata, because the implementation constructs the
entire metadata-plus-pages list first. Fresh setup replays this proof in multiple phases, causing
the exact public paired `board_fix_setup` flow to exceed its advertised 300-second operation
budget.

Required behavior:

1. Keep the current exact PDF-byte digest, parser, server-derived authority terms, exact matching
   rules, proof schema, parser version, deterministic evidence-locus order, and truthful rejection
   semantics.
2. Evaluate metadata first. Only if it does not prove applicability, extract and evaluate pages
   lazily in document order and return at the first valid match.
3. A late-page match and a no-match document must still inspect every required page and produce
   the same proof/failure result as before.
4. Add focused automated tests that prove metadata and early-page matches do not extract later
   pages, while late-page/no-match behavior remains complete and deterministic.
5. Do not add caching as live authority, a new timeout, board/part/OS/path/provider special cases,
   dependencies, public APIs, or unrelated refactors.
6. Preserve all previously accepted H05 repairs and the repository's existing dirty work.
7. Do not commit, push, deploy, flash, or operate hardware.

Every role must reread `../.codex/design_charter.md` before its first action and again before
claiming completion. The implementation role must record its concrete charter checks before
editing production, between production/test handoffs, and after implementation in this repair
runtime's `DESIGN_CHARTER_CHECKS.md`.
