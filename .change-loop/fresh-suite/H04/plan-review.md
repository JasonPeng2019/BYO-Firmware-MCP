# One-time adversarial review — H04 attachment-cache plan

- Reviewer identity: `/root/h04_plan_adversarial`
- Reviewer model: `gpt-5.6-terra`, medium reasoning
- Reviewed plan SHA-256:
  `d2afb0d35f4c08bad2096ce32e3821053e8ac2bfa42fe1a83fe83bad95db759b`
- Plan SHA-256: d2afb0d35f4c08bad2096ce32e3821053e8ac2bfa42fe1a83fe83bad95db759b

Plan SHA-256: d2afb0d35f4c08bad2096ce32e3821053e8ac2bfa42fe1a83fe83bad95db759b
- Design-charter discipline: reviewer read `../.codex/design_charter.md` in full before review,
  reread it after source inspection, and reread it before finalizing each numbered item.
- Assessment: **PROCEED**

The reviewer found no contradiction or missing behavior that makes implementation unsafe or
impossible. The following are execution risks and required adversarial test targets, not a
replacement plan.

1. **Persistence boundary must stay explicit.** The raw request says every stable selected pair;
   CL-001 intentionally persists only after `preflight_ready`. A target-research stop may already
   carry stable selected endpoints (`setup_flow/preflight.py:410-430`). Doer and testers must
   preserve the plan's completed/preflight-ready boundary and prove that an external adapter is not
   persisted before its confirmation stop is resolved.
2. **Idempotence must be stronger than record-count stability.** Current
   `AttachmentCache.confirm()` constructs a fresh timestamp before equality comparison
   (`firmstore/cache.py:245-279`), so repeating the same pair can rewrite `confirmed_at`. Test exact
   bytes or the unchanged timestamp in addition to no duplicate records; implement the smallest
   correction needed to satisfy CL-001's “idempotently retains” contract.
3. **Cache failures must not suppress direct identity.** Current status resolves the cache before
   direct matching and one broad exception can make UART unresolved (`server.py:4194-4230`).
   Cross malformed and forbidden-authority cache documents with unique, absent, and multiple
   direct matches. Only the unique independently verified direct match may remain UART-ready.
4. **External-adapter confirmation remains mandatory.** Preserve the existing friendly stop at
   `setup_flow/preflight.py:355-374`; built-in/provably-mapped stable UARTs become persistable
   without gaining a new prompt, while unconfirmed external adapters produce zero cache writes.
5. **The public diagnostic is portable, read-only, and non-authoritative.** Assert the additive
   object under a non-default temporary project root, no absolute path leakage, no file creation or
   byte change from `get_setup_status`, no readiness/gate change from cache state, and an honest
   unavailable-service fallback with no invented service-owned path.
