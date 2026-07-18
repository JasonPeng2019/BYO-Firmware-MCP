"""Configurable, provider-neutral command adapter for optional agent benchmarks.

MCP standardizes the client/server protocol, not vendor CLI launch flags. This
module therefore launches only an explicit operator-owned executable/argv
template. A CLI that cannot consume the neutral launch manifest directly can
be placed behind a small wrapper or marked as preconfigured.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Final

from pyocd_debug_mcp.kernel.processes import run_owned

_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ENV_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SECRET_NAME: Final = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|CREDENTIAL)", re.IGNORECASE
)
_ALLOWED_PLACEHOLDERS: Final = frozenset(
    {
        "workspace",
        "prompt_path",
        "result_path",
        "result_schema_path",
        "mcp_manifest_path",
        "repo_root",
    }
)
_ALLOWED_FIELDS: Final = frozenset(
    {
        "schema_version",
        "name",
        "command",
        "version_command",
        "registration_check",
        "mcp_mode",
        "result_transport",
        "permission_profile",
        "model",
        "effort",
        "inherit_env",
        "env",
    }
)
_RUNTIME_ENV_NAMES: Final = (
    "PATH",
    "SYSTEMROOT",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
)


class AgentCommandError(RuntimeError):
    """The operator adapter is invalid or its bounded command run failed."""


@dataclass(frozen=True, slots=True)
class AgentCommandConfig:
    source_path: Path
    source_sha256: str
    name: str
    command: tuple[str, ...]
    version_command: tuple[str, ...] | None
    registration_check: tuple[str, ...] | None
    mcp_mode: str
    result_transport: str
    permission_profile: str
    model: str | None
    effort: str | None
    inherit_env: tuple[str, ...]
    env: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class AgentCommandRun:
    exit_code: int
    stdout_text: str
    stderr_text: str
    result_path: Path
    prompt_path: Path
    mcp_manifest_path: Path
    started_at: str
    completed_at: str
    metadata: Mapping[str, object]


class AgentCommandResultError(AgentCommandError):
    """A completed agent process returned no usable structured result.

    The completed run is retained so callers can preserve stdout, stderr,
    timestamps, exit status, and provider metadata as blocked-run evidence.
    """

    def __init__(self, message: str, run: AgentCommandRun) -> None:
        super().__init__(message)
        self.run = run


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_text(
    document: Mapping[str, object], key: str, *, nullable: bool = False
) -> str | None:
    value = document.get(key)
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AgentCommandError(f"agent config field '{key}' must be non-empty text")
    return value.strip()


def _require_argv(value: object, label: str, *, nullable: bool = False) -> tuple[str, ...] | None:
    if nullable and value is None:
        return None
    if not isinstance(value, list) or not value:
        raise AgentCommandError(f"agent config field '{label}' must be a non-empty argv array")
    if any(
        not isinstance(item, str) or not item or "\x00" in item or "\n" in item for item in value
    ):
        raise AgentCommandError(f"agent config field '{label}' must be a non-empty argv array")
    return tuple(value)


def _placeholders(argv: tuple[str, ...]) -> set[str]:
    observed: set[str] = set()
    for token in argv:
        for _literal, field, _format_spec, _conversion in Formatter().parse(token):
            if field is not None:
                if field not in _ALLOWED_PLACEHOLDERS:
                    raise AgentCommandError(f"unknown placeholder '{{{field}}}' in agent argv")
                observed.add(field)
    return observed


def load_agent_command_config(path: Path) -> AgentCommandConfig:
    """Load one explicit trusted-operator config; never search an agent workspace for it."""

    source = path.expanduser().resolve()
    try:
        payload = source.read_bytes()
        document = json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentCommandError(f"agent config is unreadable JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise AgentCommandError("agent config must contain one JSON object")
    unknown = set(document) - _ALLOWED_FIELDS
    if unknown:
        raise AgentCommandError(f"agent config has unknown fields: {sorted(unknown)}")
    if document.get("schema_version") != 1:
        raise AgentCommandError("agent config schema_version must be 1")
    name = _require_text(document, "name")
    assert isinstance(name, str)
    if _NAME.fullmatch(name) is None:
        raise AgentCommandError("agent config name must be a portable 1-64 character identifier")
    command = _require_argv(document.get("command"), "command")
    version_command = _require_argv(
        document.get("version_command"), "version_command", nullable=True
    )
    registration = _require_argv(
        document.get("registration_check"), "registration_check", nullable=True
    )
    assert command is not None
    command_fields = _placeholders(command)
    if version_command is not None:
        _placeholders(version_command)
    if registration is not None:
        _placeholders(registration)
    if "prompt_path" not in command_fields:
        raise AgentCommandError("agent command requires placeholder '{prompt_path}'")
    mcp_mode = _require_text(document, "mcp_mode")
    if mcp_mode not in {"launch_manifest", "preconfigured"}:
        raise AgentCommandError("mcp_mode must be 'launch_manifest' or 'preconfigured'")
    if mcp_mode == "launch_manifest" and "mcp_manifest_path" not in command_fields:
        raise AgentCommandError("agent command requires placeholder '{mcp_manifest_path}'")
    result_transport = _require_text(document, "result_transport")
    if result_transport not in {"file", "stdout_json"}:
        raise AgentCommandError("result_transport must be 'file' or 'stdout_json'")
    if result_transport == "file" and "result_path" not in command_fields:
        raise AgentCommandError("agent command requires placeholder '{result_path}'")
    permission_profile = _require_text(document, "permission_profile")
    assert isinstance(permission_profile, str)
    inherit_raw = document.get("inherit_env", [])
    if not isinstance(inherit_raw, list) or any(
        not isinstance(item, str) or _ENV_NAME.fullmatch(item) is None for item in inherit_raw
    ):
        raise AgentCommandError("inherit_env must be an array of environment variable names")
    env_raw = document.get("env", {})
    if not isinstance(env_raw, dict) or any(
        not isinstance(key, str) or _ENV_NAME.fullmatch(key) is None or not isinstance(value, str)
        for key, value in env_raw.items()
    ):
        raise AgentCommandError("env must map environment variable names to string values")
    secret_literals = sorted(key for key in env_raw if _SECRET_NAME.search(key))
    if secret_literals:
        raise AgentCommandError(
            "literal env contains secret-like names; inherit those variables by name instead"
        )
    for value in env_raw.values():
        _placeholders((str(value),))
    return AgentCommandConfig(
        source,
        hashlib.sha256(payload).hexdigest(),
        name,
        command,
        version_command,
        registration,
        mcp_mode,
        result_transport,
        permission_profile,
        _require_text(document, "model", nullable=True),
        _require_text(document, "effort", nullable=True),
        tuple(dict.fromkeys(str(item) for item in inherit_raw)),
        {str(key): str(value) for key, value in env_raw.items()},
    )


def _resolve_executable(argv: tuple[str, ...]) -> tuple[str, ...]:
    executable = Path(argv[0]).expanduser()
    resolved = str(executable.resolve()) if executable.is_file() else shutil.which(argv[0])
    if resolved is None or not Path(resolved).is_file():
        raise AgentCommandError(f"agent executable is absent or not a file: {argv[0]}")
    return (resolved, *argv[1:])


def _expand(argv: tuple[str, ...], values: Mapping[str, str]) -> tuple[str, ...]:
    try:
        return tuple(token.format_map(values) for token in argv)
    except (KeyError, ValueError) as exc:  # defensive; config validation catches known fields
        raise AgentCommandError(f"agent argv substitution failed: {exc}") from exc


def _environment(config: AgentCommandConfig, values: Mapping[str, str]) -> dict[str, str]:
    names = tuple(dict.fromkeys((*_RUNTIME_ENV_NAMES, *config.inherit_env)))
    selected = {name: os.environ[name] for name in names if name in os.environ}
    selected.update({name: value.format_map(values) for name, value in config.env.items()})
    return selected


def _redacted_argv(argv: tuple[str, ...]) -> list[str]:
    """Redact common credential flags while retaining useful command shape."""

    secret_flag = re.compile(
        r"(?:api[-_]?key|token|secret|password|passwd|credential)", re.IGNORECASE
    )
    redacted: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if "=" in token:
            key, _value = token.split("=", 1)
            if secret_flag.search(key):
                redacted.append(f"{key}=<redacted>")
                continue
        redacted.append(token)
        if token.startswith("-") and secret_flag.search(token):
            redact_next = True
    return redacted


class AgentCommandAdapter:
    """Run a configured agent CLI or wrapper with finite, shell-free argv."""

    def __init__(self, config: AgentCommandConfig) -> None:
        self.config = config

    def run(
        self,
        *,
        workspace: Path,
        prompt_text: str,
        result_schema_path: Path,
        repo_root: Path,
        timeout_seconds: float,
    ) -> AgentCommandRun:
        if timeout_seconds <= 0:
            raise AgentCommandError("agent timeout must be positive")
        workspace = workspace.resolve()
        prompt_path = workspace / ".r11_agent_prompt.txt"
        result_path = workspace / ".r11_agent_result.json"
        manifest_path = workspace / ".r11_mcp_launch.json"
        if self.config.result_transport == "stdout_json":
            result_instruction = (
                "Emit the exact structured result JSON object as the only stdout content before "
                "exiting. Do not write a result file or emit progress text to stdout.\n"
            )
        else:
            result_instruction = (
                f"Write the exact structured result JSON object to `{result_path}` before exiting.\n"
            )
        prompt_path.write_text(
            prompt_text.rstrip()
            + "\n\nCommand-adapter result contract:\n"
            + result_instruction,
            encoding="utf-8",
        )
        manifest = {
            "schema_version": 1,
            "server": {
                "name": "pyocd-debug",
                "transport": "stdio",
                "command": "uv",
                "args": [
                    "run",
                    "--project",
                    str(repo_root.resolve()),
                    "--locked",
                    "pyocd-debug-mcp",
                ],
                "cwd": str(repo_root.resolve()),
                "env": {},
            },
            "note": (
                "This is a neutral launch manifest. The configured CLI or wrapper must translate "
                "it to that client's MCP configuration format."
            ),
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        values = {
            "workspace": str(workspace),
            "prompt_path": str(prompt_path),
            "result_path": str(result_path),
            "result_schema_path": str(result_schema_path.resolve()),
            "mcp_manifest_path": str(manifest_path),
            "repo_root": str(repo_root.resolve()),
        }
        cli_version: str | None = None
        if self.config.version_command is not None:
            version_argv = _resolve_executable(_expand(self.config.version_command, values))
            try:
                version = run_owned(
                    list(version_argv),
                    cwd=repo_root,
                    env=_environment(self.config, values),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=min(timeout_seconds, 15.0),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AgentCommandError(f"agent version preflight failed: {exc}") from exc
            if version.returncode != 0:
                raise AgentCommandError("agent version preflight returned nonzero")
            cli_version = (version.stdout or version.stderr or "unknown").strip()[:1024]
        if self.config.registration_check is not None:
            registration_argv = _resolve_executable(_expand(self.config.registration_check, values))
            registration = run_owned(
                list(registration_argv),
                cwd=repo_root,
                env=_environment(self.config, values),
                capture_output=True,
                text=True,
                check=False,
                timeout=min(timeout_seconds, 30.0),
            )
            if registration.returncode != 0:
                raise AgentCommandError("agent MCP registration preflight returned nonzero")
        command = _resolve_executable(_expand(self.config.command, values))
        started_at = _utc_now()
        try:
            completed = run_owned(
                list(command),
                cwd=workspace,
                env=_environment(self.config, values),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentCommandError(f"agent command timed out after {timeout_seconds:g}s") from exc
        except OSError as exc:
            raise AgentCommandError(f"agent command failed to start: {exc}") from exc
        completed_at = _utc_now()
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        metadata: dict[str, object] = {
            "adapter_name": self.config.name,
            "resolved_executable": command[0],
            "argv_template": _redacted_argv(self.config.command),
            "config_path": str(self.config.source_path),
            "config_sha256": self.config.source_sha256,
            "declared_model": self.config.model,
            "declared_effort": self.config.effort,
            "permission_profile": self.config.permission_profile,
            "cli_version": cli_version,
            "mcp_mode": self.config.mcp_mode,
            "result_transport": self.config.result_transport,
            "inherited_env_names": list(self.config.inherit_env),
            "literal_env_names": sorted(self.config.env),
        }
        run = AgentCommandRun(
            completed.returncode,
            stdout,
            stderr,
            result_path,
            prompt_path,
            manifest_path,
            started_at,
            completed_at,
            metadata,
        )
        if self.config.result_transport == "stdout_json":
            result_path.write_text(stdout, encoding="utf-8")
        if not result_path.is_file():
            raise AgentCommandResultError("agent result file is missing", run)
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgentCommandResultError(f"agent result is invalid JSON: {exc}", run) from exc
        if not isinstance(result, dict):
            raise AgentCommandResultError("agent result must be one JSON object", run)
        return run
