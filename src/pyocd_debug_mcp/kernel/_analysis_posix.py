"""Analysis-only POSIX lifetime ownership, with a retained unreaped group leader.

Normal Popen wait/communicate reaps the leader before callers clean descendants.
Here waitid(WNOWAIT) retains the exact child as a group-number anchor until
cleanup. Only observed PID/birth identities authorize detached-child signals.
Polling cannot prove that a child detached before its first observation; an
observed startup gap or inaccessible ownership observation stays unconfirmed.
"""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any

import psutil

_RETAINED: dict[int, AnalysisPopen] = {}
_RETAINED_GUARD = threading.Lock()


def _exit_status(pid: int) -> int | None:
    """Observe exact exit without releasing the child's PID/PGID anchor."""
    if sys.platform == "darwin":
        # Darwin's public 64-bit siginfo_t / waitid ABI, not Linux's union
        # layout: apple-oss-distributions/xnu bsd/sys/{signal,wait}.h.
        class Siginfo(ctypes.Structure):
            _fields_ = [
                ("signo", ctypes.c_int),
                ("error", ctypes.c_int),
                ("code", ctypes.c_int),
                ("pid", ctypes.c_int),
                ("uid", ctypes.c_uint),
                ("status", ctypes.c_int),
                ("address", ctypes.c_void_p),
                ("value", ctypes.c_void_p),
                ("band", ctypes.c_long),
                ("padding", ctypes.c_ulong * 7),
            ]

        waitid = ctypes.CDLL(None, use_errno=True).waitid
        waitid.argtypes = (ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Siginfo), ctypes.c_int)
        waitid.restype = ctypes.c_int
        result = Siginfo()
        # P_PID=1, WEXITED=4, WNOHANG=1, WNOWAIT=32 on Darwin.
        if waitid(1, pid, ctypes.byref(result), 4 | 1 | 32) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        observed_pid, code, status = result.pid, result.code, result.status
    else:
        result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is None:
            return None
        observed_pid, code, status = result.si_pid, result.si_code, result.si_status
    if observed_pid == 0:
        return None
    if observed_pid != pid or code not in {1, 2, 3}:
        raise RuntimeError("unexpected retained-child waitid result")
    return status if code == 1 else -status


def _process_state(pid: int, token: str) -> str:
    """Recycled identities retire the old child; they never authorize a signal."""
    try:
        process = psutil.Process(pid)  # Never reuse a cached Process birth time.
        if f"psutil:{process.create_time():.9f}" != token:
            return "reused"
        status = process.status()
        if status in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
            return "zombie"
        return "live"
    except psutil.ZombieProcess:
        return "unknown"  # A zombie with unreadable birth identity is not exit proof.
    except psutil.NoSuchProcess:
        return "gone"
    except (psutil.AccessDenied, OSError):
        return "unknown"


class AnalysisPopen(subprocess.Popen[Any]):
    """Popen-compatible analysis child; wait observes, cleanup finally reaps."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._ownership_guard = threading.Lock()
        self._cleanup_guard = threading.Lock()
        self._stop_observer = threading.Event()
        self._observer: threading.Thread | None = None
        self._leader_token: str | None = None
        self._captured: set[tuple[int, str]] = set()
        self._uncertainties: set[str] = set()
        self._wait_error: str | None = None
        self._reaped = False
        self.cleanup_receipt: dict[str, Any] = {}
        super().__init__(*args, **kwargs)
        with _RETAINED_GUARD:
            _RETAINED[self.pid] = self

    def _internal_poll(self, _deadstate: Any = None, **kwargs: Any) -> int | None:
        # Popen's destructor also calls this seam. It must never reap or invent
        # exit zero for ECHILD/permission errors.
        if self.returncode is not None or not hasattr(self, "pid"):
            return self.returncode
        if not self._waitpid_lock.acquire(False):
            return None
        try:
            self.returncode = _exit_status(self.pid)
        except InterruptedError:
            pass
        except (OSError, RuntimeError) as exc:
            self._wait_error = str(exc)
        finally:
            self._waitpid_lock.release()
        return self.returncode

    def _wait(self, timeout: float | None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            result = self._internal_poll()
            if result is not None:
                return result
            if self._wait_error is not None:
                raise RuntimeError("Owned child exit observation unavailable: " + self._wait_error)
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(
                0.005 if deadline is None else min(0.005, max(0, deadline - time.monotonic()))
            )

    def begin_ownership(self) -> None:
        """Called inside popen_owned's setup/cleanup boundary before markers."""
        try:
            process = psutil.Process(self.pid)
            self._leader_token = f"psutil:{process.create_time():.9f}"
        except psutil.Error as exc:
            raise RuntimeError("Owned analysis birth identity unavailable") from exc
        if os.getpgid(self.pid) != self.pid or os.getsid(self.pid) != self.pid:
            raise RuntimeError("Analysis child did not establish its owned session/group")
        self._captured.add((self.pid, self._leader_token))
        if self.poll() is not None:
            self._uncertainties.add("leader exited before initial descendant observation")
        self._capture(time.monotonic() + 0.5)

        def observe() -> None:
            while not self._stop_observer.wait(0.01):
                self._capture(time.monotonic() + 0.05)

        observer = threading.Thread(target=observe, name="analysis-owned-descendants", daemon=True)
        observer.start()
        self._observer = observer

    def _anchor_matches(self) -> bool:
        if self._reaped or self._leader_token is None or self._wait_error is not None:
            return False
        state = _process_state(self.pid, self._leader_token)
        if state not in {"live", "zombie"}:
            return False
        try:
            # WNOWAIT verifies the retained parent/child relationship as well as
            # the birth token. The still-unreaped child prevents numeric reuse.
            _exit_status(self.pid)
            return os.getpgid(self.pid) == self.pid and os.getsid(self.pid) == self.pid
        except (OSError, RuntimeError):
            return False

    def _capture(self, deadline: float) -> bool:
        if not self._ownership_guard.acquire(timeout=max(0, deadline - time.monotonic())):
            self._uncertainties.add("ownership observer exceeded its bounded phase")
            return False
        try:
            if not self._anchor_matches():
                self._uncertainties.add("retained group anchor unavailable")
                return False
            candidates: set[tuple[int, str]] = set()
            for pid in psutil.pids():
                if time.monotonic() >= deadline:
                    self._uncertainties.add("group observation exceeded its bounded phase")
                    return False
                try:
                    if os.getpgid(pid) == self.pid:
                        child = psutil.Process(pid)
                        token = f"psutil:{child.create_time():.9f}"
                        if os.getpgid(pid) == self.pid:
                            state = _process_state(pid, token)
                            if state in {"live", "zombie", "unknown"}:
                                candidates.add((pid, token))
                            if state == "unknown":
                                self._uncertainties.add("owned process liveness unavailable")
                except ProcessLookupError:
                    continue
                except psutil.NoSuchProcess:
                    continue
                except (psutil.AccessDenied, OSError):
                    self._uncertainties.add("process group membership unavailable")
            for pid, token in tuple(self._captured):
                if time.monotonic() >= deadline:
                    self._uncertainties.add("descendant observation exceeded its bounded phase")
                    return False
                if _process_state(pid, token) != "live":
                    continue
                try:
                    parent = psutil.Process(pid)
                    if f"psutil:{parent.create_time():.9f}" != token:
                        continue
                    children = {
                        (child.pid, f"psutil:{child.create_time():.9f}")
                        for child in parent.children(recursive=True)
                    }
                    if _process_state(pid, token) in {"live", "zombie"}:
                        candidates.update(children)
                    else:
                        self._uncertainties.add(
                            "parent identity lost during descendant observation"
                        )
                except psutil.NoSuchProcess:
                    continue
                except (psutil.AccessDenied, OSError):
                    self._uncertainties.add("descendant identities unavailable")
            # Retain the identity observed with membership/ancestry, never a
            # later birth token fetched from a possibly recycled candidate PID.
            self._captured.update(candidates)
            return True
        except (psutil.Error, OSError) as exc:
            self._uncertainties.add(f"ownership observation failed: {type(exc).__name__}")
            return False
        finally:
            self._ownership_guard.release()

    def _states(self, deadline: float) -> dict[tuple[int, str], str]:
        if not self._ownership_guard.acquire(timeout=max(0, deadline - time.monotonic())):
            self._uncertainties.add("owned states unavailable within cleanup reserve")
            return {(self.pid, self._leader_token or "unobserved"): "unknown"}
        try:
            states = {}
            for identity in self._captured:
                states[identity] = (
                    _process_state(*identity) if time.monotonic() < deadline else "unknown"
                )
            return states
        finally:
            self._ownership_guard.release()

    def _signal_owned(self, selected_signal: int, deadline: float) -> None:
        if time.monotonic() >= deadline:
            self._uncertainties.add("owned signal phase exceeded cleanup reserve")
            return
        if self._anchor_matches():
            try:
                os.killpg(self.pid, selected_signal)
            except ProcessLookupError:
                pass
            except OSError:
                self._uncertainties.add("owned group signal unavailable")
        else:
            self._uncertainties.add("group signal refused without retained exact anchor")
        for (pid, token), state in self._states(deadline).items():
            if time.monotonic() >= deadline:
                self._uncertainties.add("descendant signal phase exceeded cleanup reserve")
                return
            if state != "live":
                continue
            try:
                process = psutil.Process(pid)
                if f"psutil:{process.create_time():.9f}" == token:
                    # psutil additionally checks reuse immediately before kill.
                    process.send_signal(selected_signal)
            except psutil.NoSuchProcess:
                pass
            except (psutil.AccessDenied, OSError):
                self._uncertainties.add("owned descendant signal unavailable")

    def cleanup(self, grace_seconds: float) -> bool:
        started = time.monotonic()
        deadline = started + 3 * grace_seconds
        if not self._cleanup_guard.acquire(timeout=max(0, deadline - time.monotonic())):
            return False
        try:
            if self._reaped:
                return bool(self.cleanup_receipt.get("confirmed"))
            self._capture(deadline)
            for selected_signal in (signal.SIGTERM, signal.SIGKILL):
                self._signal_owned(selected_signal, deadline)
                phase_end = min(deadline, time.monotonic() + grace_seconds)
                while time.monotonic() < phase_end:
                    self._capture(phase_end)
                    states = self._states(phase_end)
                    if self.poll() is not None and not any(
                        s in {"live", "unknown"} for s in states.values()
                    ):
                        break
                    time.sleep(min(0.01, max(0, phase_end - time.monotonic())))
            self._stop_observer.set()
            if self._observer is not None:
                self._observer.join(timeout=max(0, deadline - time.monotonic()))
                if self._observer.is_alive():
                    self._uncertainties.add(
                        "descendant observer did not stop within cleanup reserve"
                    )
            complete_snapshot = self._capture(deadline)
            states = self._states(deadline)
            retired = (
                complete_snapshot
                and self.poll() is not None
                and not any(s in {"live", "unknown"} for s in states.values())
                and not self._uncertainties
                and self._wait_error is None
            )
            # Never release a PGID anchor while a known process is live/unknown.
            # Failures retain both the Popen and recovery marker for review.
            if retired:
                try:
                    if not self._waitpid_lock.acquire(timeout=max(0, deadline - time.monotonic())):
                        raise TimeoutError("leader reap lock exceeded cleanup reserve")
                    try:
                        pid, status = os.waitpid(self.pid, os.WNOHANG)
                        if pid == self.pid:
                            observed = os.waitstatus_to_exitcode(status)
                            if observed != self.returncode:
                                self._uncertainties.add("retained/reaped exit status disagreement")
                            self._reaped = True
                        else:
                            self._uncertainties.add("retained leader did not reap")
                    finally:
                        self._waitpid_lock.release()
                except OSError as exc:
                    self._uncertainties.add(
                        "leader reap unavailable: " + errno.errorcode.get(exc.errno or 0, "unknown")
                    )
            confirmed = (
                retired and self._reaped and not self._uncertainties and self._wait_error is None
            )
            self.cleanup_receipt = {
                "pid": self.pid,
                "start_token": self._leader_token,
                "group": self.pid,
                "captured": [
                    {"pid": pid, "start_token": token, "state": state}
                    for (pid, token), state in sorted(states.items())
                ],
                "leader_exit": self.returncode,
                "leader_reaped": self._reaped,
                "confirmed": confirmed,
                "coverage": "retained_group_and_captured_descendants",
                "unobserved_detached_descendants": "not_proven",
                "uncertainties": sorted(self._uncertainties.copy()),
                "wait_error": self._wait_error,
                "elapsed_seconds": time.monotonic() - started,
                "budget_seconds": 3 * grace_seconds,
            }
            if confirmed:
                with _RETAINED_GUARD:
                    _RETAINED.pop(self.pid, None)
            return confirmed
        finally:
            self._cleanup_guard.release()
