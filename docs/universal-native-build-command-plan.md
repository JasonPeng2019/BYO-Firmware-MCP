# Universal native build command — implementation plan

1. Extend `native_build` CLI with argv-after-`--`, optional cwd, repeatable environment overrides,
   explicit artifact paths, and opt-in offline mode.
2. Route explicit argv directly through the existing owned-process runner and inherited environment;
   bypass all named provider/environment logic.
3. Generalize artifact discovery to loadable ELF headers (excluding relocatable objects) plus
   linker-map uniqueness. Permit generic commands
   to use existing/in-source output roots and explicit output paths anywhere, while retaining the
   clean output contract and exact Zephyr domain handling only for named conveniences.
4. Make network inheritance the default for every mode; apply offline environment/CMake flags only
   when requested.
5. Reframe command templates and all normative guidance so agent-resolved argv is the universal
   path and Zephyr/Make are conveniences.
6. Add non-redundant tests for unknown build systems, arbitrary argv, cwd/env, explicit and
   extension-independent artifacts, network defaults, offline opt-in, and recovery errors.
7. Run focused suites, Ruff, Pyright, and full pytest. Then run a fresh GPT 5.6 Terra high/fast
   read-only diff audit, vet every finding, and repeat until clean.
