"""Provider-neutral, local-only firmware build launcher.

The launcher owns no hardware or safety authority. It detects the project's
native build provider, selects an already-installed local environment, and
executes exactly one native build command without provisioning dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from pyocd_debug_mcp.kernel.processes import run_owned


BUILD_TIMEOUT_SECONDS = 1800.0


@dataclass(frozen=True)
class LocalBuildEnvironment:
    provider: str
    workspace_dir: Path
    toolchain_env: Path | None
    executable: Path
    environment: dict[str, str]


def _default_install_root_values(platform_name: str) -> tuple[str, str]:
    platform_root = "C:/ncs" if platform_name == "nt" else "/opt/ncs"
    return ("~/ncs", platform_root)


def _candidate_install_roots(environ: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    env = os.environ if environ is None else environ
    explicit: list[Path] = []
    for key in ("NCS_INSTALL_ROOT", "NCS_ROOT"):
        if value := env.get(key):
            explicit.append(Path(value).expanduser())
    defaults = [Path(value).expanduser() for value in _default_install_root_values(os.name)]
    candidates = explicit or defaults
    unique: list[Path] = []
    for candidate in candidates:
        expanded = candidate.expanduser()
        if not expanded.is_absolute():
            raise RuntimeError(f"Local install root must be absolute: {candidate}")
        try:
            resolved = expanded.resolve()
        except OSError as exc:
            raise RuntimeError(f"Cannot resolve local install root: {candidate}") from exc
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def _find_ncs_workspace(roots: Sequence[Path]) -> Path:
    candidates: list[Path] = []
    for root in roots:
        if (root / ".west" / "config").is_file() and (root / "zephyr").is_dir():
            candidates.append(root)
        if root.is_dir():
            candidates.extend(
                child
                for child in root.iterdir()
                if child.is_dir()
                and (child / ".west" / "config").is_file()
                and (child / "zephyr").is_dir()
            )
    if not candidates:
        raise RuntimeError(
            "No complete local NCS workspace was found. Install NCS locally or set "
            "NCS_INSTALL_ROOT; this helper never downloads a workspace."
        )
    unique = set(candidates)
    if len(unique) != 1:
        choices = ", ".join(str(path) for path in sorted(unique))
        raise RuntimeError(
            "Multiple local NCS workspaces are visible in one install root; set "
            f"NCS_INSTALL_ROOT to one coherent install: {choices}"
        )
    return unique.pop()


def _find_toolchain_environment(roots: Sequence[Path]) -> Path:
    candidates: list[Path] = []
    for root in roots:
        direct = root / "environment.json"
        if direct.is_file():
            candidates.append(direct)
        toolchains = root / "toolchains"
        if toolchains.is_dir():
            candidates.extend(toolchains.glob("*/environment.json"))
    if not candidates:
        raise RuntimeError(
            "No complete local NCS toolchain environment was found. Install the NCS toolchain "
            "locally or set NCS_INSTALL_ROOT; this helper never downloads a toolchain."
        )
    unique = set(candidates)
    if len(unique) != 1:
        choices = ", ".join(str(path) for path in sorted(unique))
        raise RuntimeError(
            "Multiple local NCS toolchain environments are visible in one install root; set "
            f"NCS_INSTALL_ROOT to one coherent install: {choices}"
        )
    return unique.pop()


def _load_toolchain_environment(
    metadata_path: Path, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    try:
        document = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Malformed local toolchain environment: {metadata_path}") from exc
    entries = document.get("env_vars") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError(f"Malformed local toolchain environment: {metadata_path}")

    result = dict(os.environ if environ is None else environ)
    base = metadata_path.parent.resolve()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            raise RuntimeError(f"Malformed local toolchain environment: {metadata_path}")
        key = entry["key"]
        kind = entry.get("type")
        if kind == "string" and isinstance(entry.get("value"), str):
            result[key] = entry["value"]
            continue
        values = entry.get("values")
        if kind != "relative_paths" or not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise RuntimeError(f"Malformed local toolchain environment: {metadata_path}")
        resolved = os.pathsep.join(str((base / value).resolve()) for value in values)
        treatment = entry.get("existing_value_treatment", "overwrite")
        existing = result.get(key, "")
        if treatment == "prepend_to":
            result[key] = os.pathsep.join(part for part in (resolved, existing) if part)
        elif treatment == "append_to":
            result[key] = os.pathsep.join(part for part in (existing, resolved) if part)
        elif treatment == "overwrite":
            result[key] = resolved
        else:
            raise RuntimeError(f"Malformed local toolchain environment: {metadata_path}")
    return result


def _explicit_executable(
    environ: Mapping[str, str], name: str, description: str
) -> Path | None:
    value = environ.get(name, "").strip()
    if not value:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise RuntimeError(f"{description} path in {name} must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{description} path in {name} does not exist: {candidate}") from exc
    if not _is_executable_file(resolved):
        raise RuntimeError(f"{description} path in {name} is not executable: {resolved}")
    return resolved


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))


def _explicit_tool_root(
    environ: Mapping[str, str], name: str, executable_name: str, description: str
) -> Path | None:
    """Resolve one exact executable from an explicit absolute toolchain root."""

    value = environ.get(name, "").strip()
    if not value:
        return None
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise RuntimeError(f"{description} root in {name} must be absolute")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{description} root in {name} does not exist: {root}") from exc
    if not resolved_root.is_dir():
        raise RuntimeError(f"{description} root in {name} is not a directory: {resolved_root}")
    names = (executable_name, f"{executable_name}.exe")
    candidates = {
        candidate.resolve()
        for base in (resolved_root, resolved_root / "bin")
        for candidate_name in names
        if _is_executable_file(candidate := base / candidate_name)
    }
    if len(candidates) != 1:
        raise RuntimeError(
            f"{description} root in {name} must contain exactly one {executable_name} "
            f"in the root or bin directory; found {len(candidates)}."
        )
    return next(iter(candidates))


def _vendor_tool_roots(environ: Mapping[str, str]) -> tuple[Path, ...]:
    explicit = [
        Path(value).expanduser()
        for key in ("STM32CUBEIDE_ROOT", "CUBEIDE_ROOT")
        if (value := environ.get(key, "").strip())
    ]
    if explicit:
        candidates = explicit
    elif os.name == "nt":
        candidates = [Path("C:/ST")]
        candidates.extend(
            Path(value) / "STMicroelectronics"
            for key in ("ProgramFiles", "ProgramFiles(x86)")
            if (value := environ.get(key, "").strip())
        )
    elif sys.platform == "darwin":
        candidates = [Path("/Applications"), Path.home() / "Applications"]
    else:
        candidates = [
            Path("/opt/st"),
            Path("/opt/STMicroelectronics"),
            Path.home() / "STMicroelectronics",
        ]
    unique: list[Path] = []
    for candidate in candidates:
        if not candidate.is_absolute():
            raise RuntimeError(f"Vendor tool root must be absolute: {candidate}")
        resolved = candidate.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def _bounded_vendor_tools(
    roots: Sequence[Path], patterns: Sequence[str]
) -> tuple[Path, ...]:
    matches: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        bases = [root]
        bases.extend(root.glob("STM32CubeIDE*/STM32CubeIDE"))
        bases.extend(root.glob("STM32CubeIDE*.app/Contents/Eclipse"))
        bases.extend(root.glob("stm32cubeide*"))
        if root.name.casefold().endswith(".app"):
            bases.append(root / "Contents" / "Eclipse")
        for base in bases:
            for pattern in patterns:
                matches.update(
                    path.resolve() for path in base.glob(pattern) if _is_executable_file(path)
                )
    return tuple(sorted(matches))


def _one_or_none(paths: Sequence[Path], description: str, variable: str) -> Path | None:
    unique = tuple(dict.fromkeys(paths))
    if len(unique) > 1:
        choices = ", ".join(str(path) for path in unique)
        raise RuntimeError(
            f"Multiple local {description} executables were found; set {variable} explicitly: "
            f"{choices}"
        )
    return unique[0] if unique else None


def _discover_make_environment(
    environ: Mapping[str, str] | None = None,
) -> LocalBuildEnvironment:
    child_env = dict(os.environ if environ is None else environ)
    make = _explicit_executable(child_env, "NATIVE_MAKE", "GNU Make")
    if make is None:
        found = next(
            (
                value
                for name in ("make", "gmake", "mingw32-make")
                if (value := shutil.which(name, path=child_env.get("PATH"))) is not None
            ),
            None,
        )
        make = Path(found).resolve() if found is not None else None
    roots = _vendor_tool_roots(child_env)
    if make is None:
        make = _one_or_none(
            _bounded_vendor_tools(
                roots,
                (
                    "plugins/com.st.stm32cube.ide.mcu.externaltools.make.*/tools/bin/make",
                    "plugins/com.st.stm32cube.ide.mcu.externaltools.make.*/tools/bin/make.exe",
                    "STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.make.*/tools/bin/make",
                    "STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.make.*/tools/bin/make.exe",
                ),
            ),
            "GNU Make",
            "NATIVE_MAKE",
        )
    if make is None:
        raise RuntimeError(
            "No local GNU Make was found. Install it, put it on PATH, or set NATIVE_MAKE; "
            "this helper never downloads build tools."
        )

    gcc = _explicit_tool_root(
        child_env, "ARM_GCC_ROOT", "arm-none-eabi-gcc", "Arm GCC"
    )
    if gcc is None:
        # Compatibility escape hatch for hosts that know the exact compiler
        # executable but do not have a conventional toolchain root.
        gcc = _explicit_executable(child_env, "ARM_GCC", "Arm GCC")
    if gcc is None:
        found_gcc = shutil.which("arm-none-eabi-gcc", path=child_env.get("PATH"))
        gcc = Path(found_gcc).resolve() if found_gcc is not None else None
    if gcc is None:
        gcc = _one_or_none(
            _bounded_vendor_tools(
                roots,
                (
                    "plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*/tools/bin/arm-none-eabi-gcc",
                    "plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*/tools/bin/arm-none-eabi-gcc.exe",
                    "STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*/tools/bin/arm-none-eabi-gcc",
                    "STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*/tools/bin/arm-none-eabi-gcc.exe",
                ),
            ),
            "Arm GCC",
            "ARM_GCC_ROOT or ARM_GCC",
        )
    path_parts = [str(make.parent)]
    if gcc is not None:
        path_parts.insert(0, str(gcc.parent))
    if existing := child_env.get("PATH"):
        path_parts.append(existing)
    child_env["PATH"] = os.pathsep.join(path_parts)
    return LocalBuildEnvironment(
        provider="gnu-make",
        workspace_dir=make.parent,
        toolchain_env=gcc,
        executable=make,
        environment=child_env,
    )


def discover_local_environment(
    *, provider: str = "zephyr-west", environ: Mapping[str, str] | None = None
) -> LocalBuildEnvironment:
    if provider == "gnu-make":
        return _discover_make_environment(environ)
    if provider != "zephyr-west":
        raise RuntimeError(f"Unsupported native build provider: {provider}")
    roots = _candidate_install_roots(environ)
    pairs: list[tuple[Path, Path]] = []
    for root in roots:
        try:
            pair = (_find_ncs_workspace((root,)), _find_toolchain_environment((root,)))
        except (OSError, RuntimeError):
            continue
        if pair not in pairs:
            pairs.append(pair)
    if len(pairs) != 1:
        choices = ", ".join(f"{workspace} + {metadata}" for workspace, metadata in pairs)
        raise RuntimeError(
            "A single coherent local NCS workspace/toolchain pair was not found. Set "
            f"NCS_INSTALL_ROOT to one complete install. Candidates: {choices or 'none'}"
        )
    workspace, metadata = pairs[0]
    child_env = _load_toolchain_environment(metadata, environ)
    executable_value = shutil.which("west", path=child_env.get("PATH"))
    if executable_value is None:
        raise RuntimeError(f"The local toolchain environment does not contain west: {metadata}")
    executable = Path(executable_value).resolve()
    if not executable.is_relative_to(metadata.parent.resolve()):
        raise RuntimeError(
            "The selected toolchain does not contain its own west executable; refusing a global "
            f"PATH fallback: {executable}"
        )
    return LocalBuildEnvironment(
        provider="zephyr-west",
        workspace_dir=workspace,
        toolchain_env=metadata,
        executable=executable,
        environment=child_env,
    )


def detect_provider(project_dir: Path) -> str:
    cmake_path = project_dir / "CMakeLists.txt"
    if (project_dir / "prj.conf").is_file() and cmake_path.is_file():
        try:
            cmake = cmake_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"Cannot read project metadata: {cmake_path}") from exc
        if "find_package(Zephyr" in cmake:
            return "zephyr-west"
    if (project_dir / "Makefile").is_file():
        return "gnu-make"
    raise RuntimeError(
        "No supported native build provider was detected from the project files. "
        "The local-only helper recognizes Zephyr CMake applications and ordinary Makefiles."
    )


def _validate_paths(project_value: str, build_value: str) -> tuple[Path, Path]:
    project_dir = Path(project_value).expanduser().resolve()
    build_dir = Path(build_value).expanduser().resolve()
    if not project_dir.is_dir():
        raise RuntimeError(f"Project directory does not exist: {project_dir}")
    root = Path(build_dir.anchor).resolve()
    if build_dir in (root, Path.home().resolve()):
        raise RuntimeError("Build directory must be a dedicated non-root, non-home directory.")
    if project_dir == build_dir or project_dir.is_relative_to(build_dir):
        raise RuntimeError("Build directory must not equal or contain the project directory.")
    if build_dir.exists() and any(build_dir.iterdir()):
        raise RuntimeError(
            f"Build directory must be new or empty; preserve existing contents: {build_dir}"
        )
    return project_dir, build_dir


def _validate_target(value: str) -> str:
    target = value.strip()
    if (
        not target
        or target.startswith(("-", "/", "\\"))
        or "=" in target
        or ".." in target.replace("\\", "/").split("/")
    ):
        raise RuntimeError("Target must be a nonempty project-native target name.")
    return target


def build_command(
    environment: LocalBuildEnvironment,
    *,
    project_dir: Path,
    build_dir: Path,
    target: str,
) -> list[str]:
    if environment.provider == "zephyr-west":
        return [
            str(environment.executable),
            "build",
            "--board",
            target,
            "--build-dir",
            str(build_dir),
            str(project_dir),
            "--",
            "-DFETCHCONTENT_FULLY_DISCONNECTED=ON",
            "-DFETCHCONTENT_UPDATES_DISCONNECTED=ON",
        ]
    if environment.provider == "gnu-make":
        return [
            str(environment.executable),
            "-C",
            str(project_dir),
            f"BUILD_DIR={build_dir}",
            target,
        ]
    raise RuntimeError(f"Unsupported native build provider: {environment.provider}")


def _artifact_paths(
    build_dir: Path,
    provider: str = "zephyr-west",
    *,
    expected_root: Path | None = None,
) -> dict[str, str | None]:
    root = expected_root or build_dir.resolve(strict=True)
    if build_dir.is_symlink() or build_dir.resolve(strict=True) != root:
        raise RuntimeError("Fresh build directory was replaced or redirected during the build.")
    if provider == "gnu-make":
        elves = tuple(
            path.resolve()
            for path in build_dir.rglob("*.elf")
            if path.is_file() and path.resolve().is_relative_to(root)
        )
        if len(elves) != 1:
            raise RuntimeError(
                "Native Make build must produce exactly one ELF below the fresh build directory; "
                f"found {len(elves)}."
            )
        elf = elves[0]
        maps = tuple(
            path.resolve()
            for path in build_dir.rglob("*.map")
            if path.is_file() and path.resolve().is_relative_to(root)
        )
        if len(maps) != 1:
            raise RuntimeError(
                "Native Make build must produce exactly one linker map; "
                f"found {len(maps)}."
            )
        hexes = tuple(
            path.resolve()
            for path in build_dir.rglob("*.hex")
            if path.is_file()
            and path.resolve().is_relative_to(root)
            and path.stem == elf.stem
        )
        if len(hexes) > 1:
            raise RuntimeError("Native Make build produced multiple same-stem HEX artifacts.")
        return {
            "elf": str(elf),
            "hex": str(hexes[0]) if hexes else None,
            "map": str(maps[0]),
        }
    if provider != "zephyr-west":
        raise RuntimeError(f"Unsupported native build provider: {provider}")
    artifact_dir = build_dir / "zephyr"
    domains_path = build_dir / "domains.yaml"
    if domains_path.exists():
        try:
            document = yaml.safe_load(domains_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise RuntimeError(f"Cannot read native build domain metadata: {domains_path}") from exc
        default = document.get("default") if isinstance(document, dict) else None
        domains = document.get("domains") if isinstance(document, dict) else None
        matches = (
            [item for item in domains if isinstance(item, dict) and item.get("name") == default]
            if isinstance(default, str) and isinstance(domains, list)
            else []
        )
        if len(matches) != 1 or not isinstance(matches[0].get("build_dir"), str):
            raise RuntimeError(f"Invalid native build domain metadata: {domains_path}")
        raw_domain_dir = Path(matches[0]["build_dir"]).expanduser()
        domain_dir = (
            raw_domain_dir.resolve()
            if raw_domain_dir.is_absolute()
            else (build_dir / raw_domain_dir).resolve()
        )
        if not domain_dir.is_relative_to(root):
            raise RuntimeError(f"Native build domain escapes the build directory: {domain_dir}")
        artifact_dir = domain_dir / "zephyr"

    return {
        role: str(path.resolve()) if path.is_file() else None
        for role, path in {
            "elf": artifact_dir / "zephyr.elf",
            "hex": artifact_dir / "zephyr.hex",
            "map": artifact_dir / "zephyr.map",
        }.items()
    }


def _offline_environment(environment: Mapping[str, str]) -> dict[str, str]:
    result = dict(environment)
    result.update(
        {
            "PIP_NO_INDEX": "1",
            "UV_OFFLINE": "1",
            "CARGO_NET_OFFLINE": "true",
            "NPM_CONFIG_OFFLINE": "true",
            "GIT_TERMINAL_PROMPT": "0",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
            "http_proxy": "http://127.0.0.1:9",
            "https_proxy": "http://127.0.0.1:9",
            "all_proxy": "http://127.0.0.1:9",
            "no_proxy": "",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.proxy",
            "GIT_CONFIG_VALUE_0": "http://127.0.0.1:9",
            "GIT_CONFIG_KEY_1": "https.proxy",
            "GIT_CONFIG_VALUE_1": "http://127.0.0.1:9",
        }
    )
    return result


def run_build(args: argparse.Namespace) -> int:
    project_dir, build_dir = _validate_paths(args.project_dir, args.build_dir)
    target = _validate_target(args.target)
    provider = detect_provider(project_dir)
    environment = (
        discover_local_environment()
        if provider == "zephyr-west"
        else discover_local_environment(provider=provider)
    )
    if provider != environment.provider:
        raise RuntimeError(f"No complete local environment is available for provider {provider}.")
    build_dir.mkdir(parents=True, exist_ok=True)
    if build_dir.is_symlink():
        raise RuntimeError("Fresh build directory must not be a symbolic link.")
    created_build_root = build_dir.resolve(strict=True)
    if created_build_root != build_dir:
        raise RuntimeError("Fresh build directory resolved somewhere unexpected.")
    argv = build_command(
        environment,
        project_dir=project_dir,
        build_dir=build_dir,
        target=target,
    )
    completed = run_owned(
        argv,
        cwd=environment.workspace_dir if provider == "zephyr-west" else project_dir,
        env=_offline_environment(environment.environment),
        check=False,
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    if build_dir.is_symlink() or build_dir.resolve(strict=True) != created_build_root:
        raise RuntimeError("Fresh build directory was replaced or redirected during the build.")
    artifacts = (
        _artifact_paths(build_dir, provider, expected_root=created_build_root)
        if completed.returncode == 0
        else {"elf": None, "hex": None, "map": None}
    )
    if completed.returncode == 0 and (artifacts["elf"] is None or artifacts["map"] is None):
        raise RuntimeError(
            "Native build succeeded without one coherent ELF and linker map in its selected domain."
        )
    evidence = {
        "schema_version": 1,
        "provider": provider,
        "workspace_dir": str(environment.workspace_dir),
        "toolchain_env": (
            str(environment.toolchain_env) if environment.toolchain_env is not None else None
        ),
        "argv": argv,
        "exit_code": completed.returncode,
        "artifacts": artifacts,
        "offline_guards": True,
        "helper_provisioning": False,
    }
    print(json.dumps(evidence, sort_keys=True))
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one native firmware build using an existing local environment only."
    )
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--build-dir", required=True)
    parser.add_argument("--target", required=True, help="Project-native board/target name")
    return parser


def command_template() -> dict[str, object]:
    argv = [
        sys.executable,
        "-m",
        "pyocd_debug_mcp.native_build",
        "--project-dir",
        "<project-dir>",
        "--build-dir",
        "<new-empty-build-dir>",
        "--target",
        "<project-native-target>",
    ]
    result: dict[str, object] = {
        "argv_template": argv,
        "powershell_template": subprocess.list2cmdline(argv),
        "provider_selection": "detected_from_project_files",
        "environment_selection": "complete_local_install_only",
        "offline_guards": True,
        "helper_provisioning": False,
    }
    try:
        selected = discover_local_environment()
    except (OSError, RuntimeError) as exc:
        result["resolved_local_environment"] = {
            "status": "unavailable",
            "error": str(exc),
        }
    else:
        result["resolved_local_environment"] = {
            "status": "ready",
            "provider": selected.provider,
            "workspace_dir": str(selected.workspace_dir),
            "toolchain_env": (
                str(selected.toolchain_env) if selected.toolchain_env is not None else None
            ),
            "build_executable": str(selected.executable),
        }
    environments: dict[str, object] = {
        "zephyr-west": result["resolved_local_environment"]
    }
    try:
        make = discover_local_environment(provider="gnu-make")
    except (OSError, RuntimeError) as exc:
        environments["gnu-make"] = {"status": "unavailable", "error": str(exc)}
    else:
        environments["gnu-make"] = {
            "status": "ready",
            "provider": make.provider,
            "workspace_dir": str(make.workspace_dir),
            "toolchain_env": str(make.toolchain_env) if make.toolchain_env is not None else None,
            "build_executable": str(make.executable),
        }
    result["resolved_local_environments"] = environments
    return result


def main() -> None:
    try:
        raise SystemExit(run_build(build_parser().parse_args()))
    except RuntimeError as exc:
        print(
            json.dumps({"error": str(exc), "offline_guards": True, "helper_provisioning": False}),
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
