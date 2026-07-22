"""Build-system-neutral firmware process launcher.

The launcher owns no build-system, hardware, or safety authority. The agent
supplies the project's exact argv, cwd, environment, and expected outputs.
Network access is inherited unless the caller explicitly requests offline
execution.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence, cast

from pyocd_debug_mcp.kernel.processes import run_owned


BUILD_TIMEOUT_SECONDS = 1800.0
_ARTIFACT_ROLE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*\Z")


class BuildEvidenceError(RuntimeError):
    """A post-execution failure with enough evidence to diagnose the build."""

    def __init__(self, message: str, evidence: Mapping[str, object]) -> None:
        super().__init__(message)
        self.evidence = dict(evidence)


def _validate_paths(
    project_value: str, build_value: str, *, require_fresh_build: bool = True
) -> tuple[Path, Path]:
    project_dir = Path(project_value).expanduser().resolve()
    build_dir = Path(build_value).expanduser().resolve()
    if not project_dir.is_dir():
        raise RuntimeError(f"Project directory does not exist: {project_dir}")
    root = Path(build_dir.anchor).resolve()
    if build_dir == root:
        raise RuntimeError("Build directory must be a dedicated non-root directory.")
    if require_fresh_build and build_dir == Path.home().resolve():
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


def _artifact_paths(
    build_dir: Path,
    *,
    expected_root: Path | None = None,
) -> dict[str, str | None]:
    root = expected_root or build_dir.resolve(strict=True)
    if build_dir.is_symlink() or build_dir.resolve(strict=True) != root:
        raise RuntimeError("Artifact search root was replaced or redirected during the build.")
    files = tuple(
        path
        for path in build_dir.rglob("*")
        if path.is_file() and path.resolve().is_relative_to(root)
    )
    elves = tuple(path.resolve() for path in files if _is_loadable_elf(path))
    if len(elves) != 1:
        raise RuntimeError(
            "Native build must produce exactly one ELF below the artifact search root; "
            f"found {len(elves)}. Pass --artifact-elf and --artifact-map for multi-image or "
            "nonstandard output layouts."
        )
    elf = elves[0]
    maps = tuple(
        path.resolve()
        for path in files
        if path.suffix.casefold() == ".map" and path.stat().st_size > 0 and not _has_elf_magic(path)
    )
    if len(maps) != 1:
        raise RuntimeError(
            "Native build must produce exactly one linker map; "
            f"found {len(maps)}. Pass --artifact-elf and --artifact-map for multi-image or "
            "nonstandard output layouts."
        )
    hexes = tuple(
        path.resolve()
        for path in files
        if path.suffix.casefold() == ".hex" and path.stem == elf.stem
    )
    if len(hexes) > 1:
        raise RuntimeError(
            "Native build produced multiple same-stem HEX artifacts; pass --artifact-hex."
        )
    return {
        "elf": str(elf),
        "hex": str(hexes[0]) if hexes else None,
        "map": str(maps[0]),
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
    if command and not command[0]:
        raise RuntimeError("Build executable must be nonempty.")
    return command


def _environment_overrides(
    values: Sequence[str], *, platform_name: str = os.name
) -> dict[str, str]:
    overrides: dict[str, str] = {}
    normalized_keys: dict[str, str] = {}
    for value in values:
        key, separator, setting = value.partition("=")
        if not separator or not key or "\x00" in key or "\x00" in setting:
            raise RuntimeError("Environment overrides must use nonempty KEY=VALUE form.")
        normalized = key.casefold() if platform_name == "nt" else key
        if previous := normalized_keys.get(normalized):
            overrides.pop(previous, None)
        normalized_keys[normalized] = key
        overrides[key] = setting
    return overrides


def _apply_environment_overrides(
    environment: Mapping[str, str],
    overrides: Mapping[str, str],
    *,
    platform_name: str = os.name,
) -> dict[str, str]:
    result = dict(environment)
    if platform_name == "nt":
        effective_overrides: dict[str, str] = {}
        normalized_overrides: dict[str, str] = {}
        for key, value in overrides.items():
            normalized = key.casefold()
            if previous := normalized_overrides.get(normalized):
                effective_overrides.pop(previous, None)
            normalized_overrides[normalized] = key
            effective_overrides[key] = value
        inherited_keys = {key.casefold(): key for key in result}
        for key in effective_overrides:
            if previous := inherited_keys.get(key.casefold()):
                result.pop(previous, None)
        result.update(effective_overrides)
        return result
    result.update(overrides)
    return result


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
    for declaration in getattr(args, "artifact", ()) or ():
        role, separator, path = declaration.partition("=")
        if not separator or not _ARTIFACT_ROLE.fullmatch(role) or not path or "\x00" in path:
            raise RuntimeError(
                "Named artifacts must use ROLE=PATH with a non-empty alphanumeric role."
            )
        normalized = role.casefold()
        if normalized in values and values[normalized] is not None:
            raise RuntimeError(f"Artifact role was declared more than once: {normalized}")
        values[normalized] = path
    if not any(values.values()):
        return None
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
    for role, value in values.items():
        if value is None:
            result[role] = None
            continue
        candidate = _artifact_candidate(build_dir, value)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(
                f"Declared {role.upper()} artifact does not exist: {candidate}"
            ) from exc
        if not resolved.is_file():
            raise RuntimeError(f"Declared {role.upper()} artifact is not a file: {resolved}")
        if resolved.stat().st_size == 0:
            raise RuntimeError(f"Declared {role.upper()} artifact is empty: {resolved}")
        result[role] = str(resolved)
    declared_paths = [path for path in result.values() if path is not None]
    if len(set(declared_paths)) != len(declared_paths):
        raise RuntimeError("Declared artifact roles must refer to different files.")
    if elf_value := result.get("elf"):
        elf = Path(elf_value)
        if not _is_loadable_elf(elf):
            raise RuntimeError(f"Declared ELF artifact is not a loadable ELF image: {elf}")
    if map_value := result.get("map"):
        linker_map = Path(map_value)
        if _has_elf_magic(linker_map):
            raise RuntimeError(
                f"Declared linker-map artifact is an ELF file, not a map: {linker_map}"
            )
    if hex_value := result.get("hex"):
        intel_hex = Path(hex_value)
        if not _is_intel_hex(intel_hex):
            raise RuntimeError(f"Declared HEX artifact is not valid Intel HEX: {intel_hex}")
    return result


def _artifact_assurance(
    artifacts: Mapping[str, str | None],
    *,
    declared: bool,
) -> dict[str, object]:
    elf_path = artifacts.get("elf")
    if elf_path is not None and not _is_loadable_elf(Path(elf_path)):
        raise RuntimeError(f"Reported ELF artifact is not a loadable ELF image: {elf_path}")
    hex_path = artifacts.get("hex")
    if hex_path is not None and not _is_intel_hex(Path(hex_path)):
        raise RuntimeError(f"Reported HEX artifact is not valid Intel HEX: {hex_path}")
    map_assurance = (
        ("agent-declared-existing" if declared else "unique-discovered-existing")
        if artifacts.get("map") is not None
        else None
    )
    known = {"elf", "hex", "map"}
    opaque = {
        role: "existing-nonempty-file; format-not-interpreted"
        for role, path in artifacts.items()
        if role not in known and path is not None
    }
    return {
        "elf": "loadable-elf-structure-verified" if elf_path is not None else None,
        "hex": "intel-hex-format-verified" if hex_path is not None else None,
        "map": map_assurance,
        "map_elf_coherence": "not-machine-verifiable; downstream consumers must not assume it",
        "opaque_declared_outputs": opaque,
    }


def _timeout_seconds(value: str | int | float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Build timeout must be a positive number of seconds.") from exc
    if not 0 < timeout < float("inf"):
        raise RuntimeError("Build timeout must be a positive finite number of seconds.")
    return timeout


def _powershell_command(argv: Sequence[str]) -> str:
    """Render literal argv as a pasteable PowerShell command."""

    rendered = ("'" + value.replace("'", "''") + "'" for value in argv)
    return "& " + " ".join(rendered)


def _offline_environment(
    environment: Mapping[str, str], *, platform_name: str = os.name
) -> dict[str, str]:
    guards = {
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
    if platform_name == "nt":
        for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            guards.pop(key)
    return _apply_environment_overrides(
        environment,
        guards,
        platform_name=platform_name,
    )


def run_build(args: argparse.Namespace) -> int:
    command = _command_tokens(args)
    if not command:
        raise RuntimeError(
            "No build command was supplied. Inspect the project's build files and pass its exact "
            "native argv after '--'. The server does not select a build system, SDK, compiler, "
            "or target."
        )
    project_dir, build_dir = _validate_paths(
        args.project_dir,
        args.build_dir,
        require_fresh_build=False,
    )
    declared_artifacts = _declared_artifacts(args)
    for value in declared_artifacts.values() if declared_artifacts else ():
        if value is not None:
            _artifact_candidate(build_dir, value)

    offline = bool(getattr(args, "offline", False))
    timeout_seconds = _timeout_seconds(getattr(args, "timeout_seconds", BUILD_TIMEOUT_SECONDS))
    overrides = _environment_overrides(getattr(args, "env", ()) or ())
    argv = command
    cwd = _resolve_cwd(getattr(args, "cwd", None), default=project_dir)
    child_environment = _apply_environment_overrides(os.environ, overrides)
    if offline:
        child_environment = _offline_environment(child_environment)

    build_dir.mkdir(parents=True, exist_ok=True)
    if build_dir.is_symlink():
        raise RuntimeError("Artifact search root must not be a symbolic link.")
    created_build_root = build_dir.resolve(strict=True)
    if created_build_root != build_dir:
        raise RuntimeError("Artifact search root resolved somewhere unexpected.")
    evidence: dict[str, object] = {
        "schema_version": 2,
        "cwd": str(cwd),
        "argv": argv,
        "exit_code": None,
        "artifacts": {"elf": None, "hex": None, "map": None},
        "artifact_assurance": None,
        "network_policy": "best_effort_offline_guards" if offline else "inherited",
        "offline_guards": offline,
        "environment_overrides": sorted(overrides),
        "timeout_seconds": timeout_seconds,
        "helper_provisioning": False,
    }
    try:
        completed = run_owned(
            argv,
            cwd=cwd,
            env=child_environment,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        message = (
            f"Build command exceeded the {timeout_seconds:g}-second timeout; "
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
    except RuntimeError as exc:
        message = f"Build process ownership failed: {exc}"
        evidence["process_error"] = message
        raise BuildEvidenceError(message, evidence) from exc
    evidence["exit_code"] = completed.returncode
    try:
        try:
            root_changed = (
                build_dir.is_symlink() or build_dir.resolve(strict=True) != created_build_root
            )
        except OSError as exc:
            raise RuntimeError(
                "Artifact search root disappeared or became inaccessible during the build."
            ) from exc
        if root_changed:
            raise RuntimeError("Artifact search root was replaced or redirected during the build.")
        if completed.returncode != 0:
            artifacts = {"elf": None, "hex": None, "map": None}
        elif declared_artifacts is not None:
            artifacts = _validate_declared_artifacts(
                build_dir, declared_artifacts, expected_root=created_build_root
            )
        else:
            artifacts = _artifact_paths(build_dir, expected_root=created_build_root)
        if completed.returncode == 0 and not any(artifacts.values()):
            raise RuntimeError(
                "Native build succeeded without a declared or discoverable output. Declare any "
                "project-native output with --artifact ROLE=PATH."
            )
        assurance = (
            _artifact_assurance(
                artifacts,
                declared=declared_artifacts is not None,
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
            "Run one firmware build as an owned process. Supply the project's exact native argv "
            "after '--'; the server does not select a build system or toolchain."
        )
    )
    parser.add_argument(
        "--project-dir", required=True, help="Project root used for discovery and relative context"
    )
    parser.add_argument(
        "--build-dir",
        required=True,
        help=("Artifact-search root. Existing and in-source trees are permitted."),
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
        "--timeout-seconds",
        default=BUILD_TIMEOUT_SECONDS,
        help="Positive finite build timeout in seconds (default: 1800)",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="ROLE=PATH",
        help=(
            "Repeatable named output declaration for any firmware format; relative paths use "
            "--build-dir"
        ),
    )
    parser.add_argument("--artifact-elf", help="Expected ELF path; relative paths use --build-dir")
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
        "--artifact",
        "<role>=<output-path>",
        "--",
        "<build-executable>",
        "<build-argument>",
    ]
    result: dict[str, object] = {
        "argv_template": argv,
        "powershell_template": _powershell_command(argv),
        "powershell_compatibility": (
            "Windows PowerShell 5 may omit empty native-command arguments. If exact argv contains "
            "an empty token, invoke the helper from a process API or a shell/runtime that preserves "
            "empty arguments."
        ),
        "environment_selection": "inherited_with_repeatable_env_overrides",
        "network_policy": "inherited_by_default",
        "offline_guards": False,
        "offline_option": "--offline (best-effort common-client guards; not a network sandbox)",
        "dependency_acquisition": "allowed_when_no_compatible_local_resource_exists",
        "helper_provisioning": False,
        "resolved_local_environment": {
            "status": "not_selected",
            "reason": (
                "The agent resolves the project's real command and environment from project "
                "metadata and available host tools."
            ),
        },
        "parameter_help": {
            "project_dir": "Project root; inspect its build files before choosing argv.",
            "build_dir": (
                "Artifact-search root. It may be existing/in-source with explicit argv; relative "
                "artifact paths resolve from it."
            ),
            "cwd": "Optional child working directory; defaults to project_dir.",
            "env": "Repeatable KEY=VALUE child-environment overrides.",
            "timeout_seconds": "Positive finite child-process timeout.",
            "artifact": (
                "Repeatable ROLE=PATH for any output format. Every path must exist and be "
                "nonempty after the build; unknown formats are reported as opaque, not validated."
            ),
            "artifact_elf_map_hex": (
                "Compatibility shorthands with structural ELF/HEX validation. Without any output "
                "declaration, exactly one loadable ELF image (excluding object files) and one "
                ".map are discovered under build_dir."
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
            "missing_command": "Inspect project files and pass exact argv after '--'.",
            "executable_not_found": "Correct argv, cwd, PATH, or --env; acquire the tool if absent.",
            "missing_or_ambiguous_outputs": (
                "Pass explicit --artifact ROLE=PATH declarations for the successful build."
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
