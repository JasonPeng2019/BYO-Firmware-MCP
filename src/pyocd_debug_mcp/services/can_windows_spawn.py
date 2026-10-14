# SPDX-License-Identifier: PSF-2.0
# Adapted from CPython Lib/multiprocessing/popen_spawn_win32.py.
# Copyright (c) 2001-2026 Python Software Foundation. See licenses/Python.txt.
"""Windows CAN children communicate only over owned multiprocessing pipes.

Inheriting an MCP stdin pipe with an outstanding synchronous read can deadlock
CPython child initialization before it consumes the spawn payload (CPython #78961).
Use explicit null standard handles, without changing process-global stdio or
multiprocessing settings. This module is imported only on Windows.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from multiprocessing import spawn, util
from typing import Any

# Private, platform-specific stdlib APIs have no portable typing declarations.
context: Any = importlib.import_module("multiprocessing.context")
win_spawn: Any = importlib.import_module("multiprocessing.popen_spawn_win32")
winapi: Any = importlib.import_module("_winapi")
msvcrt: Any = importlib.import_module("msvcrt")
windows_subprocess: Any = subprocess


def _close_handles(handles: list[int]) -> None:
    while handles:
        winapi.CloseHandle(handles.pop())


class _CanPopen(win_spawn.Popen):
    def __init__(self, process_obj: Any) -> None:
        preparation = spawn.get_preparation_data(process_obj.name)
        reader, writer = winapi.CreatePipe(None, 0)
        try:
            descriptor = msvcrt.open_osfhandle(writer, 0)
        except BaseException:
            winapi.CloseHandle(reader)
            winapi.CloseHandle(writer)
            raise
        try:
            bootstrap = open(descriptor, "wb", closefd=True)
        except BaseException:
            os.close(descriptor)
            winapi.CloseHandle(reader)
            raise
        owned_handles = [reader]
        try:
            self.finalizer = util.Finalize(self, _close_handles, (owned_handles,))
        except BaseException:
            bootstrap.close()
            _close_handles(owned_handles)
            raise

        native_created = False
        try:
            with bootstrap:
                executable = spawn.get_executable()
                command = spawn.get_command_line(parent_pid=os.getpid(), pipe_handle=reader)
                environment = None
                if win_spawn.WINENV and win_spawn._path_eq(executable, sys.executable):
                    executable = getattr(sys, "_base_executable")
                    command[0] = executable
                    environment = os.environ.copy()
                    environment["__PYVENV_LAUNCHER__"] = sys.executable
                startup = windows_subprocess.STARTUPINFO()
                startup.dwFlags = windows_subprocess.STARTF_USESTDHANDLES
                startup.hStdInput = startup.hStdOutput = startup.hStdError = 0
                handle, thread, pid, _ = winapi.CreateProcess(
                    executable,
                    subprocess.list2cmdline(command),
                    None,
                    None,
                    False,
                    windows_subprocess.CREATE_NO_WINDOW,
                    environment,
                    None,
                    startup,
                )
                owned_handles.extend((handle, thread))
                self.pid = pid
                self.returncode = None
                self._handle = handle
                self.sentinel = int(handle)
                native_created = True
                try:
                    winapi.CloseHandle(thread)
                    owned_handles.remove(thread)
                    context.set_spawning_popen(self)
                    try:
                        context.reduction.dump(preparation, bootstrap)
                        context.reduction.dump(process_obj, bootstrap)
                    finally:
                        context.set_spawning_popen(None)
                finally:
                    # Publish even on post-create failure so the launch owner
                    # can retire the child. Publishing before serialization
                    # would put live parent handles into the Process payload.
                    process_obj._popen = self
                    process_obj._sentinel = self.sentinel
        except BaseException:
            if not native_created:
                self.finalizer()
            raise


class CanSpawnProcess(context.SpawnProcess):
    @staticmethod
    def _Popen(process_obj: Any) -> _CanPopen:
        return _CanPopen(process_obj)


class CanSpawnContext(context.SpawnContext):
    Process = CanSpawnProcess
