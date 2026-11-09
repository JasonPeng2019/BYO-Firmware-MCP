"""Shared workspace fixture support fixtures; no collected cases."""

from __future__ import annotations
import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path


def _workspace_source() -> Path:
    """Select explicit fixture input or the supported sibling source layout."""
    configured = os.environ.get("BYO_MCP_TEST_WORKSPACE_ROOT")
    if configured:
        # A bad explicit input must stay visible rather than selecting other code.
        return Path(configured).expanduser().resolve(strict=True)
    siblings = Path(__file__).resolve().parents[2]
    checkout = siblings / "CodexClaudeWorkflow"
    if (checkout / "internal" / "manual_permissions.py").is_file():
        return checkout
    return siblings / ".agent-workspace"


WORKSPACE = _workspace_source()


WORKSPACE_HELPER = WORKSPACE / "internal" / "manual_permissions.py"


ATTACH_PROJECT = WORKSPACE / "bin" / "attach-project"


def _workspace_helper(path: Path = WORKSPACE_HELPER):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("tiered_workspace_manual_permissions", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load shipped workspace helper: {WORKSPACE_HELPER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _attach_project_module():  # type: ignore[no-untyped-def]
    loader = SourceFileLoader("tiered_workspace_attach_project", str(ATTACH_PROJECT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load attached-workspace installer: {ATTACH_PROJECT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
