#!/usr/bin/env python3
"""One-shot checkout command for migrating the three tracked board profiles to schema v2."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

CHECKOUT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = CHECKOUT_ROOT / "src"
if SRC_ROOT.is_dir():
    sys.path.insert(0, str(SRC_ROOT))

from pyocd_debug_mcp.board_config import ConfigError  # noqa: E402
from pyocd_debug_mcp.firmstore.profiles import (  # noqa: E402
    BoardProfile,
    ProfileRepository,
    StagedProfile,
)
from pyocd_debug_mcp.firmstore.store import FirmStore, FirmStoreError  # noqa: E402

TRACKED_BOARD_IDS = ("nrf52833dk", "nrf52840dk", "nucleo_l476rg")
_MIGRATION_TIMESTAMPS = frozenset({"created_at", "updated_at"})


class MigrationError(RuntimeError):
    """The migration cannot proceed safely with the supplied checkout or mapping."""


def parse_part_number_mapping(values: Sequence[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise MigrationError(
                "Each --part-number must use BOARD_ID=EXACT_MCU_PART_NUMBER syntax"
            )
        board_id, part_number = value.split("=", 1)
        board_id = board_id.strip()
        if not board_id or not part_number.strip():
            raise MigrationError(
                "Each --part-number must include both a board ID and exact MCU part number"
            )
        if board_id in mapping:
            raise MigrationError(f"Duplicate MCU part-number mapping for '{board_id}'")
        mapping[board_id] = part_number
    return mapping


def _validate_mapping(part_numbers: Mapping[str, str]) -> None:
    expected = set(TRACKED_BOARD_IDS)
    supplied = set(part_numbers)
    missing = sorted(expected - supplied)
    unknown = sorted(supplied - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing mappings for {missing}")
        if unknown:
            details.append(f"unknown mappings for {unknown}")
        raise MigrationError(
            "Explicit MCU part-number mapping must name exactly the three tracked boards: "
            + "; ".join(details)
        )
    invalid = [board_id for board_id, value in part_numbers.items() if not value.strip()]
    if invalid:
        raise MigrationError(f"MCU part numbers must be explicit non-empty strings: {invalid}")


def _assert_existing_matches(existing: BoardProfile, staged: StagedProfile) -> None:
    expected = staged.profile.to_document()
    actual = existing.to_document()
    mismatches = [
        field_name
        for field_name, expected_value in expected.items()
        if field_name not in _MIGRATION_TIMESTAMPS and actual.get(field_name) != expected_value
    ]
    if mismatches:
        raise MigrationError(
            f"Existing schema-v2 profile '{existing.board_id}' conflicts with the migration "
            f"for fields {sorted(mismatches)}; it was not overwritten"
        )


def migrate_boards(project_root: Path, part_numbers: Mapping[str, str]) -> dict[str, str]:
    """Migrate or verify all tracked profiles without guessing or overwriting."""

    _validate_mapping(part_numbers)
    project = Path(project_root).expanduser().resolve()
    legacy_dir = project / "boards"
    if not legacy_dir.is_dir():
        raise MigrationError(f"Legacy board directory does not exist: {legacy_dir}")

    repository = ProfileRepository(FirmStore(project), legacy_board_dir=legacy_dir)
    staged_by_id = {
        board_id: repository.stage_legacy_migration(board_id, part_numbers[board_id])
        for board_id in TRACKED_BOARD_IDS
    }
    existing_by_id = {
        profile.board_id: profile for profile in repository.load_all(include_legacy=False)
    }

    results: dict[str, str] = {}
    for board_id in TRACKED_BOARD_IDS:
        existing = existing_by_id.get(board_id)
        if existing is not None:
            _assert_existing_matches(existing, staged_by_id[board_id])
            results[board_id] = "unchanged"
            continue
        repository.commit_legacy_migration(staged_by_id[board_id])
        results[board_id] = "created"
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate the three tracked boards/ profiles into .firm/boards schema v2. "
            "No MCU part number is inferred; all three must be supplied explicitly."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Checkout root containing boards/ (default: current directory)",
    )
    parser.add_argument(
        "--part-number",
        action="append",
        default=[],
        metavar="BOARD_ID=EXACT_MCU_PART_NUMBER",
        help="Required once for each tracked board; quote values containing spaces",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        part_numbers = parse_part_number_mapping(args.part_number)
        results = migrate_boards(args.project_root, part_numbers)
    except (ConfigError, FirmStoreError, MigrationError) as exc:
        parser.error(str(exc))
    for board_id, status in results.items():
        print(f"{status}: {board_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
