"""Deterministic green-check execution owned by Server A."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pyocd_debug_mcp.kernel.processes import run_owned, validate_argv


class GreenCheckError(RuntimeError):
    """The green-check contract or execution is invalid."""


@dataclass(frozen=True, slots=True)
class GreenCheckResult:
    passed: bool
    command: tuple[str, ...]
    exit_code: int
    output: str
    missing_outputs: tuple[str, ...]


_SCAN_CHUNK_BYTES = 64 * 1024
_MAX_RESULT_BYTES_PER_STREAM = 32 * 1024


def _observed_expected_values(paths: tuple[Path, ...], expected: tuple[str, ...]) -> set[str]:
    encoded = {item: item.encode("utf-8") for item in expected}
    remaining = dict(encoded)
    observed: set[str] = set()
    overlap = max((len(item) for item in remaining.values()), default=1) - 1
    for path in paths:
        tail = b""
        with path.open("rb") as stream:
            while remaining:
                chunk = stream.read(_SCAN_CHUNK_BYTES)
                if not chunk:
                    break
                window = tail + chunk
                matched = {text for text, literal in remaining.items() if literal in window}
                observed.update(matched)
                for text in matched:
                    remaining.pop(text)
                tail = window[-overlap:] if overlap else b""
        if not remaining:
            break
    return observed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_output(path: Path, label: str) -> str:
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if size <= _MAX_RESULT_BYTES_PER_STREAM:
        content = path.read_bytes()
    else:
        half = _MAX_RESULT_BYTES_PER_STREAM // 2
        with path.open("rb") as stream:
            head = stream.read(half)
            stream.seek(-half, 2)
            tail = stream.read(half)
        content = head + b"\n...[bounded output omitted]...\n" + tail
    text = content.decode("utf-8", errors="replace")
    return f"[{label} bytes={size} sha256={digest.hexdigest()}]\n{text}"


def _script_command(
    script: Path,
    args: tuple[str, ...],
    command_template: tuple[str, ...],
) -> tuple[str, ...]:
    replacements = {"{script}": str(script), "{python}": sys.executable}
    command = tuple(replacements.get(item, item) for item in command_template)
    if command_template.count("{script}") != 1:
        raise GreenCheckError("green-check command must contain {script} exactly once")
    try:
        return validate_argv((*command, *args))
    except ValueError as exc:
        raise GreenCheckError(f"green-check command is invalid: {exc}") from exc


class GreenCheckRunner:
    def run(
        self,
        *,
        script_path: Path,
        script_args: tuple[str, ...],
        expected_outputs: tuple[str, ...],
        command_template: tuple[str, ...],
        workspace: Path,
        artifact_root: Path,
        timeout_seconds: float,
        trusted_script_root: Path | None = None,
        expected_script_sha256: str | None = None,
    ) -> GreenCheckResult:
        root = workspace.resolve()
        expanded = script_path.expanduser()
        script = (root / expanded).resolve() if not expanded.is_absolute() else expanded.resolve()
        if script.is_symlink() or not script.is_file():
            raise GreenCheckError(f"green_check_script is not a file: {script}")
        owned_root = (trusted_script_root or artifact_root).resolve()
        try:
            script.relative_to(owned_root)
        except ValueError as exc:
            raise GreenCheckError(
                "green_check_script must stay inside its server-owned call root"
            ) from exc
        if expected_script_sha256 is not None and _sha256_file(script) != expected_script_sha256:
            raise GreenCheckError("green_check_script changed after Client A supplied it")
        command = _script_command(script, script_args, command_template)
        stdout_path = artifact_root / "green-check.stdout"
        stderr_path = artifact_root / "green-check.stderr"
        try:
            with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
                completed = run_owned(
                    command,
                    cwd=root,
                    check=False,
                    timeout=timeout_seconds,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                )
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            raise GreenCheckError(f"green check execution failed: {exc}") from exc
        observed = _observed_expected_values((stdout_path, stderr_path), expected_outputs)
        missing = tuple(item for item in expected_outputs if item not in observed)
        output = _bounded_output(stdout_path, "stdout")
        if stderr_path.stat().st_size:
            output += "\n" + _bounded_output(stderr_path, "stderr")
        return GreenCheckResult(
            completed.returncode == 0 and not missing,
            command,
            completed.returncode,
            output,
            missing,
        )
