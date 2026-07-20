# Universal native build command — implementation plan

1. Delete native-build provider detection, fixed SDK/toolchain-root discovery, target synthesis, and
   provider-specific artifact conventions. Require exact argv after `--`.
2. Keep optional cwd, repeatable environment overrides, arbitrary repeatable named artifact paths,
   caller-adjustable timeout, and opt-in offline mode on the one generic path. Execute argv directly
   through the owned-process runner and inherit the environment by default.
3. Use only content-based loadable-ELF detection (excluding relocatable objects), linker-map
   uniqueness, and optional Intel HEX validation. Permit existing/in-source output roots and
   explicit output paths anywhere.
4. Remove named-provider fields and templates from evidence and guidance. Teach the agent to inspect
   project files, prefer compatible installed resources, and acquire missing resources normally.
5. Remove the provider-specific Zephyr command from normative product documentation and host setup.
   Retain its legacy console entry point only while repository benchmark fixtures still invoke it;
   it must never appear in MCP guidance or the normal workflow.
6. Add non-redundant tests for missing argv refusal, arbitrary argv, cwd/env, arbitrary opaque
   outputs, explicit and extension-independent ELF artifacts, adjustable timeout, network defaults,
   offline opt-in, and recovery errors.
7. Run focused suites, Ruff, Pyright, and full pytest. Then run a fresh GPT 5.6 Terra high/fast
   read-only diff audit, vet every finding, and repeat until clean.
