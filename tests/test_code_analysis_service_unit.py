"""Implementer checks for service boundaries, MCP JSON and real clangd queries."""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pyocd_debug_mcp.code_analysis_config import load_analysis_config, sha256_bytes
from pyocd_debug_mcp.code_analysis_runtime import (
    AnalysisRuntimeContext,
    ResolvedAnalysisProgram,
    resolve_analysis_program,
)
from pyocd_debug_mcp.kernel.operations import operation_timeout_seconds
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP
from pyocd_debug_mcp.services.code_analysis import CodeAnalysisService
from pyocd_debug_mcp.tools.code_analysis import (
    CodeAnalysisToolServices,
    build_code_analysis_handlers,
)
from pyocd_debug_mcp.tools.plans import forbid_unknown_tool_arguments

from test_clangd_adapter_unit import fake_factory, make_project


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="analysis service é 😀 ")
        self.root = Path(self.temp.name).resolve()
        self.source = make_project(self.root)
        self.program = ResolvedAnalysisProgram(
            "clangd", Path(sys.executable), "fake", "path", ("fake",)
        )
        self.service = CodeAnalysisService(
            AnalysisRuntimeContext(self.root),
            fake_factory(self.root),
            program_resolver=lambda *args: self.program,
        )

    def tearDown(self):
        self.service.shutdown()
        self.temp.cleanup()

    def test_saved_revision_counts_and_empty_versioned_diagnostics(self):
        first = self.service.diagnostics("main.cpp")
        self.assertEqual(first["status"], "ok", first)
        self.assertEqual(first["data"]["observed_count"], 0)
        second = self.service.diagnostics("main.cpp")
        self.assertGreater(
            second["source"]["document_version"], first["source"]["document_version"]
        )
        self.source.write_bytes(b"int value = 2;\r\n")
        hover = self.service.hover("main.cpp", 1, 5)
        self.assertEqual(hover["source"]["sha256"], sha256_bytes(self.source.read_bytes()))
        self.assertEqual(hover["status"], "ok", hover)
        self.assertEqual(self.service.references("main.cpp", 1, 5)["status"], "not_found")

    def test_concurrent_same_file_versions_and_source_change(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.service.diagnostics("main.cpp"), range(2)))
        self.assertTrue(all(r["status"] == "ok" for r in results), results)
        self.assertEqual(len({r["source"]["document_version"] for r in results}), 2)
        self.service.shutdown()
        self.service = CodeAnalysisService(
            AnalysisRuntimeContext(self.root),
            fake_factory(self.root, "delayed"),
            program_resolver=lambda *args: self.program,
        )

        def edit():
            time.sleep(0.12)
            self.source.write_bytes(b"int value = 3;\r\n")

        worker = threading.Thread(target=edit)
        worker.start()
        result = self.service.hover("main.cpp", 1, 5)
        worker.join()
        self.assertEqual(result.get("code"), "analysis/source-changed", result)

    def test_unversioned_diagnostics_are_incomplete(self):
        self.service.shutdown()
        self.service = CodeAnalysisService(
            AnalysisRuntimeContext(self.root),
            fake_factory(self.root, "unversioned"),
            program_resolver=lambda *args: self.program,
        )
        result = self.service.diagnostics("main.cpp", timeout_seconds=0.2)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertEqual(result["code"], "analysis/diagnostics-unversioned", result)

    def test_related_diagnostics_preserve_flat_coordinates_and_missing_target(self):
        text = "/*😀*/ int value;\r\n"
        raw_range = {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 7}}
        missing = self.root / "missing.h"
        result = self.service._envelope("diagnostics")
        self.service._normalize(
            "diagnostics",
            [
                {
                    "message": "issue",
                    "range": raw_range,
                    "relatedInformation": [
                        {
                            "message": "saved target",
                            "location": {"uri": self.source.as_uri(), "range": raw_range},
                        },
                        {
                            "message": "unreadable target",
                            "location": {"uri": missing.as_uri(), "range": raw_range},
                        },
                    ],
                }
            ],
            SimpleNamespace(position_encoding="utf-16"),
            self.source,
            text,
            result,
            100,
        )
        related = result["data"]["diagnostics"][0]["related_locations"]
        self.assertEqual(related[0]["range"]["start"], {"line": 1, "column": 6})
        self.assertEqual(related[0]["path"], str(self.source))
        self.assertNotIn("location", related[0])
        self.assertEqual(related[1]["raw_lsp_range"], raw_range)
        self.assertEqual(related[1]["lsp_position_encoding"], "utf-16")
        self.assertEqual(related[1]["position_mapping"], "unavailable")
        self.assertIsNone(related[1]["range"])

    def test_strict_config_selection_and_fragments(self):
        (self.root / "byo-analysis.json").write_text(
            '{"schema_version":1,"schema_version":1}', encoding="utf-8"
        )
        self.assertEqual(
            load_analysis_config(self.root).problems[0].code, "analysis/config-invalid"
        )
        (self.root / "byo-analysis.json").unlink()
        (self.root / "build").mkdir()
        shutil.copyfile(
            self.root / "compile_commands.json", self.root / "build/compile_commands.json"
        )
        self.assertEqual(
            load_analysis_config(self.root).problems[0].code, "analysis/database-ambiguous"
        )
        (self.root / "byo-analysis.json").write_text(
            '{"schema_version":1,"compilation_database":"compile_commands.json"}', encoding="utf-8"
        )
        (self.root / ".clangd").write_text(
            "If:\n  PathMatch: .*\\.cpp\nCompileFlags:\n  CompilationDatabase: build\n",
            encoding="utf-8",
        )
        self.assertEqual(
            self.service.hover("main.cpp", 1, 5)["code"], "analysis/clangd-config-conflict"
        )

    def test_status_lazy_shutdown_and_managed_unavailable(self):
        self.assertEqual(self.service.status()["data"]["session_state"], "not_started")
        self.assertIsNone(self.service._adapter)
        self.service.shutdown()
        self.service.shutdown()
        self.assertEqual(self.service.status()["data"]["session_state"], "closed")
        managed = resolve_analysis_program(
            AnalysisRuntimeContext(self.root, self.root, True), "clangd", time.monotonic() + 1
        )
        self.assertEqual(managed.problems[0].code, "analysis/executable-unavailable")

    @unittest.skipUnless(os.name == "nt", "Windows batch interpretation")
    def test_windows_batch_program_and_query_driver_never_execute(self):
        wrapper = self.root / "clangd.cmd"
        wrapper.write_text("@echo should never execute\n", encoding="utf-8")
        with (
            patch("pyocd_debug_mcp.code_analysis_runtime.shutil.which", return_value=str(wrapper)),
            patch("pyocd_debug_mcp.code_analysis_runtime.spawn_before_deadline") as spawn,
        ):
            result = resolve_analysis_program(
                AnalysisRuntimeContext(self.root), "clangd", time.monotonic() + 1
            )
            self.assertEqual(result.problems[0].code, "analysis/executable-unavailable")
            spawn.assert_not_called()
        (self.root / "byo-analysis.json").write_text(
            json.dumps(
                {"schema_version": 1, "clangd": {"query_driver_paths": [str(wrapper)]}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            load_analysis_config(self.root).problems[0].code, "analysis/driver-invalid"
        )

    def test_escaped_non_bmp_database_is_an_explicit_native_blocker(self):
        database = self.root / "compile_commands.json"
        database.write_text(
            json.dumps(json.loads(database.read_text(encoding="utf-8"))), encoding="utf-8"
        )
        result = self.service.hover("main.cpp", 1, 5)
        self.assertEqual(result["status"], "not_configured", result)
        self.assertEqual(result["code"], "analysis/database-invalid", result)
        self.assertIn("literal UTF-8", result["remedy"])
        self.assertFalse(load_analysis_config(self.root).problems)

    def test_budget_and_strict_mcp_arguments_json_checkin(self):
        self.assertEqual(operation_timeout_seconds("get_code_hover", {"timeout_seconds": 60}), 65)
        self.assertEqual(
            self.service.hover("main.cpp", True, 5)["code"], "analysis/invalid-argument"
        )
        mcp = RegistryFastMCP("analysis-unit")
        for name, handler in build_code_analysis_handlers(
            CodeAnalysisToolServices(self.service)
        ).items():
            mcp.add_tool(handler, name=name, structured_output=False)
            forbid_unknown_tool_arguments(mcp, name)

        class Monitor:
            def begin(self, *args):
                return None

            def consume_checkin_prompt(self):
                return "monitor prompt"

        mcp.configure_monitor(Monitor())

        async def check():
            blocks = await mcp.call_tool(
                "get_code_hover", {"file_path": "main.cpp", "line": 1, "column": 5}
            )
            self.assertEqual(json.loads(blocks[0].text)["status"], "ok")
            self.assertEqual(blocks[1].text, "monitor prompt")
            for extra in ({"executable": "fake"}, {"line": "1"}, {"line": True}):
                with self.assertRaises(Exception):
                    await mcp.call_tool(
                        "get_code_hover", {"file_path": "main.cpp", "line": 1, "column": 5, **extra}
                    )

            with patch.object(
                mcp,
                "_finalizer_resolver",
                side_effect=AssertionError("hardware finalizer consulted"),
            ):
                with self.assertRaises(Exception) as error:
                    await mcp.call_tool(
                        "get_code_hover",
                        {
                            "file_path": "main.cpp",
                            "line": 1,
                            "column": 5,
                            "board_id": "unrelated",
                            "on_exit": {"action": "uart_write", "data": "x"},
                        },
                    )
                self.assertNotIn("hardware finalizer consulted", str(error.exception))

        anyio.run(check)


@unittest.skipUnless(
    os.environ.get("BYO_TEST_CLANGD"), "Set explicit BYO_TEST_CLANGD private test fixture."
)
class RealClangdTests(unittest.TestCase):
    def record_evidence(self, name, result):
        destination = os.environ.get("BYO_TEST_ANALYSIS_EVIDENCE")
        if not destination:
            return
        directory = Path(destination) / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if result.get("compilation_database"):
            (directory / "compile_commands.json").write_bytes(
                Path(result["compilation_database"]).read_bytes()
            )
        if result.get("source"):
            (directory / "source.saved").write_bytes(Path(result["source"]["path"]).read_bytes())
        if self.service and self.service._adapter:
            adapter = self.service._adapter
            (directory / "clangd.stderr-tail.txt").write_text(adapter.stderr_tail, encoding="utf-8")
            (directory / "argv.json").write_text(
                json.dumps(adapter._argv(), ensure_ascii=False), encoding="utf-8"
            )
        (directory / "executable.sha256").write_text(
            sha256_bytes(self.executable.read_bytes()), encoding="ascii"
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="analysis actual é 😀 ")
        self.root = Path(self.temp.name).resolve()
        self.executable = Path(os.environ["BYO_TEST_CLANGD"]).resolve(strict=True)
        self.service = None
        self.env_patch = patch.dict(
            os.environ, {"PATH": str(self.executable.parent) + os.pathsep + os.environ["PATH"]}
        )
        self.env_patch.start()

    def tearDown(self):
        if self.service is not None:
            self.service.shutdown()
        self.env_patch.stop()
        self.temp.cleanup()

    def test_semantics_unicode_saved_diagnostics_and_cleanup(self):
        code = "// é中😀é\r\nint add(int x) { return x + 1; }\r\nfloat add(float x) { return x + 2; }\r\ntemplate<class T> T twice(T x) { return x+x; }\r\nint main() { /*é中😀é*/ return add(twice(2)); }\r\n"
        source = self.root / "main.cpp"
        source.write_bytes(code.encode())
        (self.root / "compile_commands.json").write_text(
            json.dumps(
                [
                    {
                        "directory": str(self.root),
                        "file": str(source),
                        "arguments": [
                            "clang++",
                            "--target=arm-none-eabi",
                            "-mcpu=cortex-m4",
                            "-mthumb",
                            "-ffreestanding",
                            "-std=c++17",
                            "-c",
                            str(source),
                        ],
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.service = CodeAnalysisService(AnalysisRuntimeContext(self.root))
        status = self.service.status()
        self.assertEqual(status["status"], "ok", status)
        print(
            "REAL CLANGD",
            status["data"]["version"],
            "sha256",
            sha256_bytes(self.executable.read_bytes()),
        )
        clean = self.service.diagnostics("main.cpp")
        self.record_evidence("unicode-clean", clean)
        self.assertEqual(clean["status"], "ok", clean)
        self.assertEqual(clean["data"]["observed_count"], 0, clean)
        for column in (
            code.splitlines()[4].index("add") + 1,
            code.splitlines()[4].index("twice") + 1,
        ):
            definition = self.service.definition("main.cpp", 5, column)
            self.assertEqual(definition["status"], "ok", definition)
            self.assertEqual(definition["data"]["locations"][0]["position_mapping"], "available")
        hover = self.service.hover("main.cpp", 5, code.splitlines()[4].index("add") + 1)
        self.record_evidence("unicode-hover", hover)
        self.assertEqual(hover["status"], "ok", hover)
        refs = self.service.references("main.cpp", 2, 5, include_declaration=True)
        self.assertEqual(refs["status"], "ok", refs)
        self.assertEqual(refs["index_completeness"], "unverified")
        process = self.service._adapter.process
        source.write_bytes((code + "int broken = missing_symbol;\r\n").encode())
        defect = self.service.diagnostics("main.cpp")
        self.record_evidence("unicode-defect", defect)
        self.assertEqual(defect["status"], "ok", defect)
        self.assertGreater(defect["data"]["observed_count"], 0, defect)
        source.write_bytes(code.encode())
        restored = self.service.diagnostics("main.cpp")
        self.record_evidence("unicode-restored", restored)
        self.assertEqual(restored["status"], "ok", restored)
        self.assertEqual(restored["data"]["observed_count"], 0)
        self.assertGreater(
            restored["source"]["document_version"], defect["source"]["document_version"]
        )
        self.service.shutdown()
        self.assertIsNotNone(process.poll())

    @unittest.skipUnless(
        os.environ.get("BYO_TEST_ARM_FIXTURE"), "ROOT built ARM fixture not supplied."
    )
    def test_actual_built_arm_fixture_and_literal_query_driver(self):
        original = Path(os.environ["BYO_TEST_ARM_FIXTURE"]).resolve()
        target = self.root / "firmware"
        shutil.copytree(original, target)

        def relocate(value):
            if isinstance(value, str):
                return value.replace(str(original), str(target)).replace(
                    original.as_posix(), target.as_posix()
                )
            if isinstance(value, list):
                return [relocate(item) for item in value]
            if isinstance(value, dict):
                return {key: relocate(item) for key, item in value.items()}
            return value

        database = target / "build/compile_commands.json"
        database.write_text(
            json.dumps(
                relocate(json.loads(database.read_text(encoding="utf-8"))), ensure_ascii=False
            ),
            encoding="utf-8",
        )
        config = json.loads((target / "byo-analysis.json").read_text(encoding="utf-8"))
        entries = json.loads(database.read_text(encoding="utf-8"))
        config["clangd"] = {
            "query_driver_paths": list(dict.fromkeys(e["arguments"][0] for e in entries))
        }
        (target / "byo-analysis.json").write_text(json.dumps(config), encoding="utf-8")
        self.service = CodeAnalysisService(AnalysisRuntimeContext(target))
        for entry in entries:
            result = self.service.diagnostics(entry["file"])
            self.record_evidence("arm-" + Path(entry["file"]).stem, result)
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["data"]["observed_count"], 0, result)
        print(
            "REAL ARM selected database sha256",
            sha256_bytes(database.read_bytes()),
            "units",
            len(entries),
        )
        definition = self.service.definition("src/main.c", 4, 37)
        self.record_evidence("arm-definition", definition)
        self.assertEqual(definition["status"], "ok", definition)
        self.assertTrue(
            any(
                location["path"].endswith("heartbeat.cpp")
                for location in definition["data"]["locations"]
            ),
            definition,
        )
        hover = self.service.hover("src/main.c", 4, 37)
        self.record_evidence("arm-hover", hover)
        self.assertEqual(hover["status"], "ok", hover)
        references = self.service.references("src/heartbeat.cpp", 3, 10, include_declaration=True)
        self.record_evidence("arm-references", references)
        self.assertEqual(references["status"], "ok", references)
        self.assertGreaterEqual(references["data"]["observed_count"], 2, references)

    def test_actual_mcp_stdio_queries_and_eof_cleanup(self):
        make_project(self.root)
        env = dict(
            os.environ,
            BYO_MCP_ARTIFACT_ROOT=str(self.root),
            PYOCD_MCP_RUNS_ROOT=str(self.root / ".firm/runs"),
            BYO_MCP_MONITOR_ROOT=str(self.root / "monitor"),
        )

        async def run():
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "pyocd_debug_mcp.server"], env=env
            )
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    inventory = await session.list_tools()
                    self.assertTrue(
                        {
                            "get_code_analysis_status",
                            "find_code_definition",
                            "find_code_references",
                            "get_code_hover",
                            "get_code_diagnostics",
                        }
                        <= {t.name for t in inventory.tools}
                    )
                    diagnostics = await session.call_tool(
                        "get_code_diagnostics", {"file_path": "main.cpp"}
                    )
                    self.assertFalse(diagnostics.isError, diagnostics)
                    self.assertEqual(json.loads(diagnostics.content[0].text)["status"], "ok")
                    hover = await session.call_tool(
                        "get_code_hover", {"file_path": "main.cpp", "line": 1, "column": 5}
                    )
                    self.assertEqual(json.loads(hover.content[0].text)["status"], "ok")

        anyio.run(run)
        self.assertFalse(list((self.root / ".firm/runs/owned-processes").glob("*.json")))

    def test_native_database_command_quoting_both_fields_and_relative_directory(self):
        source = self.root / "main.cpp"
        include = self.root / "include"
        include.mkdir()
        (include / "selected.h").write_text("#define INCLUDED_BUILD 7\n", encoding="utf-8")
        source.write_text(
            '#include "selected.h"\nstatic_assert(SELECTED_BUILD == INCLUDED_BUILD);\nstatic_assert(sizeof(void*) == 4);\nint selected = SELECTED_BUILD;\n',
            encoding="utf-8",
        )
        argv = [
            "clang++",
            "--target=arm-none-eabi",
            "-mcpu=cortex-m4",
            "-mthumb",
            "-std=c++17",
            "-DSELECTED_BUILD=7",
            "-I",
            include.as_posix(),
            "-c",
            source.as_posix(),
        ]
        command = " ".join('"' + argument.replace('"', '\\"') + '"' for argument in argv)
        self.service = CodeAnalysisService(AnalysisRuntimeContext(self.root))
        for name, entry in (
            (
                "both-fields",
                {
                    "directory": str(self.root),
                    "file": str(source),
                    "arguments": argv,
                    "command": command.replace("SELECTED_BUILD=7", "SELECTED_BUILD=99"),
                },
            ),
            (
                "command-only",
                {"directory": str(self.root), "file": str(source), "command": command},
            ),
            ("relative-directory", {"directory": ".", "file": "main.cpp", "arguments": argv}),
        ):
            (self.root / "compile_commands.json").write_text(
                json.dumps([entry], ensure_ascii=False), encoding="utf-8"
            )
            result = self.service.diagnostics("main.cpp")
            self.record_evidence(name, result)
            if name == "relative-directory":
                snapshot = load_analysis_config(self.root)
                self.assertFalse(snapshot.problems)
                self.assertEqual(snapshot.entries[0].directory, self.root)
                self.assertEqual(result["status"], "not_configured", result)
                self.assertEqual(result["code"], "analysis/database-invalid", result)
                self.assertIn("absolute directory", result["remedy"])
                continue
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["data"]["observed_count"], 0, result)


if __name__ == "__main__":
    unittest.main()
