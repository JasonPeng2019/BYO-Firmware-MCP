from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "artifact",
    [
        ".firm/cache/attachments.json",
        ".firm/packs/files/vendor.pack",
    ],
)
def test_host_local_and_staged_pack_artifacts_are_gitignored(artifact: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", artifact],
        cwd=REPO_ROOT,
        check=False,
    )

    assert result.returncode == 0


def test_pack_manifest_is_not_ignored() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "packs/manifest.yaml"],
        cwd=REPO_ROOT,
        check=False,
    )

    assert result.returncode == 1
