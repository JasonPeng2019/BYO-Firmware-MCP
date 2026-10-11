#!/usr/bin/env python3
"""Parse every shipped Python source with the release's CPython 3.10 floor."""

from __future__ import annotations

import ast
from pathlib import Path


ROOTS = (Path("src"), Path("scripts"), Path("client-workspace"), Path("tests"))


def main() -> int:
    files = sorted(
        {path for root in ROOTS for path in root.rglob("*.py") if "__pycache__" not in path.parts}
    )
    for path in files:
        compile(
            path.read_bytes(),
            str(path),
            "exec",
            flags=ast.PyCF_ONLY_AST,
            dont_inherit=True,
        )
    print(f"parsed_files={len(files)}")
    print("result=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
