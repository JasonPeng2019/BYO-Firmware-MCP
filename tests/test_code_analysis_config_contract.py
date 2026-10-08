"""G1: ``byo-analysis.json`` and compilation-database selection contract.

Derived from docs/code-analysis-contract.md "Project configuration" and
"Database algorithm".  Only the public loader ``load_analysis_config`` is used;
every expectation is about returned snapshot data or typed problems.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

from tests.code_analysis_support import (
    AnalysisProject,
    analysis_module,
    describe,
    field,
    problem_codes,
    problems_with,
    same_path,
    sha256_bytes,
    sha256_file,
    temporary_directory,
)

CONFIG_INVALID = "analysis/config-invalid"
DATABASE_MISSING = "analysis/database-missing"
DATABASE_AMBIGUOUS = "analysis/database-ambiguous"
DATABASE_INVALID = "analysis/database-invalid"
DATABASE_CONFLICT = "analysis/database-conflict"
DRIVER_INVALID = "analysis/driver-invalid"


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

    def assert_clean(self, snapshot) -> None:
        self.assertEqual(
            problem_codes(snapshot), [], [describe(p) for p in field(snapshot, "problems")]
        )


class ConfigurationFileContract(ConfigCase):
    def test_absent_file_uses_defaults_and_is_never_created(self) -> None:
        self.default_database()
        before = self.project.tree_digest()
        snapshot = load(self.project.root)
        self.assert_clean(snapshot)
        self.assertIsNone(field(snapshot, "config_sha256"))
        self.assertEqual(list(field(snapshot, "query_driver_paths")), [])
        self.assertFalse(self.project.path("byo-analysis.json").exists())
        self.assertEqual(self.project.tree_digest(), before, "loader mutated the project")

    def test_minimal_valid_file_hashes_original_bytes(self) -> None:
        self.default_database()
        raw = b'{"schema_version": 1}\r\n'
        self.project.write_bytes("byo-analysis.json", raw)
        snapshot = load(self.project.root)
        self.assert_clean(snapshot)
        self.assertEqual(field(snapshot, "config_sha256"), sha256_bytes(raw))
        self.assertTrue(
            same_path(field(snapshot, "config_path"), self.project.path("byo-analysis.json"))
        )
        self.assertEqual(self.project.path("byo-analysis.json").read_bytes(), raw)

    def test_full_documented_example_is_accepted(self) -> None:
        self.default_database("build/compile_commands.json")
        self.project.write_text("config/cppcheck-platform.xml", "<platform/>\n")
        self.project.write_text("config/cppcheck-suppressions.txt", "\n")
        self.project.write_config(
            {
                "schema_version": 1,
                "compilation_database": "build/compile_commands.json",
                "cppcheck": {
                    "platform_file": "config/cppcheck-platform.xml",
                    "suppressions_file": "config/cppcheck-suppressions.txt",
                    "enable_style": False,
                    "timeout_seconds": 120,
                },
                "clangd": {"query_driver_paths": []},
            }
        )
        snapshot = load(self.project.root)
        self.assert_clean(snapshot)
        self.assertTrue(
            same_path(
                field(snapshot, "database_path"), self.project.path("build/compile_commands.json")
            )
        )

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

    def test_positive_finite_timeout_and_bool_style_are_accepted(self) -> None:
        self.default_database()
        for timeout in (0.5, 1, 600):
            with self.subTest(timeout=timeout):
                self.project.write_config(
                    {
                        "schema_version": 1,
                        "cppcheck": {"timeout_seconds": timeout, "enable_style": True},
                    }
                )
                self.assertNotIn(CONFIG_INVALID, problem_codes(load(self.project.root)))

    def test_missing_cppcheck_platform_does_not_block_clangd(self) -> None:
        self.default_database()
        self.project.write_config(
            {"schema_version": 1, "cppcheck": {"platform_file": "config/missing-platform.xml"}}
        )
        snapshot = load(self.project.root)
        # Analyzer-specific resource problems affect only that analyzer: the
        # database is still selected for clangd.
        self.assertTrue(
            same_path(field(snapshot, "database_path"), self.project.path("compile_commands.json"))
        )
        for code in (DATABASE_MISSING, DATABASE_INVALID, DRIVER_INVALID):
            self.assertNotIn(code, problem_codes(snapshot))


class QueryDriverContract(ConfigCase):
    def test_exact_existing_compiler_is_canonicalized(self) -> None:
        self.default_database()
        compiler = Path(sys.executable).resolve()
        self.project.write_config(
            {"schema_version": 1, "clangd": {"query_driver_paths": [str(compiler)]}}
        )
        snapshot = load(self.project.root)
        self.assertNotIn(DRIVER_INVALID, problem_codes(snapshot))
        drivers = list(field(snapshot, "query_driver_paths"))
        self.assertEqual(len(drivers), 1)
        self.assertTrue(same_path(drivers[0], compiler))

    def test_globs_commands_missing_and_directories_are_rejected(self) -> None:
        self.default_database()
        compiler = Path(sys.executable).resolve()
        cases = {
            "glob star": str(compiler.parent / "*"),
            "glob question": str(compiler.parent / "pytho?.exe"),
            "command string": f"{compiler} --version",
            "missing": str(self.project.path("tools/arm-none-eabi-gcc")),
            "directory": str(compiler.parent),
            "comma list": f"{compiler},{compiler}",
        }
        for name, value in cases.items():
            with self.subTest(name):
                self.project.write_config(
                    {"schema_version": 1, "clangd": {"query_driver_paths": [value]}}
                )
                snapshot = load(self.project.root)
                codes = problem_codes(snapshot)
                self.assertTrue(
                    {DRIVER_INVALID, CONFIG_INVALID} & set(codes),
                    f"{name}: driver {value!r} accepted; problems={codes}",
                )
                self.assertFalse(
                    any(
                        same_path(item, compiler.parent)
                        for item in field(snapshot, "query_driver_paths")
                    ),
                    "an invalid literal must never widen to a directory allowlist",
                )


class DatabaseSelectionContract(ConfigCase):
    def test_zero_candidates_is_missing(self) -> None:
        snapshot = load(self.project.root)
        self.assert_problem(snapshot, DATABASE_MISSING)
        self.assertIsNone(field(snapshot, "database_path"))
        self.assertIsNone(field(snapshot, "database_sha256"))

    def test_root_candidate_selected_and_hashed(self) -> None:
        path = self.default_database()
        snapshot = load(self.project.root)
        self.assert_clean(snapshot)
        self.assertTrue(same_path(field(snapshot, "database_path"), path))
        self.assertEqual(field(snapshot, "database_sha256"), sha256_file(path))

    def test_build_candidate_selected(self) -> None:
        path = self.default_database("build/compile_commands.json")
        snapshot = load(self.project.root)
        self.assert_clean(snapshot)
        self.assertTrue(same_path(field(snapshot, "database_path"), path))

    def test_no_recursive_or_other_directory_search(self) -> None:
        self.default_database("out/compile_commands.json")
        self.default_database("build/debug/compile_commands.json")
        snapshot = load(self.project.root)
        self.assert_problem(snapshot, DATABASE_MISSING)
        self.assertIsNone(field(snapshot, "database_path"))

    def test_two_distinct_files_with_equal_bytes_are_ambiguous(self) -> None:
        first = self.default_database()
        second = self.default_database("build/compile_commands.json")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        snapshot = load(self.project.root)
        self.assert_problem(snapshot, DATABASE_AMBIGUOUS)
        self.assertIsNone(field(snapshot, "database_path"))

    def test_hardlink_aliases_collapse_to_one_file(self) -> None:
        first = self.default_database()
        alias = self.project.path("build/compile_commands.json")
        alias.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(first, alias)
        except OSError as exc:
            self.skipTest(f"UNMET PREREQUISITE (not a pass): hardlinks unavailable: {exc}")
        snapshot = load(self.project.root)
        self.assertNotIn(DATABASE_AMBIGUOUS, problem_codes(snapshot))
        self.assertTrue(
            same_path(field(snapshot, "database_path"), first)
            or same_path(field(snapshot, "database_path"), alias)
        )

    def test_symlink_aliases_collapse_to_one_file(self) -> None:
        first = self.default_database()
        alias = self.project.path("build/compile_commands.json")
        alias.parent.mkdir(parents=True, exist_ok=True)
        try:
            alias.symlink_to(first)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"UNMET PREREQUISITE (not a pass): symlinks unavailable: {exc}")
        snapshot = load(self.project.root)
        self.assertNotIn(DATABASE_AMBIGUOUS, problem_codes(snapshot))
        self.assertTrue(same_path(field(snapshot, "database_path"), first))

    def test_malformed_existing_candidate_is_a_problem_not_skipped(self) -> None:
        self.project.write_text("compile_commands.json", "[{")
        self.default_database("build/compile_commands.json")
        snapshot = load(self.project.root)
        codes = set(problem_codes(snapshot))
        self.assertTrue({DATABASE_INVALID, DATABASE_AMBIGUOUS} & codes, codes)
        self.assertFalse(
            same_path(
                field(snapshot, "database_path"), self.project.path("build/compile_commands.json")
            )
            and not codes,
            "malformed root candidate was silently ignored",
        )

    def test_malformed_only_candidate_is_invalid(self) -> None:
        self.project.write_text("build/compile_commands.json", "not json")
        self.assert_problem(load(self.project.root), DATABASE_INVALID)

    def test_empty_array_is_not_a_usable_database(self) -> None:
        self.project.write_database([])
        snapshot = load(self.project.root)
        self.assertTrue(
            {DATABASE_INVALID, "analysis/compile-context-missing"} & set(problem_codes(snapshot)),
            problem_codes(snapshot),
        )

    def test_explicit_database_selected_relative_to_project_not_cwd(self) -> None:
        explicit = self.default_database("cfg/one/compile_commands.json")
        self.project.write_config(
            {"schema_version": 1, "compilation_database": "cfg/one/compile_commands.json"}
        )
        decoy = AnalysisProject(self.root / "decoy-cwd")
        decoy.write_database([decoy.entry("x.c")], "cfg/one/compile_commands.json")
        previous = os.getcwd()
        os.chdir(decoy.root)
        try:
            snapshot = load(self.project.root)
        finally:
            os.chdir(previous)
        self.assert_clean(snapshot)
        self.assertTrue(same_path(field(snapshot, "database_path"), explicit))
        self.assertEqual(field(snapshot, "database_sha256"), sha256_file(explicit))

    def test_broken_explicit_database_never_falls_back(self) -> None:
        discovered = self.default_database()
        self.project.write_config(
            {"schema_version": 1, "compilation_database": "missing/compile_commands.json"}
        )
        snapshot = load(self.project.root)
        self.assertTrue(problem_codes(snapshot), "missing explicit database reported no problem")
        self.assertFalse(
            same_path(field(snapshot, "database_path"), discovered), "fell back to discovery"
        )

    def test_explicit_database_must_be_named_compile_commands_json(self) -> None:
        self.default_database()
        self.project.write_database([self.project.entry("src/main.c")], "build/commands.json")
        self.project.write_config(
            {"schema_version": 1, "compilation_database": "build/commands.json"}
        )
        snapshot = load(self.project.root)
        found = [p for p in field(snapshot, "problems") if "compile_commands.json" in describe(p)]
        self.assertTrue(found, f"wrong filename not explained: {problem_codes(snapshot)}")
        self.assertEqual(
            list(field(snapshot, "entries")), [], "invalid filename cannot supply commands"
        )

    def test_explicit_directory_is_rejected(self) -> None:
        self.default_database("build/compile_commands.json")
        self.project.write_config({"schema_version": 1, "compilation_database": "build"})
        snapshot = load(self.project.root)
        self.assertTrue(problem_codes(snapshot))

    def test_unicode_and_spaced_project_root(self) -> None:
        project = AnalysisProject(self.root / "proj café 中文 😀")
        project.write_text("src/main.c", "int main(void) { return 0; }\n")
        path = project.write_database([project.entry("src/main.c")])
        snapshot = load(project.root)
        self.assert_clean(snapshot)
        self.assertTrue(same_path(field(snapshot, "database_path"), path))
        self.assertTrue(same_path(field(snapshot, "project_root"), project.root))

    def test_escaped_and_literal_non_bmp_paths_are_both_valid_json(self) -> None:
        # ROOT 834b1e1f: the shared loader is analyzer-neutral.  An escaped
        # UTF-16 surrogate pair is valid JSON for the same path; any
        # clangd-specific blocker belongs to the clangd backend, never here.
        project = AnalysisProject(self.root / "json paths café中😀")
        project.write_text("src/main.c", "int main(void) { return 0; }\n")
        decoded = {}
        for escaped in (False, True):
            with self.subTest(escaped=escaped):
                path = project.write_database(
                    [project.entry("src/main.c")], escape_non_ascii=escaped
                )
                raw = path.read_bytes()
                self.assertEqual(b"\\ud83d\\ude00" in raw, escaped, raw[:200])
                snapshot = load(project.root)
                self.assert_clean(snapshot)
                self.assertEqual(field(snapshot, "database_sha256"), sha256_bytes(raw))
                self.assertEqual(path.read_bytes(), raw, "database bytes were rewritten")
                (entry,) = list(field(snapshot, "entries"))
                self.assertTrue(same_path(field(entry, "source"), project.path("src/main.c")))
                self.assertTrue(same_path(field(entry, "directory"), project.root))
                decoded[escaped] = list(field(entry, "arguments", None) or [])
        if set(decoded) == {False, True}:  # subtest failures above already report a missing side
            self.assertEqual(
                decoded[False], decoded[True], "both representations decode identically"
            )


class DatabaseEntriesContract(ConfigCase):
    def entries(self, snapshot) -> list:
        return list(field(snapshot, "entries"))

    def test_malformed_entries_report_index_and_field(self) -> None:
        good = self.project.entry("src/main.c")
        cases = {
            "missing file": (
                [good, {"directory": str(self.project.root), "arguments": ["cc", "-c", "x.c"]}],
                "1",
                "file",
            ),
            "missing directory": ([good, {"file": "x.c", "arguments": ["cc"]}], "1", "directory"),
            "no command": (
                [good, {"directory": str(self.project.root), "file": "src/main.c"}],
                "1",
                "command",
            ),
            "empty arguments": ([good, {**good, "arguments": []}], "1", "arguments"),
            "non-string argument": ([good, {**good, "arguments": ["cc", 3]}], "1", "arguments"),
            "empty command": (
                [{"directory": str(self.project.root), "file": "src/main.c", "command": ""}],
                "0",
                "command",
            ),
            "not object": ([good, good, "cc -c x.c"], "2", ""),
            "file not string": ([{**good, "file": 7}], "0", "file"),
        }
        for name, (entries, index, name_fragment) in cases.items():
            with self.subTest(name):
                self.project.write_database(entries)
                found = self.assert_problem(load(self.project.root), DATABASE_INVALID, index)
                if name_fragment:
                    self.assertIn(name_fragment, " ".join(describe(p) for p in found))

    def test_relative_directory_from_root_and_file_from_directory(self) -> None:
        self.project.write_text("build/obj/.keep", "")
        self.project.write_database(
            [
                {
                    "directory": "build/obj",
                    "file": "../../src/main.c",
                    "arguments": ["cc", "-c", "../../src/main.c"],
                }
            ]
        )
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            snapshot = load(self.project.root)
        finally:
            os.chdir(previous)
        self.assert_clean(snapshot)
        (entry,) = self.entries(snapshot)
        self.assertTrue(same_path(field(entry, "directory"), self.project.path("build/obj")))
        self.assertTrue(same_path(field(entry, "source"), self.project.path("src/main.c")))

    def test_missing_working_directory_or_source_is_invalid(self) -> None:
        for name, entry in {
            "directory": {
                "directory": "no/such/dir",
                "file": str(self.project.path("src/main.c")),
                "arguments": ["cc"],
            },
            "source": {
                "directory": str(self.project.root),
                "file": "src/absent.c",
                "arguments": ["cc"],
            },
        }.items():
            with self.subTest(name):
                self.project.write_database([entry])
                self.assertIn(DATABASE_INVALID, problem_codes(load(self.project.root)))

    def test_arguments_preferred_and_argument_order_preserved(self) -> None:
        arguments = [
            "arm-none-eabi-gcc",
            "-DB=2",
            "-DA=1",
            "-Iinc",
            "-mcpu=cortex-m4",
            "-c",
            "src/main.c",
        ]
        self.project.write_database(
            [
                {
                    "directory": str(self.project.root),
                    "file": "src/main.c",
                    "arguments": arguments,
                    "command": "ignored -DIGNORED -c src/main.c",
                    "output": "build/main.o",
                }
            ]
        )
        snapshot = load(self.project.root)
        self.assert_clean(snapshot)
        (entry,) = self.entries(snapshot)
        self.assertEqual(list(field(entry, "arguments")), arguments)
        self.assertEqual(field(entry, "raw")["output"], "build/main.o")

    def test_command_text_is_opaque_never_whitespace_split(self) -> None:
        command = 'cc "-DNAME=\\"a b\\"" -I "inc dir" -c src/main.c'
        self.project.write_database(
            [{"directory": str(self.project.root), "file": "src/main.c", "command": command}]
        )
        snapshot = load(self.project.root)
        self.assert_clean(snapshot)
        (entry,) = self.entries(snapshot)
        self.assertEqual(field(entry, "command"), command)
        arguments = field(entry, "arguments", None)
        self.assertNotEqual(list(arguments or []), command.split(), "command was whitespace split")

    def test_identical_entries_deduplicate(self) -> None:
        entry = self.project.entry("src/main.c")
        self.project.write_database([entry, dict(entry)])
        snapshot = load(self.project.root)
        self.assert_clean(snapshot)
        self.assertEqual(len(self.entries(snapshot)), 1)

    def test_conflicts_block_selection_for_every_unit(self) -> None:
        self.project.write_text("src/other.c", "int other(void) { return 1; }\n")
        base = ["cc", "-c", str(self.project.path("src/main.c"))]
        cases = {
            "different argv": [
                self.project.entry("src/main.c", arguments=base),
                self.project.entry("src/main.c", arguments=["cc", "-DX", *base[1:]]),
            ],
            "argument order": [
                self.project.entry("src/main.c", arguments=["cc", "-DA", "-DB", "-c", "m.c"]),
                self.project.entry("src/main.c", arguments=["cc", "-DB", "-DA", "-c", "m.c"]),
            ],
            "different directory": [
                self.project.entry("src/main.c", arguments=base),
                self.project.entry(
                    "src/main.c", arguments=base, directory=self.project.path("src")
                ),
            ],
            "argv versus command": [
                self.project.entry("src/main.c", arguments=base),
                {
                    "directory": str(self.project.root),
                    "file": str(self.project.path("src/main.c")),
                    "command": " ".join(base),
                },
            ],
        }
        other = self.project.entry("src/other.c")
        for name, entries in cases.items():
            with self.subTest(name):
                self.project.write_database([other, *entries])
                self.assert_problem(load(self.project.root), DATABASE_CONFLICT)

    def test_database_bytes_are_never_mutated(self) -> None:
        raw = (
            json.dumps([self.project.entry("src/main.c")], indent=4).replace("\n", "\r\n").encode()
        )
        path = self.project.write_bytes("compile_commands.json", raw)
        snapshot = load(self.project.root)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(field(snapshot, "database_sha256"), sha256_bytes(raw))


if __name__ == "__main__":
    unittest.main()
