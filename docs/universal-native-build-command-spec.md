# Universal native build command — specification

## Problem

The provider-neutral build entry point is not actually universal. It recognizes only Zephyr/west
and GNU Make, rejects every other project shape, requires one of two server-selected local
environments, and unconditionally disables network access. That turns two useful conveniences into
a product ceiling and makes every new build system require a server source change.

## Required behavior

1. **Agent-resolved command path.** The helper must accept an exact argv supplied by the agent after
   it has read the project's own build files and discovered the real tools/environment. The helper
   executes the argv directly without a shell. PlatformIO, ESP-IDF, CMake/Ninja, Cargo, SCons,
   Bazel, IAR, Keil, vendor CLIs, wrappers, and future tools all use this same path without a new
   provider branch.
2. **No server-selected provider.** The general helper must not inspect project markers to select a
   build provider, SDK, compiler, target name, or installation root. There is one execution path:
   exact agent-supplied argv. Zephyr, Make, and every other build system are peers on that path.
   Provider-specific helpers may not appear in setup/build guidance or the normal product workflow
   because their defaults bias agents toward one ecosystem. A legacy repository-fixture command may
   remain temporarily for existing benchmark assets, but it is not advertised to agents and must
   not influence general helper behavior.
3. **Agent-owned environment.** The generic path inherits the invocation environment, permits
   explicit `KEY=VALUE` overrides, and accepts an explicit working directory. It must not assume an
   OS, SDK root, compiler family, or build-system layout.
4. **Local-first, network-capable.** Network access is inherited by default. Guidance tells the
   agent to prefer a compatible local SDK/toolchain, but permits normal dependency/toolchain
   acquisition when none is usable. Offline guards are an explicit `--offline` choice only; they
   are never silently forced.
5. **Arbitrary output layouts and honest evidence.** The generic command path must not require a
   clean build or force outputs into a server-selected directory: incremental trees, in-source
   builds, IDE layouts, and fixed vendor output paths are valid. `--build-dir` is the artifact-search
   root and may already exist. Repeatable named output declarations may identify any project-native
   artifact (ELF, map, HEX, BIN, UF2, signed image, vendor container, or a future format) anywhere;
   the existing typed flags remain compatibility shorthands. The helper verifies every declared
   output exists and is nonempty, applies structural checks only to formats it actually understands,
   and never claims semantic validation for opaque formats. The helper never deletes or cleans
   project files. It owns the child process with a caller-adjustable positive timeout and reports
   the exact argv/cwd/network policy/exit code. Because linker-map formats have no universal
   machine-verifiable identity or ELF-binding
   field, the helper reports whether the map was agent-declared or uniquely
   discovered and never claims semantic coherence it cannot prove. Without explicit paths,
   discovery recognizes ELF content independent of filename extension and requires one unambiguous
   linker map below the search root.
6. **No authority expansion.** Build commands and reported artifacts remain advisory host-side
   evidence. Collection, flash planning, live identity, containment, and hardware permissions stay
   separate.
7. **Usable recovery.** Tool guidance documents the single generic mode, parameters, examples,
   returns, and common recovery: inspect project files, supply exact argv/cwd/env/artifacts, prefer
   an already compatible local dependency, allow ordinary acquisition when none is usable, or opt
   into offline mode only when appropriate. Guidance must not manufacture a build command from the
   MCU, board, or datasheet; resolving the project's build entry point belongs to the agent.

## Assumption

The user and agent are cooperative firmware engineers. Host build scripts are ordinary project
code, not hardware authority. The helper should report and contain their process execution, not
attempt to predict every build tool or forbid normal dependency installation.

## Acceptance

- An otherwise unrecognized project builds through an arbitrary argv without calling provider
  detection or server-specific environment discovery.
- Default execution preserves network-related environment values; `--offline` applies the existing
  offline guard intentionally.
- Explicit cwd/env/arbitrary named artifact paths, adjustable timeouts, and existing/in-source
  output layouts are honored and reported.
- A Zephyr, Make, PlatformIO, ESP-IDF, plain CMake, Cargo, or vendor build reaches the same helper
  path solely by supplying its native argv; adding a build system requires no server edit.
- The general helper contains no named provider detector, SDK-root search, compiler search, target
  synthesis, or provider-specific artifact convention.
- Setup status, handshake, README, architecture, and agent contract teach the universal path.
- Focused tests, Ruff, Pyright, full pytest, and adversarial diff review are green.
