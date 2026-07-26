"""Regression coverage for H00's default Pyright discovery boundary."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class H00RepositoryRegressionTests(unittest.TestCase):
    def test_default_pyright_ignores_test_scaffolding_but_reports_shipped_source_errors(self) -> None:
        """The source-only gate must neither regress to tests nor become a no-op."""
        command = [
            "uv",
            "run",
            "--project",
            str(ROOT),
            "--locked",
            "--no-sync",
            "pyright",
        ]
        self.assertNotIn("--with", command)
        self.assertNotIn("--no-project", command)
        self.assertIn("--locked", command)
        self.assertIn("--no-sync", command)
        with tempfile.TemporaryDirectory(prefix="h00 pyright scope ") as directory:
            candidate = Path(directory)
            (candidate / "pyproject.toml").write_text(
                (ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8"
            )
            test_only_error = candidate / "tests" / "h00_pyright_test_scope_sentinel.py"
            source_error = (
                candidate / "src" / "pyocd_debug_mcp" / "h00_pyright_source_scope_sentinel.py"
            )
            test_only_error.parent.mkdir(parents=True)
            source_error.parent.mkdir(parents=True)
            test_only_error.write_text('value: int = "test-only error"\n', encoding="utf-8")
            excluded_result = subprocess.run(
                [*command, "--project", str(candidate)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                excluded_result.returncode,
                0,
                excluded_result.stdout + excluded_result.stderr,
            )

            source_error.write_text('value: int = "source error"\n', encoding="utf-8")
            included_result = subprocess.run(
                [*command, "--project", str(candidate)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(
                included_result.returncode,
                0,
                "default Pyright must report errors in the shipped package",
            )
            self.assertIn(source_error.name, included_result.stdout + included_result.stderr)


if __name__ == "__main__":
    unittest.main()
