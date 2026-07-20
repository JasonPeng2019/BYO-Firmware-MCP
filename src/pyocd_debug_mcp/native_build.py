"""Provider-neutral firmware build process launcher.

The launcher owns no hardware or safety authority. An agent may provide the
project's exact argv, cwd, environment, and expected outputs for any build
system. Zephyr/west and GNU Make detection remain optional conveniences, not a
closed provider list. Network access is inherited unless the caller explicitly
requests offline execution.
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
from typing import Mapping, Sequence, cast

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


class BuildEvidenceError(RuntimeError):
    """A post-execution failure with enough evidence to diagnose the build."""

    def __init__(self, message: str, evidence: Mapping[str, object]) -> None:
        super().__init__(message)
        self.evidence = dict(evidence)


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


def detect_provider(project_dir: Path) -> str | None:
    """Detect a convenience provider, or return ``None`` for agent resolution."""

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
    return None


def _validate_paths(
    project_value: str, build_value: str, *, require_fresh_build: bool = True
) -> tuple[Path, Path]:
    project_dir = Path(project_value).expanduser().resolve()
    build_dir = Path(build_value).expanduser().resolve()
    if not project_dir.is_dir():
        raise RuntimeError(f"Project directory does not exist: {project_dir}")
    root = Path(build_dir.anchor).resolve()
    if require_fresh_build and build_dir in (root, Path.home().resolve()):
        raise RuntimeError("Build directory must be a dedicated non-root, non-home directory.")
    if require_fresh_build and (project_dir == build_dir or project_dir.is_relative_to(build_dir)):
        raise RuntimeError("Build directory must not equal or contain the project directory.")
    if build_dir.exists() and not build_dir.is_dir():
        raise RuntimeError(f"Build path exists but is not a directory: {build_dir}")
    if require_fresh_build and build_dir.exists() and any(build_dir.iterdir()):
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
    offline: bool = False,
) -> list[str]:
    if environment.provider == "zephyr-west":
        argv = [
            str(environment.executable),
            "build",
            "--board",
            target,
            "--build-dir",
            str(build_dir),
            str(project_dir),
        ]
        if offline:
            argv.extend(
                [
                    "--",
                    "-DFETCHCONTENT_FULLY_DISCONNECTED=ON",
                    "-DFETCHCONTENT_UPDATES_DISCONNECTED=ON",
                ]
            )
        return argv
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
        raise RuntimeError("Artifact search root was replaced or redirected during the build.")
    if provider in {"gnu-make", "agent-command"}:
        files = tuple(
            path
            for path in build_dir.rglob("*")
            if path.is_file() and path.resolve().is_relative_to(root)
        )
        elves = tuple(
            path.resolve()
            for path in files
            if _is_loadable_elf(path)
        )
        if len(elves) != 1:
            raise RuntimeError(
                "Native build must produce exactly one ELF below the artifact search root; "
                f"found {len(elves)}."
            )
        elf = elves[0]
        maps = tuple(
            path.resolve()
            for path in files
            if path.suffix.casefold() == ".map"
            and path.stat().st_size > 0
            and not _has_elf_magic(path)
        )
        if len(maps) != 1:
            raise RuntimeError(
                "Native build must produce exactly one linker map; "
                f"found {len(maps)}."
            )
        hexes = tuple(
            path.resolve()
            for path in files
            if path.suffix.casefold() == ".hex" and path.stem == elf.stem
        )
        if len(hexes) > 1:
            raise RuntimeError("Native build produced multiple same-stem HEX artifacts.")
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


def _has_elf_magic(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(4) == b"\x7fELF"
    except OSError as exc:
        raise RuntimeError(f"Cannot inspect build output: {path}") from exc


def _is_loadable_elf(path: Path) -> bool:
    """Return whether *path* has a complete loadable ELF structure."""

    try:
        with path.open("rb") as stream:
            header = stream.read(64)
            file_size = path.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"Cannot inspect build output: {path}") from exc
    if len(header) < 52 or header[:4] != b"\x7fELF" or header[6] != 1:
        return False
    elf_class = header[4]
    byte_order = "little" if header[5] == 1 else "big" if header[5] == 2 else None
    if byte_order is None or elf_class not in {1, 2}:
        return False
    elf_type = int.from_bytes(header[16:18], byte_order)
    machine = int.from_bytes(header[18:20], byte_order)
    version = int.from_bytes(header[20:24], byte_order)
    if elf_type not in {2, 3} or machine == 0 or version != 1:
        return False
    if elf_class == 1:
        expected_header_size = 52
        expected_program_header_size = 32
        program_offset = int.from_bytes(header[28:32], byte_order)
        header_size = int.from_bytes(header[40:42], byte_order)
        program_header_size = int.from_bytes(header[42:44], byte_order)
        program_count = int.from_bytes(header[44:46], byte_order)
    else:
        if len(header) < 64:
            return False
        expected_header_size = 64
        expected_program_header_size = 56
        program_offset = int.from_bytes(header[32:40], byte_order)
        header_size = int.from_bytes(header[52:54], byte_order)
        program_header_size = int.from_bytes(header[54:56], byte_order)
        program_count = int.from_bytes(header[56:58], byte_order)
    if (
        header_size != expected_header_size
        or program_header_size != expected_program_header_size
        or program_count == 0
        or program_offset < header_size
        or program_offset + (program_header_size * program_count) > file_size
    ):
        return False
    try:
        with path.open("rb") as stream:
            stream.seek(program_offset)
            for _ in range(program_count):
                program_header = stream.read(program_header_size)
                if len(program_header) != program_header_size:
                    return False
                if int.from_bytes(program_header[:4], byte_order) == 1:
                    return True
    except OSError as exc:
        raise RuntimeError(f"Cannot inspect build output: {path}") from exc
    return False


def _is_intel_hex(path: Path) -> bool:
    """Return whether *path* is a checksum-valid Intel HEX stream with EOF."""

    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Cannot inspect HEX build output: {path}") from exc
    saw_record = False
    saw_eof = False
    for line in lines:
        record = line.strip()
        if not record:
            continue
        if saw_eof or not record.startswith(":"):
            return False
        try:
            payload = bytes.fromhex(record[1:])
        except ValueError:
            return False
        if len(payload) < 5 or len(payload) != payload[0] + 5 or sum(payload) % 256:
            return False
        saw_record = True
        address = int.from_bytes(payload[1:3], "big")
        record_type = payload[3]
        if record_type not in {0, 1, 2, 3, 4, 5}:
            return False
        fixed_lengths = {1: 0, 2: 2, 3: 4, 4: 2, 5: 4}
        if record_type in fixed_lengths and (
            payload[0] != fixed_lengths[record_type] or address != 0
        ):
            return False
        if record_type == 1:
            saw_eof = True
    return saw_record and saw_eof


def _command_tokens(args: argparse.Namespace) -> list[str]:
    command = list(cast(Sequence[str], getattr(args, "command", ()) or ()))
    if command[:1] == ["--"]:
        command = command[1:]
    if any("\x00" in token for token in command):
        raise RuntimeError("Build argv must not contain NUL characters.")
    return command


def _environment_overrides(values: Sequence[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values:
        key, separator, setting = value.partition("=")
        if not separator or not key or "\x00" in key or "\x00" in setting:
            raise RuntimeError("Environment overrides must use nonempty KEY=VALUE form.")
        overrides[key] = setting
    return overrides


def _resolve_cwd(value: str | None, *, default: Path) -> Path:
    cwd = Path(value).expanduser().resolve() if value else default.resolve()
    if not cwd.is_dir():
        raise RuntimeError(f"Build working directory does not exist: {cwd}")
    return cwd


def _declared_artifacts(args: argparse.Namespace) -> dict[str, str | None] | None:
    values = {
        "elf": getattr(args, "artifact_elf", None),
        "hex": getattr(args, "artifact_hex", None),
        "map": getattr(args, "artifact_map", None),
    }
    if not any(values.values()):
        return None
    if not values["elf"] or not values["map"]:
        raise RuntimeError(
            "Explicit artifacts require both --artifact-elf and --artifact-map; HEX is optional."
        )
    return values


def _artifact_candidate(build_dir: Path, value: str) -> Path:
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else build_dir / raw
    return candidate.resolve()


def _validate_declared_artifacts(
    build_dir: Path,
    values: Mapping[str, str | None],
    *,
    expected_root: Path,
) -> dict[str, str | None]:
    if build_dir.is_symlink() or build_dir.resolve(strict=True) != expected_root:
        raise RuntimeError("Artifact search root was replaced or redirected during the build.")
    result: dict[str, str | None] = {}
    for role in ("elf", "hex", "map"):
        value = values.get(role)
        if value is None:
            result[role] = None
            continue
        candidate = _artifact_candidate(build_dir, value)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"Declared {role.upper()} artifact does not exist: {candidate}") from exc
        if not resolved.is_file():
            raise RuntimeError(
                f"Declared {role.upper()} artifact is not a file: {resolved}"
            )
        result[role] = str(resolved)
    declared_paths = [path for path in result.values() if path is not None]
    if len(set(declared_paths)) != len(declared_paths):
        raise RuntimeError("Declared ELF, map, and HEX artifacts must be different files.")
    elf = Path(str(result["elf"]))
    if not _is_loadable_elf(elf):
        raise RuntimeError(f"Declared ELF artifact is not a loadable ELF image: {elf}")
    linker_map = Path(str(result["map"]))
    if _has_elf_magic(linker_map):
        raise RuntimeError(f"Declared linker-map artifact is an ELF file, not a map: {linker_map}")
    if linker_map.stat().st_size == 0:
        raise RuntimeError(f"Declared linker-map artifact is empty: {linker_map}")
    return result


def _artifact_assurance(
    artifacts: Mapping[str, str | None],
    *,
    declared: bool,
    provider: str,
) -> dict[str, str | None]:
    elf_path = artifacts.get("elf")
    if elf_path is None or not _is_loadable_elf(Path(elf_path)):
        raise RuntimeError(f"Reported ELF artifact is not a loadable ELF image: {elf_path}")
    hex_path = artifacts.get("hex")
    if hex_path is not None and not _is_intel_hex(Path(hex_path)):
        raise RuntimeError(f"Reported HEX artifact is not valid Intel HEX: {hex_path}")
    if declared:
        map_assurance = "agent-declared-existing"
    elif provider == "zephyr-west":
        map_assurance = "provider-conventional-path-existing"
    else:
        map_assurance = "unique-discovered-existing"
    return {
        "elf": "loadable-elf-structure-verified",
        "hex": "intel-hex-format-verified" if hex_path is not None else None,
        "map": map_assurance,
        "map_elf_coherence": "not-machine-verifiable; downstream consumers must not assume it",
    }


def _powershell_command(argv: Sequence[str]) -> str:
    """Render literal argv as a pasteable PowerShell command."""

    return " ".join("'" + value.replace("'", "''") + "'" for value in argv)


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
    command = _command_tokens(args)
    project_dir, build_dir = _validate_paths(
        args.project_dir,
        args.build_dir,
        require_fresh_build=not command,
    )
    declared_artifacts = _declared_artifacts(args)
    for value in declared_artifacts.values() if declared_artifacts else ():
        if value is not None:
            _artifact_candidate(build_dir, value)

    offline = bool(getattr(args, "offline", False))
    overrides = _environment_overrides(getattr(args, "env", ()) or ())
    environment: LocalBuildEnvironment | None = None
    if command:
        provider = "agent-command"
        provider_selection = "agent-supplied-argv"
        argv = command
        cwd = _resolve_cwd(getattr(args, "cwd", None), default=project_dir)
        child_environment = dict(os.environ)
    else:
        provider = detect_provider(project_dir)
        if provider is None:
            raise RuntimeError(
                "No convenience build provider was detected. Inspect the project's build files "
                "and supply its exact argv after '--'; use --cwd, --env KEY=VALUE, and explicit "
                "artifact paths when the build layout is not self-describing."
            )
        target_value = getattr(args, "target", None)
        if target_value is None:
            raise RuntimeError(
                "The detected convenience provider requires --target, or supply exact argv "
                "after '--' to use the universal build path."
            )
        target = _validate_target(target_value)
        environment = discover_local_environment(provider=provider)
        if provider != environment.provider:
            raise RuntimeError(
                f"No complete local environment is available for convenience provider {provider}. "
                "Supply the exact build argv after '--' to use another environment."
            )
        provider_selection = "detected-convenience"
        argv = build_command(
            environment,
            project_dir=project_dir,
            build_dir=build_dir,
            target=target,
            offline=offline,
        )
        default_cwd = environment.workspace_dir if provider == "zephyr-west" else project_dir
        cwd = _resolve_cwd(getattr(args, "cwd", None), default=default_cwd)
        child_environment = dict(environment.environment)
    child_environment.update(overrides)
    if offline:
        child_environment = _offline_environment(child_environment)

    build_dir.mkdir(parents=True, exist_ok=True)
    if build_dir.is_symlink():
        raise RuntimeError("Artifact search root must not be a symbolic link.")
    created_build_root = build_dir.resolve(strict=True)
    if created_build_root != build_dir:
        raise RuntimeError("Artifact search root resolved somewhere unexpected.")
    evidence: dict[str, object] = {
        "schema_version": 1,
        "provider": provider,
        "provider_selection": provider_selection,
        "cwd": str(cwd),
        "workspace_dir": str(environment.workspace_dir) if environment is not None else None,
        "toolchain_env": (
            str(environment.toolchain_env)
            if environment is not None and environment.toolchain_env is not None
            else None
        ),
        "argv": argv,
        "exit_code": None,
        "artifacts": {"elf": None, "hex": None, "map": None},
        "artifact_assurance": None,
        "network_policy": "best_effort_offline_guards" if offline else "inherited",
        "offline_guards": offline,
        "environment_overrides": sorted(overrides),
        "helper_provisioning": False,
    }
    try:
        completed = run_owned(
            argv,
            cwd=cwd,
            env=child_environment,
            check=False,
            timeout=BUILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        message = (
            f"Build command exceeded the {BUILD_TIMEOUT_SECONDS:g}-second timeout; "
            "inspect its output, then retry with a build command that terminates."
        )
        evidence["process_error"] = message
        raise BuildEvidenceError(message, evidence) from exc
    except OSError as exc:
        message = (
            f"Cannot start build executable {argv[0]!r}; verify the exact argv, cwd, PATH, "
            "and --env overrides."
        )
        evidence["process_error"] = message
        raise BuildEvidenceError(message, evidence) from exc
    evidence["exit_code"] = completed.returncode
    try:
        if build_dir.is_symlink() or build_dir.resolve(strict=True) != created_build_root:
            raise RuntimeError("Artifact search root was replaced or redirected during the build.")
        if completed.returncode != 0:
            artifacts = {"elf": None, "hex": None, "map": None}
        elif declared_artifacts is not None:
            artifacts = _validate_declared_artifacts(
                build_dir, declared_artifacts, expected_root=created_build_root
            )
        else:
            artifacts = _artifact_paths(build_dir, provider, expected_root=created_build_root)
        if completed.returncode == 0 and (artifacts["elf"] is None or artifacts["map"] is None):
            raise RuntimeError(
                "Native build succeeded without a declared or discoverable loadable ELF image "
                "and linker map in its selected domain."
            )
        assurance = (
            _artifact_assurance(
                artifacts,
                declared=declared_artifacts is not None,
                provider=provider,
            )
            if completed.returncode == 0
            else None
        )
    except RuntimeError as exc:
        evidence["artifact_validation_error"] = str(exc)
        raise BuildEvidenceError(str(exc), evidence) from exc
    evidence["artifacts"] = artifacts
    evidence["artifact_assurance"] = assurance
    print(json.dumps(evidence, sort_keys=True))
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one firmware build as an owned process. Supply exact argv after '--' for any "
            "build system; provider detection is an optional convenience."
        )
    )
    parser.add_argument(
        "--project-dir", required=True, help="Project root used for discovery and relative context"
    )
    parser.add_argument(
        "--build-dir",
        required=True,
        help=(
            "Artifact-search root. Exact-command mode permits existing and in-source trees; "
            "detected conveniences require a new or empty directory."
        ),
    )
    parser.add_argument(
        "--target", help="Project-native board/target for an auto-detected convenience provider"
    )
    parser.add_argument("--cwd", help="Child working directory; defaults to the project directory")
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Repeatable child-environment override",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Apply best-effort offline environment guards for common dependency clients",
    )
    parser.add_argument(
        "--artifact-elf", help="Expected ELF path; relative paths use --build-dir"
    )
    parser.add_argument(
        "--artifact-map", help="Expected linker-map path; relative paths use --build-dir"
    )
    parser.add_argument(
        "--artifact-hex", help="Optional expected HEX path; relative paths use --build-dir"
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Exact build argv after '--'; executed directly without a shell",
    )
    return parser


def command_template() -> dict[str, object]:
    argv = [
        sys.executable,
        "-m",
        "pyocd_debug_mcp.native_build",
        "--project-dir",
        "<project-dir>",
        "--build-dir",
        "<artifact-search-root>",
        "--cwd",
        "<build-working-directory>",
        "--artifact-elf",
        "<elf-output-path>",
        "--artifact-map",
        "<linker-map-output-path>",
        "--",
        "<build-executable>",
        "<build-argument>",
    ]
    convenience_argv = [
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
        "powershell_template": _powershell_command(argv),
        "convenience_argv_template": convenience_argv,
        "provider_selection": "agent_argv_or_optional_detection",
        "environment_selection": "inherited_with_repeatable_env_overrides",
        "network_policy": "inherited_by_default",
        "offline_guards": False,
        "offline_option": "--offline (best-effort common-client guards; not a network sandbox)",
        "dependency_acquisition": "allowed_when_no_compatible_local_resource_exists",
        "helper_provisioning": False,
        "optional_convenience_providers": ["zephyr-west", "gnu-make"],
        "resolved_local_environment": {
            "status": "not_selected",
            "reason": "Resolve the project's real build command before choosing an environment.",
        },
        "resolved_local_environments": {},
        "parameter_help": {
            "project_dir": "Project root; inspect its build files before choosing argv.",
            "build_dir": (
                "Artifact-search root. It may be existing/in-source with explicit argv; relative "
                "artifact paths resolve from it."
            ),
            "cwd": "Optional child working directory; defaults to project_dir.",
            "env": "Repeatable KEY=VALUE child-environment overrides.",
            "artifact_elf_map_hex": (
                "Explicit ELF and map plus optional HEX outputs; absolute paths are allowed. "
                "Otherwise exactly one loadable ELF image (excluding object files) and one .map "
                "are discovered under build_dir."
            ),
            "offline": (
                "Optional best-effort common-client guards, not an OS network sandbox; omitted "
                "means ordinary inherited network access."
            ),
            "command": "Exact argv after '--', executed without a shell.",
        },
        "examples": [
            {
                "build_system": "plain-cmake",
                "command_after_separator": ["cmake", "--build", "<artifact-search-root>"],
            },
            {
                "build_system": "platformio",
                "command_after_separator": ["platformio", "run", "--environment", "<env-name>"],
            },
            {
                "build_system": "vendor-or-future-tool",
                "command_after_separator": ["<vendor-build-executable>", "<native-arguments>"],
            },
        ],
        "common_failures": {
            "no_detected_provider": "Inspect project files and pass exact argv after '--'.",
            "executable_not_found": "Correct argv, cwd, PATH, or --env; acquire the tool if absent.",
            "missing_or_ambiguous_outputs": (
                "Pass explicit --artifact-elf and --artifact-map paths from the successful build."
            ),
            "dependency_unavailable": (
                "Prefer a compatible local copy; otherwise acquire it with normal network access."
            ),
        },
    }
    return result


def main() -> None:
    args = build_parser().parse_args()
    try:
        raise SystemExit(run_build(args))
    except BuildEvidenceError as exc:
        payload = dict(exc.evidence)
        payload["error"] = str(exc)
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        raise SystemExit(2) from exc
    except RuntimeError as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "offline_guards": bool(getattr(args, "offline", False)),
                    "network_policy": (
                        "best_effort_offline_guards"
                        if bool(getattr(args, "offline", False))
                        else "inherited"
                    ),
                    "helper_provisioning": False,
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
