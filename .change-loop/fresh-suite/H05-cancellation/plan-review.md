# One-time adversarial plan review

Plan SHA-256: 2bb6ba02d19fefb0096bf5484ae817452c57c90f52e625f26d5b3fa35a05eb6a

- Reviewed plan SHA-256:
  `2bb6ba02d19fefb0096bf5484ae817452c57c90f52e625f26d5b3fa35a05eb6a`
- Reviewer thread: `019f9ccd-2546-7373-8fab-2e86a8d6fb95`
- Reviewer model: `gpt-5.6-terra`
- Reasoning effort: `medium`
- Service tier: `priority` (Fast)
- Workspace/sandbox: repository root, read-only
- Verdict: `RISKS` (accepted as execution and test targets; no plan replacement)

## Numbered risks and required execution targets

1. **Atomic success boundary.** CL-001 must put both the sole success event and exact returned
   success text inside the same existing `run_if_not_cancelled` atomic commit. A standalone
   checkpoint followed by `record_event` leaves a cancellation race that can write false success.
   The doer must implement one atomic action; the spec tester must deterministically assert that a
   cancellation winning at this boundary yields zero success events and no success result.

2. **Prove both race orderings.** Tests must control both sides of the completion/cancellation race:
   cancellation before commit produces `OperationCancelledError` and no event; cancellation after
   `completion_committed` preserves exactly one success event and the byte-for-byte existing
   response.

3. **Raw public-stdio identities.** The wire test must parse every stdout line as JSON-RPC; assert
   request `410` never has a successful tool result; accept only no response or the pinned SDK's
   exact `{code: 0, message: "Request cancelled"}` response for `410`; assert the prompt success is
   specifically request `420`; and prove another request succeeds on the same still-open transport.
   It must not rewrite or test for `-32800`.

These findings sharpen the already accepted CL-001 behavior and objective verification. They do
not expand production scope or require a plan amendment.
