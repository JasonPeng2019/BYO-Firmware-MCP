Authorized local firmware-server validation. Targets are limited to the named local
`BYO-Firmware-MCP` repository. This is a host-only, read-only plan review. No physical board,
serial device, remote target, or third-party system is in scope.

Act as the one-time independent adversarial reviewer of the main-authored server repair plan.
Your workspace is:

`C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP`

Read these files in full before judging:

1. `C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\.codex\design_charter.md`;
2. `.change-loop\fresh-suite\H05-marker-unlink\changes.md`;
3. `.change-loop\fresh-suite\H05-marker-unlink\plan.md`;
4. the production source and existing tests named by the plan; and
5. the current Git diff, so the accepted pre-existing H05 cancellation repair is not mistaken for
   this proposed slice.

The exact reviewed plan SHA-256 is:
`2b1107796f7a5437304e4a440279b589fbdd2f7c8933ac599d08aa5f5a82186a`.

Review the plan, not the implementation. Do not edit any file, generate a replacement plan, operate
hardware, start a server, or run a command with side effects. Evaluate whether CL-001 is
implementation-ready and whether its tests can distinguish the intended retained-marker cleanup
failure from ordinary diagnostic graceful-close errors, complete invalidation, unconfirmed
termination, and marker-only retry.

Return:

- the plan SHA;
- a verdict of `READY` or `RISKS`;
- numbered, concrete execution risks and required test targets;
- any charter conflict, unnecessary complexity, overbroad scope, missing preservation contract, or
  ambiguous state transition.

Do not demand repeated review. Findings will be accepted as execution/test targets unless they
prove a genuine plan mistake. Return only after the review is complete.
