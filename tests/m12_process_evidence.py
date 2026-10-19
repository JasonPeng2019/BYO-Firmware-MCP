"""Read-only exact Windows process handles retained for truthful child exits."""

import ctypes
from ctypes import wintypes

import psutil

from pyocd_debug_mcp.kernel.processes import _start_token, identity_matches


class ChildExits:
    def __init__(self):
        self.rows = []
        self.handles = []
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)

    def capture(self, parent_pid, role):
        for process in psutil.Process(parent_pid).children(recursive=True):
            token = _start_token(process.pid)
            assert token
            if any(r["pid"] == process.pid and r["token"] == token for r in self.rows):
                continue
            handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, process.pid)
            assert handle, (process.pid, ctypes.get_last_error())
            assert identity_matches(process.pid, token)
            self.rows.append(
                dict(
                    pid=process.pid,
                    token=token,
                    parent_pid=parent_pid,
                    role=role,
                    argv=process.cmdline(),
                )
            )
            self.handles.append(handle)

    def reap(self):
        for row, handle in zip(self.rows, self.handles):
            try:
                assert self.kernel.WaitForSingleObject(handle, 10000) == 0, row
                code = wintypes.DWORD()
                assert self.kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                row.update(
                    raw_exit=code.value,
                    exact_identity_absent=not identity_matches(row["pid"], row["token"]),
                )
                assert row["exact_identity_absent"]
            finally:
                self.kernel.CloseHandle(handle)
        return self.rows
