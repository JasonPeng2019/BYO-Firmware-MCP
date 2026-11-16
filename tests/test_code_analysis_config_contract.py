"""G1: ``byo-analysis.json`` and compilation-database selection contract.

Derived from docs/code-analysis-contract.md "Project configuration" and
"Database algorithm".  Only the public loader ``load_analysis_config`` is used;
every expectation is about returned snapshot data or typed problems.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.code_analysis_support import AnalysisProject, analysis_module, describe, field, problem_codes, problems_with, temporary_directory

CONFIG_INVALID = "analysis/config-invalid"


def load(root: Path):
    return analysis_module("code_analysis_config").load_analysis_config(root)


class ConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        context = temporary_directory("byo-analysis-config-")
        self.root = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.project = AnalysisProject(self.root / "project")
        self.project.write_text("src/main.c", "int main(void) { return 0; }\n")

    def default_database(self, relative: str = "compile_commands.json") -> Path:
        return self.project.write_database([self.project.entry("src/main.c")], relative)

    def assert_problem(self, snapshot, code: str, *fragments: str):
        found = problems_with(snapshot, code)
        self.assertTrue(found, f"expected {code}; got {problem_codes(snapshot)}")
        for problem in found:
            for name in ("message", "remedy"):
                value = field(problem, name)
                self.assertIsInstance(value, str)
                self.assertTrue(value.strip(), f"{code} has empty {name}")
        text = " ".join(describe(problem) for problem in found)
        for fragment in fragments:
            self.assertIn(fragment, text, f"{code} does not name {fragment!r}: {text}")
        return found


class ConfigurationFileContract(ConfigCase):

    def test_syntax_and_schema_errors_name_field_and_remedy(self) -> None:
        cases = {
            "malformed json": ("{", ()),
            "not an object": ("[1]", ()),
            "missing version": ("{}", ("schema_version",)),
            "string version": ('{"schema_version": "1"}', ("schema_version",)),
            "bool version": ('{"schema_version": true}', ("schema_version",)),
            "other version": ('{"schema_version": 2}', ("schema_version",)),
            "float version": ('{"schema_version": 1.0}', ("schema_version",)),
            "duplicate key": ('{"schema_version": 1, "schema_version": 1}', ("schema_version",)),
            "duplicate nested key": (
                '{"schema_version": 1, "clangd": {"query_driver_paths": [], "query_driver_paths": []}}',
                ("query_driver_paths",),
            ),
            "unknown top": ('{"schema_version": 1, "compile_db": "x"}', ("compile_db",)),
            "unknown cppcheck": ('{"schema_version": 1, "cppcheck": {"jobs": 4}}', ("jobs",)),
            "unknown clangd": (
                '{"schema_version": 1, "clangd": {"arguments": []}}',
                ("arguments",),
            ),
            "null cppcheck": ('{"schema_version": 1, "cppcheck": null}', ("cppcheck",)),
            "null clangd": ('{"schema_version": 1, "clangd": null}', ("clangd",)),
            "null database": (
                '{"schema_version": 1, "compilation_database": null}',
                ("compilation_database",),
            ),
            "empty database": (
                '{"schema_version": 1, "compilation_database": ""}',
                ("compilation_database",),
            ),
            "nul database": (
                '{"schema_version": 1, "compilation_database": "a\\u0000/compile_commands.json"}',
                ("compilation_database",),
            ),
            "string style": (
                '{"schema_version": 1, "cppcheck": {"enable_style": "true"}}',
                ("enable_style",),
            ),
            "numeric style": (
                '{"schema_version": 1, "cppcheck": {"enable_style": 1}}',
                ("enable_style",),
            ),
            "bool timeout": (
                '{"schema_version": 1, "cppcheck": {"timeout_seconds": true}}',
                ("timeout_seconds",),
            ),
            "string timeout": (
                '{"schema_version": 1, "cppcheck": {"timeout_seconds": "5"}}',
                ("timeout_seconds",),
            ),
            "zero timeout": (
                '{"schema_version": 1, "cppcheck": {"timeout_seconds": 0}}',
                ("timeout_seconds",),
            ),
            "negative timeout": (
                '{"schema_version": 1, "cppcheck": {"timeout_seconds": -1}}',
                ("timeout_seconds",),
            ),
            "nan timeout": (
                '{"schema_version": 1, "cppcheck": {"timeout_seconds": NaN}}',
                ("timeout_seconds",),
            ),
            "infinity timeout": (
                '{"schema_version": 1, "cppcheck": {"timeout_seconds": Infinity}}',
                ("timeout_seconds",),
            ),
            "overflow timeout": (
                '{"schema_version": 1, "cppcheck": {"timeout_seconds": 1e999}}',
                ("timeout_seconds",),
            ),
            "drivers not list": (
                '{"schema_version": 1, "clangd": {"query_driver_paths": "/usr/bin/gcc"}}',
                ("query_driver_paths",),
            ),
            "driver not string": (
                '{"schema_version": 1, "clangd": {"query_driver_paths": [3]}}',
                ("query_driver_paths",),
            ),
            "cppcheck not object": ('{"schema_version": 1, "cppcheck": []}', ("cppcheck",)),
        }
        self.default_database()
        for name, (raw, fragments) in cases.items():
            with self.subTest(name):
                self.project.write_config(raw)
                snapshot = load(self.project.root)
                self.assert_problem(snapshot, CONFIG_INVALID, *fragments)
                self.assertEqual(
                    self.project.path("byo-analysis.json").read_text(encoding="utf-8"), raw
                )


if __name__ == "__main__":
    unittest.main()
