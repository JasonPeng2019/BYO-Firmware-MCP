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
2. **Convenience detection, never a ceiling.** Existing Zephyr/west and GNU Make detection may stay
   as optional shortcuts. An unrecognized project must explain how to provide argv; it must not say
   the project is unsupported. An explicit argv always bypasses named detection and named
   environment selection.
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
   root and may already exist; explicit ELF/map and optional HEX paths may name outputs elsewhere.
   The helper never deletes or cleans project files. It owns the child process and timeout and
   reports the exact argv/cwd/network policy/exit code. ELF and optional Intel HEX formats are
   verified. Because linker-map formats have no universal machine-verifiable identity or ELF-binding
   field, the helper reports whether the map was agent-declared, provider-conventional, or uniquely
   discovered and never claims semantic coherence it cannot prove. Without explicit paths,
   discovery recognizes ELF content independent of filename extension and requires one unambiguous
   linker map below the search root. Auto-detected convenience providers may retain their clean-output
   contract because the agent can always choose exact argv.
6. **No authority expansion.** Build commands and reported artifacts remain advisory host-side
   evidence. Collection, flash planning, live identity, containment, and hardware permissions stay
   separate.
7. **Usable recovery.** Tool guidance documents both modes, parameters, examples, returns, and
   common recovery: inspect project files, supply exact argv/cwd/env/artifacts, allow network when a
   compatible local dependency is absent, or opt into offline mode only when appropriate.

## Assumption

The user and agent are cooperative firmware engineers. Host build scripts are ordinary project
code, not hardware authority. The helper should report and contain their process execution, not
attempt to predict every build tool or forbid normal dependency installation.

## Acceptance

- An otherwise unrecognized project builds through an arbitrary argv without calling provider
  detection or server-specific environment discovery.
- Default execution preserves network-related environment values; `--offline` applies the existing
  offline guard intentionally.
- Explicit cwd/env/artifact paths and existing/in-source output layouts are honored and reported.
- Auto-detected Zephyr and Make still work as conveniences.
- Setup status, handshake, README, architecture, and agent contract teach the universal path.
- Focused tests, Ruff, Pyright, full pytest, and adversarial diff review are green.
