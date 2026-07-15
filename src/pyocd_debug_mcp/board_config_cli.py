"""CLI wrapper for resolving board configs through the shared loader."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pyocd_debug_mcp.board_config import (
    DEFAULT_BOARD_CONFIG_DIR,
    BoardConfig,
    ConfigError,
    load_selected_board_configs,
)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve tracked board configs through the shared Phase A loader."
    )
    parser.add_argument(
        "--board-config-dir",
        default=str(DEFAULT_BOARD_CONFIG_DIR),
        help="Directory containing tracked board YAML or JSON files.",
    )
    parser.add_argument(
        "--board-config",
        action="append",
        default=[],
        help="Additional board config file to merge with the tracked set.",
    )
    parser.add_argument(
        "--board-id",
        action="append",
        default=[],
        help="Restrict the loaded board set to one or more board_id values.",
    )
    return parser


def board_summary(board: BoardConfig) -> dict[str, object]:
    return {
        "board_id": board.board_id,
        "display_name": board.display_name,
        "mcu_family": board.mcu_family,
        "probe_family": board.probe_family,
        "pyocd_target": board.pyocd_target,
        "source_path": str(board.source_path) if board.source_path else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)

    try:
        boards = load_selected_board_configs(
            board_config_dir=Path(args.board_config_dir).expanduser().resolve(),
            extra_paths=[Path(path).expanduser().resolve() for path in args.board_config],
            requested_ids=args.board_id,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    json.dump([board_summary(board) for board in boards], sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
