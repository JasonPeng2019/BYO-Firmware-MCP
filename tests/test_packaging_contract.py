from __future__ import annotations

import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _metadata() -> dict[str, object]:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_public_scripts_and_dependencies_are_byo_only() -> None:
    metadata = _metadata()
    project = metadata["project"]
    assert isinstance(project, dict)

    assert project["scripts"] == {
        "pyocd-collect-artifacts": "pyocd_debug_mcp.artifact_collector:main",
        "pyocd-debug-mcp": "pyocd_debug_mcp.server:main",
        "pyocd-pack-repair": "pyocd_debug_mcp.pack_index_repair:main",
        "pyocd-zephyr-build": "pyocd_debug_mcp.zephyr_build:main",
    }

    dependencies = project["dependencies"]
    assert isinstance(dependencies, list)
    normalized = "\n".join(str(item).lower() for item in dependencies)
    for prohibited in (
        "rich",
        "prompt_toolkit",
        "anthropic",
        "openai",
        "redis",
    ):
        assert prohibited not in normalized

    lock_text = (PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8").lower()
    assert '\nname = "rich"\n' not in lock_text
    assert '\nname = "typer"\n' not in lock_text
    assert '\nname = "prompt-toolkit"\n' not in lock_text


def test_wheel_contains_code_not_checkout_assets() -> None:
    metadata = _metadata()
    tool = metadata["tool"]
    assert isinstance(tool, dict)
    hatch = tool["hatch"]
    assert isinstance(hatch, dict)
    build = hatch["build"]
    assert isinstance(build, dict)
    targets = build["targets"]
    assert isinstance(targets, dict)
    wheel = targets["wheel"]
    assert isinstance(wheel, dict)
    assert wheel["packages"] == ["src/pyocd_debug_mcp"]
    assert "force-include" not in build
    assert "force-include" not in wheel


def test_required_byo_docs_state_checkout_and_proof_limits() -> None:
    required = (
        "README.md",
        "init.md",
        "stage0_setup.md",
        "docs/architecture.md",
        "docs/verification.md",
    )
    combined: list[str] = []
    for relative in required:
        path = PROJECT_ROOT / relative
        assert path.is_file(), relative
        text = path.read_text(encoding="utf-8")
        assert "## Verified" in text
        assert "## Pending verification" in text
        combined.append(text.lower())

    documentation = "\n".join(combined)
    for required_phrase in (
        "checkout-only",
        "inmemorysessionstore",
        "agent-command",
        "nrf52833dk",
        "nucleo_l476rg",
        "nrf52840dk",
        "license",
        "process-tree",
    ):
        assert required_phrase in documentation


def test_every_mcp_tool_contract_has_a_docstring() -> None:
    from pyocd_debug_mcp import server

    tools = server.mcp._tool_manager.list_tools()
    tool_names = {tool.name for tool in tools}

    assert {"reset", "read_core_register", "write_core_register"}.isdisjoint(
        tool_names
    )
    assert {
        "connect_override",
        "reset_and_run",
        "reset_and_halt",
        "connect_under_reset",
        "read_cpu_register",
        "read_execution_state",
        "write_cpu_register",
        "set_execution_state",
        "register_write",
    }.issubset(tool_names)
    assert all(tool.description for tool in tools)
