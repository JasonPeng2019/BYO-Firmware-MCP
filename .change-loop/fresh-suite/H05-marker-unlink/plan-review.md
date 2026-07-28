# One-time adversarial review — H05 marker-unlink plan

Plan SHA-256: 2b1107796f7a5437304e4a440279b589fbdd2f7c8933ac599d08aa5f5a82186a

- Reviewed plan SHA-256:
  `2b1107796f7a5437304e4a440279b589fbdd2f7c8933ac599d08aa5f5a82186a`
- Reviewer thread: `019f9d10-a2e0-7ae0-b4aa-6f4a2feeaea8`
- Reviewer model: `gpt-5.6-terra`
- Reasoning/service tier: `medium`, `priority` (Fast)
- Review mode: independent, read-only, one time
- Verdict: `READY`
- Raw review:
  `.change-loop/fresh-suite/H05-marker-unlink/plan-review.last-message.md`

## Numbered execution risks and required test targets

1. Drive the real nested route `close -> call("close") -> _invalidate` with confirmed
   termination and one marker-removal `OSError`; the existing typed error and direct cause must
   escape, the marker must remain, and the initial close must perform exactly one removal and one
   termination attempt.
2. After restoring marker removal, a second `close()` must remove only the retained marker, clear
   `_marker`, and perform no extra provider request or termination.
3. Distinguish incomplete ownership cleanup from harmless graceful-close diagnostics: a
   provider/protocol close error that does not invalidate the client remains non-fatal after
   successful termination/removal, and a completed invalidation whose marker was removed also
   remains a successful outer close.
4. Preserve fail-closed unconfirmed termination with no marker removal, plus existing
   expired/racing-deadline cleanup behavior.

## Charter and scope judgment

The reviewer found no charter conflict, unnecessary complexity, scope expansion, missing
preservation contract, or ambiguous state transition. The accepted execution target remains one
local `_WorkerClient.close` distinction with no retry loop or environment-specific behavior.
