from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from pyocd_debug_mcp import server

FIXTURE_PATH = Path(__file__).parent / "contracts" / "source-server-tools.json"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _source_contract() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_mcp_tool_contracts_match_the_source_snapshot() -> None:
    expected = _source_contract()
    tools = server.mcp._tool_manager.list_tools()
    actual = {
        tool.name: _sha256(
            _canonical_json(
                {
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
            )
        )
        for tool in tools
    }

    assert actual == expected["tool_contract_sha256"]
    assert set(expected["excluded_tools"]).isdisjoint(actual)


def test_ordinary_tool_implementations_match_the_source_snapshot() -> None:
    expected = _source_contract()
    source_path = Path(server.__file__).resolve()
    syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
    actual = {
        node.name: _sha256(ast.dump(node, include_attributes=False))
        for node in syntax_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        )
    }

    assert actual == expected["tool_ast_sha256"]
    assert set(expected["excluded_tools"]).isdisjoint(actual)
