from __future__ import annotations

import importlib.util
from pathlib import Path


def test_server_module_import_matches_mcp_dev_style() -> None:
    server_path = Path("src/pyocd_debug_mcp/server.py").resolve()
    spec = importlib.util.spec_from_file_location("server_module", server_path)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "mcp")
    assert hasattr(module, "connect")
    assert hasattr(module, "read_symbol_u32")
