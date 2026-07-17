from __future__ import annotations

import ast
from pathlib import Path

from pyocd_debug_mcp.safety.map_build import NO_INTERNALS as SAFETY_NO_INTERNALS
from pyocd_debug_mcp.setup_flow.preflight import (
    NO_INTERNALS_RELAY_INSTRUCTION,
    PreflightEngine,
    PreflightInventory,
    SetupUserInput,
)
from pyocd_debug_mcp.setup_flow.research import ResearchTracker, make_research_request
from pyocd_debug_mcp.tools.unlock import NO_INTERNALS as UNLOCK_NO_INTERNALS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src" / "pyocd_debug_mcp"


def _assert_plain_relay_text(text: str, instruction: str) -> None:
    assert text.strip() == text
    assert instruction in text
    assert not text.startswith(("{", "[", "<"))
    assert "```" not in text
    assert "continuation_id" not in text
    assert '"agent_prompt"' not in text


def test_cc_7_setup_and_research_prompts_are_plain_user_relay_text() -> None:
    blocked = PreflightEngine().evaluate(
        SetupUserInput(
            "m10_board",
            "probe:m10",
            "開発ボード Café",
            "STM32L476RGT6 Exact",
            115200,
        ),
        PreflightInventory(),
    )
    _assert_plain_relay_text(blocked.agent_prompt, NO_INTERNALS_RELAY_INSTRUCTION)

    research = ResearchTracker().prompt(
        make_research_request(
            fact_id="target",
            continuation_token="opaque-token",
            board_id="m10_board",
            mcu_part_number="STM32L476RGT6 Exact",
            unresolved_fact="The exact debug target is unknown.",
            requested_fields=("pyocd_target",),
            authoritative_facts={"probe_family": "stlink"},
        )
    )
    prompt = research["agent_prompt"]
    assert isinstance(prompt, str)
    _assert_plain_relay_text(prompt, NO_INTERNALS_RELAY_INSTRUCTION)
    assert "opaque-token" not in prompt
    assert "m10_board" not in prompt


def test_cc_7_all_relay_domains_carry_an_explicit_no_internals_instruction() -> None:
    for instruction in (
        NO_INTERNALS_RELAY_INSTRUCTION,
        SAFETY_NO_INTERNALS,
        UNLOCK_NO_INTERNALS,
    ):
        folded = instruction.casefold()
        assert "relay" in folded
        assert "structured" in folded
        assert "internal" in folded


def test_cc_7_agent_prompt_construction_never_serializes_a_payload() -> None:
    violations: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parent: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_serializer = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"dumps", "model_dump_json"}
            )
            if not is_serializer:
                continue
            ancestor = parent.get(node)
            while ancestor is not None and not isinstance(
                ancestor, (ast.Assign, ast.Return, ast.Dict)
            ):
                ancestor = parent.get(ancestor)
            if isinstance(ancestor, ast.Dict):
                for key, value in zip(ancestor.keys, ancestor.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "agent_prompt"
                        and node in ast.walk(value)
                    ):
                        violations.append(
                            f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}"
                        )
    assert violations == []
