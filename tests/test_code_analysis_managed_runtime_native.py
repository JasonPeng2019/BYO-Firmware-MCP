"""Windows staged development-input checks; never installed release proof.

Native input records are explicitly supplied by the harness, SHA-pinned and
copied into lane-local runtime/project roots. No PATH discovery or downloads.
Managed service/MCP calls retain the production resolver. The separately named
payload control runs real clangd --check to establish usable staged inputs; its
success cannot make a managed service test pass.
"""

from __future__ import annotations

import contextlib
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
from types import SimpleNamespace
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


class NativeStatusReceiptBoundary(NativeCase):
    """R1: a late integrity change must survive real session cleanup boundaries."""

    def check_late_status_change(self, boundary: str) -> None:
        module = production("services.code_analysis")
        target = self.fixture.root / "analysis/clangd/lib/clang/23/include/stddef.h"
        original = target.read_bytes()
        held, release = threading.Event(), threading.Event()
        holder = None
        with OwnedSpawns(self.base) as observed:
            service = self.service(observed)
            try:
                lazy = service.status()
                self.assert_ok(lazy)
                self.assertEqual(lazy["data"]["session_state"], "not_started")
                self.assertTrue(all(a[1:] == ["--version"] for a in observed.calls))
                line, column = self.position("choose")
                control = service.hover(str(self.main), line, column, timeout_seconds=30)
                self.assert_ok(control)
                self.assertIn("choose", str(control["data"]))
                self.assert_ok(service.diagnostics(str(self.main), timeout_seconds=30))
                adapter = self.native_adapters[-1]
                process, marker = adapter.process, adapter.marker
                self.assertIsNotNone(process)
                self.assertIsNotNone(marker)
                assert process is not None and marker is not None
                creation = native_start_token(process.pid)
                self.assertIsNotNone(creation)
                self.assertTrue(marker.is_file())
                marker_bytes = marker.read_bytes()
                marker_record = json.loads(marker_bytes)
                self.assertEqual(
                    (marker_record["pid"], marker_record["start_token"]), (process.pid, creation)
                )
                before = len(observed.calls)
                expected_stat = target.stat()
                observe = service._observe_runtime
                changes = []

                def late_change(*args: Any) -> None:
                    observe(*args)
                    target.write_bytes(original + b"\n/* independent late receipt change */\n")
                    changes.append(str(target))

                with (
                    mock.patch.object(service, "_observe_runtime", side_effect=late_change),
                    contextlib.ExitStack() as stack,
                ):
                    if boundary == "failed-drain":
                        stack.enter_context(
                            mock.patch.object(
                                production("adapters.clangd"),
                                "terminate_process_group",
                                return_value=False,
                            )
                        )
                    elif boundary == "held-lock":

                        def hold() -> None:
                            with service._session_guard:
                                held.set()
                                release.wait()

                        holder = threading.Thread(target=hold)
                        holder.start()
                        self.assertTrue(held.wait(5), "Owned holder did not acquire session guard")
                        stack.enter_context(
                            mock.patch.object(
                                module,
                                "current_operation",
                                return_value=SimpleNamespace(
                                    started_at=time.monotonic(),
                                    timeout_seconds=0.5,
                                    cancellation_requested=threading.Event(),
                                ),
                            )
                        )
                    result = service.status()
                    observed_stat = target.stat()
                    # The leader's own exit state and the currently observed
                    # native identity are recorded separately from ownership.
                    leader_poll = process.poll()
                    try:
                        observed_token = native_start_token(process.pid)
                    except OSError as exc:
                        observed_token = f"unknown: {exc}"
                    retained = {
                        "same_adapter": service._adapter is adapter,
                        "same_process_object": adapter.process is process,
                        "same_marker_path": adapter.marker == marker,
                        "marker_exists": marker.is_file(),
                        "marker_bytes_unchanged": marker.is_file()
                        and marker.read_bytes() == marker_bytes,
                        "leader_poll": leader_poll,
                        "observed_native_token": observed_token,
                        "same_native_child": observed_token == creation,
                        "generation_closed": service._generation.is_set(),
                    }
                    write_json(
                        self.base / "r1-boundary.json",
                        {
                            "boundary": boundary,
                            "lazy_control": lazy,
                            "real_hover_control": control,
                            "result": result,
                            "mutations": changes,
                            "ownership": retained,
                            "native_creation": creation,
                            "pid": process.pid,
                            "marker": str(marker),
                            "spawn_count_before": before,
                            "spawn_count_after": len(observed.calls),
                        },
                    )
                    expected = {
                        "failed-drain": ("error", "analysis/cleanup-failed", "failed"),
                        "held-lock": ("incomplete", "analysis/timeout", "restarting"),
                        "successful-drain": (
                            "unavailable",
                            "analysis/executable-unavailable",
                            "restarting",
                        ),
                    }[boundary]
                    self.assertEqual(
                        (result["status"], result["code"], result["data"]["session_state"]),
                        expected,
                        result,
                    )
                    self.assertEqual(changes, [str(target)])
                    self.assertEqual(len(observed.calls), before, "Status restarted an analyzer")
                    self.assertTrue(retained["generation_closed"])
                    problems = result["data"]["configuration_problems"]
                    integrity = [p for p in problems if str(target) in p["message"]]
                    self.assertTrue(integrity, result)
                    self.assertEqual(integrity[0]["code"], "analysis/executable-unavailable")
                    self.assertTrue(integrity[0]["remedy"])
                    self.assertIn("receipt invalidated", integrity[0]["message"])
                    self.assertTrue(result["remedy"])
                    identities = result["data"]
                    expected_identity = identities["expected_identity"][str(target)]
                    observed_identity = identities["observed_identity"][str(target)]["identity"]
                    self.assertEqual(expected_identity[1], expected_stat.st_ino)
                    self.assertEqual(expected_identity[3], len(original))
                    self.assertEqual(observed_identity[1], observed_stat.st_ino)
                    self.assertEqual(observed_identity[3], observed_stat.st_size)
                    self.assertNotEqual(expected_identity, observed_identity)
                    if boundary == "successful-drain":
                        self.assertFalse(retained["same_adapter"])
                        self.assertFalse(retained["same_native_child"])
                        self.assertFalse(retained["marker_exists"])
                        observed.check_cleanup()
                    else:
                        self.assertTrue(retained["same_adapter"], retained)
                        self.assertTrue(retained["same_process_object"], retained)
                        self.assertTrue(retained["same_marker_path"], retained)
                        self.assertTrue(retained["marker_exists"], retained)
                        self.assertTrue(retained["marker_bytes_unchanged"], retained)
                        if boundary == "held-lock" or retained["leader_poll"] is None:
                            # Shutdown has not run (held lock), or the leader is
                            # still live: it must be the identical native child.
                            self.assertIsNone(retained["leader_poll"], retained)
                            self.assertTrue(retained["same_native_child"], retained)
                        else:
                            # The graceful drain lets a responsive leader exit
                            # normally. That does not prove group cleanup, so the
                            # unconfirmed failure and its ownership stay visible.
                            # An exited leader has no live identity: an unknown or
                            # different live observation fails at this boundary.
                            self.assertEqual(retained["leader_poll"], 0, retained)
                            self.assertIsNone(retained["observed_native_token"], retained)
                if boundary == "failed-drain":
                    # Explicit later recovery with real termination releases
                    # exactly the retained process and marker.
                    service.shutdown()
                    recovery = {
                        "adapter_released": service._adapter is None,
                        "process_released": adapter.process is None,
                        "marker_released": adapter.marker is None,
                        "marker_exists": marker.exists(),
                        "leader_exited": process.poll() is not None,
                        "spawn_count": len(observed.calls),
                    }
                    write_json(
                        self.base / "r1-recovery.json",
                        {**recovery, "leader_exit": process.poll()},
                    )
                    self.assertEqual(
                        recovery,
                        {
                            "adapter_released": True,
                            "process_released": True,
                            "marker_released": True,
                            "marker_exists": False,
                            "leader_exited": True,
                            "spawn_count": before,
                        },
                    )
                    if retained["leader_poll"] is not None:
                        self.assertEqual(process.poll(), retained["leader_poll"])
                    observed.check_cleanup()
            finally:
                release.set()
                if holder is not None:
                    holder.join(5)
                    self.assertFalse(holder.is_alive(), "Owned lock holder remained")
                service.shutdown()
                target.write_bytes(original)
            observed.check_cleanup()
            self.check_readers(service)

    def test_late_receipt_failed_drain_returns_typed_error_and_retains_ownership(self) -> None:
        self.check_late_status_change("failed-drain")

    def test_late_receipt_held_guard_returns_typed_timeout_and_retains_ownership(self) -> None:
        self.check_late_status_change("held-lock")

    def test_late_receipt_successful_drain_returns_unavailable_without_restart(self) -> None:
        self.check_late_status_change("successful-drain")


class NativeManagedBinding(NativeCase):
    def test_real_staged_cppcheck_and_clangd_resolve_versions_and_owned_data(self) -> None:
        runtime = production("code_analysis_runtime")
        with OwnedSpawns(self.base) as observed:
            for name in ("clangd", "cppcheck"):
                with self.subTest(program=name):
                    result = runtime.resolve_analysis_program(
                        self.context, name, time.monotonic() + 30
                    )
                    write_json(self.base / (name + "-resolution.json"), {"result": repr(result)})
                    self.assertIsNotNone(result.executable, result)
                    self.assertFalse(result.problems, result)
                    self.assertEqual(result.origin, "managed_runtime")
                    self.assertIn(self.fixture.mapping["programs"][name]["version"], result.version)
            self.assertEqual(len(observed.calls), 2)

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
        source_root = next(Path(p).parent for p in originals if p.endswith("README.txt"))
        arm_root = self.project.root / "arm"
        copied = {}
        for source, expected in originals.items():
            path = Path(source)
            self.assertEqual(digest(path), expected)
            target = arm_root / path.relative_to(source_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            copied[target.relative_to(arm_root).as_posix()] = digest(target)
        # Preserve the genuinely built corpus bytes. A separate analysis DB
        # relocates its project includes and selects the same ARM ABI using
        # clang's freestanding builtin headers instead of GCC's supplied ones.
        entries = []
        original_db = json.loads((arm_root / "build/compile_commands.json").read_bytes())
        for entry in original_db:
            relative = Path(entry["file"]).relative_to(source_root).as_posix()
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

    def test_saved_file_header_template_and_nonbmp_crlf_positions(self) -> None:
        with OwnedSpawns(self.base) as observed:
            service = self.service(observed)
            line, column = self.position("twice(3)")
            template = service.definition(str(self.main), line, column, timeout_seconds=30)
            write_json(self.base / "template.json", template)
            self.assert_ok(template)
            self.assertTrue(
                any(
                    Path(p["path"]).resolve() == self.header.resolve()
                    for p in template["data"]["locations"]
                ),
                template,
            )
            original = self.main.read_bytes()
            self.main.write_bytes(original + b"int broken = missing_symbol;\r\n")
            changed = service.diagnostics(str(self.main), timeout_seconds=30)
            write_json(self.base / "saved-change.json", changed)
            try:
                self.assert_ok(changed)
                self.assertEqual(changed["source"]["sha256"], digest(self.main))
                self.assertTrue(
                    any("missing_symbol" in d["message"] for d in changed["data"]["diagnostics"])
                )
            finally:
                self.main.write_bytes(original)
            restored = service.diagnostics(str(self.main), timeout_seconds=30)
            self.assert_ok(restored)
            self.assertEqual(restored["data"]["diagnostics"], [])
            self.assertGreater(
                restored["source"]["document_version"], changed["source"]["document_version"]
            )
            hline, hcolumn = self.position("twice", self.header)
            header = service.hover(str(self.header), hline, hcolumn, timeout_seconds=30)
            write_json(self.base / "header-hover.json", header)
            self.assert_ok(header)
            self.assertTrue(
                any("infer" in w.lower() or "header" in w.lower() for w in header["warnings"]),
                header,
            )
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


class NativePayloadPreparationControl(NativeCase):
    def test_real_staged_program_version_payload_controls_with_owned_cleanup(self) -> None:
        with OwnedSpawns(self.base) as observed:
            for name in ("clangd", "cppcheck"):
                with self.subTest(program=name):
                    executable = (
                        self.fixture.root / self.fixture.mapping["programs"][name]["executable"]
                    )
                    argv = [str(executable), "--version"]
                    process, marker = observed.runtime.spawn_before_deadline(
                        argv,
                        time.monotonic() + 5,
                        None,
                        observed.store,
                        cwd=str(self.project.root),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    try:
                        stdout, stderr = process.communicate(timeout=5)
                        write_json(
                            self.base / (name + "-version-control.json"),
                            {
                                "scope": "PREPARATION ONLY; direct payload version, not managed binding",
                                "argv": argv,
                                "exit": process.returncode,
                                "stdout": stdout.decode("utf-8", "replace"),
                                "stderr": stderr.decode("utf-8", "replace"),
                                "sha256": digest(executable),
                            },
                        )
                        self.assertEqual(process.returncode, 0, stderr)
                        self.assertIn(
                            self.fixture.mapping["programs"][name]["version"],
                            stdout.decode("utf-8", "replace"),
                        )
                    finally:
                        self.assertTrue(observed.processes.terminate_process_group(process))
                        observed.store.remove(marker)
                        for pipe in (process.stdout, process.stderr):
                            if pipe:
                                pipe.close()

    def test_staged_native_payload_check_uses_builtin_resources_without_analyzer_path(self) -> None:
        # This is a direct native input control, not managed resolver proof.
        executable = self.fixture.root / "analysis/clangd/bin/clangd.exe"
        argv = [
            str(executable),
            "--check=" + str(self.main),
            "--compile-commands-dir=" + str(self.project.root),
            "--resource-dir=" + str(self.fixture.root / "analysis/clangd/lib/clang/23"),
            "--log=verbose",
        ]
        with OwnedSpawns(self.base) as observed:
            process, marker = observed.runtime.spawn_before_deadline(
                argv,
                time.monotonic() + 30,
                None,
                observed.store,
                cwd=str(self.project.root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                stdout, stderr = process.communicate(timeout=30)
                (self.base / "clangd-check.stdout").write_bytes(stdout)
                (self.base / "clangd-check.stderr").write_bytes(stderr)
                write_json(
                    self.base / "native-input-control.json",
                    {
                        "scope": "PREPARATION ONLY; direct native payload, no managed binding claim",
                        "argv": argv,
                        "exit": process.returncode,
                        "executable_sha256": digest(executable),
                    },
                )
                self.assertEqual(process.returncode, 0, stderr.decode("utf-8", "replace")[-5000:])
                log = stderr.decode("utf-8", "replace")
                self.assertIn("All checks completed, 0 errors", log)
                self.assertIn("-resource-dir", log)
                self.assertIn("23", log)
            finally:
                self.assertTrue(observed.processes.terminate_process_group(process))
                observed.store.remove(marker)
                for pipe in (process.stdout, process.stderr):
                    if pipe:
                        pipe.close()


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
