"""Independent Windows acceptance contracts for the clangd code-analysis server.

These tests are designed from the public contract rather than the owner's
regressions: a real ARM query driver must change what clangd can see, a
database edit alone must change diagnostics, live generations are tracked by
held native process handles, and every product cleanup claim is checked
before any test-owned emergency cleanup runs (which is recorded separately).
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from typing import Any, Iterator

from pyocd_debug_mcp.adapters.clangd import ClangdAdapter, ClangdFailure
from pyocd_debug_mcp.code_analysis_config import load_analysis_config
from pyocd_debug_mcp.kernel.operations import ARGUMENT_TIMEOUT_GRACE_SECONDS
from pyocd_debug_mcp.kernel.processes import ProcessMarkerStore
from tests.code_analysis_support import make_program, simple_project

FIXTURES = Path(__file__).resolve().parent / "code_analysis_fixtures" / "windows-acceptance"
UNREAD_STDIN_PEER = FIXTURES / "unread_stdin_peer.py"
EVIDENCE_ENV = "BYO_WINDOWS_ACCEPTANCE_EVIDENCE"
SCRATCH_ENV = "BYO_WINDOWS_ACCEPTANCE_SCRATCH"


def record(name: str, data: dict[str, Any]) -> None:
    if directory := os.environ.get(EVIDENCE_ENV):
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / (name + ".json")).write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )


@contextlib.contextmanager
def scratch(prefix: str, *, same_volume_as: Path | None = None) -> Iterator[Path]:
    """Keep relative ARM paths on the real compiler's volume, without relocating it."""
    parent = os.environ.get(SCRATCH_ENV)
    if same_volume_as is not None:
        anchor = same_volume_as.resolve(strict=True).anchor
        if parent:
            if Path(parent).resolve(strict=True).anchor.casefold() != anchor.casefold():
                raise AssertionError(f"{SCRATCH_ENV} must be on the ARM compiler volume {anchor}")
        else:
            short = Path(anchor) / "t"
            parent = str(short if short.is_dir() else Path(anchor))
    elif not parent:
        parent = "C:\\t" if Path("C:\\t").is_dir() else None
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=parent)).resolve()
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        record(
            "scratch-" + directory.name + "-cleanup",
            {
                "owned_scratch": str(directory),
                "survived_cleanup": directory.exists(),
                "compiler_volume": str(same_volume_as) if same_volume_as else None,
                "scope": "Test-owned directory only; acquired toolchain was not relocated or removed",
            },
        )


class HeldProcess:
    """A native handle held from first observation, so PID reuse cannot mask survival."""

    _SYNCHRONIZE = 0x00100000
    _QUERY_LIMITED = 0x1000
    _TERMINATE = 0x0001

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self._k32.OpenProcess.restype = wintypes.HANDLE
        self._k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (
            ctypes.POINTER(wintypes.FILETIME),
        ) * 4
        self._k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        self._k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self._k32.WaitForSingleObject.restype = wintypes.DWORD
        self._k32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self._k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self._k32.OpenProcess(
            self._SYNCHRONIZE | self._QUERY_LIMITED | self._TERMINATE, False, pid
        )
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        times = [wintypes.FILETIME() for _ in range(4)]
        if not self._k32.GetProcessTimes(self.handle, *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.creation = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not self._k32.QueryFullProcessImageNameW(self.handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.image = buffer.value
        self.emergency_terminated = False
        self.fault_injected: int | None = None

    def crash(self, code: int) -> None:
        """Fault injection: the child dies on its own, outside any product cleanup."""
        self.fault_injected = code
        if not self._k32.TerminateProcess(self.handle, code):
            raise ctypes.WinError(ctypes.get_last_error())
        if self.alive(5):
            raise AssertionError("injected crash was not confirmed")

    def alive(self, wait_seconds: float = 0) -> bool:
        result = self._k32.WaitForSingleObject(self.handle, int(wait_seconds * 1000))
        if result not in (0, 258):
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 258

    def exit_code(self) -> int | None:
        code = wintypes.DWORD()
        if not self._k32.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return None if code.value == 259 else code.value

    def evidence(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "creation_filetime": self.creation,
            "image": self.image,
            "alive": self.alive(),
            "exit_code": self.exit_code(),
        }

    def release(self) -> dict[str, Any]:
        """Close the handle; terminate first only as emergency test cleanup."""
        observed = self.evidence()
        if observed["alive"]:
            self.emergency_terminated = True
            self._k32.TerminateProcess(self.handle, 97)
            observed["emergency_wait_confirmed"] = not self.alive(5)
        self._k32.CloseHandle(self.handle)
        observed["emergency_terminated"] = self.emergency_terminated
        return observed


def thread_snapshot(adapter: ClangdAdapter) -> dict[str, bool]:
    return {thread.name: thread.is_alive() for thread in adapter._threads}


def wait_until(predicate: Any, timeout: float, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class UnreadStdinAdapter(ClangdAdapter):
    ready_file: Path

    def _argv(self) -> list[str]:
        return [sys.executable, str(UNREAD_STDIN_PEER), str(self.ready_file)]


@unittest.skipUnless(os.name == "nt", "Windows synchronous pipe writes")
class UnreadStdinCancellation(unittest.TestCase):
    def start_blocked(self, base: Path) -> tuple[UnreadStdinAdapter, HeldProcess, Path]:
        project = simple_project(base / "p")
        adapter = UnreadStdinAdapter(
            make_program(Path(sys.executable)),
            load_analysis_config(project.root),
            ProcessMarkerStore(base / "markers"),
        )
        adapter.ready_file = base / "ready.txt"
        adapter.start(time.monotonic() + 20)
        self.addCleanup(lambda: adapter.shutdown(time.monotonic() + 5))
        assert adapter.process is not None and adapter.marker is not None
        self.assertTrue(wait_until(adapter.ready_file.exists, 10), "peer never became ready")
        identity = HeldProcess(adapter.process.pid)
        # 32 MiB cannot fit the pipe; a sentinel behind it proves the writer is
        # stuck inside the write, not idle.
        adapter.notify("acceptance/fill", {"blob": "x" * (32 << 20)})
        adapter.notify("acceptance/sentinel", {})
        self.assertFalse(wait_until(lambda: adapter._outgoing.qsize() == 0, 0.4))
        self.assertTrue(thread_snapshot(adapter)["clangd-writer"])
        return adapter, identity, adapter.marker

    def run_blocked(self, label: str, *, deadline_after: float, cancel_after: float | None) -> None:
        with scratch("sa-us-") as base:
            adapter, identity, marker = self.start_blocked(base)
            outcomes: dict[str, Any] = {}
            cancel = threading.Event()

            def call(name: str, deadline: float, cancellation: Any = None) -> None:
                try:
                    adapter.request("acceptance/" + name, {}, deadline, cancellation)
                    outcomes[name] = ("returned", time.monotonic())
                except BaseException as exc:  # noqa: BLE001 - outcome is the assertion
                    outcomes[name] = (type(exc).__name__, time.monotonic(), str(exc))

            data: dict[str, Any] = {"label": label}
            try:
                started = time.monotonic()
                sibling = threading.Thread(target=call, args=("sibling", started + 60))
                sibling.start()
                expire = started + deadline_after
                primary = threading.Thread(
                    target=call,
                    args=("primary", expire, cancel if cancel_after is not None else None),
                )
                primary.start()
                if cancel_after is not None:
                    time.sleep(cancel_after)
                    cancel.set()
                    expire = time.monotonic()
                bound = ARGUMENT_TIMEOUT_GRACE_SECONDS + 1.0
                primary.join(bound + deadline_after)
                sibling.join(bound)
                data.update(
                    outcomes={k: [v[0], v[1] - started, *v[2:]] for k, v in outcomes.items()},
                    expire_offset=expire - started,
                    primary_alive=primary.is_alive(),
                    sibling_alive=sibling.is_alive(),
                    threads=thread_snapshot(adapter),
                    process=identity.evidence(),
                    marker_exists=marker.exists(),
                    adapter_process_cleared=adapter.process is None,
                    pending=len(adapter._pending),
                )
                self.assertFalse(data["primary_alive"], data)
                self.assertFalse(data["sibling_alive"], data)
                kind, primary_at = outcomes["primary"][:2]
                self.assertEqual(kind, "TimeoutError", data)
                # Not early: the primary expires at its own deadline/cancellation...
                self.assertGreaterEqual(primary_at, expire - 0.05, data)
                # ...and the blocked writer cannot extend it past the bounded grace.
                self.assertLess(primary_at - expire, bound, data)
                sibling_kind, sibling_at = outcomes["sibling"][:2]
                self.assertEqual(sibling_kind, "ClangdFailure", data)
                self.assertLess(sibling_at - expire, bound, data)
                self.assertFalse(any(data["threads"].values()), data)
                self.assertFalse(data["process"]["alive"], data)
                self.assertFalse(data["marker_exists"], data)
                self.assertTrue(data["adapter_process_cleared"], data)
                self.assertEqual(data["pending"], 0, data)
                with self.assertRaises(ClangdFailure):
                    adapter.request("acceptance/after", {}, time.monotonic() + 1)
            finally:
                data["emergency_cleanup"] = identity.release()
                record("unread-stdin-" + label, data)
            self.assertFalse(identity.emergency_terminated, data)

    def test_deadline_expiry_bounds_blocked_writer_and_releases_sibling(self) -> None:
        self.run_blocked("deadline", deadline_after=0.6, cancel_after=None)

    def test_cancellation_bounds_blocked_writer_and_releases_sibling(self) -> None:
        self.run_blocked("cancel", deadline_after=60, cancel_after=0.5)


if __name__ == "__main__":
    unittest.main()
