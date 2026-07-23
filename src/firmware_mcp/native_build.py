"""Build-system-neutral firmware process launcher.

The launcher owns no build-system, hardware, or safety authority. The agent
supplies the project's exact argv, cwd, environment, and expected outputs.
Network access and environment are exactly those supplied by the caller.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence, cast

from firmware_mcp.kernel.processes import run_owned


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
    # Preserve the caller's directory link. Its resolved identity is captured
    # only after the selected path exists, then checked after the build.
    build_dir = Path(os.path.abspath(str(Path(build_value).expanduser())))
    if not project_dir.is_dir():
        raise RuntimeError(f"Project directory does not exist: {project_dir}")
    if build_dir.exists() and not build_dir.is_dir():
        raise RuntimeError(f"Build path exists but is not a directory: {build_dir}")
    if require_fresh_build and build_dir.exists() and any(build_dir.iterdir()):
        raise RuntimeError(
            f"Build directory must be new or empty; preserve existing contents: {build_dir}"
        )
    return project_dir, build_dir


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


def _discover_artifacts(build_dir: Path) -> list[dict[str, object]]:
    """Return every nonempty supported output under *build_dir* in stable order."""

    result: list[dict[str, object]] = []
    for path in sorted(build_dir.rglob("*"), key=lambda item: str(item).casefold()):
        if not path.is_file() or path.stat().st_size == 0:
            continue
        suffix = path.suffix.casefold()
        if suffix not in {".elf", ".axf", ".hex", ".bin", ".map"}:
            continue
        if suffix in {".elf", ".axf"} and not _is_loadable_elf(path):
            continue
        if suffix == ".hex" and not _is_intel_hex(path):
            continue
        result.append(
            {
                "path": str(path.resolve()),
                "format": suffix.removeprefix("."),
                "size_bytes": path.stat().st_size,
                "validation": (
                    "loadable-elf-structure-verified"
                    if suffix in {".elf", ".axf"}
                    else "intel-hex-format-verified"
                    if suffix == ".hex"
                    else "existing-nonempty-file"
                ),
            }
        )
    return result


def build_firmware(
    project_dir: str,
    build_dir: str,
    command: Sequence[str],
    *,
    working_dir: str | None = None,
    environment: Mapping[str, str] | None = None,
    artifacts: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, object]:
    """Run one trusted direct-argv build with closed stdin and complete evidence.

    Build commands obtain all input through their exact argv, working directory,
    and environment. They never read the MCP stdio protocol stream.
    """

    if isinstance(command, str | bytes) or not command:
        raise RuntimeError("command must be a nonempty JSON list of exact argv strings")
    argv = tuple(command)
    if any(not isinstance(token, str) or not token or "\x00" in token for token in argv):
        raise RuntimeError("command must contain nonempty NUL-free argv strings")
    if environment is not None and (
        not isinstance(environment, Mapping)
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or "\x00" in key
            or "\x00" in value
            for key, value in environment.items()
        )
    ):
        raise RuntimeError("environment must map NUL-free strings to NUL-free strings")
    project, output = _validate_paths(project_dir, build_dir, require_fresh_build=False)
    output.mkdir(parents=True, exist_ok=True)
    cwd = _resolve_cwd(working_dir, default=project)
    overrides = dict(environment or {})
    child_environment = _apply_environment_overrides(os.environ, overrides)
    effective_timeout = None if timeout_seconds is None else _timeout_seconds(timeout_seconds)
    evidence: dict[str, object] = {
        "argv": list(argv),
        "cwd": str(cwd),
        "environment_overrides": dict(overrides),
        "timeout_seconds": effective_timeout,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "duration_seconds": None,
        "artifacts": [],
    }
    started = time.monotonic()
    try:
        completed = run_owned(
            argv,
            cwd=cwd,
            env=child_environment,
            capture_output=True,
            text=True,
            check=False,
            timeout_seconds=effective_timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        evidence.update(
            {
                "status": "build_timeout",
                "duration_seconds": time.monotonic() - started,
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
                "cleanup": "owned-process-cleanup-attempted",
            }
        )
        return evidence
    except (OSError, RuntimeError) as exc:
        evidence.update(
            {
                "status": "build_start_failed",
                "duration_seconds": time.monotonic() - started,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        return evidence
    evidence.update(
        {
            "exit_code": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
            "duration_seconds": time.monotonic() - started,
        }
    )
    if completed.returncode != 0:
        evidence["status"] = "build_failed"
        return evidence
    try:
        if artifacts is not None:
            declared: list[dict[str, object]] = []
            for role, value in artifacts.items():
                if (
                    not isinstance(role, str)
                    or not role
                    or not isinstance(value, str)
                    or not value
                    or "\x00" in value
                ):
                    raise RuntimeError("artifacts must map nonempty role strings to nonempty paths")
                candidate = _artifact_candidate(output, value)
                if not candidate.is_file() or candidate.stat().st_size == 0:
                    raise RuntimeError(
                        f"Declared {role} artifact must be an existing nonempty file: {candidate}"
                    )
                suffix = candidate.suffix.casefold()
                if suffix in {".elf", ".axf"} and not _is_loadable_elf(candidate):
                    raise RuntimeError(
                        f"Declared {role} artifact is not a loadable ELF/AXF: {candidate}"
                    )
                if suffix == ".hex" and not _is_intel_hex(candidate):
                    raise RuntimeError(
                        f"Declared {role} artifact is not valid Intel HEX: {candidate}"
                    )
                declared.append(
                    {
                        "role": role,
                        "path": str(candidate),
                        "format": suffix.removeprefix("."),
                        "size_bytes": candidate.stat().st_size,
                    }
                )
            evidence["artifacts"] = sorted(
                declared, key=lambda item: (str(item["role"]), str(item["path"]))
            )
        else:
            evidence["artifacts"] = _discover_artifacts(output)
    except RuntimeError as exc:
        evidence.update({"status": "build_artifact_invalid", "error": str(exc)})
        return evidence
    evidence["status"] = "build_succeeded"
    if not evidence["artifacts"]:
        evidence["next_step"] = (
            "Build exited successfully without firmware artifacts; inspect build output or declare artifacts explicitly."
        )
    return evidence


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


def run_build(args: argparse.Namespace) -> int:
    """CLI adapter for :func:`build_firmware`; it does not own a second build path."""

    command = _command_tokens(args)
    if not command:
        raise RuntimeError(
            "No build command was supplied. Inspect the project's build files and pass its exact "
            "native argv after '--'. The server does not select a build system, SDK, compiler, "
            "or target."
        )
    declared_artifacts = _declared_artifacts(args)
    declared = (
        {role: value for role, value in declared_artifacts.items() if value is not None}
        if declared_artifacts is not None
        else None
    )
    evidence = build_firmware(
        args.project_dir,
        args.build_dir,
        command,
        working_dir=getattr(args, "cwd", None),
        environment=_environment_overrides(getattr(args, "env", ()) or ()),
        artifacts=declared,
        timeout_seconds=getattr(args, "timeout_seconds", None),
    )
    print(json.dumps(evidence, sort_keys=True))
    exit_code = evidence.get("exit_code")
    return int(exit_code) if isinstance(exit_code, int) else 1


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
        "--timeout-seconds",
        default=None,
        help="Optional positive finite build timeout in seconds; omitted means no server deadline",
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
        "firmware_mcp.native_build",
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
        "dependency_acquisition": "allowed_when_no_compatible_local_resource_exists",
        "helper_provisioning": False,
        "resolved_build_environment": {
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
            "timeout_seconds": "Optional positive finite child-process timeout; omitted has no server deadline.",
            "artifact": (
                "Repeatable ROLE=PATH for any output format. Every path must exist and be "
                "nonempty after the build; unknown formats are reported as opaque, not validated."
            ),
            "artifact_elf_map_hex": (
                "Compatibility shorthands with structural ELF/HEX validation. Without any output "
                "declaration, exactly one loadable ELF image (excluding object files) and one "
                ".map are discovered under build_dir."
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
                    "network_policy": "inherited",
                    "helper_provisioning": False,
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
