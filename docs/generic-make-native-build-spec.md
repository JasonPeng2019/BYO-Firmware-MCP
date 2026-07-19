# Generic Make Native-Build Provider Specification

## Problem

The provider-neutral `native_build` entry point detects only Zephyr/west projects. A fresh
bare-metal or RTOS firmware repository with an ordinary Makefile cannot use the server-returned
general helper even when a complete local compiler and GNU Make are already installed.

## Required behavior

- Detect a non-Zephyr project containing `Makefile` as provider `gnu-make` (the stable provider
  identifier; ordinary Make-compatible implementations are supported).
- Preserve the existing command shape. Interpret `--target` as the project-native Make target and
  pass the fresh build directory through the conventional `BUILD_DIR` variable.
- Resolve an existing GNU Make without downloading or provisioning. Prefer explicit
  `NATIVE_MAKE`; otherwise use PATH; on supported desktop platforms, boundedly discover vendor IDE
  tool directories. Explicit paths must be executable on POSIX. Prefer explicit `ARM_GCC_ROOT`
  (or exact-executable compatibility override `ARM_GCC`), then PATH, then bounded vendor-IDE discovery
  for an optional `arm-none-eabi` toolchain and prepend only its bin directory.
- Reject missing/ambiguous executables, unsafe targets, nonempty build roots, and source/build
  overlap before the child starts.
- Reuse existing offline environment guards and managed process-tree cleanup.
- Discover exactly one ELF and map below the fresh build root, with an optional same-stem HEX.
  Reject missing or ambiguous outputs rather than guessing.
- Report provider, exact argv, selected executables/toolchain, artifacts, offline guards, and
  no-provisioning status in the same machine-readable result.
- Do not make ordinary Zephyr builds harder or change their command, discovery, or artifacts.
- Remain board/RTOS/vendor neutral: STM32CubeIDE is one discoverable local tool source, not a
  required project format or hard dependency.

## Safety and usability

This build helper grants no hardware authority. Normal Make firmware uses one ordinary command and
needs no extra MCP step. Explicit environment variables resolve unusual installations without
hardcoded host paths.

