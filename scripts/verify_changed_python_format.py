#!/usr/bin/env python3
"""Run Ruff's format check on every changed or untracked Python source file.

Run from anywhere inside the checkout with:

    uv run --locked python scripts/verify_changed_python_format.py
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _is_python_source(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix == ".py":
        return True
    try:
        with path.open("rb") as handle:
            first_line = handle.readline(256)
    except OSError:
        return False
    return first_line.startswith(b"#!") and b"python" in first_line.lower()


def _git_paths(root: Path, arguments: list[str]) -> set[Path]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return {root / line for line in result.stdout.splitlines() if _is_python_source(root / line)}


def _commit_exists(root: Path, revision: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
            cwd=root,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def _git_output(root: Path, arguments: list[str]) -> str | None:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _changed_python_paths(root: Path, *, base: str, default_branch: str) -> set[Path]:
    paths = _git_paths(root, ["diff", "--name-only", "--diff-filter=ACMRTUXB", "HEAD", "--"])
    paths.update(_git_paths(root, ["ls-files", "--others", "--exclude-standard", "--"]))
    if not base:
        return paths

    comparison: str | None = None
    if set(base) != {"0"} and _commit_exists(root, base):
        comparison = base
    elif default_branch:
        default_revision = f"origin/{default_branch}"
        if _commit_exists(root, default_revision):
            comparison = _git_output(root, ["merge-base", default_revision, "HEAD"])

    head = _git_output(root, ["rev-parse", "HEAD"])
    if comparison and comparison != head:
        paths.update(
            _git_paths(
                root,
                [
                    "diff",
                    "--name-only",
                    "--diff-filter=ACMRTUXB",
                    f"{comparison}...HEAD",
                    "--",
                ],
            )
        )
    else:
        # GitHub uses an all-zero before SHA for first pushes, and a force push
        # can leave a nonzero before SHA unreachable. If no distinct default-
        # branch merge base is available, checking every tracked Python file is
        # the only complete, fail-safe format gate.
        paths.update(_git_paths(root, ["ls-files", "--"]))
    return paths


def main() -> int:
    paths = _changed_python_paths(
        ROOT,
        base=os.environ.get("BYO_FORMAT_BASE", "").strip(),
        default_branch=os.environ.get("BYO_FORMAT_DEFAULT_BRANCH", "").strip(),
    )
    relative = sorted(path.relative_to(ROOT).as_posix() for path in paths)
    if not relative:
        print("No changed Python files to format-check.")
        return 0
    command = ["ruff", "format", "--check", "--", *relative]
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
