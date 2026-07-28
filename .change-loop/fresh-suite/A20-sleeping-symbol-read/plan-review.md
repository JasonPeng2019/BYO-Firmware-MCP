# One-time adversarial plan review

- Reviewed plan SHA-256:
  `a974e693dd8b16d03d993b27b0c16f1113891482de58e67160608b2cc6da0a07`
Plan SHA-256: a974e693dd8b16d03d993b27b0c16f1113891482de58e67160608b2cc6da0a07
- Reviewer identity: `A20-plan-adversarial-reviewer-001`
- Reviewer thread: `019fa754-1e2a-74f2-9e5f-2bf59a66a4ee`
- Model/settings: `gpt-5.6-terra`, medium reasoning, `priority` / Fast service tier
- Review log:
  `.change-loop/fresh-suite/A20-sleeping-symbol-read/plan-review.codex.jsonl`
- Controller record:
  `.change-loop/fresh-suite/A20-sleeping-symbol-read/plan-review.controller.status.json`

## Overall assessment

The independent reviewer verified the requested charter and plan hashes, inspected the current
working tree, relevant implementation/wiring/tests, and judged the plan sound. It identified the
following execution checks as the important adversarial risks. The reviewer attempted to persist
this report itself, but its evidence-only session enforced read-only writes; the main model
transcribed the review from the immutable JSONL/last-message evidence rather than launching a
second reviewer or changing the plan.

## Numbered execution risks and test targets

1. **Lifecycle ordering and state preservation.** A superficially correct patch could read before
   halt completes, resume an already halted target, omit resume after a read failure, or return
   before restoration succeeds. Tests must assert the exact ordered calls for halted, running, and
   sleeping states, plus every primary/cleanup failure path.
2. **Dual-error truthfulness.** A cleanup implementation could replace the read error with the
   resume error or swallow the resume error. Tests must prove the primary read failure remains
   identifiable and the restoration failure type/message is also visible when both occur.
3. **Generic state handling.** A fixture-derived implementation could special-case `SLEEPING` or
   assume exact provider casing. Tests must use case variants and at least one other non-halted
   state, while source inspection must reject board/MCU/probe/OS/toolchain branches.
4. **Production wiring and constructor blast radius.** Adding lifecycle callables can leave the
   server constructor or existing test fakes stale. Regression coverage must exercise the
   production `target_control` wiring and every `MemoryToolServices` constructor surface.
5. **Adjacent-tool isolation.** Reusing an overly broad helper could accidentally halt targets for
   raw address/block reads, writes, or explicit execution actions. Regression tests must show
   those paths retain their prior call sequences and behavior.
6. **Published help and honest result boundary.** Updating only an internal comment would not
   change MCP help, and recording success before resume would fabricate a completed operation.
   Tests must inspect the handler description actually supplied to FastMCP and prove that a
   restoration failure cannot yield or record a successful symbol result.

## Nonblocking implementation guidance

- Keep the helper local and explicit; do not broaden it into a target transaction framework.
- A genuine coherent zero is valid. The repair is lifecycle correctness, not value heuristics.
- The one-time review is complete. Do not rewrite or re-review the plan unless execution proves a
  genuine plan mistake under the recorded amendment procedure.

## Integrity statement

The reviewer did not edit the plan, production source, or tests. The main transcription above does
not alter the review's assessment or create a second review.

## Post-implementation evidence review — not a second plan review

- Reviewer identity: `A20-diff-adversarial-reviewer-001`
- Reviewer thread: `019fa770-05b1-7e80-bcb3-56bf1d0ca3b1`
- Model/settings: `gpt-5.6-terra`, medium reasoning, `priority` / Fast service tier
- Immutable review log:
  `.change-loop/fresh-suite/A20-sleeping-symbol-read/diff-review.codex.jsonl`
- Last message:
  `.change-loop/fresh-suite/A20-sleeping-symbol-read/diff-review.last-message.md`
- Reviewed charter SHA-256:
  `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`
- Reviewed plan SHA-256:
  `a974e693dd8b16d03d993b27b0c16f1113891482de58e67160608b2cc6da0a07`

The independent post-gate diff review returned `NEEDS_FIX`. The main model inspected the cited
source, analogous helper, plan, and charter and accepted both findings as implementation/test
gaps within the unchanged plan:

1. **Guaranteed cleanup for non-ordinary failures.** The current helper catches only
   `Exception`. A `BaseException` such as synchronous cancellation/interrupt raised after the
   inserted halt bypasses `resume`, contradicting CL-001's guaranteed cleanup requirement. The
   smallest repair must attempt exactly one resume after every failure from the read, preserve and
   re-raise the primary failure when restoration succeeds, and preserve both primary and
   restoration facts when restoration also fails. Add an objective test using a non-`Exception`
   primary failure. Do not swallow cancellation/interrupt or broaden lifecycle behavior.
2. **Complete published parameter/example help.** The current docstring explains the lifecycle,
   return, failures, and recovery but does not explicitly explain `board_id`, `symbol`, `width`,
   and optional `elf_artifact`, nor give an invocation example. Extend only this published
   docstring and its exact help assertion, preserving the public schema and concise MCP-facing
   form.

These are execution corrections under CL-001/CL-002, not evidence of a plan mistake. The plan is
not amended or re-reviewed. All earlier numbered risks and scope exclusions remain binding.
