"""Implementer checks for service boundaries, MCP JSON and real clangd queries."""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
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
from pyocd_debug_mcp import code_analysis_runtime as runtime
from pyocd_debug_mcp.code_analysis_runtime import (
    AnalysisRuntimeContext,
    ResolvedAnalysisProgram,
    resolve_analysis_program,
)
from pyocd_debug_mcp.kernel.operations import (
    ARGUMENT_TIMEOUT_GRACE_SECONDS,
    operation_timeout_seconds,
)
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP
from pyocd_debug_mcp.services.code_analysis import CodeAnalysisService
from pyocd_debug_mcp.tools.code_analysis import (
    CodeAnalysisToolServices,
    build_code_analysis_handlers,
)
from pyocd_debug_mcp.tools.plans import forbid_unknown_tool_arguments

from test_clangd_adapter_unit import PEER, fake_factory, make_project
from test_code_analysis_managed_runtime_unit import staged_runtime


def record_regression(name, value):
    destination = os.environ.get("BYO_TEST_ANALYSIS_EVIDENCE")
    if destination:
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (name + ".json")).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class ServiceTests(unittest.TestCase):
    def test_resolved_runtime_status_and_query_report_identity_without_fallback_warning(self):
        from dataclasses import replace

        self.service.context = AnalysisRuntimeContext(self.root, self.root)
        self.program = replace(
            self.program,
            origin="managed_runtime",
            runtime_root=self.root,
            runtime_manifest_sha256="a" * 64,
            analysis_mapping_sha256="b" * 64,
            executable_sha256="c" * 64,
            data_paths=(self.root / "resource",),
        )
        for result in (self.service.status(), self.service.hover("main.cpp", 1, 5)):
            with self.subTest(operation=result["operation"]):
                self.assertEqual(result["status"], "ok", result)
                self.assertEqual(result["data"]["executable_origin"], "managed_runtime")
                self.assertEqual(result["data"]["runtime_manifest_sha256"], "a" * 64)
                self.assertEqual(result["data"]["analysis_mapping_sha256"], "b" * 64)
                self.assertEqual(result["data"]["executable_sha256"], "c" * 64)
                self.assertEqual(result["data"]["data_paths"], [str(self.root / "resource")])
                self.assertNotIn("mapping is unavailable", " ".join(result["warnings"]))

    def _managed_service(self, mode="normal"):
        self.service.shutdown()
        runtime_root = self.root / "runtime"
        staged_runtime(runtime_root)
        from pyocd_debug_mcp import code_analysis_manifest as manifest

        self.enterContext(patch.object(manifest, "_metadata_cache", {}))
        self.enterContext(patch.object(manifest, "_receipt_cache", {}))
        self.enterContext(patch.object(runtime, "_probe_cache", {}))
        # Version bytes are a deterministic unit seam. LSP uses the real owned
        # framed peer, pipes, reader/writer threads and cleanup markers.
        self.enterContext(patch.object(runtime, "_probe_program", side_effect=lambda p, *args: p))
        self.service = CodeAnalysisService(
            AnalysisRuntimeContext(self.root, runtime_root, managed=True),
            fake_factory(self.root, mode),
        )
        return runtime_root / "analysis/clangd/lib/clang/23/include/stddef.h"

    def test_resource_mutation_during_query_invalidates_result_and_drains_session(self):
        header = self._managed_service()
        initial = self.service.hover("main.cpp", 1, 5)
        self.assertEqual(initial["status"], "ok", initial)
        adapter = self.service._adapter
        process = adapter.process
        request = adapter.request

        def mutate(method, *args, **kwargs):
            response = request(method, *args, **kwargs)
            if method == "textDocument/hover":
                header.write_bytes(b"changed during query")
            return response

        with patch.object(adapter, "request", side_effect=mutate):
            result = self.service.hover("main.cpp", 1, 5)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertEqual(result["code"], "analysis/configuration-changed", result)
        self.assertIsNone(self.service._adapter)
        self.assertTrue(self.service._generation.is_set())
        self.assertEqual(self.service._documents, {})
        self.assertIsNone(
            adapter.diagnostics_for(self.source.as_uri(), initial["source"]["document_version"])
        )
        self.assertIsNotNone(process.poll())
        self.assertFalse(list((self.root / "markers").glob("*.json")))
        self.assertTrue(all(not t.is_alive() for t in adapter._threads))

    def test_status_detects_runtime_change_cancels_pending_query_and_closes_admission(self):
        header = self._managed_service("never-definition")
        self.assertEqual(self.service.hover("main.cpp", 1, 5)["status"], "ok")
        adapter = self.service._adapter
        process = adapter.process
        admitted = threading.Event()
        request = adapter.request

        def observe(method, *args, **kwargs):
            if method == "textDocument/definition":
                admitted.set()
            return request(method, *args, **kwargs)

        with patch.object(adapter, "request", side_effect=observe):
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                pending = pool.submit(self.service.definition, "main.cpp", 1, 5, 3)
                self.assertTrue(admitted.wait(1), "query was not admitted")
                header.write_bytes(b"changed while pending")
                status = self.service.status()
                result = pending.result(2)
        self.assertEqual(status["status"], "unavailable", status)
        self.assertEqual(result["code"], "analysis/configuration-changed", result)
        self.assertIsNone(self.service._adapter)
        self.assertIsNotNone(process.poll())
        self.assertFalse(self.service._documents)
        self.assertFalse(list((self.root / "markers").glob("*.json")))
        self.assertTrue(all(not t.is_alive() for t in adapter._threads))
        self.assertEqual(self.service.hover("main.cpp", 1, 5)["status"], "unavailable")

    def test_receipt_is_checked_again_at_session_admission(self):
        header = self._managed_service()
        resolve = self.service.program_resolver

        def mutate_after_resolution(*args):
            program = resolve(*args)
            header.write_bytes(b"changed before session admission")
            return program

        with patch.object(self.service, "program_resolver", side_effect=mutate_after_resolution):
            result = self.service.hover("main.cpp", 1, 5)
        self.assertEqual(result["code"], "analysis/configuration-changed", result)
        self.assertIsNone(self.service._adapter)
        self.assertFalse((self.root / "received.jsonl").exists())

    def test_status_checks_receipt_after_resolution_without_starting_lsp(self):
        header = self._managed_service()
        resolve = self.service.program_resolver

        def mutate_after_resolution(*args):
            program = resolve(*args)
            header.write_bytes(b"changed before status return")
            return program

        with patch.object(self.service, "program_resolver", side_effect=mutate_after_resolution):
            result = self.service.status()
        self.assertEqual(result["status"], "unavailable", result)
        self.assertTrue(result["data"]["expected_identity"])
        self.assertTrue(result["data"]["observed_identity"])
        self.assertIsNone(self.service._adapter)

    def _late_status_mutation(self):
        header = self._managed_service()
        warm = self.service.hover("main.cpp", 1, 5)
        self.assertEqual(warm["status"], "ok", warm)
        self.assertEqual(self.service.status()["status"], "ok")
        adapter = self.service._adapter
        resolve = self.service.program_resolver

        def mutate_after_resolution(*args):
            program = resolve(*args)
            self.assertFalse(program.problems, program)
            header.write_bytes(b"changed after valid status resolution")
            return program

        return header, adapter, mutate_after_resolution

    def _assert_status_integrity_cause(self, result, header):
        self.assertTrue(result["data"]["expected_identity"], result)
        self.assertTrue(result["data"]["observed_identity"], result)
        self.assertIn(str(header), result["data"]["expected_identity"])
        self.assertIn(str(header), result["data"]["observed_identity"])
        problem = result["data"]["configuration_problems"][-1]
        self.assertEqual(problem["code"], "analysis/executable-unavailable", result)
        self.assertIn("receipt invalidated", problem["message"])
        self.assertIn(str(header), problem["message"])
        self.assertTrue(problem["remedy"])
        self.assertTrue(result["remedy"])

    def test_status_late_integrity_change_successfully_drains_ready_session(self):
        header, adapter, mutate = self._late_status_mutation()
        process = adapter.process
        with (
            patch.object(self.service, "program_resolver", side_effect=mutate) as resolver,
            patch.object(adapter, "shutdown", wraps=adapter.shutdown) as shutdown,
        ):
            result = self.service.status()
        self.assertEqual(result["status"], "unavailable", result)
        self.assertEqual(result["code"], "analysis/executable-unavailable", result)
        self.assertEqual(result["data"]["session_state"], "restarting")
        self._assert_status_integrity_cause(result, header)
        resolver.assert_called_once()
        shutdown.assert_called_once()
        self.assertTrue(self.service._generation.is_set())
        self.assertIsNone(self.service._adapter)
        self.assertEqual(self.service._documents, {})
        self.assertIsNotNone(process.poll())
        self.assertFalse(list((self.root / "markers").glob("*.json")))
        self.assertTrue(all(not thread.is_alive() for thread in adapter._threads))
        record_regression("status-late-integrity-drained", result)

    def test_status_late_integrity_change_retains_unconfirmed_drain_ownership(self):
        header, adapter, mutate = self._late_status_mutation()
        process, marker = adapter.process, adapter.marker
        marker_before = marker.read_bytes()
        stderr_tail = "clangd: failed-drain diagnostic\n"
        with adapter._guard:
            adapter._stderr.extend(stderr_tail.encode("utf-8"))
        self.assertEqual(adapter.stderr_tail, stderr_tail)
        with (
            patch.object(self.service, "program_resolver", side_effect=mutate) as resolver,
            patch.object(adapter, "shutdown", wraps=adapter.shutdown) as shutdown,
            patch("pyocd_debug_mcp.adapters.clangd.terminate_process_group", return_value=False),
        ):
            result = self.service.status()
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["code"], "analysis/cleanup-failed", result)
        self.assertEqual(result["data"]["session_state"], "failed")
        self.assertEqual(result.get("stderr_tail"), stderr_tail)
        self._assert_status_integrity_cause(result, header)
        resolver.assert_called_once()
        shutdown.assert_called_once()
        self.assertTrue(self.service._generation.is_set())
        self.assertIs(self.service._adapter, adapter)
        self.assertIs(adapter.process, process)
        # Graceful drain delivers shutdown/exit and writer-owned stdin EOF, so a
        # responsive leader may legitimately exit 0 by itself. Its liveness is
        # not the contract: unconfirmed native cleanup (of the leader's group)
        # must stay visible and retain the exact process object and marker for
        # recovery whether or not the leader already exited.
        received = [
            json.loads(line).get("method")
            for line in (self.root / "received.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn("shutdown", received)
        self.assertIn(process.poll(), (None, 0))
        self.assertEqual(marker.read_bytes(), marker_before)
        self.assertIsNone(adapter.diagnostics_for(self.source.as_uri(), 1))
        record_regression(
            "status-late-integrity-drain-unconfirmed",
            {"result": result, "leader_poll": process.poll(), "received": received},
        )
        # Explicit teardown after restoring the terminator, not a status retry.
        self.service.shutdown()
        self.assertIsNotNone(process.poll())
        self.assertFalse(marker.exists())

    def test_status_late_integrity_change_lock_expiry_retains_closed_generation(self):
        from pyocd_debug_mcp.services import code_analysis as service_module

        header, adapter, mutate = self._late_status_mutation()
        process, marker = adapter.process, adapter.marker
        marker_before = marker.read_bytes()
        held, release, waiting = threading.Event(), threading.Event(), threading.Event()

        def hold_guard():
            with self.service._session_guard:
                held.set()
                release.wait()

        holder = threading.Thread(target=hold_guard)
        holder.start()
        self.assertTrue(held.wait(5))
        clock = [time.monotonic()]
        operation = SimpleNamespace(
            started_at=clock[0], timeout_seconds=30, cancellation_requested=threading.Event()
        )
        acquire = service_module._acquire

        def observe_wait(lock, deadline, cancellation):
            if lock is self.service._session_guard:
                waiting.set()
            return acquire(lock, deadline, cancellation)

        try:
            with (
                patch.object(self.service, "program_resolver", side_effect=mutate) as resolver,
                patch.object(adapter, "shutdown", wraps=adapter.shutdown) as shutdown,
                patch.object(service_module, "current_operation", return_value=operation),
                patch.object(service_module, "_acquire", side_effect=observe_wait),
                patch.object(runtime.time, "monotonic", side_effect=lambda: clock[0]),
                concurrent.futures.ThreadPoolExecutor(1) as pool,
            ):
                pending = pool.submit(self.service.status)
                self.assertTrue(waiting.wait(5), "status never attempted the held session guard")
                clock[0] = operation.started_at + operation.timeout_seconds + 1
                result = pending.result(5)
                resolver.assert_called_once()
                shutdown.assert_not_called()
            self.assertEqual(result["status"], "incomplete", result)
            self.assertEqual(result["code"], "analysis/timeout", result)
            self.assertEqual(result["data"]["session_state"], "restarting")
            self._assert_status_integrity_cause(result, header)
            self.assertTrue(self.service._generation.is_set())
            self.assertIs(self.service._adapter, adapter)
            self.assertIs(adapter.process, process)
            self.assertIsNone(process.poll())
            self.assertEqual(marker.read_bytes(), marker_before)
            record_regression("status-late-integrity-lock-expired", result)
        finally:
            release.set()
            holder.join(5)
            self.assertFalse(holder.is_alive())
        self.service.shutdown()
        self.assertIsNotNone(process.poll())
        self.assertFalse(marker.exists())

    def test_runtime_change_cancels_initialization_before_old_session_admission(self):
        header = self._managed_service()
        factory = self.service.adapter_factory
        initializing = threading.Event()

        def waiting_factory(*args):
            adapter = factory(*args)

            def wait_start(deadline, cancellation):
                initializing.set()
                self.assertTrue(self.service._generation.wait(2))
                runtime.remaining(deadline, cancellation)

            adapter.start = wait_start
            return adapter

        self.service.adapter_factory = waiting_factory
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.service.hover, "main.cpp", 1, 5, 3)
            self.assertTrue(initializing.wait(1))
            header.write_bytes(b"changed during initialization")
            status = self.service.status()
            query = pending.result(2)
        self.assertEqual(status["status"], "unavailable", status)
        self.assertEqual(query["code"], "analysis/configuration-changed", query)
        self.assertIsNone(self.service._adapter)
        self.assertFalse((self.root / "received.jsonl").exists())

    def test_explicit_development_fallback_keeps_runtime_problem_and_warning(self):
        header = self._managed_service()
        header.unlink()
        self.service.context = AnalysisRuntimeContext(self.root, self.service.context.runtime_root)
        with patch.object(runtime, "_which_path_only", return_value=sys.executable):
            status = self.service.status()
            query = self.service.hover("main.cpp", 1, 5)
        for result in (status, query):
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["data"]["executable_origin"], "path")
            self.assertIn("PATH fallback", " ".join(result["warnings"]))
            self.assertTrue(result["data"]["configuration_problems"])

    def test_runtime_change_retains_unconfirmed_cleanup_error(self):
        from pyocd_debug_mcp.adapters.clangd import ClangdFailure

        header = self._managed_service()
        self.assertEqual(self.service.hover("main.cpp", 1, 5)["status"], "ok")
        adapter = self.service._adapter
        process = adapter.process
        request = adapter.request

        def mutate(method, *args, **kwargs):
            result = request(method, *args, **kwargs)
            if method == "textDocument/hover":
                header.write_bytes(b"changed before unconfirmed cleanup")
            return result

        with (
            patch.object(adapter, "request", side_effect=mutate),
            patch.object(
                adapter,
                "shutdown",
                side_effect=ClangdFailure("cleanup-failed", "unit cleanup unconfirmed"),
            ),
        ):
            result = self.service.hover("main.cpp", 1, 5)
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["code"], "analysis/cleanup-failed", result)
        self.assertIs(self.service._adapter, adapter)
        self.service.shutdown()
        self.assertIsNotNone(process.poll())
        self.assertFalse(list((self.root / "markers").glob("*.json")))

    def test_known_generated_header_invalidates_without_duplicate_walked_headers(self):
        generated = self.root / ".firm/build/generated/selected.h"
        generated.parent.mkdir(parents=True)
        generated.write_text("#define SELECTED 7\n", encoding="utf-8")
        ordinary = self.root / "selected.h"
        ordinary.write_text("#define ORDINARY 1\n", encoding="utf-8")
        before = self.service._dependency_identity(time.monotonic() + 1, None)
        self.assertNotIn(str(generated), dict(before))
        ordinary_hover = self.service.hover(str(ordinary), 1, 9)
        self.assertEqual(ordinary_hover["status"], "ok", ordinary_hover)
        self.assertEqual(self.service._dependency_identity(time.monotonic() + 1, None), before)
        opened = self.service.hover(str(generated), 1, 9)
        self.assertEqual(opened["status"], "ok", opened)
        clean = self.service.diagnostics("main.cpp")
        generated.write_text("#define SELECTED 9\n", encoding="utf-8")
        changed = self.service.diagnostics("main.cpp")
        records = self.service._dependency_identity(time.monotonic() + 1, None)
        record_regression(
            "known-generated-header-framed",
            {"opened": opened, "clean": clean, "changed": changed, "dependencies": records},
        )
        self.assertEqual(changed["status"], "ok", changed)
        self.assertGreater(
            changed["source"]["document_version"], clean["source"]["document_version"]
        )
        self.assertEqual(changed["source"]["sha256"], clean["source"]["sha256"])
        self.assertEqual(dict(records)[str(generated)], sha256_bytes(generated.read_bytes()))
        self.assertEqual(len(records), len(dict(records)))

    def test_missing_current_diagnostics_forces_unchanged_saved_revision(self):
        self.service.shutdown()
        factory = fake_factory(self.root, "silent")
        peer = self.root / "peer.py"
        peer.write_text(
            PEER.replace(
                "if mode == 'silent': return",
                "if mode == 'silent' and not __import__('os').path.exists(sys.argv[2]+'.enable'): return",
            ),
            encoding="utf-8",
        )
        self.service = CodeAnalysisService(
            AnalysisRuntimeContext(self.root), factory, program_resolver=lambda *args: self.program
        )
        definition = self.service.definition("main.cpp", 1, 5)
        self.assertEqual(definition["status"], "ok", definition)
        adapter = self.service._adapter
        self.assertIsNone(
            adapter.diagnostics_for(self.source.as_uri(), definition["source"]["document_version"])
        )
        (self.root / "received.jsonl.enable").touch()
        result = self.service.diagnostics("main.cpp", timeout_seconds=0.5)
        record_regression(
            "missing-current-diagnostics", {"definition": definition, "result": result}
        )
        self.assertEqual(result["status"], "ok", result)
        self.assertGreater(
            result["source"]["document_version"], definition["source"]["document_version"]
        )
        self.assertEqual(result["source"]["sha256"], definition["source"]["sha256"])
        self.assertEqual(result["data"]["observed_count"], 0)
        again = self.service.diagnostics("main.cpp", timeout_seconds=0.5)
        self.assertEqual(again["source"], result["source"])
        frames = [
            json.loads(line)
            for line in (self.root / "received.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        changes = [frame for frame in frames if frame.get("method") == "textDocument/didChange"]
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0]["params"]["wantDiagnostics"])
        self.assertEqual(
            changes[0]["params"]["textDocument"]["version"], result["source"]["document_version"]
        )
        self.assertEqual(
            changes[0]["params"]["contentChanges"],
            [{"text": self.source.read_bytes().decode("utf-8")}],
        )
        record_regression("missing-current-diagnostics-frames", frames)

    def test_missing_current_diagnostics_still_silent_is_bounded_and_incomplete(self):
        self.service.shutdown()
        self.service = CodeAnalysisService(
            AnalysisRuntimeContext(self.root),
            fake_factory(self.root, "silent"),
            program_resolver=lambda *args: self.program,
        )
        definition = self.service.definition("main.cpp", 1, 5)
        started = time.monotonic()
        result = self.service.diagnostics("main.cpp", timeout_seconds=0.1)
        elapsed = time.monotonic() - started
        record_regression("still-silent-diagnostics", {"result": result, "elapsed": elapsed})
        self.assertEqual(result["status"], "incomplete", result)
        self.assertEqual(result["code"], "analysis/diagnostics-missing", result)
        self.assertGreater(
            result["source"]["document_version"], definition["source"]["document_version"]
        )
        self.assertLess(elapsed, 0.1 + ARGUMENT_TIMEOUT_GRACE_SECONDS)

    def test_regression_every_tool_description_discloses_saved_scope(self):
        for name, handler in build_code_analysis_handlers(
            CodeAnalysisToolServices(self.service)
        ).items():
            with self.subTest(tool=name):
                self.assertIn("saved", handler.__doc__.lower())

    def test_status_discloses_selected_database_native_cache_without_starting_session(self):
        result = self.service.status()
        self.assertEqual(result["status"], "ok", result)
        self.assertIn(str(self.root / ".cache" / "clangd" / "index"), " ".join(result["warnings"]))
        self.assertIsNone(self.service._adapter)

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

    def test_regression_conditional_config_only_evaluates_actual_selected_paths(self):
        (self.root / "src").mkdir()
        self.source = self.source.rename(self.root / "src/main.cpp")
        database = self.root / "compile_commands.json"
        entries = json.loads(database.read_text(encoding="utf-8"))
        entries[0]["file"] = str(self.source)
        entries[0]["arguments"][-1] = str(self.source)
        database.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        config = self.root / ".clangd"
        config.write_text(
            "If:\n  PathExclude: src/.*\nCompileFlags:\n  CompilationDatabase: None\n",
            encoding="utf-8",
        )
        before = config.read_bytes()
        status = self.service.status()
        query = self.service.hover("src/main.cpp", 1, 5)
        record_regression("conditional-fake", {"status": status, "query": query})
        self.assertEqual(status["status"], "ok", status)
        self.assertEqual(status["data"]["session_state"], "not_started")
        self.assertEqual(query["status"], "ok", query)
        self.assertEqual(config.read_bytes(), before)
        config.write_text(
            "If:\n  PathMatch: src/.*\nCompileFlags:\n  CompilationDatabase: None\n",
            encoding="utf-8",
        )
        self.assertEqual(self.service.status()["code"], "analysis/clangd-config-conflict")
        self.assertEqual(self.service.hover("src/main.cpp", 1, 5)["status"], "not_configured")
        # The same excluded fragment genuinely applies to a queried header.
        config.write_bytes(before)
        header = self.root / "outside.h"
        header.write_text("int value;\n", encoding="utf-8")
        self.assertEqual(
            self.service.hover("outside.h", 1, 5)["code"], "analysis/clangd-config-conflict"
        )

    def test_regression_nonfinite_config_names_the_offending_field(self):
        for number in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(number=number):
                (self.root / "byo-analysis.json").write_text(
                    '{"schema_version":1,"cppcheck":{"timeout_seconds":' + number + "}}",
                    encoding="utf-8",
                )
                problem = load_analysis_config(self.root).problems[0]
                self.assertEqual(problem.code, "analysis/config-invalid")
                self.assertEqual(problem.field, "cppcheck.timeout_seconds")
                self.assertIn("timeout_seconds", problem.message)
                self.assertTrue(problem.remedy)

    @unittest.skipUnless(os.name == "nt", "Windows drive URI semantics")
    def test_regression_encoded_drive_location_maps_codepoints_and_preserves_raw_missing_range(
        self,
    ):
        target = self.root / "target é 😀 %20.h"
        text = "/*😀*/ int value;\r\n"
        target.write_text(text, encoding="utf-8", newline="")
        uri = target.as_uri().replace(
            "file:///" + target.drive, "file:///" + target.drive[0].lower() + "%3A", 1
        )
        raw_range = {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 7}}
        warnings = []
        mapped = self.service._location(
            {"uri": uri, "range": raw_range}, "utf-16", warnings, self.source, ""
        )
        record_regression("encoded-drive-mapped", mapped)
        self.assertEqual(mapped["position_mapping"], "available", mapped)
        self.assertEqual(mapped["range"]["start"], {"line": 1, "column": 6})
        self.assertEqual(mapped["uri"], uri)
        self.assertTrue(Path(mapped["path"]).samefile(target))
        target.unlink()
        unavailable = self.service._location(
            {"uri": uri, "range": raw_range}, "utf-16", warnings, self.source, ""
        )
        self.assertEqual(unavailable["position_mapping"], "unavailable")
        self.assertIsNone(unavailable["range"])
        self.assertEqual(unavailable["raw_lsp_range"], raw_range)
        self.assertEqual(unavailable["lsp_position_encoding"], "utf-16")

    def test_regression_timeout_cancel_frames_reach_peer_before_owned_shutdown(self):
        attempts = []
        for index in range(8):
            root = self.root / f"cancel-{index}"
            root.mkdir()
            make_project(root)
            service = CodeAnalysisService(
                AnalysisRuntimeContext(root),
                fake_factory(root, "never-definition"),
                program_resolver=lambda *args: self.program,
            )
            try:
                self.assertEqual(service.diagnostics("main.cpp")["status"], "ok")
                process = service._adapter.process
                marker = json.loads(service._adapter.marker.read_text(encoding="utf-8"))
                started = time.monotonic()
                result = service.definition("main.cpp", 1, 5, timeout_seconds=0.1)
                elapsed = time.monotonic() - started
                service.shutdown()
                received = [
                    json.loads(line)
                    for line in (root / "received.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                attempts.append(
                    {
                        "result": result,
                        "elapsed": elapsed,
                        "received_frames": received,
                        "owned_identity": marker,
                        "exit_code": process.poll(),
                        "remaining_markers": list(map(str, (root / "markers").glob("*.json"))),
                    }
                )
            finally:
                service.shutdown()
        record_regression("cancel-delivery", attempts)
        for attempt in attempts:
            self.assertEqual(attempt["result"]["code"], "analysis/timeout", attempt)
            requests = [
                m["id"]
                for m in attempt["received_frames"]
                if m.get("method") == "textDocument/definition"
            ]
            cancellations = [
                m["params"]["id"]
                for m in attempt["received_frames"]
                if m.get("method") == "$/cancelRequest"
            ]
            self.assertEqual(len(requests), 1, attempt)
            self.assertTrue(set(requests) <= set(cancellations), attempt)
            self.assertLess(attempt["elapsed"], ARGUMENT_TIMEOUT_GRACE_SECONDS + 0.1)
            self.assertIsNotNone(attempt["exit_code"])
            self.assertEqual(attempt["remaining_markers"], [])

    def test_saved_revision_counts_and_empty_versioned_diagnostics(self):
        first = self.service.diagnostics("main.cpp")
        self.assertEqual(first["status"], "ok", first)
        self.assertEqual(first["data"]["observed_count"], 0)
        second = self.service.diagnostics("main.cpp")
        self.assertEqual(second["source"], first["source"])
        self.source.write_bytes(b"int value = 2;\r\n")
        hover = self.service.hover("main.cpp", 1, 5)
        self.assertEqual(hover["source"]["sha256"], sha256_bytes(self.source.read_bytes()))
        self.assertGreater(hover["source"]["document_version"], first["source"]["document_version"])
        self.assertEqual(hover["status"], "ok", hover)
        self.assertEqual(self.service.references("main.cpp", 1, 5)["status"], "not_found")

    def test_concurrent_same_file_versions_and_source_change(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.service.diagnostics("main.cpp"), range(2)))
        self.assertTrue(all(r["status"] == "ok" for r in results), results)
        self.assertEqual(len({r["source"]["document_version"] for r in results}), 1)
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
        # The 200 ms budget measures notification waiting, not Windows cold
        # process startup. A completed hover observes initialization, didOpen,
        # its unversioned notification and a synchronized document revision.
        warm = self.service.hover("main.cpp", 1, 5, timeout_seconds=5)
        self.assertEqual(warm["status"], "ok", warm)
        self.assertEqual(self.service._state, "ready")
        version = warm["source"]["document_version"]
        self.assertIsInstance(version, int)
        self.assertEqual(self.service._documents[self.source].version, version)
        self.assertIn(self.source.as_uri(), self.service._adapter._unversioned)
        result = self.service.diagnostics("main.cpp", timeout_seconds=0.2)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertEqual(result["code"], "analysis/diagnostics-unversioned", result)
        record_regression("unversioned-diagnostics-ready", {"warm": warm, "result": result})

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


class VersionProbeTests(unittest.TestCase):
    def test_public_status_own_cap_is_cached_and_changed_executable_reprobes(self):
        executable = self.root / "status-version.exe"
        shutil.copyfile(sys.executable, executable)
        original_spawn = runtime.popen_owned

        def private_version_peer(argv, **kwargs):
            self.assertEqual(argv, [str(executable), "--version"])
            return original_spawn([str(Path(sys.executable).resolve()), "--version"], **kwargs)

        (self.root / "byo-analysis.json").write_text(
            '{"schema_version":1,"compilation_database":"compile_commands.json"}',
            encoding="utf-8",
        )
        (self.root / ".clangd").write_text(
            "CompileFlags:\n  Add: [-DSTATUS_FIXTURE=1]\n", encoding="utf-8"
        )
        protected = {
            str(path): sha256_bytes(path.read_bytes())
            for path in [
                self.root / "main.cpp",
                self.root / "compile_commands.json",
                self.root / "byo-analysis.json",
                self.root / ".clangd",
                Path(sys.executable),
            ]
        }
        service = CodeAnalysisService(self.context)
        self.addCleanup(service.shutdown)
        handler = build_code_analysis_handlers(CodeAnalysisToolServices(service))[
            "get_code_analysis_status"
        ]
        calls = []
        with (
            patch.object(runtime, "_which_path_only", return_value=str(executable)),
            patch.object(runtime, "popen_owned", side_effect=private_version_peer),
        ):
            for index in range(4):
                if index == 2:
                    executable.write_bytes(executable.read_bytes() + b"changed identity fixture")
                    self.control.write_text("good", encoding="utf-8")
                started = time.monotonic()
                result = json.loads(handler())
                calls.append(
                    {
                        "result": result,
                        "elapsed": time.monotonic() - started,
                        "child_count": len(self.children),
                        "cache_size": len(runtime._probe_cache),
                        "cache_keys": list(runtime._probe_cache),
                    }
                )
        after = {path: sha256_bytes(Path(path).read_bytes()) for path in protected}
        self.observe(
            "public-status-cache", calls=calls, protected_before=protected, protected_after=after
        )
        first, repeated, changed, changed_repeated = calls
        self.assertEqual(first["result"]["status"], "unavailable", first)
        self.assertEqual(first["result"]["code"], "analysis/executable-unavailable")
        self.assertIn("5-second spawn/probe", first["result"]["message"])
        self.assertEqual(first["result"]["data"]["executable"], str(executable))
        self.assertEqual(first["cache_size"], 1)
        self.assertGreaterEqual(first["elapsed"], 5)
        self.assertLess(first["elapsed"], 5 + ARGUMENT_TIMEOUT_GRACE_SECONDS)
        self.assertEqual(repeated["result"], first["result"])
        self.assertEqual(repeated["child_count"], 1)
        self.assertLess(repeated["elapsed"], 1)
        self.assertEqual(changed["result"]["status"], "ok", changed)
        self.assertEqual(changed["child_count"], 2)
        self.assertEqual(changed["cache_size"], 2)
        self.assertIn("executable version fixture", changed["result"]["data"]["version"])
        self.assertEqual(changed_repeated["result"], changed["result"])
        self.assertEqual(changed_repeated["child_count"], 2)
        self.assertEqual(protected, after)
        self.assertIsNone(service._adapter)

    def test_public_status_managed_expiry_is_transient_then_independent_retry(self):
        from pyocd_debug_mcp.kernel.operations import ManagedOperation, _current_operation

        for budget in (0.0, 0.15):
            with self.subTest(budget=budget), patch.object(runtime, "_probe_cache", {}):
                self.control.write_text("hung", encoding="utf-8")
                service = CodeAnalysisService(self.context)
                self.addCleanup(service.shutdown)
                handler = build_code_analysis_handlers(CodeAnalysisToolServices(service))[
                    "get_code_analysis_status"
                ]
                managed = ManagedOperation(
                    "status-expiry",
                    "request",
                    "get_code_analysis_status",
                    None,
                    budget,
                    False,
                    False,
                )
                token = _current_operation.set(managed)
                started = time.monotonic()
                try:
                    expired = json.loads(handler())
                finally:
                    _current_operation.reset(token)
                elapsed = time.monotonic() - started
                transient_cache = dict(runtime._probe_cache)
                self.control.write_text("good", encoding="utf-8")
                recovered = json.loads(handler())
                self.observe(
                    "public-status-expiry-" + str(budget),
                    expired=expired,
                    recovered=recovered,
                    elapsed=elapsed,
                    transient_cache=transient_cache,
                )
                self.assertEqual(expired["status"], "incomplete", expired)
                self.assertEqual(expired["code"], "analysis/timeout", expired)
                self.assertEqual(transient_cache, {})
                self.assertLess(elapsed, budget + ARGUMENT_TIMEOUT_GRACE_SECONDS)
                self.assertEqual(recovered["status"], "ok", recovered)
                self.assertIsNone(service._adapter)

    def test_public_status_managed_cancellation_is_transient_then_independent_retry(self):
        from pyocd_debug_mcp.kernel.operations import ManagedOperation, _current_operation

        managed = ManagedOperation(
            "status-cancel", "request", "get_code_analysis_status", None, 60, False, False
        )
        service = CodeAnalysisService(self.context)
        self.addCleanup(service.shutdown)
        handler = build_code_analysis_handlers(CodeAnalysisToolServices(service))[
            "get_code_analysis_status"
        ]

        def call():
            token = _current_operation.set(managed)
            try:
                return json.loads(handler())
            finally:
                _current_operation.reset(token)

        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pending = pool.submit(call)
            limit = time.monotonic() + 3
            while not (self.root / "started.jsonl").exists() and time.monotonic() < limit:
                managed.cancellation_requested.wait(0.005)
            self.assertTrue((self.root / "started.jsonl").exists())
            managed.request_cancel("status fixture cancelled")
            cancelled = pending.result(3)
        transient_cache = dict(runtime._probe_cache)
        self.control.write_text("good", encoding="utf-8")
        recovered = json.loads(handler())
        self.observe(
            "public-status-cancel",
            cancelled=cancelled,
            recovered=recovered,
            transient_cache=transient_cache,
        )
        self.assertEqual(cancelled["status"], "incomplete", cancelled)
        self.assertEqual(cancelled["code"], "analysis/timeout", cancelled)
        self.assertIn("cancelled", cancelled["message"])
        self.assertEqual(transient_cache, {})
        self.assertEqual(recovered["status"], "ok", recovered)
        self.assertIsNone(service._adapter)

    """Run actual owned version peers; clocks, communicate and admission stay real."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="analysis version cap ")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        make_project(self.root)
        self.context = AnalysisRuntimeContext(self.root)
        self.control = self.root / "version-mode.txt"
        self.control.write_text("hung", encoding="utf-8")
        self.peer = self.root / "version-peer.py"
        self.peer.write_text(
            "import json, os, sys, threading\n"
            "from pathlib import Path\n"
            "assert sys.argv[-1] == '--version'\n"
            "mode = Path(sys.argv[1]).read_text(encoding='utf-8')\n"
            "with open(sys.argv[2], 'a', encoding='utf-8') as log:\n"
            "    log.write(json.dumps({'pid':os.getpid(), 'mode':mode, 'argv':sys.argv})+'\\n')\n"
            "if mode == 'hung': threading.Event().wait(60)\n"
            "if mode == 'bad': print('failed version', file=sys.stderr); sys.exit(7)\n"
            "print('clangd executable version fixture 1')\n",
            encoding="utf-8",
        )
        from pyocd_debug_mcp.kernel.processes import ProcessMarkerStore

        self.store = ProcessMarkerStore(self.root / "version-markers")
        self.children = []
        self.identities = []
        original_popen = runtime.popen_owned

        def route(argv, **kwargs):
            if argv != [str(Path(sys.executable).resolve()), "--version"]:
                return original_popen(argv, **kwargs)
            process, marker = original_popen(
                [
                    sys.executable,
                    str(self.peer),
                    str(self.control),
                    str(self.root / "started.jsonl"),
                    "--version",
                ],
                **kwargs,
            )
            self.children.append((process, marker))
            self.identities.append(json.loads(marker.read_text(encoding="utf-8")))
            return process, marker

        for patcher in [
            patch.object(runtime, "_which_path_only", return_value=sys.executable),
            patch.object(runtime, "popen_owned", side_effect=route),
            patch.object(runtime, "ProcessMarkerStore", return_value=self.store),
            patch.object(runtime, "_probe_cache", {}),
            patch.object(runtime, "_probe_lock", threading.Lock()),
            patch.object(runtime, "_spawn_admission", threading.Lock()),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.cleanup_children)
        self.assertEqual(
            Path(runtime.__file__).resolve(),
            Path(__file__).resolve().parents[1] / "src/pyocd_debug_mcp/code_analysis_runtime.py",
        )

    def cleanup_children(self):
        for process, marker in self.children:
            if process.poll() is None:
                self.assertTrue(runtime.terminate_process_group(process))
                self.store.remove(marker)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()

    def observe(self, name, **data):
        data.update(
            owned_identities=self.identities,
            child_exits=[p.poll() for p, _ in self.children],
            remaining_markers=list(map(str, self.store.root.glob("*.json"))),
            cache_problems=[
                [p.to_payload() for p in item.problems] for item in runtime._probe_cache.values()
            ],
        )
        record_regression(name, data)
        self.assertTrue(all(code is not None for code in data["child_exits"]), data)
        self.assertEqual(data["remaining_markers"], [], data)

    def test_own_five_second_cap_is_unavailable_and_cached_while_caller_lives(self):
        deadline = time.monotonic() + 60
        started = time.monotonic()
        result = None
        timeout = None
        try:
            result = resolve_analysis_program(self.context, "clangd", deadline)
        except TimeoutError as exc:
            timeout = str(exc)
        elapsed = time.monotonic() - started
        self.observe(
            "own-cap",
            caller_remaining=deadline - time.monotonic(),
            elapsed=elapsed,
            timeout=timeout,
            problems=[p.to_payload() for p in result.problems] if result else None,
        )
        self.assertIsNotNone(result, "own cap incorrectly propagated caller TimeoutError")
        self.assertEqual(result.problems[0].code, "analysis/executable-unavailable")
        self.assertGreaterEqual(elapsed, 5)
        self.assertLess(elapsed, 5 + ARGUMENT_TIMEOUT_GRACE_SECONDS)
        self.assertGreater(deadline - time.monotonic(), 40)
        self.assertEqual(len(self.children), 1)
        again = resolve_analysis_program(self.context, "clangd", time.monotonic() + 60)
        self.assertIs(again, result)
        self.assertEqual(len(self.children), 1, "negative cache unexpectedly restarted the peer")

    def test_public_query_with_live_caller_reports_own_cap_unavailable(self):
        service = CodeAnalysisService(self.context)
        self.addCleanup(service.shutdown)
        started = time.monotonic()
        result = service.hover("main.cpp", 1, 5, timeout_seconds=60)
        elapsed = time.monotonic() - started
        self.observe("own-cap-public", result=result, elapsed=elapsed)
        self.assertEqual(result["status"], "unavailable", result)
        self.assertEqual(result["code"], "analysis/executable-unavailable", result)
        self.assertIsNone(service._adapter)
        self.assertLess(elapsed, 5 + ARGUMENT_TIMEOUT_GRACE_SECONDS)

    def test_own_cap_cause_survives_caller_expiry_during_confirmed_cleanup(self):
        original_cleanup = runtime.terminate_process_group

        def delayed_cleanup(process):
            threading.Event().wait(0.2)
            return original_cleanup(process)

        deadline = time.monotonic() + 5.1
        result = None
        timeout = None
        with patch.object(runtime, "terminate_process_group", side_effect=delayed_cleanup):
            try:
                result = resolve_analysis_program(self.context, "clangd", deadline)
            except TimeoutError as exc:
                timeout = str(exc)
        self.observe(
            "own-cap-cleanup-overlap",
            timeout=timeout,
            caller_remaining=deadline - time.monotonic(),
            problems=[p.to_payload() for p in result.problems] if result else None,
        )
        self.assertLess(deadline - time.monotonic(), 0)
        self.assertIsNotNone(result, "cleanup reclassified the already established own cap")
        self.assertEqual(result.problems[0].code, "analysis/executable-unavailable")
        self.assertEqual(len(runtime._probe_cache), 1)

    def test_actual_caller_expiry_is_transient_and_independent_peer_recovers(self):
        for budget in (-0.01, 0.15):
            with self.subTest(budget=budget), patch.object(runtime, "_probe_cache", {}):
                self.control.write_text("hung", encoding="utf-8")
                with self.assertRaises(TimeoutError):
                    resolve_analysis_program(self.context, "clangd", time.monotonic() + budget)
                self.assertEqual(runtime._probe_cache, {})
                self.control.write_text("good", encoding="utf-8")
                result = resolve_analysis_program(self.context, "clangd", time.monotonic() + 5)
                self.assertFalse(result.problems)
                self.assertIn("executable version fixture", result.version)
                self.observe("caller-expiry-" + str(budget), version=result.version)

    def test_actual_cancellation_is_transient_and_independent_peer_recovers(self):
        event = threading.Event()
        with patch.object(
            runtime, "current_operation", return_value=SimpleNamespace(cancellation_requested=event)
        ):
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                future = pool.submit(
                    resolve_analysis_program, self.context, "clangd", time.monotonic() + 60
                )
                limit = time.monotonic() + 3
                while not (self.root / "started.jsonl").exists() and time.monotonic() < limit:
                    event.wait(0.005)
                self.assertTrue((self.root / "started.jsonl").exists())
                event.set()
                with self.assertRaises(TimeoutError):
                    future.result(3)
        self.assertEqual(runtime._probe_cache, {})
        self.control.write_text("good", encoding="utf-8")
        result = resolve_analysis_program(self.context, "clangd", time.monotonic() + 5)
        self.assertFalse(result.problems)
        self.observe("caller-cancellation", version=result.version)

    def test_admission_queue_expiry_is_not_executable_failure_evidence(self):
        runtime._spawn_admission.acquire()
        try:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                resolve_analysis_program(self.context, "clangd", time.monotonic() + 60)
            self.assertGreaterEqual(time.monotonic() - started, 5)
            self.assertFalse(self.children)
            self.assertEqual(runtime._probe_cache, {})
        finally:
            runtime._spawn_admission.release()
        self.control.write_text("good", encoding="utf-8")
        result = resolve_analysis_program(self.context, "clangd", time.monotonic() + 5)
        self.assertFalse(result.problems)
        self.observe("admission-queue", version=result.version)

    def test_probe_queue_and_late_spawn_caller_expiry_do_not_poison_cache(self):
        runtime._probe_lock.acquire()
        try:
            with self.assertRaises(TimeoutError):
                resolve_analysis_program(self.context, "clangd", time.monotonic() + 0.05)
            self.assertFalse(self.children)
            self.assertEqual(runtime._probe_cache, {})
        finally:
            runtime._probe_lock.release()
        original = runtime.popen_owned
        entered = threading.Event()
        release = threading.Event()

        def delayed(*args, **kwargs):
            entered.set()
            release.wait(1)
            return original(*args, **kwargs)

        with patch.object(runtime, "popen_owned", side_effect=delayed):
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                future = pool.submit(
                    resolve_analysis_program, self.context, "clangd", time.monotonic() + 0.1
                )
                self.assertTrue(entered.wait(1))
                with self.assertRaises(TimeoutError):
                    future.result(2)
                self.assertEqual(runtime._probe_cache, {})
                release.set()
                # Admission releases only after actual late-owned-child cleanup.
                self.assertTrue(runtime._spawn_admission.acquire(timeout=3))
                runtime._spawn_admission.release()
        self.control.write_text("good", encoding="utf-8")
        result = resolve_analysis_program(self.context, "clangd", time.monotonic() + 5)
        self.assertFalse(result.problems)
        self.observe("probe-queue-and-late-spawn", version=result.version)

    def test_public_caller_expiry_is_incomplete_then_independent_query_recovers(self):
        service = CodeAnalysisService(self.context)
        self.addCleanup(service.shutdown)
        result = service.hover("main.cpp", 1, 5, timeout_seconds=0.15)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertEqual(result["code"], "analysis/timeout", result)
        self.assertEqual(runtime._probe_cache, {})
        self.control.write_text("good", encoding="utf-8")
        program = resolve_analysis_program(self.context, "clangd", time.monotonic() + 5)
        self.assertFalse(program.problems)
        service.adapter_factory = fake_factory(self.root)
        recovered = service.hover("main.cpp", 1, 5, timeout_seconds=3)
        self.assertEqual(recovered["status"], "ok", recovered)
        service.shutdown()
        self.assertFalse(list((self.root / "markers").glob("*.json")))
        self.observe("caller-expiry-public", result=result, recovered=recovered)


@unittest.skipUnless(
    os.environ.get("BYO_TEST_CLANGD"), "Set explicit BYO_TEST_CLANGD private test fixture."
)
class RealClangdTests(unittest.TestCase):
    def test_known_generated_header_saved_change_and_restore_native(self):
        from pyocd_debug_mcp.adapters.clangd import ClangdAdapter

        source = make_project(self.root)
        header = self.root / ".firm/build/generated/selected.h"
        header.parent.mkdir(parents=True)
        header.write_text("#define SELECTED 7\n", encoding="utf-8")
        source.write_text(
            '#include ".firm/build/generated/selected.h"\nstatic_assert(SELECTED == 7);\nint value;\n',
            encoding="utf-8",
        )
        database = self.root / "compile_commands.json"
        original_database = database.read_bytes()
        incoming = []

        def factory(program, snapshot, marker_store):
            adapter = ClangdAdapter(program, snapshot, marker_store)
            route = adapter._route

            def observed(message):
                incoming.append(message)
                return route(message)

            adapter._route = observed
            return adapter

        self.service = CodeAnalysisService(AnalysisRuntimeContext(self.root), factory)
        evidence = {"executable_sha256": sha256_bytes(self.executable.read_bytes())}
        adapter = None
        try:
            initial = self.service.diagnostics("main.cpp", timeout_seconds=5)
            evidence["initial"] = initial
            self.assertEqual(initial["status"], "ok", initial)
            adapter = self.service._adapter
            process, marker = adapter.process, adapter.marker
            evidence["argv"] = adapter._argv()
            evidence["owned_marker"] = json.loads(marker.read_text(encoding="utf-8"))
            opened = self.service.hover(str(header), 1, 9, timeout_seconds=5)
            clean = self.service.diagnostics("main.cpp", timeout_seconds=5)
            evidence.update(opened=opened, clean=clean)
            self.assertEqual(opened["status"], "ok", opened)
            self.assertEqual(clean["status"], "ok", clean)
            self.assertEqual(clean["data"]["observed_count"], 0)
            header.write_text("#define SELECTED 9\n", encoding="utf-8")
            changed = self.service.diagnostics("main.cpp", timeout_seconds=5)
            evidence["changed"] = changed
            self.assertEqual(changed["status"], "ok", changed)
            self.assertEqual(changed["source"]["sha256"], clean["source"]["sha256"])
            self.assertGreater(
                changed["source"]["document_version"], clean["source"]["document_version"]
            )
            self.assertEqual(changed["data"]["observed_count"], 1, changed)
            header.write_text("#define SELECTED 7\n", encoding="utf-8")
            restored = self.service.diagnostics("main.cpp", timeout_seconds=5)
            evidence["restored"] = restored
            self.assertEqual(restored["status"], "ok", restored)
            self.assertGreater(
                restored["source"]["document_version"], changed["source"]["document_version"]
            )
            self.assertEqual(restored["data"]["observed_count"], 0)
            self.assertEqual(database.read_bytes(), original_database)
        finally:
            self.service.shutdown()
            evidence["received_messages"] = incoming
            if adapter is not None:
                evidence.update(
                    child_exit=process.poll(),
                    marker_exists=marker.exists(),
                    stderr=adapter.stderr_tail,
                )
            record_regression("known-generated-header-native", evidence)
        self.assertIsNotNone(process.poll())
        self.assertFalse(marker.exists())

    def test_native_missing_current_diagnostics_protocol_characterization(self):
        make_project(self.root)
        self.service = CodeAnalysisService(AnalysisRuntimeContext(self.root))
        first = self.service.diagnostics("main.cpp", timeout_seconds=5)
        self.assertEqual(first["status"], "ok", first)
        adapter = self.service._adapter
        process = adapter.process
        incoming = []
        outgoing = []
        original_route = adapter._route
        original_send = adapter._send

        def route(message):
            incoming.append(message)
            return original_route(message)

        def send(message):
            outgoing.append(message)
            return original_send(message)

        # Simulate absent client revision evidence without changing saved bytes,
        # the native child, its negotiated protocol or its actual diagnostics.
        adapter.clear_diagnostics(Path(first["source"]["path"]).as_uri())
        with (
            patch.object(adapter, "_route", side_effect=route),
            patch.object(adapter, "_send", side_effect=send),
        ):
            result = self.service.diagnostics("main.cpp", timeout_seconds=5)
        self.service.shutdown()
        record_regression(
            "native-missing-current-characterization",
            {
                "first": first,
                "result": result,
                "queued_messages": outgoing,
                "native_received_messages": incoming,
                "stderr": adapter.stderr_tail,
                "argv": adapter._argv(),
                "executable_sha256": sha256_bytes(self.executable.read_bytes()),
                "child_exit": process.poll(),
            },
        )
        changes = [m for m in outgoing if m.get("method") == "textDocument/didChange"]
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0]["params"]["wantDiagnostics"])
        self.assertGreater(
            result["source"]["document_version"], first["source"]["document_version"]
        )
        self.assertIn(result["status"], {"ok", "incomplete"}, result)
        matching = [
            m
            for m in incoming
            if m.get("method") == "textDocument/publishDiagnostics"
            and m["params"].get("version") == result["source"]["document_version"]
        ]
        if result["status"] == "ok":
            self.assertTrue(matching, "native success needs explicit version evidence")
        else:
            self.assertEqual(result["code"], "analysis/diagnostics-missing", result)
            self.assertFalse(matching, "record the native limitation without relabeling evidence")
        self.assertIsNotNone(process.poll())

    def test_known_header_change_reopens_same_saved_bytes_for_fresh_native_diagnostics(self):
        source = make_project(self.root)
        header = self.root / "selected.h"
        header.write_text("#define SELECTED 7\n", encoding="utf-8")
        source.write_text(
            '#include "selected.h"\nstatic_assert(SELECTED == 7);\nint value;\n', encoding="utf-8"
        )
        self.service = CodeAnalysisService(AnalysisRuntimeContext(self.root))
        clean = self.service.diagnostics("main.cpp", timeout_seconds=5)
        self.assertEqual(clean["status"], "ok", clean)
        self.assertEqual(clean["data"]["observed_count"], 0)
        header.write_text("#define SELECTED 9\n", encoding="utf-8")
        defect = self.service.diagnostics("main.cpp", timeout_seconds=5)
        self.assertEqual(defect["status"], "ok", defect)
        self.assertEqual(defect["source"]["sha256"], clean["source"]["sha256"])
        self.assertGreater(
            defect["source"]["document_version"], clean["source"]["document_version"]
        )
        self.assertGreater(defect["data"]["observed_count"], 0)
        header.write_text("#define SELECTED 7\n", encoding="utf-8")
        restored = self.service.diagnostics("main.cpp", timeout_seconds=5)
        self.assertEqual(restored["status"], "ok", restored)
        self.assertGreater(
            restored["source"]["document_version"], defect["source"]["document_version"]
        )
        self.assertEqual(restored["data"]["observed_count"], 0)
        record_regression(
            "header-revision-native", {"clean": clean, "defect": defect, "restored": restored}
        )

    def test_regression_relative_directory_with_absolute_source_preserves_arm_command(self):
        source = self.root / "main.cpp"
        source.write_text(
            "static_assert(sizeof(void*) == 4);\nstatic_assert(SELECTED_BUILD == 7);\nint selected = SELECTED_BUILD;\n",
            encoding="utf-8",
        )
        database = self.root / "compile_commands.json"
        database.write_text(
            json.dumps(
                [
                    {
                        "directory": ".",
                        "file": str(source),
                        "arguments": [
                            "clang++",
                            "--target=arm-none-eabi",
                            "-mcpu=cortex-m4",
                            "-mthumb",
                            "-std=c++17",
                            "-DSELECTED_BUILD=7",
                            "-c",
                            str(source),
                        ],
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        original_bytes = database.read_bytes()
        from pyocd_debug_mcp.kernel.processes import run_owned

        argv = [
            str(self.executable),
            "--enable-config",
            "--check=" + str(source),
            "--compile-commands-dir=" + str(self.root),
            "--query-driver=",
        ]
        native = run_owned(argv, timeout=15, capture_output=True, text=True, cwd=self.root)
        self.assertEqual(native.returncode, 0, native.stderr)
        self.assertIn("thumbv7em", native.stderr)
        self.assertFalse(load_analysis_config(self.root).problems)
        self.service = CodeAnalysisService(AnalysisRuntimeContext(self.root))
        result = self.service.diagnostics("main.cpp", timeout_seconds=5)
        record_regression(
            "relative-absolute-source",
            {
                "argv": argv,
                "native_exit": native.returncode,
                "native_stderr": native.stderr,
                "result": result,
            },
        )
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["data"]["observed_count"], 0)
        self.assertEqual(database.read_bytes(), original_bytes)

    def test_regression_unchanged_saved_revision_has_explicit_diagnostics(self):
        make_project(self.root)
        self.service = CodeAnalysisService(AnalysisRuntimeContext(self.root))
        definition = self.service.definition("main.cpp", 1, 5, timeout_seconds=5)
        diagnostics = self.service.diagnostics("main.cpp", timeout_seconds=2)
        record_regression(
            "unchanged-native", {"definition": definition, "diagnostics": diagnostics}
        )
        self.assertEqual(definition["status"], "ok", definition)
        self.assertEqual(diagnostics["status"], "ok", diagnostics)
        self.assertEqual(diagnostics["source"]["sha256"], definition["source"]["sha256"])
        self.assertEqual(diagnostics["data"]["observed_count"], 0)
        again = self.service.diagnostics("main.cpp", timeout_seconds=2)
        self.assertEqual(again["status"], "ok", again)
        self.assertEqual(again["source"], diagnostics["source"])
        source = self.root / "main.cpp"
        source.write_text("int value = missing;\n", encoding="utf-8")
        defect = self.service.diagnostics("main.cpp", timeout_seconds=5)
        self.assertEqual(defect["status"], "ok", defect)
        self.assertGreater(
            defect["source"]["document_version"], diagnostics["source"]["document_version"]
        )
        self.assertGreater(defect["data"]["observed_count"], 0)
        source.write_bytes(b"int value = 1;\r\n")
        restored = self.service.diagnostics("main.cpp", timeout_seconds=5)
        self.assertEqual(restored["status"], "ok", restored)
        self.assertGreater(
            restored["source"]["document_version"], defect["source"]["document_version"]
        )
        self.assertEqual(restored["data"]["observed_count"], 0)

    def test_regression_background_cache_preserves_other_project_inputs(self):
        # ROOT clarified the exact native cache exception at 8d18af229c3608be7c7a16be5ce79e328e7d06ff.
        # The earlier strict oracle failure stays in retained red/green evidence.
        make_project(self.root)
        build = self.root / "build"
        build.mkdir()
        database = (self.root / "compile_commands.json").rename(build / "compile_commands.json")
        (self.root / "byo-analysis.json").write_text(
            json.dumps(
                {"schema_version": 1, "compilation_database": "build/compile_commands.json"}
            ),
            encoding="utf-8",
        )
        protected = {
            "include/generated.h": b"#define TARGET_VALUE 7\n",
            ".clangd": b"# Project-owned clangd configuration.\n",
            "policy/board.json": b'{"project_owned": true}\n',
            "config/suppressions.txt": b"project_owned_suppression\n",
            ".firm/policy.json": b'{"project_owned": true}\n',
        }
        for name, content in protected.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        native_cache = build / ".cache" / "clangd" / "index"
        cache_directories = {build / ".cache", native_cache.parent, native_cache}

        def tree():
            return {
                path.relative_to(self.root).as_posix(): sha256_bytes(path.read_bytes())
                if path.is_file()
                else "<dir>"
                for path in self.root.rglob("*")
                if not path.is_relative_to(self.root / ".firm" / "code-analysis")
                and path != self.root / ".firm"
                and path not in cache_directories
                and not (
                    path.is_file()
                    and path.parent == native_cache
                    and (path.name == ".gitignore" or path.suffix == ".idx")
                )
            }

        before = tree()
        self.service = CodeAnalysisService(AnalysisRuntimeContext(self.root))
        result = self.service.diagnostics("main.cpp", timeout_seconds=5)
        self.assertEqual(result["status"], "ok", result)
        self.service.shutdown()
        after = tree()
        generated = [p.relative_to(self.root).as_posix() for p in native_cache.glob("*")]
        record_regression(
            "native-cache-boundary",
            {
                "contract_commit": "8d18af229c3608be7c7a16be5ce79e328e7d06ff",
                "before": before,
                "after": after,
                "native_cache_files": generated,
                "result": result,
            },
        )
        self.assertTrue(generated, "native cache was not exercised")
        self.assertEqual(before, after, "analysis changed protected project inputs")

        # The exception must not mask protected inputs, the whole .firm tree,
        # another cache root, nested cache data or arbitrary files in this cache.
        for path in [database, self.root / "main.cpp", *(self.root / name for name in protected)]:
            with self.subTest(protected=path.relative_to(self.root).as_posix()):
                content = path.read_bytes()
                try:
                    path.write_bytes(content + b"unapproved mutation")
                    self.assertNotEqual(before, tree())
                finally:
                    path.write_bytes(content)
        for name in [
            ".cache/clangd/index/wrong-root.idx",
            "build/.cache/clangd/other.idx",
            "build/.cache/clangd/index/arbitrary.txt",
            "build/.cache/clangd/index/nested/other.idx",
            ".firm/unapproved.txt",
        ]:
            with self.subTest(unapproved=name):
                path = self.root / name
                new_parents = []
                parent = path.parent
                while parent != self.root and not parent.exists():
                    new_parents.append(parent)
                    parent = parent.parent
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    path.write_bytes(b"unapproved write")
                    self.assertNotEqual(before, tree())
                finally:
                    path.unlink()
                    for parent in new_parents:
                        parent.rmdir()
                self.assertEqual(before, tree())

    def test_regression_transient_version_deadlines_do_not_poison_independent_retry(self):
        context = AnalysisRuntimeContext(self.root)
        attempts = []
        for scenario in ("expired", "short-probe", "cancelled-probe"):
            with self.subTest(scenario=scenario), patch.object(runtime, "_probe_cache", {}):
                event = threading.Event()
                original = runtime.spawn_before_deadline
                processes = []

                def spawn(*args, **kwargs):
                    process, marker = original(*args, **kwargs)
                    processes.append((process, marker))

                    def stalled(*args, **kwargs):
                        if scenario == "cancelled-probe":
                            event.set()
                        raise subprocess.TimeoutExpired(process.args, kwargs["timeout"])

                    process.communicate = stalled
                    return process, marker

                first = None
                error = None
                deadline = (
                    time.monotonic() - 0.01 if scenario == "expired" else time.monotonic() + 1
                )
                with (
                    patch.object(runtime, "spawn_before_deadline", side_effect=spawn),
                    patch.object(
                        runtime,
                        "current_operation",
                        return_value=SimpleNamespace(cancellation_requested=event),
                    ),
                ):
                    try:
                        first = resolve_analysis_program(context, "clangd", deadline)
                    except TimeoutError as exc:
                        error = str(exc)
                retry = resolve_analysis_program(context, "clangd", time.monotonic() + 5)
                attempts.append(
                    {
                        "scenario": scenario,
                        "first_timeout": error,
                        "first_result": [p.to_payload() for p in first.problems] if first else None,
                        "retry_version": retry.version,
                        "retry_problems": [p.to_payload() for p in retry.problems],
                        "owned_exits": [p.poll() for p, _ in processes],
                        "remaining_markers": [str(m) for _, m in processes if m and m.exists()],
                    }
                )
        record_regression("transient-probe", attempts)
        for attempt in attempts:
            self.assertIsNotNone(attempt["first_timeout"], attempt)
            self.assertFalse(attempt["retry_problems"], attempt)
            self.assertIn("clangd", attempt["retry_version"])
            self.assertTrue(all(code is not None for code in attempt["owned_exits"]))
            self.assertEqual(attempt["remaining_markers"], [])

    @unittest.skipUnless(os.name == "nt", "Windows implicit current-directory lookup")
    def test_regression_program_resolution_uses_only_explicit_path(self):
        shutil.copyfile(self.executable, self.root / "clangd.exe")
        empty_path = self.root / "isolated-path"
        empty_path.mkdir()
        before = Path.cwd()
        try:
            os.chdir(self.root)
            with (
                patch.dict(os.environ, {"PATH": str(empty_path)}),
                patch.object(runtime, "_probe_cache", {}),
            ):
                os.environ.pop("NoDefaultCurrentDirectoryInExePath", None)
                result = resolve_analysis_program(
                    AnalysisRuntimeContext(self.root), "clangd", time.monotonic() + 5
                )
        finally:
            os.chdir(before)
        record_regression(
            "path-only",
            {
                "executable": str(result.executable),
                "problems": [p.to_payload() for p in result.problems],
            },
        )
        self.assertIsNone(result.executable, "incidental cwd executable was selected")
        self.assertEqual(result.problems[0].code, "analysis/executable-unavailable")

    def test_regression_conditional_config_matches_native_real_unit(self):
        source = make_project(self.root)
        (self.root / "src").mkdir()
        source = source.rename(self.root / "src/main.cpp")
        database = self.root / "compile_commands.json"
        entries = json.loads(database.read_text(encoding="utf-8"))
        entries[0]["file"] = str(source)
        entries[0]["arguments"][-1] = str(source)
        database.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        (self.root / ".clangd").write_text(
            "If:\n  PathExclude: src/.*\nCompileFlags:\n  CompilationDatabase: None\n",
            encoding="utf-8",
        )
        argv = [
            str(self.executable),
            "--check=" + str(source),
            "--compile-commands-dir=" + str(self.root),
            "--enable-config",
        ]
        from pyocd_debug_mcp.kernel.processes import run_owned

        native = run_owned(argv, timeout=15, capture_output=True, text=True, cwd=self.root)
        self.assertEqual(native.returncode, 0, native.stderr)
        self.service = CodeAnalysisService(AnalysisRuntimeContext(self.root))
        status = self.service.status()
        query = self.service.hover("src/main.cpp", 1, 5)
        record_regression(
            "conditional-native",
            {
                "argv": argv,
                "exit": native.returncode,
                "stdout": native.stdout,
                "stderr": native.stderr,
                "status": status,
                "query": query,
            },
        )
        self.assertEqual(status["status"], "ok", status)
        self.assertEqual(status["data"]["session_state"], "not_started")
        self.assertEqual(query["status"], "ok", query)

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
