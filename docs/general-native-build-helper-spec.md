# General Native Build Helper Specification

## Problem

`get_setup_status.build_guidance` currently describes a native build but does not return an exact
general helper command. The live acceptance requires a provider-neutral helper that selects an
already-installed local toolchain, never provisions build dependencies itself, applies standard
offline guards to the native child build, and does not invoke the legacy Zephyr-specific helper.

## Required behavior

- Add `python -m pyocd_debug_mcp.native_build` and `pyocd-native-build` entry points.
- Accept only a project directory, a new/empty build directory, and a project-native target.
- Detect the build provider from project files. Initially support Zephyr applications through the
  ordinary local `west build` CLI, while keeping provider selection inside the general helper.
- Discover complete local NCS installations and their toolchain `environment.json` on Windows,
  Linux, and macOS. Bind the workspace, toolchain metadata, and `west` executable to one coherent
  install; fail on ambiguity. Never download, install, upgrade, or mutate a toolchain.
- Reject source/build overlap, nonempty build directories, missing local environments, ambiguous
  providers, and malformed environment metadata before starting a build.
- Apply offline guards to the child build and emit machine-readable evidence containing the
  selected provider, local workspace/toolchain, exact child argv, exit code, and coherent
  ELF/HEX/map paths (including sysbuild default-domain resolution).
- Own the native process group for the complete build lifetime. Timeout, cancellation, and other
  abnormal helper exits must terminate and reap the complete descendant group before its recovery
  marker is removed. Normal leader exit must also clear any background descendants. Use a
  kill-on-close Job Object on Windows and verified process-group cleanup on POSIX; retain a
  cwd-independent marker using a cross-platform process-birth identity (without assuming Linux
  `/proc`) and fail explicitly when cleanup cannot be confirmed. A POSIX marker whose leader
  identity is already gone must be retained for operator recovery, never treated as authority to
  signal a potentially reused numeric process group.
- Bind every marker to its owning server/helper PID and birth token. Concurrent live owners must
  never clean one another's processes; startup recovery is allowed only after the recorded owner is
  demonstrably gone or reused.
- Treat live-owner skips as benign, but abort server startup with the retained marker location when
  orphan cleanup, identity validation, or the bounded hygiene pass cannot be completed.
- Every managed operation must surface an explicit cleanup failure when its subprocess descendants
  cannot be confirmed stopped. Chain any existing handler, timeout, or cancellation error as the
  original cause rather than hiding either failure.
- State the network boundary truthfully: project-owned build scripts remain arbitrary code, so live
  acceptance must also inspect the run for attempted downloads rather than claiming an OS network
  sandbox the cross-platform helper does not provide.
- Return this general helper command from `get_setup_status.build_guidance`. Do not return or invoke
  `pyocd_debug_mcp.zephyr_build` as the normal path.
- Return the resolved local workspace, toolchain metadata, and build executable alongside the exact
  argv template so an agent can inspect local headers/project files without guessing or scanning.

## Safety boundary

The helper is a local build utility, not an MCP hardware action. It grants no ranges, plan,
permission, gate, safety-map authority, or flash authorization.
