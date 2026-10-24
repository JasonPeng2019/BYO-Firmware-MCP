"""Real-program smoke: the delivered service/adapter against a real clangd.

Gated by ``BYO_MCP_TEST_CLANGD_EXECUTABLE`` (a harness-supplied fixture, never a
public tool argument); an unset variable is an UNMET PREREQUISITE skip, never a
pass.  The ARM cases additionally need ``BYO_MCP_TEST_ARM_GNU_ROOT`` for the
literal query-driver allowlist.  A development clangd on one host is not
four-platform proof.  No hardware is used.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from functools import partial
from pathlib import Path
from typing import Any

from tests.code_analysis_support import (
    OWNED_CLEANUP_SECONDS,
    SCHEDULING_SLACK_SECONDS,
    AnalysisProject,
    AnalysisStdioServer,
    arm_gnu_root_or_skip,
    assert_envelope,
    assert_failure,
    assert_source,
    copy_fixture,
    host_database,
    lines_of,
    make_service,
    materialize_arm_fixture,
    real_clangd_or_skip,
    same_path,
    sha256_file,
    temporary_directory,
    unpermitted_tree_changes,
    wait_pids_gone,
)

QUERY_TIMEOUT = 90.0
UNICODE_ROOT = "proj café 中文 😀"
SHUTDOWN_BOUND = 5.0 + OWNED_CLEANUP_SECONDS + SCHEDULING_SLACK_SECONDS
CONFIGURATION_CODES = {
    "analysis/config-invalid",
    "analysis/database-invalid",
    "analysis/database-conflict",
    "analysis/compile-context-missing",
}
# Unicode positions with real clangd: accented, combining, CJK, non-BMP and a
# tab precede the queried call on the same CRLF line.  The header include under
# the non-BMP root is what makes clangd 23.1.0 fail on the escaped database
# (ROOT probe: VFS CWD mapping error and pp_file_not_found); without it the
# escaped form happens to work and would not exercise the blocker.
UNICODE_HEADER = "#pragma once\r\nstatic inline int uni_value(void) { return 7; }\r\n"
UNICODE_SOURCE = (
    '#include "uni.h"\r\n'
    "/* café é 中文 😀 */\r\n"
    "static int target_symbol(int value) { return value + 1; }\r\n"
    "\r\n"
    'int main(void) { const char *s = "😀中é́";\t(void)s; return /* 😀 */ target_symbol(uni_value()); }\r\n'
)
PROTECTED_ARM_INPUTS = (
    "byo-analysis.json",
    "build/compile_commands.json",
    "build/generated/build_config.h",
    "config/arm32.xml",
    "firmware.ld",
    "include/firmware.h",
    "src/main.c",
    "src/heartbeat.cpp",
    "src/startup.c",
)


def position(
    project: AnalysisProject, relative: str, needle: str, occurrence: int = 0
) -> tuple[int, int]:
    text = project.path(relative).read_bytes().decode("utf-8")
    for number, line in enumerate(lines_of(text), start=1):
        start = 0
        while (index := line.find(needle, start)) >= 0:
            if occurrence == 0:
                return number, index + 1
            occurrence -= 1
            start = index + 1
    raise AssertionError(f"{needle!r} not found in {relative}")


def query(
    service: Any, operation: str, project: AnalysisProject, relative: str, needle: str, **extra: Any
):
    line, column = position(project, relative, needle, extra.pop("occurrence", 0))
    method = getattr(service, operation)
    return method(
        file_path=str(project.path(relative)),
        line=line,
        column=column,
        timeout_seconds=QUERY_TIMEOUT,
        **extra,
    )


def diagnostics(service: Any, project: AnalysisProject, relative: str) -> dict[str, Any]:
    return service.diagnostics(file_path=str(project.path(relative)), timeout_seconds=QUERY_TIMEOUT)


def location_points(envelope: dict[str, Any]) -> list[tuple[str, int, int]]:
    points = []
    for item in envelope["data"]["locations"]:
        start = item["range"]["start"]
        points.append((Path(item["path"]).name, start["line"], start["column"]))
    return points


class RealClangdCase(unittest.TestCase):
    def setUp(self) -> None:
        self.executable = real_clangd_or_skip(self)
        directory = temporary_directory("byo-ca-real-")
        self.base = directory.__enter__()
        self.addCleanup(directory.__exit__, None, None, None)

    def service(self, project: AnalysisProject) -> Any:
        return make_service(project.root, executable=self.executable, testcase=self)


class ProgramIdentity(RealClangdCase):
    def test_version_and_status_identify_the_supplied_program(self) -> None:
        completed = subprocess.run(  # noqa: S603 - harness-supplied fixture path
            [str(self.executable), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("clangd version", completed.stdout)
        print(
            f"\nreal clangd {self.executable} sha256={sha256_file(self.executable)}: {completed.stdout.strip()}"
        )
        project = copy_fixture("host_c", self.base / "host_c")
        host_database(project, ["src/main.c", "src/util.c"])
        envelope = assert_envelope(self, self.service(project).status(), "status", status="ok")
        self.assertTrue(same_path(envelope["data"]["executable"], self.executable))
        self.assertEqual(envelope["data"]["session_state"], "not_started")


class HostC(RealClangdCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = copy_fixture("host_c", self.base / "host_c")
        host_database(self.project, ["src/main.c", "src/util.c"])
        self.analysis = self.service(self.project)

    def test_definition_references_and_hover(self) -> None:
        definition = assert_envelope(
            self,
            query(
                self.analysis, "definition", self.project, "src/main.c", "scale_value", occurrence=1
            ),
            "definition",
            status="ok",
        )
        assert_source(self, definition, self.project.path("src/main.c"))
        self.assertTrue(
            set(location_points(definition)) & {("util.h", 3, 5), ("util.c", 3, 5)},
            json.dumps(definition, indent=2),
        )
        references = assert_envelope(
            self,
            query(
                self.analysis, "references", self.project, "src/main.c", "scale_value", occurrence=1
            ),
            "references",
            status="ok",
        )
        points = set(location_points(references))
        self.assertTrue({("main.c", 4, 38), ("main.c", 8, 17)} <= points, points)
        self.assertEqual(references["data"]["observed_count"], len(references["data"]["locations"]))
        hover = assert_envelope(
            self,
            query(self.analysis, "hover", self.project, "src/main.c", "scale_value", occurrence=1),
            "hover",
            status="ok",
        )
        self.assertIn("scale_value", hover["data"]["contents"]["value"])

    def test_clean_file_has_a_genuinely_empty_fresh_notification(self) -> None:
        envelope = assert_envelope(
            self, diagnostics(self.analysis, self.project, "src/main.c"), "diagnostics", status="ok"
        )
        assert_source(self, envelope, self.project.path("src/main.c"))
        data = envelope["data"]
        self.assertEqual(
            (data["diagnostics"], data["observed_count"], data["truncated"]), ([], 0, False)
        )

    def test_saved_file_updates_are_seen_error_then_fix(self) -> None:
        source = self.project.path("src/main.c")
        original = source.read_bytes()
        clean = assert_envelope(
            self, diagnostics(self.analysis, self.project, "src/main.c"), "diagnostics", status="ok"
        )
        source.write_bytes(
            original.replace(b"int total = scale_value(3, 4);", b"int total = undefined_symbol;")
        )
        broken = assert_envelope(
            self, diagnostics(self.analysis, self.project, "src/main.c"), "diagnostics", status="ok"
        )
        self.assertEqual(broken["source"]["sha256"], sha256_file(source))
        messages = [item["message"] for item in broken["data"]["diagnostics"]]
        self.assertTrue(any("undefined_symbol" in message for message in messages), messages)
        self.assertIn(1, [item["severity"] for item in broken["data"]["diagnostics"]])
        source.write_bytes(original)
        fixed = assert_envelope(
            self, diagnostics(self.analysis, self.project, "src/main.c"), "diagnostics", status="ok"
        )
        self.assertEqual(fixed["data"]["diagnostics"], [])
        versions = [envelope["source"]["document_version"] for envelope in (clean, broken, fixed)]
        self.assertEqual(
            versions, sorted(set(versions)), "each saved revision is a newer document version"
        )


class HostCpp(RealClangdCase):
    def test_overloads_and_template_resolve_to_the_selected_declarations(self) -> None:
        project = copy_fixture("host_cpp", self.base / "host_cpp")
        host_database(project, ["src/shapes.cpp"])
        analysis = self.service(project)
        expected = {
            # (needle occurrence in shapes.cpp) -> acceptable (file, line) targets
            ("area(2.0)", 0): {("shapes.hpp", 4), ("shapes.cpp", 5)},  # double overload
            ("area(3)", 0): {("shapes.hpp", 3), ("shapes.cpp", 4)},  # int overload
            ("doubled", 0): {("shapes.hpp", 5)},
        }
        for (needle, occurrence), targets in expected.items():
            with self.subTest(needle=needle, occurrence=occurrence):
                envelope = assert_envelope(
                    self,
                    query(
                        analysis,
                        "definition",
                        project,
                        "src/shapes.cpp",
                        needle,
                        occurrence=occurrence,
                    ),
                    "definition",
                    status="ok",
                )
                found = {(name, line) for name, line, _column in location_points(envelope)}
                self.assertTrue(found & targets, (found, targets))


class UnicodeProject(RealClangdCase):
    def _project(self, escaped: bool) -> AnalysisProject:
        project = AnalysisProject(
            self.base / f"{'escaped' if escaped else 'literal'}" / UNICODE_ROOT
        )
        project.write_text("include/uni.h", UNICODE_HEADER)
        project.write_text("src/main.c", UNICODE_SOURCE)
        project.write_database(
            [
                project.entry(
                    "src/main.c",
                    arguments=[
                        "clang",
                        "-std=c11",
                        "-I",
                        str(project.path("include")),
                        "-c",
                        str(project.path("src/main.c")),
                    ],
                )
            ],
            escape_non_ascii=escaped,
        )
        return project

    def test_unicode_crlf_positions_round_trip_under_a_unicode_root(self) -> None:
        project = self._project(escaped=False)
        analysis = self.service(project)
        envelope = assert_envelope(
            self,
            query(analysis, "definition", project, "src/main.c", "target_symbol", occurrence=1),
            "definition",
            status="ok",
        )
        assert_source(self, envelope, project.path("src/main.c"))
        location = envelope["data"]["locations"][0]
        self.assertTrue(same_path(location["path"], project.path("src/main.c")))
        self.assertEqual(location["range"]["start"], {"line": 3, "column": 12})
        self.assertEqual(location["range"]["end"], {"line": 3, "column": 25})
        clean = assert_envelope(
            self, diagnostics(analysis, project, "src/main.c"), "diagnostics", status="ok"
        )
        self.assertEqual(clean["data"]["diagnostics"], [])
        self.assertIn(
            b"\r\n", project.path("src/main.c").read_bytes(), "saved CRLF bytes are never rewritten"
        )

    def test_raw_clangd_distinguishes_the_two_database_representations(self) -> None:
        # Backend characterization independent of the product: both files are
        # valid JSON for the same paths, but clangd only resolves the include
        # through the literal form.  If a newer clangd stops failing here, the
        # backend blocker below must be revisited rather than this test relaxed.
        outcomes = {}
        for escaped in (False, True):
            project = self._project(escaped=escaped)
            completed = subprocess.run(  # noqa: S603 - harness-supplied fixture path
                [
                    str(self.executable),
                    f"--check={project.path('src/main.c')}",
                    f"--compile-commands-dir={project.root}",
                ],
                capture_output=True,
                timeout=QUERY_TIMEOUT,
                check=False,
            )
            log = (completed.stdout + completed.stderr).decode("utf-8", "replace")
            outcomes[escaped] = (completed.returncode, log)
        literal_exit, literal_log = outcomes[False]
        escaped_exit, escaped_log = outcomes[True]
        print(f"\nclangd --check literal exit={literal_exit} escaped exit={escaped_exit}")
        self.assertEqual(literal_exit, 0, literal_log[-4000:])
        self.assertNotEqual(escaped_exit, 0, escaped_log[-4000:])
        self.assertIn("uni.h", escaped_log)

    def test_escaped_surrogate_database_is_an_explicit_problem_not_success(self) -> None:
        # ROOT 815d19ab/834b1e1f: clangd 23.1.0 mishandles the escaped UTF-16
        # surrogate-pair form of this valid JSON.  The backend must say so with
        # a literal-UTF-8 re-export remedy and leave the database untouched;
        # the literal form of the same database is the working control.
        escaped = self._project(escaped=True)
        database = escaped.path("compile_commands.json")
        raw = database.read_bytes()
        self.assertIn(b"\\ud83d\\ude00", raw)
        analysis = self.service(escaped)
        envelope = assert_envelope(
            self,
            query(analysis, "definition", escaped, "src/main.c", "target_symbol", occurrence=1),
            "definition",
        )
        self.assertNotIn(
            envelope["status"],
            {"ok", "not_found"},
            json.dumps(envelope, indent=2, ensure_ascii=False),
        )
        assert_failure(self, envelope, "definition", "not_configured", CONFIGURATION_CODES)
        self.assertRegex(envelope["remedy"].lower(), r"utf-?8")
        self.assertRegex(envelope["remedy"].lower(), r"re-?export|regenerat|export")
        status = assert_envelope(self, analysis.status(), "status", status="not_configured")
        self.assertTrue(status["data"]["configuration_problems"], status)
        self.assertEqual(database.read_bytes(), raw, "the project database must never be rewritten")
        self.assertFalse((escaped.root / "compile_commands.json.bak").exists())

        literal = self._project(escaped=False)
        control = query(
            self.service(literal),
            "definition",
            literal,
            "src/main.c",
            "target_symbol",
            occurrence=1,
        )
        assert_envelope(self, control, "definition", status="ok")


class AnalysisWriteBoundaryOracle(unittest.TestCase):
    """Hermetic proof that the ARM tree oracle rejects out-of-allowlist writes."""

    SHARD = "main.c.0123456789ABCDEF.idx"
    PROTECTED = (
        "src/main.c",
        "include/firmware.h",
        "build/generated/build_config.h",
        "build/compile_commands.json",
        "byo-analysis.json",
        ".clangd",
        ".firm/board-policy.json",
    )

    def setUp(self) -> None:
        directory = temporary_directory("byo-ca-oracle-")
        self.project = AnalysisProject(directory.__enter__() / "project")
        self.addCleanup(directory.__exit__, None, None, None)
        for relative in self.PROTECTED:
            self.project.write_text(relative, f"original {relative}\n")
        self.before = self.project.tree_digest()

    def changes(self) -> list[str]:
        return unpermitted_tree_changes(self.before, self.project.tree_digest(), "build")

    def test_documented_analysis_writes_are_permitted(self) -> None:
        for relative in (
            ".firm/code-analysis/reports/run-1/report.json",
            "build/.cache/clangd/index/.gitignore",
            f"build/.cache/clangd/index/{self.SHARD}",
        ):
            self.project.write_text(relative, "analysis data\n")
        self.assertEqual(self.changes(), [])

    def test_every_other_write_is_reported(self) -> None:
        write = self.project.write_text
        rewritten = [name for name in self.PROTECTED if name != "include/firmware.h"]
        cases = {
            **{f"changed: {name}": partial(write, name, "rewritten\n") for name in rewritten},
            "removed: include/firmware.h": self.project.path("include/firmware.h").unlink,
            "created: .firm/other/state.json": lambda: write(".firm/other/state.json", "{}"),
            f"created: .cache/clangd/index/{self.SHARD}": lambda: write(
                f".cache/clangd/index/{self.SHARD}", "wrong database directory"
            ),
            "created: build/.cache/clangd/index/notes.txt": lambda: write(
                "build/.cache/clangd/index/notes.txt", "not a shard"
            ),
            f"created: build/.cache/clangd/index/{self.SHARD}.tmp": lambda: write(
                f"build/.cache/clangd/index/{self.SHARD}.tmp", "not a shard"
            ),
            f"created: build/.cache/clangd/{self.SHARD}": lambda: write(
                f"build/.cache/clangd/{self.SHARD}", "outside the index directory"
            ),
            f"created: build/.cache/clangd/index/nested/{self.SHARD}": lambda: write(
                f"build/.cache/clangd/index/nested/{self.SHARD}", "nested"
            ),
            "created: build/firmware.o": lambda: write("build/firmware.o", "build output"),
        }
        for expected, mutate in cases.items():
            with self.subTest(expected=expected):
                mutate()
                self.assertIn(expected, self.changes())


class ArmFirmware(RealClangdCase):
    def setUp(self) -> None:
        super().setUp()
        self.arm_root = arm_gnu_root_or_skip(self)

    def test_query_driver_gives_clean_target_analysis(self) -> None:
        project = materialize_arm_fixture(self.base / "arm", self.arm_root)
        before = project.tree_digest()
        for protected in PROTECTED_ARM_INPUTS:
            self.assertIn(protected, before, "the write oracle must cover every real input")
        analysis = self.service(project)
        for unit in ("src/main.c", "src/heartbeat.cpp"):
            with self.subTest(unit=unit):
                envelope = assert_envelope(
                    self, diagnostics(analysis, project, unit), "diagnostics", status="ok"
                )
                self.assertTrue(
                    same_path(
                        envelope["compilation_database"],
                        project.path("build/compile_commands.json"),
                    )
                )
                self.assertEqual(
                    envelope["data"]["diagnostics"], [], json.dumps(envelope, indent=2)
                )
        definition = assert_envelope(
            self,
            query(analysis, "definition", project, "src/main.c", "advance_heartbeat"),
            "definition",
            status="ok",
        )
        found = {(name, line) for name, line, _column in location_points(definition)}
        self.assertTrue(found & {("firmware.h", 9), ("heartbeat.cpp", 3)}, found)
        hover = assert_envelope(
            self,
            query(analysis, "hover", project, "src/heartbeat.cpp", "HEARTBEAT_STEP"),
            "hover",
            status="ok",
        )
        self.assertIn("HEARTBEAT_STEP", hover["data"]["contents"]["value"])
        references = assert_envelope(
            self,
            query(
                analysis,
                "references",
                project,
                "src/main.c",
                "firmware_heartbeat",
                include_declaration=True,
            ),
            "references",
            status="ok",
        )
        self.assertGreaterEqual(references["data"]["observed_count"], 2)
        # Stop clangd first so a still-running background indexer cannot race
        # the final snapshot; shutdown is idempotent with the cleanup call.
        analysis.shutdown()
        after = project.tree_digest()
        created = sorted(set(after) - set(before))
        print(f"\nARM analysis created {len(created)} permitted path(s): {created}")
        self.assertEqual(
            unpermitted_tree_changes(before, after, "build"),
            [],
            "only .firm/code-analysis/ and build/.cache/clangd/index/ shards/.gitignore "
            "may be written; every other project file must be unchanged",
        )

    def test_empty_allowlist_still_analyzes_the_exact_database_command(self) -> None:
        # An empty allowlist grants no compiler execution, but the exported
        # argv still names the cross driver (target inferred from its name) and
        # passes the toolchain headers with -I; native clangd --check is clean.
        project = materialize_arm_fixture(
            self.base / "arm-nodriver", self.arm_root, query_drivers=False
        )
        envelope = assert_envelope(
            self,
            diagnostics(self.service(project), project, "src/main.c"),
            "diagnostics",
            status="ok",
        )
        self.assertEqual(envelope["data"]["diagnostics"], [], json.dumps(envelope, indent=2))

    def test_generic_host_driver_is_never_reported_as_clean_arm_analysis(self) -> None:
        # Contract: generic host flags are not_configured.  The same sources
        # under a plain host driver fail the ILP32 assertion in native clangd.
        project = materialize_arm_fixture(
            self.base / "arm-hostdriver", self.arm_root, query_drivers=False
        )
        database = project.path("build/compile_commands.json")
        entries = json.loads(database.read_text(encoding="utf-8"))
        for entry in entries:
            entry["arguments"][0] = "clang"
        project.write_database(entries, "build/compile_commands.json")
        envelope = assert_envelope(
            self, diagnostics(self.service(project), project, "src/main.c"), "diagnostics"
        )
        if envelope["status"] == "ok":
            messages = " ".join(item["message"] for item in envelope["data"]["diagnostics"])
            self.assertRegex(
                messages,
                r"(?i)ARM ILP32|unsupported option",
                "host ABI analysis must not look like clean ARM analysis",
            )
        else:
            self.assertEqual(envelope["status"], "not_configured", envelope)


SELECTED_SOURCE = (
    '#include "selected.h"\n'
    'static_assert(SELECTED == 7, "fresh header required");\n'
    "int main() { return SELECTED; }\n"
)


class KnownHeaderFreshness(RealClangdCase):
    """A saved change to a header the selected build includes is never stale-clean.

    The header directory comes from the original database include path and the
    header is explicitly queried first, so it is known to the session.  A header
    under the project's ``.firm/build/generated`` (a generated build header) gets
    the same promise as an ordinary ``include`` header: after ``7 -> 9`` the next
    diagnostics call is a newer document version carrying the current error, a
    fresh session agrees, and restoring the header returns to a fresh clean result.
    """

    def _owned_clangd_pids(self) -> list[int]:
        import psutil

        return [
            child.pid
            for child in psutil.Process().children(recursive=True)
            if same_path(child.exe(), self.executable)
        ]

    def _assert_header_change_is_fresh(self, relative_header: str) -> None:
        project = AnalysisProject(self.base / "project")
        project.write_text("src/main.cpp", SELECTED_SOURCE)
        header = project.write_text(relative_header, "#define SELECTED 7\n")
        database = project.write_database(
            [
                project.entry(
                    "src/main.cpp",
                    arguments=[
                        "clang++",
                        "-std=c++17",
                        "-I",
                        str(header.parent),
                        "-c",
                        str(project.path("src/main.cpp")),
                    ],
                )
            ]
        )
        protected = {path: path.read_bytes() for path in (database, project.path("src/main.cpp"))}
        analysis = self.service(project)

        initial = assert_envelope(
            self, diagnostics(analysis, project, "src/main.cpp"), "diagnostics", status="ok"
        )
        self.assertEqual(initial["data"]["diagnostics"], [], json.dumps(initial, indent=2))
        known = assert_envelope(
            self,
            query(analysis, "hover", project, relative_header, "SELECTED"),
            "hover",
            status="ok",
        )
        assert_source(self, known, header)
        self.assertIn("SELECTED 7", known["data"]["contents"]["value"])

        header.write_text("#define SELECTED 9\n", encoding="utf-8", newline="\n")
        changed = diagnostics(analysis, project, "src/main.cpp")
        self.assertEqual(
            (changed["status"], changed.get("code")),
            ("ok", None),
            f"a known header change needs a current result: {json.dumps(changed, indent=2)}",
        )
        assert_source(self, changed, project.path("src/main.cpp"))
        messages = [item["message"] for item in changed["data"]["diagnostics"]]
        self.assertTrue(
            any("fresh header required" in message for message in messages),
            f"stale clean result after the saved header changed 7 -> 9: {json.dumps(changed, indent=2)}",
        )
        self.assertIn(1, [item["severity"] for item in changed["data"]["diagnostics"]])
        self.assertGreater(
            changed["source"]["document_version"], initial["source"]["document_version"]
        )

        control_service = self.service(project)
        control = assert_envelope(
            self, diagnostics(control_service, project, "src/main.cpp"), "diagnostics", status="ok"
        )
        self.assertTrue(
            any(
                "fresh header required" in item["message"]
                for item in control["data"]["diagnostics"]
            ),
            f"fresh-session native control: {json.dumps(control, indent=2)}",
        )

        header.write_text("#define SELECTED 7\n", encoding="utf-8", newline="\n")
        restored = assert_envelope(
            self, diagnostics(analysis, project, "src/main.cpp"), "diagnostics", status="ok"
        )
        self.assertEqual(restored["data"]["diagnostics"], [], json.dumps(restored, indent=2))
        self.assertGreater(
            restored["source"]["document_version"], changed["source"]["document_version"]
        )

        pids = self._owned_clangd_pids()
        self.assertTrue(pids, "both sessions own a clangd child")
        analysis.shutdown()
        control_service.shutdown()
        self.assertEqual(
            wait_pids_gone(pids, OWNED_CLEANUP_SECONDS), [], "clangd outlived shutdown"
        )
        for path, data in protected.items():
            self.assertEqual(path.read_bytes(), data, f"{path} must not be modified")

    def test_ordinary_include_header_change_is_fresh(self) -> None:
        self._assert_header_change_is_fresh("include/selected.h")

    def test_generated_header_under_firm_build_change_is_fresh(self) -> None:
        self._assert_header_change_is_fresh(".firm/build/generated/selected.h")


class RealStdioShutdown(RealClangdCase):
    def test_stdin_eof_terminates_owned_clangd_within_the_bound(self) -> None:
        project = copy_fixture("host_c", self.base / "host_c")
        host_database(project, ["src/main.c", "src/util.c"])
        server = AnalysisStdioServer(self.base / "stdio", project.root, executable=self.executable)
        self.addCleanup(server.kill)
        server.initialize()
        line, column = position(project, "src/main.c", "scale_value", 1)
        envelope = server.call_envelope(
            "find_code_definition",
            {
                "file_path": str(project.path("src/main.c")),
                "line": line,
                "column": column,
                "timeout_seconds": 90,
            },
        )
        assert_envelope(self, envelope, "definition", status="ok")
        children = server.descendants()
        self.assertTrue(children, "a ready session owns a clangd child")
        self.assertTrue(server.markers(), "owned children are recorded while running")
        code, elapsed = server.close_stdin_and_wait(timeout=60)
        print(f"\nreal clangd stdio EOF: exit={code} elapsed={elapsed:.2f}s children={children}")
        self.assertEqual(code, 0, server.stderr_text()[-4000:])
        self.assertLess(elapsed, SHUTDOWN_BOUND, "EOF shutdown exceeded 5 s + cleanup ceiling")
        self.assertEqual(
            wait_pids_gone(children, OWNED_CLEANUP_SECONDS), [], "clangd outlived the server"
        )
        self.assertEqual(server.markers(), [], "owned-process markers must be resolved at exit")
        for raw in server.stdout_lines:
            if raw.strip():
                self.assertEqual(json.loads(raw).get("jsonrpc"), "2.0", raw[:200])


if __name__ == "__main__":
    unittest.main()
