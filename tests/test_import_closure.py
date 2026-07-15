from __future__ import annotations

import ast
import importlib
import importlib.util
import pkgutil
from pathlib import Path

import pyocd_debug_mcp

PACKAGE_ROOT = Path(pyocd_debug_mcp.__file__).resolve().parent
FORBIDDEN_MODULES = {
    "pyocd_debug_mcp.brain",
    "pyocd_debug_mcp.ux",
    "pyocd_debug_mcp.services.codex_activity",
    "pyocd_debug_mcp.services.codex_app_server",
}


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def test_every_local_import_resolves_inside_the_extracted_package() -> None:
    local_imports: set[str] = set()
    for source_path in PACKAGE_ROOT.rglob("*.py"):
        syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(syntax_tree):
            if isinstance(node, ast.Import):
                local_imports.update(
                    alias.name for alias in node.names if alias.name.startswith("pyocd_debug_mcp")
                )
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and node.module.startswith("pyocd_debug_mcp")
            ):
                local_imports.add(node.module)

    for module_name in sorted(local_imports):
        spec = importlib.util.find_spec(module_name)
        assert spec is not None, module_name
        if spec.origin is not None:
            assert _is_within(Path(spec.origin).resolve(), PACKAGE_ROOT), module_name


def test_every_extracted_module_imports_from_the_standalone_tree() -> None:
    module_names = {
        pyocd_debug_mcp.__name__,
        *(
            info.name
            for info in pkgutil.walk_packages(
                pyocd_debug_mcp.__path__,
                prefix=f"{pyocd_debug_mcp.__name__}.",
            )
        ),
    }

    assert FORBIDDEN_MODULES.isdisjoint(module_names)
    for module_name in sorted(module_names):
        module = importlib.import_module(module_name)
        assert module.__file__ is not None, module_name
        module_path = Path(module.__file__).resolve()
        assert _is_within(module_path, PACKAGE_ROOT), module_name


def test_brain_and_codex_runtime_files_are_absent() -> None:
    assert not (PACKAGE_ROOT / "brain").exists()
    assert not (PACKAGE_ROOT / "ux").exists()
    assert not (PACKAGE_ROOT / "services" / "codex_activity.py").exists()
    assert not (PACKAGE_ROOT / "services" / "codex_app_server.py").exists()
