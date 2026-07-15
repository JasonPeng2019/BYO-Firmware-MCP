"""Helpers for canonical repo-owned reference firmware artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.target_errors import ReferenceArtifactError

REPO_ROOT = Path(__file__).resolve().parents[2]
FIRMWARE_ROOT = REPO_ROOT / "firmware"


@dataclass(frozen=True)
class ReferenceArtifactPair:
    board_id: str
    flash_artifact: Path
    symbol_artifact: Path


def _canonical_build_dir(board_id: str) -> Path:
    return FIRMWARE_ROOT / board_id / "reference" / "build"


def _resolve_path(path_value: Path | str | None) -> Path | None:
    if path_value is None:
        return None
    return Path(path_value).expanduser().resolve()


def resolve_reference_artifacts(
    board: BoardConfig | str,
    *,
    flash_artifact: Path | str | None = None,
    elf_path: Path | str | None = None,
) -> ReferenceArtifactPair:
    board_id = board.board_id if isinstance(board, BoardConfig) else str(board).strip().lower()
    build_dir = _canonical_build_dir(board_id)

    symbol_artifact = _resolve_path(elf_path) or (build_dir / "firmware.elf").resolve()
    if flash_artifact is None:
        hex_candidate = (build_dir / "firmware.hex").resolve()
        flash_resolved = hex_candidate if hex_candidate.exists() else symbol_artifact
    else:
        resolved_override = _resolve_path(flash_artifact)
        if resolved_override is None:
            raise ReferenceArtifactError(f"Invalid flash artifact override for {board_id}")
        flash_resolved = resolved_override

    if not symbol_artifact.exists():
        raise ReferenceArtifactError(
            f"Missing canonical symbol artifact for {board_id}: {symbol_artifact}"
        )
    if not flash_resolved.exists():
        raise ReferenceArtifactError(f"Missing flash artifact for {board_id}: {flash_resolved}")

    return ReferenceArtifactPair(
        board_id=board_id,
        flash_artifact=flash_resolved,
        symbol_artifact=symbol_artifact,
    )
