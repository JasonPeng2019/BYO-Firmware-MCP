from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.store import FirmStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "migrate_boards_to_firm.py"
BOARD_IDS = ("nrf52833dk", "nrf52840dk", "nucleo_l476rg")
PART_NUMBERS = {
    "nrf52833dk": "nRF52833-QIAA",
    "nrf52840dk": "nRF52840-QIAA",
    "nucleo_l476rg": "STM32L476RGT6",
}
PACKAGE_FIELDS = {"pack_name", "pack_id", "pack_version", "pack_url", "pack_sha256"}


def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    boards = root / "boards"
    boards.mkdir(parents=True)
    for board_id in BOARD_IDS:
        shutil.copy2(REPO_ROOT / "boards" / f"{board_id}.yaml", boards / f"{board_id}.yaml")
    return root


def command(root: Path, mapping: dict[str, str] = PART_NUMBERS) -> list[str]:
    args = [sys.executable, str(SCRIPT), "--project-root", str(root)]
    for board_id, part_number in mapping.items():
        args.extend(["--part-number", f"{board_id}={part_number}"])
    return args


def run_migration(root: Path, mapping: dict[str, str] = PART_NUMBERS) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command(root, mapping),
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_migration_round_trip_is_package_free_exact_and_idempotent(tmp_path: Path) -> None:
    root = checkout(tmp_path)

    first = run_migration(root)
    assert first.returncode == 0, first.stderr
    assert first.stdout.count("created:") == 3
    repository = ProfileRepository(FirmStore(root), legacy_board_dir=root / "boards")
    profiles = repository.load_all(include_legacy=False)
    assert [profile.board_id for profile in profiles] == sorted(BOARD_IDS)
    for profile in profiles:
        document = profile.to_document()
        assert profile.mcu_part_number == PART_NUMBERS[profile.board_id]
        assert PACKAGE_FIELDS.isdisjoint(document)
        assert document["schema_version"] == 2
        assert document["expected_uart_substring"] == "boot ok"

    before = {path.name: path.read_bytes() for path in repository.store.layout.boards.iterdir()}
    second = run_migration(root)
    after = {path.name: path.read_bytes() for path in repository.store.layout.boards.iterdir()}

    assert second.returncode == 0, second.stderr
    assert second.stdout.count("unchanged:") == 3
    assert after == before


def test_migration_refuses_missing_mapping_before_writing(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    incomplete = {board_id: PART_NUMBERS[board_id] for board_id in BOARD_IDS[:-1]}

    result = run_migration(root, incomplete)

    assert result.returncode != 0
    assert "missing mappings" in result.stderr
    assert not (root / ".firm").exists()


def test_migration_does_not_overwrite_a_conflicting_v2_profile(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    assert run_migration(root).returncode == 0
    store = FirmStore(root)
    target = store.layout.board_profile("nrf52833dk")
    document = ProfileRepository(store, legacy_board_dir=root / "boards").load(
        "nrf52833dk", include_legacy=False
    ).to_document()
    document["mcu_part_number"] = "CONFLICTING-EXPLICIT-VALUE"
    store.atomic_write_yaml(target, document)
    conflicting = target.read_bytes()

    result = run_migration(root)

    assert result.returncode != 0
    assert "conflicts with the migration" in result.stderr
    assert target.read_bytes() == conflicting
