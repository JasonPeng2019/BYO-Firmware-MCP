from __future__ import annotations

import tomllib
from pathlib import Path

import host_bootstrap
import stage0_check
from pyocd_debug_mcp import board_config, pack_provision, reference_artifacts, zephyr_build

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_asset_roots_resolve_inside_byo_server() -> None:
    assert board_config.DEFAULT_BOARD_CONFIG_DIR == PROJECT_ROOT / "boards"
    assert pack_provision.REPO_ROOT == PROJECT_ROOT
    assert pack_provision.PACKS_DIR == PROJECT_ROOT / "packs"
    assert pack_provision.MANIFEST_PATH == PROJECT_ROOT / "packs" / "manifest.yaml"
    assert reference_artifacts.REPO_ROOT == PROJECT_ROOT
    assert reference_artifacts.FIRMWARE_ROOT == PROJECT_ROOT / "firmware"
    assert zephyr_build.REPO_ROOT == PROJECT_ROOT
    assert host_bootstrap.SRC_DIR == PROJECT_ROOT / "src"
    assert stage0_check.SRC_DIR == PROJECT_ROOT / "src"


def test_approved_board_profiles_and_templates_are_local() -> None:
    board_paths = sorted(path.name for path in (PROJECT_ROOT / "boards").glob("*.yaml"))
    assert board_paths == [
        "example_custom_board.yaml",
        "example_custom_nrf52_board.yaml",
        "nrf52833dk.yaml",
        "nrf52840dk.yaml",
        "nucleo_l476rg.yaml",
    ]


def test_reference_artifacts_are_present_for_every_retained_board() -> None:
    for board_id in ("nrf52833dk", "nrf52840dk", "nucleo_l476rg"):
        pair = reference_artifacts.resolve_reference_artifacts(board_id)
        assert (
            pair.symbol_artifact.parent
            == PROJECT_ROOT / "firmware" / board_id / "reference" / "build"
        )
        assert pair.symbol_artifact.read_bytes().startswith(b"\x7fELF")
        assert pair.flash_artifact.suffix == ".hex"
        assert pair.flash_artifact.read_bytes().startswith(b":")


def test_only_canonical_tracked_outputs_exist_in_firmware_build_directories() -> None:
    allowed = {".gitignore", ".gitkeep", "firmware.elf", "firmware.hex"}
    for build_dir in (PROJECT_ROOT / "firmware").rglob("build"):
        assert {path.name for path in build_dir.iterdir()} <= allowed


def test_pack_tree_contains_metadata_but_no_downloaded_pack_binary() -> None:
    packs_dir = PROJECT_ROOT / "packs"
    assert (packs_dir / "manifest.yaml").is_file()
    assert (packs_dir / "README.md").is_file()
    assert (packs_dir / "live_index_repair.md").is_file()
    assert list(packs_dir.glob("*.pack")) == []


def test_s3_console_commands_resolve_to_the_extracted_modules() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]
    assert scripts["pyocd-pack-repair"] == "pyocd_debug_mcp.pack_index_repair:main"
    assert scripts["pyocd-zephyr-build"] == "pyocd_debug_mcp.zephyr_build:main"
