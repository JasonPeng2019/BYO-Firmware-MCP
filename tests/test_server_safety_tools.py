from __future__ import annotations

from pyocd_debug_mcp import server


def test_v1_artifact_fingerprint_helpers_are_removed() -> None:
    assert not hasattr(server, "_refresh_tracked_artifact_hashes")
    assert not hasattr(server, "_build_region_replacements")
    assert not hasattr(server, "_live_safety_inputs")


def test_obsolete_safety_setup_is_not_public_and_refresh_is_the_only_maintenance_tool() -> None:
    assert not hasattr(server, "_run_board_safety_setup")
    assert server.mcp._tool_manager.get_tool("board_safety_setup") is None
    assert server.mcp._tool_manager.get_tool("board_safety_refresh") is not None


def test_public_refresh_description_rejects_routine_build_refresh() -> None:
    tool = server.mcp._tool_manager.get_tool("board_safety_refresh")
    assert tool is not None
    description = (tool.description or "").casefold()
    assert "routine firmware rebuild" in description
    assert "missing/corrupt/old map" in description
