# Main-model plan amendments

## H01-BS-A1 — Validate and invoke each tool exactly once

- Author: root main/orchestrating model
- Timestamp: `2026-07-25T03:45Z`
- Applies to plan SHA-256:
  `ca11a4c2775ce75c7f0ac92369679506a5c0d62e1c17193aaf8b3ed356246f72`
- Evidence: the one independent read-only plan review by
  `/root/h01_batch_strict_plan_review` returned `BLOCK` because CL-002's separate
  validation-only pass followed by ordinary `Tool.run(raw_arguments)` would repeat FastMCP
  pre-parsing and every Pydantic/model validator on valid calls.
- Reason: validation must precede an unlocked plan guard, but executing validators twice is not a
  compatible or side-effect-free assumption. This amendment resolves that genuine plan defect
  before implementation. It does not alter the verified server behavior requested by CL-001,
  CL-003, CL-004, or CL-005.

The following rules supersede conflicting CL-002 wording and extend CL-005's verification:

1. **Exactly one parse/validation pass.** After the registry-lock check and tool lookup, run the
   selected registered tool metadata's ordinary `pre_parse_json()` and Pydantic
   `arg_model.model_validate()` exactly once. Retain both:
   - the caller's raw mapping for the existing board lookup, immutable plan guard, timeout, and
     finalizer contracts where those currently consume raw input; and
   - the one validated/one-level-dumped argument mapping for the eventual function invocation.
   Do not call `Tool.run(raw_arguments)` afterward, because that would validate again.
2. **Invoke the validated result without semantic drift.** Add the smallest private
   `RegistryFastMCP` invocation path that accepts the already validated mapping and preserves the
   pinned `Tool.run(..., convert_result=True)` observable contract:
   - inject the MCP context only under the tool's declared context parameter;
   - invoke async handlers asynchronously and sync handlers through the existing managed sync
     dispatch/thread path;
   - apply the same `fn_metadata.convert_result()` conversion exactly once;
   - preserve the pinned special propagation of `UrlElicitationRequiredError`;
   - wrap every other handler exception as the same `ToolError("Error executing tool <name>: ...")`
     shape before the existing Layer-2 safe-exit wrapper; and
   - never execute the function until the unlocked plan guard and every existing managed-dispatch
     precondition has succeeded.
3. **Preserve execution ordering.** The required order is:
   registry lock → one metadata pre-parse/model validation → finalizer resolution/context capture
   and managed-dispatch setup → board reservation/lock → plan/gate guard → handler invocation with
   the already validated mapping → one result conversion. Validation failure performs no plan,
   handler, managed resource, notification, finalizer, or provider work. Lock failure still
   performs no schema validation. Valid exact calls perform every stage once.
4. **No model-validator side effects.** Add a deterministic registered model/tool fixture with a
   validator counter and a handler counter. Prove a valid direct call and a valid batch child each
   run the metadata pre-parser/model validator exactly once and the handler exactly once; malformed
   input runs validation once and the handler zero times; a locked request runs both zero times.
   Also prove generated-plan populated-permission and literal-text validators retain their exact
   accepted behavior and are not duplicated.
5. **Retain raw-versus-validated compatibility controls.** Exercise a string-encoded non-text
   argument accepted by FastMCP's pre-parser and a JSON-looking literal plan-text argument
   preserved by `_PlanToolMetadata`. Prove the handler receives the once-validated value while the
   immutable plan guard continues to compare the same raw request semantics it compared before
   this repair. Reject any implementation that reparses, redumps, or substitutes validated values
   into an established raw guard/timeout/finalizer contract without an explicit regression proof.
6. **Add a wire-level MCP proof.** In addition to direct registered-boundary unit tests, use the
   repository's in-process MCP transport or a bounded raw stdio server/client test to issue real
   `tools/call` requests. Prove:
   - outer, child-envelope, and child-argument extras return wire-level `isError: true`;
   - a child refusal returns `isError: true` with exactly one recoverable structured
     `batch_failed` JSON object, the completed prefix and child fields intact, and exactly one
     safe-exit reminder;
   - a successful batch returns `isError: false`;
   - an exact nested child relock sends exactly one `notifications/tools/list_changed`; and
   - no later request inherits validation or notification nesting state after failure or
     cancellation.
   Direct `Tool.run()`/`RegistryFastMCP.call_tool()` exception assertions alone are insufficient
   for these wire-level result and notification claims.
7. **Scope remains fixed.** Production edits remain limited to
   `src/pyocd_debug_mcp/kernel/registry.py` and
   `src/pyocd_debug_mcp/tools/batch.py`. Do not edit the pinned SDK, plan metadata module,
   accepted files, configuration, dependencies, lock, fresh H01 run, or hardware behavior.

