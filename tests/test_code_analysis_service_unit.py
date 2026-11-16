"""Implementer checks for service boundaries, MCP JSON and real clangd queries."""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


from pyocd_debug_mcp import code_analysis_runtime as runtime
from pyocd_debug_mcp.code_analysis_runtime import AnalysisRuntimeContext
from pyocd_debug_mcp.services.code_analysis import CodeAnalysisService
from pyocd_debug_mcp.tools.code_analysis import (
    CodeAnalysisToolServices,
    build_code_analysis_handlers,
)

from tests.code_analysis_support import simple_project


def record_regression(name, value):
    destination = os.environ.get("BYO_TEST_ANALYSIS_EVIDENCE")
    if destination:
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (name + ".json")).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class VersionProbeTests(unittest.TestCase):
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
        simple_project(self.root, sources={"main.cpp": "int value = 1;\n"})
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


if __name__ == "__main__":
    unittest.main()
