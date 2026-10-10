"""Exercise actual owned pipes/framing using a temporary scriptable LSP peer."""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from pyocd_debug_mcp.adapters.clangd import (
    ClangdAdapter,
    ClangdFailure,
    local_uri_path,
    position_from_lsp,
    position_to_lsp,
)
from pyocd_debug_mcp.code_analysis_config import load_analysis_config
from pyocd_debug_mcp.code_analysis_runtime import ResolvedAnalysisProgram
from pyocd_debug_mcp import code_analysis_runtime as runtime
from pyocd_debug_mcp.kernel.processes import ProcessMarkerStore
from pyocd_debug_mcp.kernel.operations import ARGUMENT_TIMEOUT_GRACE_SECONDS


PEER = r"""
import json, sys, threading, time
lock = threading.Lock()
mode = sys.argv[1]
def send(value):
    data = json.dumps(value, ensure_ascii=False).encode('utf-8')
    frame = b'Content-Length: ' + str(len(data)).encode() + b'\r\n\r\n' + data
    with lock:
        for offset in range(0, len(frame), 7):
            sys.stdout.buffer.write(frame[offset:offset+7])
            sys.stdout.buffer.flush()
def result(request, value):
    send({'jsonrpc':'2.0','id':request['id'],'result':value})
def diagnostics(params):
    doc = params['textDocument']
    if mode == 'silent': return
    value = {'uri':doc['uri'], 'diagnostics':[]}
    if mode != 'unversioned': value['version'] = doc['version']
    send({'jsonrpc':'2.0','method':'textDocument/publishDiagnostics','params':value})
while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line: sys.exit(0)
        if line == b'\r\n': break
        key, value = line.decode().split(':',1)
        headers[key.lower()] = value.strip()
    data = sys.stdin.buffer.read(int(headers['content-length']))
    msg = json.loads(data)
    with open(sys.argv[2], 'a', encoding='utf-8') as log:
        log.write(json.dumps(msg, ensure_ascii=False) + '\n')
    method = msg.get('method')
    if method == 'initialize':
        result(msg, {'capabilities':{'positionEncoding':'utf-8','textDocumentSync':{'openClose':True,'change':1},
            'definitionProvider':True,'referencesProvider':True,'hoverProvider':True}})
    elif method in ('textDocument/didOpen','textDocument/didChange'): diagnostics(msg['params'])
    elif method == 'shutdown': result(msg, None)
    elif method == 'exit': sys.exit(0)
    elif method == 'test/crash': sys.exit(9)
    elif method == 'test/malformed':
        sys.stdout.buffer.write(b'Content-Length: 1\r\n\r\nx'); sys.stdout.buffer.flush()
    elif method == 'test/serverRequest':
        send({'jsonrpc':'2.0','id':'edit','method':'workspace/applyEdit','params':{'edit':{}}})
        saved = msg
    elif method is None and msg.get('id') == 'edit': result(saved, msg['result'])
    elif method == 'test/delay':
        def delayed(request):
            time.sleep(request['params']['seconds'])
            result(request, request['params']['value'])
        threading.Thread(target=delayed,args=(msg,),daemon=True).start()
    elif method == 'test/wedge': threading.Event().wait()
    elif method and method.startswith('textDocument/') and 'id' in msg:
        if mode == 'never-definition' and method.endswith('definition'): continue
        if mode == 'delayed': time.sleep(.2)
        if method.endswith('hover'): result(msg, {'contents':{'kind':'markdown','value':'type café 😀'}})
        elif method.endswith('references'): result(msg, [])
        else: result(msg, {'uri':msg['params']['textDocument']['uri'],'range':{'start':{'line':0,'character':4},'end':{'line':0,'character':9}}})
"""


def make_project(root: Path) -> Path:
    source = root / "main.cpp"
    source.write_bytes(b"int value = 1;\r\n")
    (root / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "file": str(source),
                    "arguments": ["clang++", "-std=c++17", "-c", str(source)],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return source


def fake_factory(root: Path, mode: str = "normal"):
    peer = root / "peer.py"
    peer.write_text(PEER, encoding="utf-8")

    class PeerAdapter(ClangdAdapter):
        def _argv(self) -> list[str]:
            return [sys.executable, str(peer), mode, str(root / "received.jsonl")]

    def factory(program, snapshot, marker_store=None):
        return PeerAdapter(program, snapshot, marker_store or ProcessMarkerStore(root / "markers"))

    return factory


class PositionTests(unittest.TestCase):
    def test_mapped_resource_argument_preserves_database_and_query_driver_arguments(self):
        with tempfile.TemporaryDirectory(prefix="mapped resource ") as directory:
            root = Path(directory).resolve()
            make_project(root)
            snapshot = load_analysis_config(root)
            source = ResolvedAnalysisProgram("clangd", Path(sys.executable), origin="path")
            original = ClangdAdapter(source, snapshot)._argv()
            resource = root / "analysis/clangd/lib/clang/23"
            managed = ResolvedAnalysisProgram(
                "clangd",
                Path(sys.executable),
                origin="managed_runtime",
                data_paths=(resource,),
                runtime_root=root,
            )
            argv = ClangdAdapter(managed, snapshot)._argv()
            self.assertEqual(argv, original + ["--resource-dir=" + str(resource)])
            self.assertNotIn("--resource-dir=", " ".join(original))

    def test_round_trip_code_points_and_crlf(self):
        text = "é中😀e\u0301\tX\r\nlast\r\n"
        for encoding in ("utf-8", "utf-16", "utf-32"):
            for line, column in ((1, 1), (1, 4), (1, 8), (2, 5), (3, 1)):
                value = position_to_lsp(text, line, column, encoding)
                self.assertEqual(
                    position_from_lsp(text, value, encoding), {"line": line, "column": column}
                )
        with self.assertRaises(ValueError):
            position_from_lsp(text, {"line": 0, "character": 3}, "utf-16")
        with self.assertRaises(ValueError):
            position_to_lsp(text, 1, 9, "utf-8")

    def test_uri_decode_once_and_refuse_remote(self):
        with tempfile.TemporaryDirectory(prefix="analysis é 😀 ") as directory:
            path = Path(directory).resolve() / "percent%20.h"
            self.assertEqual(local_uri_path(path.as_uri()), path)
        self.assertIsNone(local_uri_path("https://example.org/header.h"))
        self.assertIsNone(local_uri_path("file://remote/path/header.h"))

    @unittest.skipUnless(os.name == "nt", "Windows drive URI semantics")
    def test_encoded_lowercase_drive_colon_is_readable_without_double_decoding(self):
        with tempfile.TemporaryDirectory(prefix="analysis drive é 😀 ") as directory:
            path = Path(directory).resolve() / "percent%20.h"
            path.write_text("/*😀*/ int value;\r\n", encoding="utf-8", newline="")
            uri = path.as_uri()
            prefix = "file:///" + path.drive
            encoded = uri.replace(prefix, "file:///" + path.drive[0].lower() + "%3A", 1)
            mapped = local_uri_path(encoded)
            self.assertIsNotNone(mapped)
            self.assertTrue(mapped.samefile(path))
            self.assertEqual(mapped.name, "percent%20.h")
            self.assertIsNone(local_uri_path("file:////server/share/header.h"))
            self.assertIsNone(local_uri_path("file://localhost//server/share/header.h"))
            self.assertIsNone(local_uri_path("file:///c%3A/dir/header.h?query"))


class TransportTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX receipt scope; Windows Jobs unchanged")
    def test_shutdown_retains_native_scope_receipt_before_clearing_process(self):
        process, marker = self.adapter.process, self.adapter.marker
        self.assertIsNone(self.adapter._last_cleanup_receipt)
        self.adapter.shutdown(time.monotonic() + 3)
        receipt = self.adapter._last_cleanup_receipt
        self.assertIsInstance(receipt, dict)
        self.assertEqual(receipt, process.cleanup_receipt)
        self.assertEqual(receipt["pid"], process.pid)
        self.assertEqual(receipt["coverage"], "retained_group_and_captured_descendants")
        self.assertIs(receipt["scope_confirmed"], True)
        self.assertEqual(receipt["unobserved_detached_descendants"], "not_proven")
        self.assertIs(receipt["leader_reaped"], True)
        self.assertEqual(receipt["uncertainties"], [])
        self.assertIsNone(self.adapter.process)
        self.assertIsNone(self.adapter.marker)
        self.assertFalse(marker.exists())
        self.assertFalse(any(thread.is_alive() for thread in self.adapter._threads))
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        process.cleanup_receipt["uncertainties"].append("late fixture mutation")
        try:
            self.assertEqual(self.adapter._last_cleanup_receipt["uncertainties"], [])
        finally:
            process.cleanup_receipt["uncertainties"].pop()
        self.adapter.shutdown(time.monotonic() + 3)
        self.assertEqual(
            self.adapter._last_cleanup_receipt, receipt, "Idempotent shutdown retains evidence"
        )

    @unittest.skipUnless(os.name == "posix", "POSIX receipt scope; Windows Jobs unchanged")
    def test_shutdown_refuses_corrupted_native_scope_without_dropping_marker(self):
        from pyocd_debug_mcp.adapters import clangd as adapter_module

        actual_cleanup = adapter_module.terminate_process_group
        for index, case in enumerate(("missing", "wrong-coverage", "false-scope", "string-scope")):
            with self.subTest(case=case):
                if index:
                    self.adapter = fake_factory(self.root)(
                        ResolvedAnalysisProgram(
                            "clangd", Path(sys.executable), "fake", "path", ("fake",)
                        ),
                        load_analysis_config(self.root),
                    )
                    self.adapter.start(time.monotonic() + 3)
                process, marker = self.adapter.process, self.adapter.marker
                original, faulty = [], []

                def corrupt_after_actual_cleanup(candidate, *args, **kwargs):
                    self.assertIs(candidate, process)
                    scope_confirmed = actual_cleanup(candidate, *args, **kwargs)
                    self.assertTrue(scope_confirmed, candidate.cleanup_receipt)
                    original.append(candidate.cleanup_receipt)
                    receipt = dict(candidate.cleanup_receipt)
                    if case == "missing":
                        receipt.pop("coverage")
                    elif case == "wrong-coverage":
                        receipt["coverage"] = "all_descendants"
                    elif case == "false-scope":
                        receipt["scope_confirmed"] = False
                    else:
                        receipt["scope_confirmed"] = "true"
                    candidate.cleanup_receipt = receipt
                    faulty.append(receipt)
                    return scope_confirmed

                try:
                    with patch.object(
                        adapter_module,
                        "terminate_process_group",
                        side_effect=corrupt_after_actual_cleanup,
                    ):
                        with self.assertRaises(ClangdFailure) as failure:
                            self.adapter.shutdown(time.monotonic() + 3)
                    self.assertEqual(failure.exception.code, "analysis/cleanup-failed")
                    self.assertEqual(
                        len(faulty), 1, "The actual native cleanup operation must be reached"
                    )
                    self.assertEqual(self.adapter._last_cleanup_receipt, faulty[0])
                    self.assertIs(self.adapter.process, process)
                    self.assertEqual(self.adapter.marker, marker)
                    self.assertTrue(marker.exists())
                    self.assertEqual(json.loads(marker.read_bytes())["pid"], process.pid)
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(process.pid, os.WNOHANG)
                finally:
                    # Restore only the actual receipt that preceded the injected
                    # corruption, then retire this fixture through real shutdown.
                    if original:
                        process.cleanup_receipt = original[0]
                    self.adapter.shutdown(time.monotonic() + 3)

    def test_wedged_stdin_bounds_cancellation_and_releases_other_waiter(self):
        process = self.adapter.process
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            started = time.monotonic()
            blocked = pool.submit(self.adapter.request, "test/wedge", {}, started + 0.3)
            while not any(
                json.loads(line).get("method") == "test/wedge"
                for line in (self.root / "received.jsonl").read_text(encoding="utf-8").splitlines()
            ):
                self.assertLess(time.monotonic() - started, 0.3)
                threading.Event().wait(0.005)
            self.adapter.notify("test/payload", {"text": "x" * 1024 * 1024})
            other = pool.submit(self.adapter.request, "test/delay", {}, started + 3)
            with self.assertRaises(TimeoutError):
                blocked.result(ARGUMENT_TIMEOUT_GRACE_SECONDS + 1)
            with self.assertRaises(ClangdFailure):
                other.result(ARGUMENT_TIMEOUT_GRACE_SECONDS)
        self.assertLess(time.monotonic() - started, ARGUMENT_TIMEOUT_GRACE_SECONDS + 0.3)
        self.assertIsNotNone(process.poll())
        self.assertFalse(list((self.root / "markers").glob("*.json")))

    def test_unconfirmed_late_cleanup_retains_marker_and_blocks_another_spawn(self):
        store = ProcessMarkerStore(self.root / "unconfirmed-markers")
        store.root.mkdir(parents=True, exist_ok=True)
        marker = store.root / "retained.json"
        marker.write_text("{}", encoding="utf-8")
        process = Mock(stdin=None, stdout=None, stderr=None)

        def delayed(*args, **kwargs):
            time.sleep(0.05)
            return process, marker

        with (
            patch.object(runtime, "_spawn_cleanup_failure", None),
            patch.object(runtime, "popen_owned", side_effect=delayed) as spawn,
            patch.object(runtime, "terminate_process_group", return_value=False),
        ):
            with self.assertRaises(TimeoutError):
                runtime.spawn_before_deadline(["fixture"], time.monotonic() + 0.01, None, store)
            with self.assertRaisesRegex(RuntimeError, "termination unconfirmed"):
                runtime.spawn_before_deadline(["another"], time.monotonic() + 1, None, store)
            self.assertEqual(spawn.call_count, 1)
            self.assertTrue(marker.exists())

    def test_spawn_budget_and_late_child_cleanup_before_new_spawn(self):
        store = ProcessMarkerStore(self.root / "late-markers")
        original = runtime.popen_owned
        spawned = []

        def delayed(*args, **kwargs):
            if not spawned:
                time.sleep(0.08)
            result = original(*args, **kwargs)
            spawned.append(result[0])
            return result

        with patch.object(runtime, "popen_owned", side_effect=delayed):
            with self.assertRaises(TimeoutError):
                runtime.spawn_before_deadline(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    time.monotonic() + 0.02,
                    None,
                    store,
                )
            process, marker = runtime.spawn_before_deadline(
                [sys.executable, "-c", "pass"], time.monotonic() + 2, None, store
            )
            process.wait(timeout=2)
            from pyocd_debug_mcp.kernel.processes import terminate_process_group

            self.assertTrue(terminate_process_group(process))
            store.remove(marker)
        self.assertIsNotNone(spawned[0].poll())
        self.assertFalse(list((self.root / "late-markers").glob("*.json")))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="analysis protocol ")
        self.root = Path(self.temp.name).resolve()
        make_project(self.root)
        self.adapter = fake_factory(self.root)(
            ResolvedAnalysisProgram("clangd", Path(sys.executable), "fake", "path", ("fake",)),
            load_analysis_config(self.root),
        )
        self.adapter.start(time.monotonic() + 3)

    def tearDown(self):
        self.adapter.shutdown(time.monotonic() + 3)
        self.assertFalse(list((self.root / "markers").glob("*.json")))
        self.temp.cleanup()

    def test_split_frames_unicode_out_of_order_and_server_edit_refusal(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            slow = pool.submit(
                self.adapter.request,
                "test/delay",
                {"seconds": 0.15, "value": "slow 😀"},
                time.monotonic() + 2,
            )
            fast = pool.submit(
                self.adapter.request,
                "test/delay",
                {"seconds": 0.01, "value": "fast é"},
                time.monotonic() + 2,
            )
            self.assertEqual(fast.result(2), "fast é")
            self.assertEqual(slow.result(2), "slow 😀")
        response = self.adapter.request("test/serverRequest", {}, time.monotonic() + 2)
        self.assertFalse(response["applied"])

    def test_timeout_terminates_generation_and_wakes_other_request(self):
        process = self.adapter.process
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            pending = pool.submit(
                self.adapter.request,
                "test/delay",
                {"seconds": 3, "value": "late"},
                time.monotonic() + 2,
            )
            time.sleep(0.03)
            with self.assertRaises(TimeoutError):
                self.adapter.request(
                    "test/delay", {"seconds": 3, "value": "timeout"}, time.monotonic() + 0.05
                )
            with self.assertRaises(ClangdFailure):
                pending.result(2)
        self.assertIsNotNone(process.poll())

    def test_crash_or_malformed_frame_releases_waiter(self):
        with self.assertRaises(ClangdFailure) as error:
            self.adapter.request("test/malformed", {}, time.monotonic() + 2)
        self.assertEqual(error.exception.code, "analysis/protocol-error")


if __name__ == "__main__":
    unittest.main()
