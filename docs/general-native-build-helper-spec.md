# General Native Build Helper Specification

## Problem

`get_setup_status.build_guidance` currently describes a native build but does not return an exact
general helper command. The live acceptance requires a provider-neutral helper that selects an
already-installed local toolchain, never downloads dependencies, and does not invoke the legacy
Zephyr-specific helper.

## Required behavior

- Add `python -m pyocd_debug_mcp.native_build` and `pyocd-native-build` entry points.
- Accept only a project directory, a new/empty build directory, and a project-native target.
- Detect the build provider from project files. Initially support Zephyr applications through the
  ordinary local `west build` CLI, while keeping provider selection inside the general helper.
- Discover complete local NCS installations and their toolchain `environment.json` on Windows,
  Linux, and macOS. Never download, install, upgrade, or mutate a toolchain.
- Reject source/build overlap, nonempty build directories, missing local environments, ambiguous
  providers, and malformed environment metadata before starting a build.
- Emit machine-readable evidence containing the selected provider, local workspace/toolchain,
  exact child argv, exit code, and produced ELF/HEX/map paths.
- Return this general helper command from `get_setup_status.build_guidance`. Do not return or invoke
  `pyocd_debug_mcp.zephyr_build` as the normal path.

## Safety boundary

The helper is a local build utility, not an MCP hardware action. It grants no ranges, plan,
permission, gate, safety-map authority, or flash authorization.

