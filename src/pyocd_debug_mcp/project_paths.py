"""Resolve caller paths independently of the managed runtime's working directory."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_project_path(value: Path | str, *, strict: bool = False) -> Path:
    """Use the selected project for relative server inputs; CLI defaults to cwd."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        root = os.environ.get("BYO_MCP_ARTIFACT_ROOT", "").strip()
        path = (Path(root).expanduser() if root else Path.cwd()) / path
    return path.resolve(strict=strict)


def refuse_runtime_destination(value: Path) -> None:
    """Refuse collection into the immutable managed runtime before staging files."""
    runtime = os.environ.get("BYO_RUNTIME_ROOT", "").strip()
    if not runtime:
        return
    runtime_path = Path(runtime).expanduser().resolve()
    destination = value.resolve()
    # Windows resolve() can retain a verbatim prefix on only one of these paths.
    # Compare ancestor identities too, including when the destination is missing.
    for ancestor in (destination, *destination.parents):
        try:
            is_runtime = ancestor == runtime_path or ancestor.samefile(runtime_path)
        except FileNotFoundError:
            continue
        if is_runtime:
            raise ValueError(
                "Artifact bundles must not be written inside the BYO runtime; use the project."
            )
