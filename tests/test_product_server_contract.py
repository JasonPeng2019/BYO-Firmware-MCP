from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pyocd_debug_mcp import server

CONTRACT_PATH = Path(__file__).parent / "contracts" / "product-server-tools.json"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _active_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _resolve_delta(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    delta = json.loads(payload)
    base_ref = delta["base_contract"]
    base_path = path.parent / base_ref["path"]
    base_payload = base_path.read_bytes()
    assert _sha256_bytes(base_payload) == base_ref["sha256"]
    base = json.loads(base_payload)
    baseline = _resolve_delta(base_path) if "base_contract" in base else base
    baseline["tool_contract_sha256"].update(delta["tool_contract_sha256_overrides"])
    baseline["implementation_module_sha256"].update(delta["implementation_module_sha256_overrides"])
    return baseline


def _imported_baseline(contract: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    imported = contract["imported_baseline"]
    path = CONTRACT_PATH.parent / imported["path"]
    payload = path.read_bytes()
    assert _sha256_bytes(payload) == imported["sha256"]
    baseline = _resolve_delta(path)
    baseline["tool_contract_sha256"].update(contract.get("tool_contract_sha256_overrides", {}))
    baseline["implementation_module_sha256"].update(
        contract.get("implementation_module_sha256_overrides", {})
    )
    for tool_name in contract.get("removed_tools", []):
        baseline["tool_contract_sha256"].pop(tool_name, None)
    for module_path in contract.get("removed_implementation_modules", []):
        baseline["implementation_module_sha256"].pop(module_path, None)
    return path, baseline


def test_m10_active_contract_formally_supersedes_the_extraction_named_baseline() -> None:
    contract = _active_contract()

    assert contract["status"] == "active"
    assert contract["milestone"] == "post-M10-debiased-runtime-round-6"
    assert contract["supersedes"] == contract["imported_baseline"]["path"]
    assert (CONTRACT_PATH.parent / contract["supersedes"]).is_file()
    for relative_path in contract["hardening_evidence"].values():
        assert (CONTRACT_PATH.parents[2] / relative_path).is_file(), relative_path
    assert (
        _sha256_bytes((CONTRACT_PATH.parents[2] / "docs" / "plan-tool-contract.md").read_bytes())
        == contract["plan_tool_contract_sha256"]
    )


def test_m10_live_tools_and_implementation_owners_match_the_imported_baseline() -> None:
    contract = _active_contract()
    baseline_path, baseline = _imported_baseline(contract)
    tools = server.mcp._tool_manager.list_tools()
    live_tools = {
        tool.name: _sha256_text(
            _canonical_json(
                {
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
            )
        )
        for tool in tools
    }
    implementations = {
        relative_path: _sha256_text(
            (baseline_path.parents[2] / relative_path).read_text(encoding="utf-8")
        )
        for relative_path in baseline["implementation_module_sha256"]
    }

    assert live_tools == baseline["tool_contract_sha256"]
    assert set(baseline["excluded_tools"]).isdisjoint(live_tools)
    assert implementations == baseline["implementation_module_sha256"]
