"""Persistent provider-wrapper process for one Server A agentic call."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Callable, Protocol

from pyocd_debug_mcp.kernel.processes import (
    ProcessMarkerStore,
    popen_owned,
    terminate_process_group,
    validate_argv,
)
from pyocd_debug_mcp.tools.handshake import SERVER_B_CONTRACT_VERSION, SERVER_B_PRODUCT_ID
from pyocd_debug_mcp.turnkey.server_b_probe import ServerBIdentity, verify_server_b


class ProviderError(RuntimeError):
    """The configured middleman provider could not complete its protocol."""


class ProviderTerminationError(ProviderError):
    """The middleman remained alive after bounded graceful and forced termination."""


class MiddlemanSession(Protocol):
    def exchange(self, prompt: str, *, timeout_seconds: float) -> object: ...
    def close(self) -> None: ...


class MiddlemanFactory(Protocol):
    def open(
        self, *, workspace: Path, server_b_url: str, artifact_root: Path
    ) -> MiddlemanSession: ...


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    command: tuple[str, ...]
    provider_id: str
    inherit_env: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            command = validate_argv(self.command)
        except ValueError as exc:
            raise ProviderError(f"provider command is invalid: {exc}") from exc
        object.__setattr__(self, "command", command)
        if not self.provider_id.strip():
            raise ProviderError("provider_id must be non-empty text")
        object.__setattr__(self, "provider_id", self.provider_id.strip().casefold())

    @classmethod
    def load(cls, path: Path) -> ProviderConfig:
        try:
            document = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"middleman provider config is unreadable: {exc}") from exc
        if not isinstance(document, dict) or set(document) != {
            "schema_version", "provider_id", "command", "inherit_env", "env"
        }:
            raise ProviderError(
                "provider config requires exactly schema_version, provider_id, command, "
                "inherit_env, and env"
            )
        if document["schema_version"] != 2:
            raise ProviderError("middleman provider schema_version must be 2")
        provider_id = document["provider_id"]
        command = document["command"]
        inherited = document["inherit_env"]
        env = document["env"]
        if not isinstance(command, list):
            raise ProviderError("provider command must be an explicit argv array")
        if not isinstance(provider_id, str):
            raise ProviderError("provider_id must be text")
        if not isinstance(inherited, list) or any(not isinstance(item, str) for item in inherited):
            raise ProviderError("inherit_env must be an array of names")
        if not isinstance(env, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()
        ):
            raise ProviderError("provider env must map names to string values")
        return cls(tuple(command), provider_id, tuple(inherited), dict(env))


class SubprocessMiddlemanSession:
    def __init__(
        self,
        process: subprocess.Popen[str],
        marker: Path | None,
        marker_store: ProcessMarkerStore,
    ) -> None:
        self.process = process
        self.marker = marker
        self.marker_store = marker_store
        self._reader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="middleman-stdout")
        self._close_lock = threading.Lock()
        self._closed = False
        self._stderr_lines: deque[str] = deque(maxlen=200)
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="middleman-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        try:
            for line in stream:
                self._stderr_lines.append(line.rstrip())
        except (OSError, ValueError):
            return

    def _stderr_tail(self, limit: int = 2048) -> str:
        return "\n".join(self._stderr_lines)[-limit:]

    def await_ready(
        self,
        server_b_url: str,
        provider_id: str,
        server_b_identity: ServerBIdentity,
        *,
        timeout_seconds: float = 20.0,
    ) -> None:
        stdout = self._required_stream(self.process.stdout, "stdout")
        future = self._reader.submit(stdout.readline)
        try:
            line = future.result(timeout=timeout_seconds)
        except FutureTimeout as exc:
            terminate_process_group(self.process)
            raise ProviderError("middleman did not report Server B readiness in time") from exc
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProviderError("middleman readiness frame was not JSON") from exc
        expected = {
            "type": "ready",
            "provider_id": provider_id,
            "server_b_url": server_b_url,
            "server_b_product_id": SERVER_B_PRODUCT_ID,
            "server_b_contract_version": SERVER_B_CONTRACT_VERSION,
            "server_b_run_id": server_b_identity.run_id,
            "mcp_initialized": True,
            "tools_listed": True,
        }
        if frame != expected:
            raise ProviderError(
                "middleman must initialize MCP at the exact Server B URL, list its tools, and "
                "return the exact documented readiness frame"
            )

    @staticmethod
    def _required_stream(value: IO[str] | None, name: str) -> IO[str]:
        if value is None:
            raise ProviderError(f"middleman process has no {name} pipe")
        return value

    def exchange(self, prompt: str, *, timeout_seconds: float) -> object:
        if timeout_seconds <= 0:
            raise ProviderError("middleman response timeout must be positive")
        if self.process.poll() is not None:
            raise ProviderError(
                f"middleman process exited before its reply (exit {self.process.returncode})"
            )
        stdin = self._required_stream(self.process.stdin, "stdin")
        stdout = self._required_stream(self.process.stdout, "stdout")
        try:
            stdin.write(json.dumps({"type": "prompt", "prompt": prompt}) + "\n")
            stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ProviderError(f"middleman input pipe failed: {exc}") from exc
        future = self._reader.submit(stdout.readline)
        try:
            line = future.result(timeout=timeout_seconds)
        except FutureTimeout as exc:
            terminate_process_group(self.process)
            raise ProviderError(
                f"middleman reply timed out after {timeout_seconds:g} seconds"
            ) from exc
        if not line:
            raise ProviderError(
                "middleman closed stdout without a decision; "
                f"stderr_tail={self._stderr_tail()!r}"
            )
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            # The controller treats this like every other schema-invalid reply and spends one
            # iteration rather than allowing provider text to escape into the user contract.
            return {"__invalid_json__": str(exc), "__raw__": line[:2048]}

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        termination_failed = False
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        finally:
            terminate_process_group(self.process)
            termination_failed = self.process.poll() is None
            if not termination_failed:
                self.marker_store.remove(self.marker)
            self._reader.shutdown(wait=False, cancel_futures=True)
            self._stderr_thread.join(timeout=1.0)
        if termination_failed:
            raise ProviderTerminationError(
                "middleman process remained alive after forced termination; its recovery marker "
                "was retained for bounded startup cleanup"
            )


class SubprocessMiddlemanFactory:
    """Start one provider-neutral JSON-lines wrapper per tool invocation."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        endpoint_verifier: Callable[[str], ServerBIdentity | None] = verify_server_b,
    ) -> None:
        self.config = config
        self.endpoint_verifier = endpoint_verifier

    @staticmethod
    def from_environment() -> SubprocessMiddlemanFactory:
        raw = os.environ.get("BYO_MIDDLEMAN_CONFIG", "").strip()
        if not raw:
            raise ProviderError(
                "BYO_MIDDLEMAN_CONFIG must name the operator-owned provider-wrapper config"
            )
        config = ProviderConfig.load(Path(raw))
        client_provider = os.environ.get("BYO_CLIENT_PROVIDER", "").strip().casefold()
        if not client_provider:
            raise ProviderError(
                "BYO_CLIENT_PROVIDER must identify the outer Client A provider"
            )
        if client_provider != config.provider_id:
            raise ProviderError(
                f"middleman provider {config.provider_id!r} does not match Client A provider "
                f"{client_provider!r}"
            )
        return SubprocessMiddlemanFactory(config)

    def _environment(self, server_b_url: str, artifact_root: Path) -> dict[str, str]:
        base_names = ("PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE")
        names = tuple(dict.fromkeys((*base_names, *self.config.inherit_env)))
        env = {name: os.environ[name] for name in names if name in os.environ}
        env.update(self.config.env)
        env["BYO_SERVER_B_URL"] = server_b_url
        env["BYO_TURNKEY_ARTIFACT_ROOT"] = str(artifact_root)
        env["BYO_MIDDLEMAN_PROVIDER_ID"] = self.config.provider_id
        return env

    def open(
        self, *, workspace: Path, server_b_url: str, artifact_root: Path
    ) -> MiddlemanSession:
        identity = self.endpoint_verifier(server_b_url)
        if identity is None:
            raise ProviderError(
                "Server B endpoint failed product identity and guarded-capability verification"
            )
        executable = shutil.which(self.config.command[0])
        if executable is None and not Path(self.config.command[0]).is_file():
            raise ProviderError(f"middleman executable is unavailable: {self.config.command[0]}")
        argv: Sequence[str] = (
            str(Path(self.config.command[0]).resolve())
            if Path(self.config.command[0]).is_file()
            else str(executable),
            *self.config.command[1:],
        )
        marker_store = ProcessMarkerStore()
        try:
            process, marker = popen_owned(
                argv,
                marker_store=marker_store,
                cwd=workspace,
                env=self._environment(server_b_url, artifact_root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise ProviderError(f"middleman process could not start: {exc}") from exc
        session = SubprocessMiddlemanSession(process, marker, marker_store)
        try:
            session.await_ready(server_b_url, self.config.provider_id, identity)
        except BaseException:
            session.close()
            raise
        return session


class EnvironmentMiddlemanFactory:
    """Resolve operator configuration only when an agentic action actually runs."""

    def open(
        self, *, workspace: Path, server_b_url: str, artifact_root: Path
    ) -> MiddlemanSession:
        return SubprocessMiddlemanFactory.from_environment().open(
            workspace=workspace,
            server_b_url=server_b_url,
            artifact_root=artifact_root,
        )
