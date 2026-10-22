"""Bounded, reentrant state-file transactions shared by cooperating servers.

Locks live in persistent sidecars: replacing or removing a data file must never
replace its lock inode. POSIX flock is advisory, so every writer must use this
helper. Closing the descriptor (including process death) releases the OS lock.
"""

from __future__ import annotations

import errno
import os
import threading
import time
import weakref
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Generator


class FileTransactionTimeout(TimeoutError):
    """A state transaction is busy; callers may retry without losing an update."""


class _LockState:
    def __init__(self) -> None:
        self.local = threading.RLock()
        self.descriptor: int | None = None


_states: weakref.WeakValueDictionary[str, _LockState] = weakref.WeakValueDictionary()
_states_guard = threading.Lock()


def _after_fork() -> None:
    # A fork must neither inherit a locked thread mutex nor keep its parent's
    # flock alive. Close only: LOCK_UN would also unlock the parent's descriptor.
    global _states, _states_guard
    for state in list(_states.values()):
        if state.descriptor is not None:
            os.close(state.descriptor)
    _states = weakref.WeakValueDictionary()
    _states_guard = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _try_lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


class _FileTransaction(AbstractContextManager[None]):
    def __init__(self, acquisition: Generator[None, None, None]) -> None:
        self._acquisition = acquisition

    def __enter__(self) -> None:
        next(self._acquisition)

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        # Cleanup needs no access to the caller's exception. In particular,
        # generator contextlib.__exit__ would assign __traceback__ on frozen
        # routing refusals and replace them with TypeError.
        self._acquisition.close()
        return False


def file_transaction(path: Path, *, timeout_seconds: float = 5.0) -> AbstractContextManager[None]:
    """Hold a file's transaction lock, waiting at most five seconds by default.

    Acquire this before reading, then re-read/merge/write while held. The shared
    local RLock permits nested calls and separate repository objects in one
    thread. The deadline covers local and OS contention; failure changes no data.
    Callers must keep network/hardware work outside and use a consistent order
    if they need other locks. Sidecars must not be deleted while servers run.
    """

    return _FileTransaction(_acquire_transaction(path, timeout_seconds=timeout_seconds))


def _acquire_transaction(path: Path, *, timeout_seconds: float) -> Generator[None, None, None]:
    if timeout_seconds < 0:
        raise ValueError("transaction timeout must be nonnegative")
    target = Path(path).expanduser().resolve()
    lock_path = target.with_name(f".{target.name}.lock")
    with _states_guard:
        key = os.path.normcase(str(lock_path))
        state = _states.get(key)
        if state is None:
            state = _LockState()
            _states[key] = state

    def busy() -> FileTransactionTimeout:
        return FileTransactionTimeout(
            f"State transaction busy for {target.name}; retry after the other server finishes"
        )

    deadline = time.monotonic() + timeout_seconds
    owner_pid = os.getpid()
    if not state.local.acquire(timeout=timeout_seconds):
        raise busy()
    try:
        if state.descriptor is not None:
            yield
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        state.descriptor = descriptor
        acquired = False
        try:
            while True:
                try:
                    _try_lock(descriptor)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise busy() from exc
                    time.sleep(min(0.05, remaining))
            yield
        finally:
            # The at-fork hook already closed the child's inherited handle.
            # Its number may now name an unrelated child file. Inherited
            # generators may still unwind: only the acquiring process owns
            # descriptor/mutex cleanup, and body exceptions must propagate.
            if os.getpid() == owner_pid:
                state.descriptor = None
                try:
                    if acquired:
                        _unlock(descriptor)
                finally:
                    os.close(descriptor)
    finally:
        if os.getpid() == owner_pid:
            state.local.release()
