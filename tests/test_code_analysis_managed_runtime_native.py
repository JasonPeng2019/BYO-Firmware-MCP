"""Windows staged development-input checks; never installed release proof.

Native input records are explicitly supplied by the harness, SHA-pinned and
copied into lane-local runtime/project roots. No PATH discovery or downloads.
Managed service/MCP calls retain the production resolver. The separately named
payload control runs real clangd --check to establish usable staged inputs; its
success cannot make a managed service test pass.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from tests.code_analysis_managed_support import (
    EVIDENCE,
    NATIVE_INPUT_ENV,
    OwnedSpawns,
    RuntimeFixture,
    assert_import_roots,
    digest,
    native_start_token,
    production,
    tree_hashes,
    write_json,
)
from tests.code_analysis_support import AnalysisProject, permitted_analysis_write


class NativeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("Windows x86_64 only")
        raw = os.environ.get(NATIVE_INPUT_ENV)
        if not raw:
            raise unittest.SkipTest("UNMET native input prerequisite: " + NATIVE_INPUT_ENV)
        path = Path(raw)
        expected = os.environ.get(NATIVE_INPUT_ENV + "_SHA256")
        if not expected or digest(path) != expected:
            raise AssertionError("Native input record must have an explicit matching SHA256")
        cls.inputs = json.loads(path.read_bytes())
        cls.originals = {e["source"]: e["sha256"] for e in cls.inputs["payload"]}
        cls.originals.update(cls.inputs["protected_inputs"])
        cls.originals.update(cls.inputs["frozen_arm_inputs"])
        for source, expected in cls.originals.items():
            assert digest(Path(source)) == expected, source

    @classmethod
    def tearDownClass(cls) -> None:
        after = {source: digest(Path(source)) for source in cls.originals}
        write_json(
            EVIDENCE / (cls.__name__ + "-protected-before-after.json"),
            {
                "before": cls.originals,
                "after": after,
                "unchanged": after == cls.originals,
            },
        )
        assert after == cls.originals, "Protected native development inputs changed"
        assert_import_roots()

    def setUp(self) -> None:
        self.base = EVIDENCE / "n" / uuid.uuid4().hex[:6]
        self.base.mkdir(parents=True)
        write_json(self.base / "case.json", {"case": self.id()})
        self.fixture = RuntimeFixture(self.base / "R \u4e2d", self.inputs)
        self.project = AnalysisProject(self.base / "P caf\u00e9 \u4e2d \U0001f600")
        # Self-contained C++ corpus exercises templates, overloads, cross-file
        # references and builtin headers without any installed C++ stdlib.
        self.header = self.project.write_text(
            "include/math.hpp",
            "#pragma once\r\n#include <stddef.h>\r\n"
            "template<class T> T twice(T n) { return n + n; }\r\n"
            "int choose(int n);\r\ndouble choose(double n);\r\n",
        )
        self.source = self.project.write_text(
            "src/math.cpp",
            '#include "math.hpp"\r\nint choose(int n) { return twice(n); }\r\n'
            "double choose(double n) { return twice(n); }\r\n",
        )
        self.main = self.project.write_text(
            "src/main.cpp",
            '#include "math.hpp"\r\n'
            "size_t builtin_size = sizeof(int);\r\n"
            "int use(void) { return /* \U0001f600 \u4e2d e\u0301 */ choose(twice(3)); }\r\n",
        )
        entries = []
        for relative in ("src/main.cpp", "src/math.cpp"):
            entries.append(
                self.project.entry(
                    relative,
                    arguments=[
                        "clang++",
                        "--target=x86_64-pc-windows-msvc",
                        "-std=c++17",
                        "-nostdlibinc",
                        "-nostdinc++",
                        "-I",
                        str(self.project.root / "include"),
                        "-c",
                        str(self.project.path(relative)),
                    ],
                )
            )
        # Suppress ambient library includes while retaining the builtin search.
        # The adapter's resource-dir must supply the actual mapped vendor tree.
        self.project.write_database(entries)
        self.project_before = tree_hashes(self.project.root)
        self.runtime_before = tree_hashes(self.fixture.root)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "PATH": str(self.base / "no-analyzers-on-path"),
                "PATHEXT": ".EXE",
                "PYTHONDONTWRITEBYTECODE": "1",
                "UV_NO_SYNC": "1",
                "UV_OFFLINE": "1",
                "PYOCD_MCP_RUNS_ROOT": str(self.base / "runs"),
                "BYO_MCP_MONITOR_ROOT": str(self.base / "monitor"),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.check_preservation)
        self.context = self.fixture.context(self.project.root)
        self.native_adapters: list[Any] = []

    def check_preservation(self) -> None:
        project_after = tree_hashes(self.project.root)
        changes = {
            p
            for p in set(self.project_before) | set(project_after)
            if self.project_before.get(p) != project_after.get(p)
        }
        forbidden = [p for p in changes if not permitted_analysis_write(p, ".")]
        runtime_after = tree_hashes(self.fixture.root)
        write_json(
            self.base / "preservation.json",
            {
                "project_before": self.project_before,
                "project_after": project_after,
                "forbidden_changes": forbidden,
                "runtime_before": self.runtime_before,
                "runtime_after": runtime_after,
                "runtime_unchanged": runtime_after == self.runtime_before,
            },
        )
        self.assertEqual(forbidden, [], "Protected project bytes changed")
        self.assertEqual(runtime_after, self.runtime_before, "Staged immutable runtime changed")

    def position(self, needle: str, path: Path | None = None) -> tuple[int, int]:
        lines = (path or self.main).read_bytes().decode("utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            if needle in line:
                return number, line.index(needle) + 1
        raise AssertionError(needle)

    def service(self, observed: OwnedSpawns) -> Any:
        adapter = production("adapters.clangd")

        def factory(program: Any, config: Any, _: Any) -> Any:
            instance = adapter.ClangdAdapter(program, config, observed.store)
            self.native_adapters.append(instance)
            return instance

        service = production("services.code_analysis").CodeAnalysisService(
            self.context,
            adapter_factory=factory,
        )
        self.addCleanup(service.shutdown)
        self.addCleanup(self.check_readers, service)
        return service

    def check_readers(self, service: Any) -> None:
        service.shutdown()
        threads = [t for adapter in self.native_adapters for t in adapter._threads]
        remaining = [t.name for t in threads if t.is_alive()]
        write_json(
            self.base / "reader-cleanup.json",
            {"threads": [t.name for t in threads], "alive": remaining},
        )
        self.assertEqual(remaining, [])

    def assert_ok(self, result: Any) -> None:
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["index_completeness"], "unverified")


class NativeManagedBinding(NativeCase):
    def test_all_five_queries_real_windows_clangd_and_msvc_size_t_control(self) -> None:
        with OwnedSpawns(self.base) as observed:
            service = self.service(observed)
            line, column = self.position("choose")
            results = {
                "status": service.status(),
                "definition": service.definition(str(self.main), line, column, timeout_seconds=30),
                "references": service.references(
                    str(self.main), line, column, include_declaration=True, timeout_seconds=30
                ),
                "hover": service.hover(str(self.main), line, column, timeout_seconds=30),
                "diagnostics": service.diagnostics(str(self.main), timeout_seconds=30),
            }
            write_json(self.base / "five-query-results.json", results)
            for operation, result in results.items():
                with self.subTest(operation=operation):
                    self.assert_ok(result)
                    if operation in ("definition", "references"):
                        self.assertTrue(result["data"]["locations"], result)
                    elif operation == "hover":
                        self.assertIn("choose", result["data"]["contents"]["value"])
                    elif operation == "diagnostics":
                        self.assertEqual(result["data"]["diagnostics"], [])
            # This MSVC target has valid size_t hover/diagnostics, but raw
            # definition is empty. This is an input control, not proof
            # that a declaration location comes from the mapped resource tree.
            line, column = self.position("size_t")
            builtin = service.definition(str(self.main), line, column, timeout_seconds=30)
            write_json(self.base / "builtin-definition.json", builtin)
            with self.subTest(operation="msvc-size-t-input-control"):
                self.assertEqual(builtin["status"], "not_found", builtin)
                self.assertEqual(builtin["data"]["locations"], [])
                hover = service.hover(str(self.main), line, column, timeout_seconds=30)
                self.assert_ok(hover)
                self.assertIn("size_t", hover["data"]["contents"]["value"])
                adapter = service._adapter
                params = {
                    "textDocument": {"uri": self.main.as_uri()},
                    "position": {"line": line - 1, "character": column - 1},
                }
                raw = adapter.request("textDocument/definition", params, time.monotonic() + 10)
                write_json(
                    self.base / "msvc-size-t-raw-control.json",
                    {
                        "raw": raw,
                        "hover": hover,
                        "target": "x86_64-pc-windows-msvc",
                        "source": self.main.read_text(encoding="utf-8"),
                        "vendor_declaration": (
                            self.fixture.root
                            / "analysis/clangd/lib/clang/23/include/__stddef_size_t.h"
                        ).read_text(encoding="utf-8"),
                        "stderr": adapter.stderr_tail,
                    },
                )
                self.assertEqual(raw, [])
            lsp = [a for a in observed.calls if "--version" not in a]
            with self.subTest(operation="resource-argv"):
                self.assertEqual(len(lsp), 1)
                self.assertIn(
                    "--resource-dir=" + str(self.fixture.root / "analysis/clangd/lib/clang/23"),
                    lsp[0],
                )
            service.shutdown()

    def test_built_arm_corpus_vendor_uint32_definition_uses_mapped_resources(self) -> None:
        originals = self.inputs["frozen_arm_inputs"]
        readme = next((Path(p) for p in originals if p.endswith("README.txt")), None)
        self.assertIsNotNone(readme, "UNMET: no Actions-built ARM corpus was published")
        source_root = readme.parent
        arm_root = self.project.root / "arm"
        copied = {}
        for source, expected in originals.items():
            path = Path(source)
            self.assertEqual(digest(path), expected)
            target = arm_root / path.relative_to(source_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            copied[target.relative_to(arm_root).as_posix()] = digest(target)
        # This case owns its copied corpus; preservation covers later writes only.
        self.project_before = tree_hashes(self.project.root)
        # Preserve the genuinely built corpus bytes. A separate analysis DB
        # relocates its project includes and selects the same ARM ABI using
        # clang's freestanding builtin headers instead of GCC's supplied ones.
        entries = []
        original_db = json.loads((arm_root / "build/compile_commands.json").read_bytes())
        for entry in original_db:
            # The immutable DB records its original build root; join through it.
            relative = Path(entry["file"]).relative_to(entry["directory"]).as_posix()
            self.assertIn(relative, copied)
            args = [
                "clang++" if relative.endswith(".cpp") else "clang",
                "--target=arm-none-eabi",
                "-mcpu=cortex-m4",
                "-mthumb",
                "-mfloat-abi=soft",
                "-ffreestanding",
                "-nostdlibinc",
                "-nostdinc++",
                "-DBYO_TARGET_ARM=1",
                "-I",
                str(arm_root / "include"),
                "-I",
                str(arm_root / "build/generated"),
                "-std=c++17" if relative.endswith(".cpp") else "-std=c11",
                "-c",
                str(arm_root / relative),
            ]
            entries.append(self.project.entry("arm/" + relative, arguments=args))
        self.project.write_database(entries)
        self.project_before = tree_hashes(self.project.root)
        main = arm_root / "src/main.c"
        with OwnedSpawns(self.base) as observed:
            service = self.service(observed)
            line, column = self.position("uint32_t", main)
            results = {
                "status": service.status(),
                "diagnostics": service.diagnostics(str(main), timeout_seconds=30),
                "definition": service.definition(str(main), line, column, timeout_seconds=30),
                "hover": service.hover(str(main), line, column, timeout_seconds=30),
            }
            write_json(
                self.base / "arm-builtin.json",
                {"copied": copied, "entries": entries, "results": results},
            )
            for operation, result in results.items():
                with self.subTest(operation=operation):
                    self.assert_ok(result)
                    self.assertEqual(result["data"]["executable_origin"], "managed_runtime")
                    self.assertEqual(
                        result["data"]["executable_sha256"],
                        digest(self.fixture.root / "analysis/clangd/bin/clangd.exe"),
                    )
                    self.assertEqual(
                        result["data"]["runtime_manifest_sha256"],
                        digest(self.fixture.manifest_path),
                    )
                    self.assertEqual(
                        result["data"]["analysis_mapping_sha256"], digest(self.fixture.mapping_path)
                    )
                    self.assertIn("23.1.0", result["data"]["version"])
            self.assertEqual(results["diagnostics"]["data"]["diagnostics"], [])
            include = self.fixture.root / "analysis/clangd/lib/clang/23/include"
            locations = results["definition"]["data"]["locations"]
            self.assertTrue(locations)
            for location in locations:
                target = Path(location["path"]).resolve()
                self.assertTrue(target.is_relative_to(include), location)
                self.assertEqual(target.name, "stdint.h")
                relative = target.relative_to(self.fixture.root).as_posix()
                approved = next(e for e in self.inputs["payload"] if e["runtime_path"] == relative)
                self.assertEqual(digest(target), approved["sha256"])
                self.assertIn("uint32_t", target.read_text(encoding="utf-8"))
            lsp = [a for a in observed.calls if "--version" not in a]
            self.assertEqual(len(lsp), 1)
            self.assertEqual(lsp[0][0], str(self.fixture.root / "analysis/clangd/bin/clangd.exe"))
            self.assertIn("--resource-dir=" + str(include.parent), lsp[0])
            self.assertEqual(os.environ["PATH"], str(self.base / "no-analyzers-on-path"))
            service.shutdown()

    def test_actual_managed_mcp_stdio_status_lazy_and_idle_session_eof_cleanup(self) -> None:
        for path, text in {
            "byo-analysis.json": '{"schema_version": 1}\n',
            ".clangd": "# protected user configuration\n",
            "user-notes.txt": "Protected user content\n",
            ".agent-workspace/boards/protected.json": '{"locked": true}\n',
        }.items():
            self.project.write_text(path, text)
        self.project_before = tree_hashes(self.project.root)
        with ManagedStdio(self.base / "stdio", self.context) as server:
            self.assertIn(
                "result",
                server.request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "managed-native-test", "version": "1"},
                    },
                ),
            )
            server.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            self.assert_safe_startup()
            # Exact bytes now form the strict baseline through all five calls
            # and EOF. No directory-wide permission-write exemption is used.
            self.project_before = tree_hashes(self.project.root)
            names = server.request("tools/list", {})["result"]["tools"]
            for name in (
                "get_code_analysis_status",
                "find_code_definition",
                "find_code_references",
                "get_code_hover",
                "get_code_diagnostics",
            ):
                self.assertIn(name, {t["name"] for t in names})
            status = server.tool("get_code_analysis_status", {})
            write_json(self.base / "mcp-status.json", status)
            self.assertEqual(status["data"]["session_state"], "not_started")
            self.assertEqual(list(server.marker_root.glob("*.json")), [])
            self.assert_ok(status)
            line, column = self.position("choose")
            result = server.tool(
                "find_code_definition",
                {
                    "file_path": str(self.main),
                    "line": line,
                    "column": column,
                    "timeout_seconds": 30,
                },
            )
            write_json(self.base / "mcp-definition.json", result)
            self.assert_ok(result)
            for name, arguments in (
                (
                    "find_code_references",
                    {
                        "file_path": str(self.main),
                        "line": line,
                        "column": column,
                        "include_declaration": True,
                        "timeout_seconds": 30,
                    },
                ),
                (
                    "get_code_hover",
                    {
                        "file_path": str(self.main),
                        "line": line,
                        "column": column,
                        "timeout_seconds": 30,
                    },
                ),
                ("get_code_diagnostics", {"file_path": str(self.main), "timeout_seconds": 30}),
            ):
                with self.subTest(operation=name):
                    response = server.tool(name, arguments)
                    write_json(self.base / (name + ".json"), response)
                    self.assert_ok(response)
                    self.assert_permission_bytes_unchanged()
            markers = [json.loads(p.read_bytes()) for p in server.marker_root.glob("*.json")]
            self.assertEqual(len(markers), 1, "One actual idle LSP child must be owned")
            for marker in markers:
                self.assertEqual(native_start_token(marker["pid"]), marker["start_token"])
            code, elapsed = server.eof()
            cleanup = [
                {"marker": m, "matches_after": native_start_token(m["pid"]) == m["start_token"]}
                for m in markers
            ]
            write_json(
                self.base / "mcp-eof-cleanup.json",
                {"exit": code, "elapsed": elapsed, "children": cleanup},
            )
            self.assertEqual(code, 0, server.stderr.decode("utf-8", "replace"))
            self.assertLess(elapsed, 10)
            self.assertTrue(all(c["matches_after"] is False for c in cleanup))
            self.assertEqual(list(server.marker_root.glob("*.json")), [])
            self.assert_permission_bytes_unchanged()

    def assert_safe_startup(self) -> None:
        prefix = ".agent-workspace/runtime/manual-permissions/"
        expected = {
            prefix + p
            for p in ("epoch.json", "downgrade.json", "mass-erase.json", ".protocol.lock")
        }
        after = tree_hashes(self.project.root)
        changes = {
            p
            for p in set(after) | set(self.project_before)
            if after.get(p) != self.project_before.get(p)
        }
        write_json(
            self.base / "startup-control.json",
            {"before": self.project_before, "after": after, "changes": sorted(changes)},
        )
        self.assertEqual(changes, expected)
        self.assertEqual({p for p in after if p.startswith(prefix)}, expected)
        root = self.project.root / prefix
        epoch = json.loads((root / "epoch.json").read_bytes())
        self.assertEqual(set(epoch), {"schema_version", "server_run_id", "created_at"})
        self.assertEqual(type(epoch["schema_version"]), int)
        self.assertEqual(epoch["schema_version"], 1)
        self.assertTrue(epoch["server_run_id"])
        self.assertIsInstance(epoch["created_at"], str)
        for action in ("downgrade", "mass-erase"):
            record = json.loads((root / (action + ".json")).read_bytes())
            self.assertEqual(
                record,
                {
                    "schema_version": 1,
                    "action": action,
                    "state": "locked",
                    "server_run_id": epoch["server_run_id"],
                    "created_at": record["created_at"],
                    "grant_id": None,
                    "board_id": None,
                    "policy_digest": None,
                    "binding_digest": None,
                    "claim_id": None,
                },
            )
            self.assertIsInstance(record["created_at"], str)
        self.assertEqual((root / ".protocol.lock").read_bytes(), b"\0")
        self.permission_bytes = {p: (self.project.root / p).read_bytes() for p in expected}
        write_json(
            self.base / "permission-startup-bytes.json",
            {p: b.hex() for p, b in self.permission_bytes.items()},
        )

    def assert_permission_bytes_unchanged(self) -> None:
        after = {p: (self.project.root / p).read_bytes() for p in self.permission_bytes}
        self.assertEqual(after, self.permission_bytes)


_BOOTSTRAP = """\
import json, sys, threading
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_bytes())
from pyocd_debug_mcp.code_analysis_runtime import AnalysisRuntimeContext, configure_analysis_runtime
configure_analysis_runtime(AnalysisRuntimeContext(Path(config["project"]), Path(config["runtime"]), managed=True))
import pyocd_debug_mcp.server as server
assert Path(server.__file__).resolve().is_relative_to(Path(config["source"]))
imports = {name: module.__file__ for name, module in sys.modules.items()
           if name.startswith("pyocd_debug_mcp") and getattr(module, "__file__", None)}
assert all(Path(path).resolve().is_relative_to(Path(config["source"])) for path in imports.values())
Path(config["imports"]).write_text(json.dumps(imports, indent=2), encoding="utf-8")
sys.argv = [sys.argv[0]]
try:
    server.main()
finally:
    imports = {name: module.__file__ for name, module in sys.modules.items()
               if name.startswith("pyocd_debug_mcp") and getattr(module, "__file__", None)}
    assert all(Path(path).resolve().is_relative_to(Path(config["source"])) for path in imports.values())
    Path(config["imports"]).write_text(json.dumps(imports, indent=2), encoding="utf-8")
    readers = [t.name for t in threading.enumerate() if t.name.startswith("clangd-")]
    Path(config["readers"]).write_text(json.dumps(readers), encoding="utf-8")
    assert not readers, readers
"""


class ManagedStdio:
    """Finite stdio transport using real server.main and pre-import context."""

    def __init__(self, root: Path, context: Any) -> None:
        self.root = root
        root.mkdir(parents=True)
        bootstrap = root / "bootstrap.py"
        bootstrap.write_text(_BOOTSTRAP, encoding="utf-8")
        config = root / "context.json"
        from tests.code_analysis_managed_support import ROOT

        write_json(
            config,
            {
                "project": str(context.project_root),
                "runtime": str(context.runtime_root),
                "source": str(ROOT / "src"),
                "imports": str(root / "imports.json"),
                "readers": str(root / "readers-after-eof.json"),
            },
        )
        self.marker_root = root / "runs/owned-processes"
        environment = os.environ.copy()
        environment.update(
            {
                "BYO_MCP_ARTIFACT_ROOT": str(context.project_root),
                "BYO_MCP_MONITOR_ROOT": str(root / "monitor"),
                "PYOCD_MCP_RUNS_ROOT": str(root / "runs"),
                "PYTHONUTF8": "1",
                "PYTHONPATH": os.pathsep.join(str(p) for p in (ROOT / "src", ROOT / "tests", ROOT)),
            }
        )
        self.process = subprocess.Popen(
            [sys.executable, str(bootstrap), str(config)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(context.runtime_root),
            env=environment,
        )
        self.creation = native_start_token(self.process.pid)
        assert self.creation is not None
        self.stderr = bytearray()
        self.messages: queue.Queue[Any] = queue.Queue()
        self.next_id = 0
        self.stdout = []

        def read_stdout():
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.stdout.append(line.decode("utf-8", "replace"))
                try:
                    self.messages.put(json.loads(line))
                except ValueError as exc:
                    self.messages.put(exc)
            self.messages.put(EOFError("MCP stdio EOF"))

        def read_stderr():
            pipe = self.process.stderr
            assert pipe is not None
            for chunk in iter(lambda: pipe.read(4096), b""):
                self.stderr.extend(chunk)

        self.readers = [
            threading.Thread(target=read_stdout, daemon=True),
            threading.Thread(target=read_stderr, daemon=True),
        ]
        for thread in self.readers:
            thread.start()

    def __enter__(self):
        return self

    def send(self, message: Any):
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def request(self, method: str, params: Any) -> Any:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            if isinstance(message, BaseException):
                raise AssertionError(str(message) + self.stderr.decode("utf-8", "replace"))
            if message.get("id") == self.next_id:
                return message
        raise AssertionError("MCP response deadline")

    def tool(self, name: str, arguments: Any) -> Any:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        return json.loads(result["result"]["content"][0]["text"])

    def eof(self) -> tuple[int, float]:
        started = time.monotonic()
        assert self.process.stdin is not None
        if not self.process.stdin.closed:
            self.process.stdin.close()
        code = self.process.wait(timeout=10)
        for thread in self.readers:
            thread.join(2)
            assert not thread.is_alive(), "MCP pipe reader still waiting after EOF"
        return code, time.monotonic() - started

    def __exit__(self, *exc: Any) -> None:
        try:
            code, elapsed = self.eof()
        except subprocess.TimeoutExpired:
            # Only this exact owned server and children; preserve before cleanup.
            write_json(
                self.root / "eof-timeout-markers.json",
                [json.loads(p.read_bytes()) for p in self.marker_root.glob("*.json")],
            )
            if native_start_token(self.process.pid) == self.creation:
                subprocess.run(
                    [
                        str(
                            Path(os.environ.get("SystemRoot", "C:/Windows"))
                            / "System32/taskkill.exe"
                        ),
                        "/PID",
                        str(self.process.pid),
                        "/T",
                        "/F",
                    ],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            raise
        finally:
            (self.root / "stderr.txt").write_bytes(self.stderr)
            write_json(self.root / "stdout.json", self.stdout)
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
        matches = native_start_token(self.process.pid) == self.creation
        write_json(
            self.root / "server-cleanup.json",
            {
                "pid": self.process.pid,
                "native_creation": self.creation,
                "exit": code,
                "identity_matches_after": matches,
                "elapsed": elapsed,
                "remaining_markers": [str(p) for p in self.marker_root.glob("*.json")],
            },
        )
        assert matches is False
        assert not list(self.marker_root.glob("*.json"))


if __name__ == "__main__":
    unittest.main()
