"""Helpers for loading repo-local environment defaults."""

from __future__ import annotations

from pathlib import Path


def load_local_env() -> None:
    """Load a local `.env` file when one is available."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    seen: set[Path] = set()

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        load_dotenv(resolved, override=False)
        seen.add(resolved)
