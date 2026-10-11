"""Client-workspace adapter for the server's canonical manual-grant protocol.

Keep the wire/file protocol in the installed package rather than duplicating it
in a skill.  The repository-owned workspace imports that implementation so AT09
always exercises the same epoch, locking, claim, and atomic-write behavior the
server release contains.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.capabilities.manual_permission_helper import (
    unlock_manual_grant as _unlock_manual_grant,
)


def unlock_manual_grant(
    *,
    project_root: Path,
    action: str,
    board_id: str,
    policy_digest: str,
    binding_digest: str | None = None,
) -> dict[str, object]:
    """Unlock one current-run grant through the package's frozen protocol."""

    return _unlock_manual_grant(
        project_root=project_root,
        action=action,
        board_id=board_id,
        policy_digest=policy_digest,
        binding_digest=binding_digest,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unlock one BYO manual permission grant")
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--action", required=True, choices=("downgrade", "mass-erase"))
    parser.add_argument("--board-id", required=True)
    parser.add_argument("--policy-digest", required=True)
    parser.add_argument("--binding-digest")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments: Any = _parser().parse_args(argv)
    result = unlock_manual_grant(
        project_root=arguments.project_root,
        action=arguments.action,
        board_id=arguments.board_id,
        policy_digest=arguments.policy_digest,
        binding_digest=arguments.binding_digest,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "manual_grant_unlocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
