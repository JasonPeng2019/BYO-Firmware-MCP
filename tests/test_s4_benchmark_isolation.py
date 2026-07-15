from __future__ import annotations

from pathlib import Path
from types import ModuleType

from pyocd_debug_mcp import benchmark_support as r11
from pyocd_debug_mcp import board_config, probe_inventory, reference_smoke, runtime_resources
from pyocd_debug_mcp.services import session_runtime, target_control


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_ROOT = PROJECT_ROOT.parent
PACKAGE_ROOT = PROJECT_ROOT / "src" / "pyocd_debug_mcp"


def _module_path(module: ModuleType) -> Path:
    assert module.__file__ is not None
    return Path(module.__file__).resolve()


def test_r11_import_closure_comes_from_the_standalone_package() -> None:
    modules = (
        r11,
        board_config,
        probe_inventory,
        reference_smoke,
        runtime_resources,
        session_runtime,
        target_control,
    )

    for module in modules:
        assert _module_path(module).is_relative_to(PACKAGE_ROOT)

    assert _module_path(r11) == PACKAGE_ROOT / "benchmark_support.py"
    assert _module_path(r11) != PARENT_ROOT / "src" / "pyocd_debug_mcp" / "benchmark_support.py"


def test_r11_static_roots_are_owned_by_byo_server() -> None:
    assert r11.REPO_ROOT == PROJECT_ROOT
    assert r11._require_repo_root() == PROJECT_ROOT
    assert r11.CASES_ROOT == PROJECT_ROOT / "tests" / "cases"
    assert r11.SUITES_PATH == PROJECT_ROOT / "tests" / "cases" / "suites.yaml"
    assert r11.RESULT_SCHEMA_PATH == (PROJECT_ROOT / "tests" / "cases" / "r11_result_schema.json")
    assert session_runtime.RUNS_ROOT == PROJECT_ROOT / "runs"
    assert r11.WORKSPACES_ROOT == PROJECT_ROOT / "runs" / "_r11_workspaces"

    assert r11.CASES_ROOT != PARENT_ROOT / "tests" / "cases"
    assert session_runtime.RUNS_ROOT != PARENT_ROOT / "runs"
    assert r11.WORKSPACES_ROOT != PARENT_ROOT / "runs" / "_r11_workspaces"


def test_every_r11_firmware_source_resolves_inside_byo_server() -> None:
    case_dirs = sorted(path for path in r11.CASES_ROOT.iterdir() if path.is_dir())
    cases = [r11.load_case(path.name) for path in case_dirs if (path / "case.yaml").is_file()]

    assert len(cases) == 18
    for case in cases:
        source_root = r11._workspace_source_root(case)
        assert source_root.is_relative_to(PROJECT_ROOT / "firmware")
        assert not source_root.is_relative_to(PARENT_ROOT / "firmware")
