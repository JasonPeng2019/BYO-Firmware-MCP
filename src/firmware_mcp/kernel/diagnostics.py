"""Small helpers for retaining cleanup facts beside the primary failure."""

from __future__ import annotations


def attach_cleanup_error(primary: BaseException, stage: str, cleanup: BaseException) -> None:
    """Preserve a secondary cleanup failure without replacing the primary result."""

    primary.add_note(f"{stage} cleanup failed: {type(cleanup).__name__}: {cleanup}")
