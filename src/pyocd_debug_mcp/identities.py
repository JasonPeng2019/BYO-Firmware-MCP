"""Shared display-name equivalence and portable board-directory restrictions."""

from __future__ import annotations

import unicodedata

_RESERVED_BOARD_IDS = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)


def display_name_identity(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def is_reserved_board_id(value: str) -> bool:
    return value.casefold() in _RESERVED_BOARD_IDS
