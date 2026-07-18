"""Run the setup-only fresh-workspace barrier through a real MCP stdio client.

This trusted runner deliberately has no code-generation, build, flash, UART-write,
callback, command, or arbitrary-argv surface.  Its only successful outcome is a
machine-readable proof that setup and current-run validation made a named board
ready for a separate orchestrator to begin coding.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import platform
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_SUCCESS_VALIDATION = {
    "validation_passed",
    "validation_passed_uart_not_configured",
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_identity_matches(expected: str, observed: object) -> bool:
    if not isinstance(observed, str) or not observed.strip():
        return False
    left = expected.strip().casefold()
    right = observed.strip().casefold()
    if left == right:
        return True
    if left.isdecimal() and right.isdecimal():
        return (left.lstrip("0") or "0") == (right.lstrip("0") or "0")
    return False


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    artifact_root: Path
    display_name: str
    mcu_part_number: str
    probe_uid: str
    uart_id: str
    baudrate: int
    datasheet_path: Path
    timeout_seconds: float = 320.0
    setup_authorized: bool = False

    def validated(self) -> RunnerConfig:
        artifact_root = self.artifact_root.expanduser().resolve()
        datasheet = self.datasheet_path.expanduser().resolve()
        for name, value in (
            ("display_name", self.display_name),
            ("mcu_part_number", self.mcu_part_number),
            ("probe_uid", self.probe_uid),
            ("uart_id", self.uart_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if self.baudrate < 1:
            raise ValueError("baudrate must be positive")
        if not 5.0 <= self.timeout_seconds <= 330.0:
            raise ValueError("timeout_seconds must be between 5 and 330 seconds")
        if self.setup_authorized is not True:
            raise ValueError("one explicit non-destructive setup authorization is required")
        if not datasheet.is_file() or datasheet.suffix.casefold() != ".pdf":
            raise ValueError("datasheet_path must name an existing local PDF")
        return RunnerConfig(
            artifact_root,
            self.display_name.strip(),
            self.mcu_part_number.strip(),
            self.probe_uid.strip(),
            self.uart_id.strip(),
            self.baudrate,
            datasheet,
            self.timeout_seconds,
            True,
        )


class SetupClient(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, object]) -> object: ...


class SetupBarrierError(RuntimeError):
    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        self.evidence = evidence
        super().__init__(message)


def _result_text(result: object) -> str:
    if bool(getattr(result, "isError", False)):
        raise RuntimeError("MCP tool returned a transport-level error")
    content = getattr(result, "content", None)
    if not isinstance(content, list):
        raise RuntimeError("MCP tool response has no content list")
    texts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            raise RuntimeError("MCP tool response has no exact text content")
        texts.append(text)
    if not texts:
        raise RuntimeError("MCP tool response has no exact text content")
    return "\n".join(texts)


def _json_result(result: object, tool: str) -> dict[str, Any]:
    try:
        payload = json.loads(_result_text(result))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{tool} did not return JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{tool} did not return an object")
    return payload


def _evidence_arguments(value: object) -> object:
    """Remove replayable authority fields from the diagnostic transcript."""

    if isinstance(value, Mapping):
        return {
            str(key): _evidence_arguments(item)
            for key, item in value.items()
            if not any(
                marker in str(key).casefold()
                for marker in ("permission", "approval", "authorization", "grant")
            )
        }
    if isinstance(value, list):
        return [_evidence_arguments(item) for item in value]
    return value


async def execute_setup_only(client: SetupClient, config: RunnerConfig) -> dict[str, Any]:
    """Drive the fixed setup barrier; there is intentionally no post-setup hook."""

    selected = config.validated()
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "started_at": _timestamp(),
        "finished_at": None,
        "code_phase_started": False,
        "setup_authorization_present": True,
        "identity": {
            "display_name": selected.display_name,
            "mcu_part_number": selected.mcu_part_number,
            "probe_uid": selected.probe_uid,
            "uart_id": selected.uart_id,
            "baudrate": selected.baudrate,
            "datasheet_path": str(selected.datasheet_path),
        },
        "operations": [],
    }

    async def call(name: str, arguments: dict[str, object], timeout: float = 15.0) -> object:
        started_at = _timestamp()
        row: dict[str, Any] = {
            "tool": name,
            "arguments": _evidence_arguments(arguments),
            "started_at": started_at,
            "finished_at": None,
            "status": "running",
        }
        evidence["operations"].append(row)
        try:
            result = await asyncio.wait_for(
                client.call_tool(name, arguments),
                timeout=min(timeout, selected.timeout_seconds),
            )
            text = _result_text(result)
            row.update(status="completed", finished_at=_timestamp(), response_text=text)
            return result
        except BaseException as exc:
            row.update(
                status="failed",
                finished_at=_timestamp(),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise

    def stop(message: str) -> NoReturn:
        evidence.update(status="failed", finished_at=_timestamp(), failure=message)
        raise SetupBarrierError(message, evidence)

    try:
        handshake = _result_text(await call("initialization_handshake", {}))
        if "run_id:" not in handshake or "started_at:" not in handshake:
            stop("initialization handshake did not disclose the current Server Run")

        overview = _json_result(
            await call(
                "setup_overview",
                {"board_names": [selected.display_name]},
            ),
            "setup_overview",
        )
        if overview.get("status") == "setup_assignment_required":
            connections = overview.get("connections")
            if not isinstance(connections, list):
                stop("setup_overview requested assignment without friendly connections")
            matching_connections = [
                item
                for item in connections
                if isinstance(item, Mapping)
                and isinstance(item.get("connection_id"), str)
                and _stable_identity_matches(
                    selected.probe_uid,
                    str(item["connection_id"]).removeprefix("probe:"),
                )
            ]
            if len(matching_connections) != 1:
                stop("selected probe did not match exactly one server connection")
            connection_id = matching_connections[0]["connection_id"]
            assert isinstance(connection_id, str)
            overview = _json_result(
                await call(
                    "setup_overview",
                    {
                        "board_names": [selected.display_name],
                        "connection_assignments": {
                            selected.display_name: connection_id
                        },
                    },
                ),
                "setup_overview",
            )
        routes = overview.get("routes")
        if not isinstance(routes, list) or len(routes) != 1 or not isinstance(routes[0], Mapping):
            stop("setup_overview did not return one exact fresh-board route")
        route = routes[0]
        template = route.get("plan_action_parameters_template")
        board_id = route.get("board_id")
        if (
            route.get("route") != "setup"
            or not isinstance(board_id, str)
            or not board_id
            or not isinstance(template, Mapping)
            or not isinstance(template.get("connection_id"), str)
            or not _stable_identity_matches(
                selected.probe_uid,
                str(template["connection_id"]).removeprefix("probe:"),
            )
        ):
            stop("setup_overview route does not match the selected fresh board and probe")
        identity = evidence["identity"]
        assert isinstance(identity, dict)
        identity["board_id"] = board_id
        server_probe_id = str(template["connection_id"]).removeprefix("probe:")

        await call(
            "load_setup_tool",
            {"board_id": board_id, "tool_name": "board_setup-plan"},
        )
        null_guidance = _result_text(
            await call(
                "board_setup-plan",
                {
                    "board_id": None,
                    "hypothesis": None,
                    "strategy": None,
                    "hypothesis_made": None,
                    "strategy_evaluated": None,
                    "expected_fail_return": None,
                    "expected_success_return": None,
                    "max_calls": None,
                    "max_calls_buffer": None,
                    "action_parameters": None,
                    "user_permission": None,
                },
            )
        )
        required_guidance = ("board", "MCU", "datasheet", "board_validate")
        if any(token.casefold() not in null_guidance.casefold() for token in required_guidance):
            stop("board_setup-plan NULL guidance omitted required setup routing")

        action_parameters: dict[str, object] = {
            "mode": "setup",
            "connection_id": template["connection_id"],
            "display_name": selected.display_name,
            "mcu_part_number": selected.mcu_part_number,
            "requires_uart": True,
            "serial_baudrate": selected.baudrate,
            "serial_id": selected.uart_id,
            "datasheet_path": str(selected.datasheet_path),
        }
        plan = {
            "board_id": board_id,
            "hypothesis": (
                "The explicitly identified board, probe, UART, and reviewed datasheet can "
                "complete non-destructive first-time setup."
            ),
            "strategy": (
                "Verify exact inventory and silicon, commit staged configuration and safety "
                "evidence, validate this live connection, then stop at the readiness barrier."
            ),
            "hypothesis_made": True,
            "strategy_evaluated": True,
            "expected_fail_return": "A typed terminal setup status with an exact remedy.",
            "expected_success_return": "setup_completed followed by ready_for_code true.",
            "max_calls": 1,
            "max_calls_buffer": 0,
            "action_parameters": action_parameters,
            "user_permission": "one-time",
        }
        plan_text = _result_text(await call("board_setup-plan", plan))
        if "accepted" not in plan_text.casefold():
            stop("board_setup-plan was not accepted exactly")

        setup = _json_result(
            await call("board_setup", {"board_id": board_id, **action_parameters}, 305.0),
            "board_setup",
        )
        if setup.get("status") == "setup_needs_user_input":
            choices = setup.get("choices")
            if not isinstance(choices, list):
                stop("setup requested a choice without friendly choices")
            expected_ids = {selected.probe_uid, selected.uart_id}
            matches = [
                choice
                for choice in choices
                if isinstance(choice, Mapping)
                and (
                    choice.get("choice_id") in expected_ids
                    or selected.uart_id.casefold()
                    in str(choice.get("label", "")).casefold()
                )
            ]
            if len(matches) != 1 or not isinstance(matches[0].get("choice_id"), str):
                stop("explicit probe/UART identity did not match one friendly setup choice")
            continuation = setup.get("continuation_id")
            if not isinstance(continuation, str):
                stop("setup choice has no continuation identifier")
            accepted = _json_result(
                await call(
                    "continue_setup",
                    {
                        "board_id": board_id,
                        "continuation_id": continuation,
                        "response": {"choice_id": matches[0]["choice_id"]},
                    },
                ),
                "continue_setup",
            )
            if accepted.get("status") != "setup_continuation_accepted":
                stop("the explicit setup choice was not accepted")
            setup = _json_result(
                await call(
                    "board_fix_setup",
                    {"board_id": board_id, **action_parameters},
                    305.0,
                ),
                "board_fix_setup",
            )
        if setup.get("status") != "setup_completed":
            stop(f"setup stopped with terminal status {setup.get('status')!r}")

        await call(
            "load_setup_tool",
            {"board_id": board_id, "tool_name": "board_validate"},
        )
        validation = _json_result(
            await call(
                "board_validate",
                {"board_id": board_id, "probe_id": server_probe_id},
                125.0,
            ),
            "board_validate",
        )
        if validation.get("status") not in _SUCCESS_VALIDATION:
            stop(f"current-run validation did not pass: {validation.get('status')!r}")

        readiness = _json_result(
            await call("get_setup_status", {"board_id": board_id}),
            "get_setup_status",
        )
        if (
            readiness.get("status") != "setup_ready"
            or readiness.get("board_id") != board_id
            or readiness.get("configuration_ready") is not True
            or readiness.get("live_session_ready") is not True
            or readiness.get("ready_for_code") is not True
        ):
            stop("setup readiness barrier did not become true for the exact board")
        resolved_uart = readiness.get("resolved_uart")
        if not isinstance(resolved_uart, Mapping):
            stop("setup readiness did not disclose the resolved UART identity")
        stable_uart_matches = _stable_identity_matches(
            selected.uart_id, resolved_uart.get("usb_serial")
        ) or _stable_identity_matches(selected.uart_id, resolved_uart.get("serial_id"))
        if not stable_uart_matches:
            stop("resolved UART identity does not match the explicit stable selection")
        resolved_probe = readiness.get("resolved_probe")
        if not isinstance(resolved_probe, Mapping) or not _stable_identity_matches(
            selected.probe_uid, resolved_probe.get("probe_uid")
        ):
            stop("resolved probe identity does not match the explicit selection")

        identity["resolved_uart"] = dict(resolved_uart)
        evidence.update(
            status="pass",
            finished_at=_timestamp(),
            setup_terminal_status=setup["status"],
            validation_terminal_status=validation["status"],
            readiness=readiness,
        )
        return evidence
    except SetupBarrierError:
        raise
    except BaseException as exc:
        stop(f"setup runner failed closed: {type(exc).__name__}: {exc}")
        raise AssertionError("unreachable")


def _atomic_write_evidence(config: RunnerConfig, evidence: Mapping[str, Any]) -> Path:
    destination = config.artifact_root / "acceptance" / "fresh-setup-evidence.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_suffix(".json.tmp")
    staged.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    staged.replace(destination)
    return destination


async def _run_stdio(config: RunnerConfig) -> dict[str, Any]:
    selected = config.validated()
    environment = dict(os.environ)
    environment["BYO_MCP_ARTIFACT_ROOT"] = str(selected.artifact_root)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pyocd_debug_mcp.server"],
        cwd=str(Path(__file__).parents[1]),
        env=environment,
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await asyncio.wait_for(session.initialize(), timeout=15.0)
            return await execute_setup_only(session, selected)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--mcu-part-number", required=True)
    parser.add_argument("--probe-uid", required=True)
    parser.add_argument("--uart-id", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--datasheet-path", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=320.0)
    parser.add_argument(
        "--authorize-setup",
        action="store_true",
        required=True,
        help="Explicitly authorize this one bounded non-destructive setup attempt.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = RunnerConfig(
        args.artifact_root,
        args.display_name,
        args.mcu_part_number,
        args.probe_uid,
        args.uart_id,
        args.baudrate,
        args.datasheet_path,
        args.timeout_seconds,
        args.authorize_setup,
    ).validated()
    base_evidence: dict[str, Any] = {
        "schema_version": 1,
        "status": "failed",
        "started_at": _timestamp(),
        "finished_at": None,
        "code_phase_started": False,
        "runner": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "package": importlib.metadata.version("pyocd-debug-mcp"),
            "mcp_sdk": importlib.metadata.version("mcp"),
        },
        "configuration": {**asdict(config), "artifact_root": str(config.artifact_root), "datasheet_path": str(config.datasheet_path)},
    }
    try:
        evidence = asyncio.run(_run_stdio(config))
        evidence["runner"] = base_evidence["runner"]
        evidence_path = _atomic_write_evidence(config, evidence)
        print(json.dumps({"status": "pass", "evidence": str(evidence_path)}))
    except SetupBarrierError as exc:
        evidence = exc.evidence
        evidence["runner"] = base_evidence["runner"]
        evidence_path = _atomic_write_evidence(config, evidence)
        raise SystemExit(
            f"Fresh setup stopped; evidence: {evidence_path}; reason: {exc}"
        ) from exc
    except BaseException as exc:
        base_evidence.update(finished_at=_timestamp(), failure=f"{type(exc).__name__}: {exc}")
        evidence_path = _atomic_write_evidence(config, base_evidence)
        raise SystemExit(
            f"Fresh setup failed closed; evidence: {evidence_path}; reason: {exc}"
        ) from exc


if __name__ == "__main__":
    main()
