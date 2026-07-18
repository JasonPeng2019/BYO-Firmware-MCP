from __future__ import annotations

import ast
import inspect
import math
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from pyocd_debug_mcp import server
from pyocd_debug_mcp.firmstore.store import (
    PERSISTED_AUTHORITY_KEYS,
    FirmStore,
    PersistedAuthorityError,
)
from pyocd_debug_mcp.kernel.finalizers import ELIGIBLE_FINALIZER_TOOLS, parse_finalizer
from pyocd_debug_mcp.kernel.operations import operation_timeout_seconds
from pyocd_debug_mcp.setup_flow.research import (
    ResearchError,
    ResearchTracker,
    ValidationOutcome,
    make_research_request,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src" / "pyocd_debug_mcp"
SERVER_PATH = SOURCE_ROOT / "server.py"
SERVER_B_HTTP_PATH = SOURCE_ROOT / "server_b_http.py"


def _schema_field_names(value: object) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            names.update(str(name) for name in properties)
        for child in value.values():
            names.update(_schema_field_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_schema_field_names(child))
    return names


def _production_calls(function_name: str) -> list[tuple[Path, ast.Call]]:
    matches: list[tuple[Path, ast.Call]] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == function_name:
                matches.append((path, node))
    return matches


def test_cc_1_server_b_stdio_default_and_http_entrypoint_is_loopback_only() -> None:
    transport = inspect.signature(FastMCP.run).parameters["transport"]
    assert transport.default == "stdio"

    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"), filename=str(SERVER_PATH))
    imported_roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert {"socket", "uvicorn", "aiohttp", "websockets"}.isdisjoint(imported_roots)

    main = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
    )
    run_calls = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_server_b"
    ]
    assert len(run_calls) == 1
    assert len(run_calls[0].args) == 1
    assert isinstance(run_calls[0].args[0], ast.Constant)
    assert run_calls[0].args[0].value == "stdio"

    http_source = SERVER_B_HTTP_PATH.read_text(encoding="utf-8")
    assert '{"127.0.0.1", "::1", "localhost"}' in http_source
    assert "Server B HTTP host must remain loopback-only" in http_source


def test_cc_4_and_cc_5_public_schemas_expose_no_shell_or_authority_write_route() -> None:
    tools = server.mcp._tool_manager.list_tools()
    names = {tool.name for tool in tools}
    fields = set().union(*(_schema_field_names(tool.parameters) for tool in tools))

    assert {
        "command",
        "cmd",
        "shell",
        "shell_command",
        "argv",
        "executable_path",
    }.isdisjoint(fields)
    assert {
        "open_gate",
        "set_gate",
        "write_profile",
        "save_profile",
        "write_memory_map",
        "persist_permission",
        "persist_plan",
    }.isdisjoint(names)
    assert {
        "allowed_range",
        "allowed_ranges",
        "allow_range",
        "memory_regions",
        "profile_document",
        "gate_state",
        "permission_state",
        "plan_state",
    }.isdisjoint(fields)

    assert ELIGIBLE_FINALIZER_TOOLS == {"read_serial", "write_serial"}
    with pytest.raises(ValueError, match="shell strings"):
        parse_finalizer("read_serial", "reset && erase")
    with pytest.raises(ValueError, match="only structured"):
        parse_finalizer("read_serial", {"action": "shell", "command": "erase"})


def test_cc_4_research_cannot_mutate_exact_part_number() -> None:
    request = make_research_request(
        fact_id="target",
        continuation_token="m10-continuation",
        board_id="m10_board",
        mcu_part_number="STM32L476RGT6 Exact User Value",
        unresolved_fact="The exact pyOCD target is unknown.",
        requested_fields=("pyocd_target",),
        authoritative_facts={"probe_family": "stlink"},
    )
    with pytest.raises(ResearchError) as refusal:
        ResearchTracker().validate_reply(
            request,
            {
                "pyocd_target": "stm32l476rgtx",
                "mcu_part_number": "silently-changed",
            },
            lambda _candidate: ValidationOutcome(True),
        )
    assert refusal.value.code == "research/immutable-field"
    assert request.authoritative_facts["mcu_part_number"] == request.mcu_part_number


def test_cc_4_firmstore_rejects_every_authority_field_at_any_depth(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    for index, field in enumerate(sorted(PERSISTED_AUTHORITY_KEYS)):
        target = store.layout.setup / f"authority-{index}" / "report.json"
        with pytest.raises(PersistedAuthorityError, match=field):
            store.atomic_write_json(target, {"evidence": [{field: "forbidden"}]})
        assert not target.exists()


def test_cc_13_every_registered_dispatch_has_a_finite_timeout() -> None:
    for tool in server.mcp._tool_manager.list_tools():
        timeout = operation_timeout_seconds(tool.name, {})
        assert math.isfinite(timeout) and timeout > 0, tool.name

    calls = _production_calls("dispatch")
    assert calls, "the managed dispatch boundary must remain observable to this audit"
    missing: list[str] = []
    for path, call in calls:
        has_positional_timeout = len(call.args) >= 4
        has_keyword_timeout = any(keyword.arg == "timeout" for keyword in call.keywords)
        if not has_positional_timeout and not has_keyword_timeout:
            missing.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{call.lineno}")
    assert missing == []
