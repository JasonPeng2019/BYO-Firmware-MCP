from __future__ import annotations

import ast
from pathlib import Path

from pyocd_debug_mcp.kernel.operations import (
    DEFAULT_OPERATION_TIMEOUT_SECONDS,
    FLASH_OPERATION_TIMEOUT_SECONDS,
    operation_timeout_seconds,
)

PROJECT_ROOT = Path(__file__).parents[1]
PRODUCTION_PYTHON = (
    *sorted((PROJECT_ROOT / "src").rglob("*.py")),
    PROJECT_ROOT / "host_bootstrap.py",
    PROJECT_ROOT / "stage0_check.py",
)


def _calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def test_every_production_subprocess_uses_the_single_owned_abstraction() -> None:
    violations: list[str] = []
    for path in PRODUCTION_PYTHON:
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        for call in _calls(path):
            function = call.func
            if not (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "subprocess"
                and function.attr in {"run", "Popen", "call", "check_call", "check_output"}
            ):
                continue
            if relative != "src/pyocd_debug_mcp/kernel/processes.py":
                violations.append(f"{relative}:{call.lineno}:{function.attr}")
    assert violations == []


def test_every_run_owned_call_has_an_explicit_finite_timeout_argument() -> None:
    missing: list[str] = []
    for path in PRODUCTION_PYTHON:
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        for call in _calls(path):
            if not isinstance(call.func, ast.Name) or call.func.id != "run_owned":
                continue
            timeout = next((item.value for item in call.keywords if item.arg == "timeout"), None)
            if timeout is None or (isinstance(timeout, ast.Constant) and timeout.value is None):
                missing.append(f"{relative}:{call.lineno}")
    assert missing == []


def test_all_operation_timeout_classes_are_positive_and_finite() -> None:
    assert operation_timeout_seconds("get_state") == DEFAULT_OPERATION_TIMEOUT_SECONDS
    assert operation_timeout_seconds("flash_application") == FLASH_OPERATION_TIMEOUT_SECONDS
    representative_tools = (
        "connect",
        "disconnect",
        "read_cpu_register",
        "write_cpu_register",
        "read_memory_address",
        "write_memory",
        "read_serial",
        "write_serial",
        "set_breakpoint",
        "target_unlock",
    )
    for tool_name in representative_tools:
        assert 0 < operation_timeout_seconds(tool_name) < float("inf")
