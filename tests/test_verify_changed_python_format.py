from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.verify_changed_python_format import _changed_python_paths


ZERO_SHA = "0" * 40


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class ChangedPythonFormatManifestTests(unittest.TestCase):
    def _repository(self, root: Path) -> None:
        _git(root, "init", "-b", "main")
        _git(root, "config", "user.email", "format-test@example.invalid")
        _git(root, "config", "user.name", "Format Test")
        (root / "baseline.txt").write_text("baseline\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "baseline")
        baseline = _git(root, "rev-parse", "HEAD")
        _git(root, "update-ref", "refs/remotes/origin/main", baseline)

    def test_first_push_checks_python_from_every_commit_on_the_new_branch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-format-manifest-") as temporary:
            root = Path(temporary)
            self._repository(root)
            _git(root, "switch", "-c", "feature")
            (root / "early.py").write_text("value=1\n", encoding="utf-8")
            _git(root, "add", "early.py")
            _git(root, "commit", "-m", "early python")
            (root / "late.txt").write_text("late\n", encoding="utf-8")
            _git(root, "add", "late.txt")
            _git(root, "commit", "-m", "later non-python")

            paths = _changed_python_paths(root, base=ZERO_SHA, default_branch="main")

            self.assertEqual(paths, {root / "early.py"})

    def test_unresolvable_force_push_base_falls_back_to_default_branch_merge_base(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-format-manifest-") as temporary:
            root = Path(temporary)
            self._repository(root)
            _git(root, "switch", "-c", "rewritten")
            (root / "rewritten.py").write_text("value=2\n", encoding="utf-8")
            _git(root, "add", "rewritten.py")
            _git(root, "commit", "-m", "rewritten python")

            paths = _changed_python_paths(root, base="f" * 40, default_branch="main")

            self.assertEqual(paths, {root / "rewritten.py"})

    def test_missing_comparison_history_checks_every_tracked_python_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-format-manifest-") as temporary:
            root = Path(temporary)
            self._repository(root)
            (root / "existing.py").write_text("value=3\n", encoding="utf-8")
            _git(root, "add", "existing.py")
            _git(root, "commit", "-m", "existing python")

            paths = _changed_python_paths(root, base="f" * 40, default_branch="missing")

            self.assertEqual(paths, {root / "existing.py"})

    def test_extensionless_python_shebang_file_is_included(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-format-manifest-") as temporary:
            root = Path(temporary)
            self._repository(root)
            script = root / "attach-project"
            script.write_text("#!/usr/bin/env python3\nvalue=4\n", encoding="utf-8")
            _git(root, "add", "attach-project")
            _git(root, "commit", "-m", "extensionless python")

            paths = _changed_python_paths(root, base=ZERO_SHA, default_branch="main")

            self.assertEqual(paths, {script})


if __name__ == "__main__":
    unittest.main()
