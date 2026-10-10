"""Analysis-only POSIX lifetime ownership, with a retained unreaped group leader.

Normal Popen wait/communicate reaps the leader before callers clean descendants.
Here waitid(WNOWAIT) retains the exact child as a group-number anchor until
cleanup. Only observed PID/birth identities authorize detached-child signals.
Polling cannot prove that a child detached before its first observation.
Scoped proof covers the original group and exactly captured descendants;
inaccessible observed ownership stays unconfirmed.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from _thread import LockType
from typing import Any

import psutil

_RETAINED: dict[int, AnalysisPopen] = {}
_RETAINED_GUARD = threading.Lock()
_LOG = logging.getLogger(__name__)


class _ObservationDeadline(TimeoutError):
    """An incomplete internal phase, retriable within the total cleanup reserve."""


def analysis_cleanup_scope_confirmed(receipt: object) -> bool:
    """Validate the private POSIX analysis scope, never universal containment."""
    return (
        isinstance(receipt, dict)
        and receipt.get("coverage") == "retained_group_and_captured_descendants"
        and receipt.get("scope_confirmed") is True
        and receipt.get("unobserved_detached_descendants") == "not_proven"
        and receipt.get("leader_reaped") is True
        and type(receipt.get("leader_exit")) is int
        and receipt.get("uncertainties") == []
        and receipt.get("wait_error") is None
    )


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
    elif sys.platform != "win32":
        result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is None:
            return None
        observed_pid, code, status = result.si_pid, result.si_code, result.si_status
    else:
        raise RuntimeError("Retained analysis ownership requires POSIX waitid")
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


def _darwin_group_members(group: int, deadline: float) -> set[int]:
    """Kernel-filtered membership includes zombies; errors never mean empty."""
    if sys.platform != "darwin":
        raise RuntimeError("Darwin group membership requires libproc")
    # Public libproc proc_listpids(PROC_PGRP_ONLY=2) returns bytes. XNU
    # proc_info.c filters both allproc and zombproc under the process-list lock.
    listpids = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_listpids
    listpids.argtypes = (ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int)
    listpids.restype = ctypes.c_int
    capacity = 32
    while time.monotonic() < deadline and capacity <= 65536:
        buffer = (ctypes.c_int * capacity)()
        size = ctypes.sizeof(buffer)
        ctypes.set_errno(0)
        used = listpids(2, group, buffer, size)
        if used <= 0 or used > size or used % ctypes.sizeof(ctypes.c_int):
            error = ctypes.get_errno()
            raise OSError(error, "Darwin group membership unavailable")
        if used < size:
            members = set(buffer[: used // ctypes.sizeof(ctypes.c_int)])
            if any(pid <= 0 for pid in members) or group not in members:
                raise RuntimeError("Darwin group snapshot lacks retained leader")
            return members
        capacity *= 2  # A full buffer may have omitted members; retry boundedly.
    if time.monotonic() >= deadline:
        raise _ObservationDeadline("Darwin complete group snapshot exceeded bounded phase")
    raise RuntimeError("Darwin complete group snapshot exceeded member limit")


def _descendant_chain(
    candidate: int, parent: tuple[int, str], deadline: float
) -> set[tuple[int, str]]:
    """Treat a children() result only as a proposal; acquire fresh ancestry."""
    chain: dict[int, tuple[str, int]] = {}
    current = candidate
    while current != parent[0]:
        if time.monotonic() >= deadline:
            raise _ObservationDeadline("Exact descendant ancestry exceeded bounded phase")
        if current <= 1 or current in chain:
            return set()  # Stale PPID proposal or ancestry outside this owner.
        process = psutil.Process(current)
        token = f"psutil:{process.create_time():.9f}"
        ppid = process.ppid()
        state = _process_state(current, token)
        if state == "unknown":
            raise RuntimeError("Descendant birth/liveness unavailable at acquisition")
        if state not in {"live", "zombie"}:
            return set()
        chain[current] = (token, ppid)
        current = ppid
    # Recheck every exact child and PPID edge after reaching the known parent's
    # identity. New birth tokens from recycled unrelated proposals never grant
    # ownership merely because a previous global PPID map named their PID.
    if _process_state(*parent) not in {"live", "zombie"}:
        raise RuntimeError("Owned parent identity lost during ancestry acquisition")
    for pid, (token, ppid) in chain.items():
        if time.monotonic() >= deadline:
            raise _ObservationDeadline("Exact descendant ancestry exceeded bounded phase")
        process = psutil.Process(pid)
        if (
            f"psutil:{process.create_time():.9f}" != token
            or process.ppid() != ppid
            or _process_state(pid, token) not in {"live", "zombie"}
        ):
            raise RuntimeError("Exact descendant relationship changed during acquisition")
    if _process_state(*parent) not in {"live", "zombie"}:
        raise RuntimeError("Owned parent identity lost during ancestry acquisition")
    return {(pid, token) for pid, (token, _) in chain.items()}


class AnalysisPopen(subprocess.Popen[Any]):
    """Popen-compatible analysis child; wait observes, cleanup finally reaps."""

    # CPython Popen initializes this real lock before its POSIX child spawn.
    # It is private and therefore absent from the public subprocess stub.
    _waitpid_lock: LockType

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("start_new_session") is not True:
            raise ValueError("AnalysisPopen requires explicit start_new_session=True")
        self._ownership_guard = threading.Lock()
        self._cleanup_guard = threading.Lock()
        self._stop_observer = threading.Event()
        self._observer: threading.Thread | None = None
        self._leader_token: str | None = None
        self._spawn_parent_pid = os.getpid()
        self._spawn_succeeded = False
        self._session_established = False
        self._captured: set[tuple[int, str]] = set()
        self._uncertainties: set[str] = set()
        self._first_observation_failure: dict[str, Any] | None = None
        self._wait_error: str | None = None
        self._reaped = False
        self.cleanup_receipt: dict[str, Any] = {}
        self._terminal_group_receipt: list[dict[str, Any]] | None = None
        self._group_signal_operations: list[dict[str, Any]] = []
        self._incomplete_observation_count = 0
        self._last_incomplete_observation: str | None = None
        self._early_direct_child_exit = False
        super().__init__(*args, **kwargs)
        # Successful Popen returned after its child setup/exec error pipe:
        # start_new_session=True performed setsid or construction raised. This
        # exact direct child remains unreaped by our WNOWAIT wait/poll seams.
        self._spawn_succeeded = True
        self._session_established = True
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
            if timeout is not None and deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(
                0.005 if deadline is None else min(0.005, max(0, deadline - time.monotonic()))
            )

    def begin_ownership(self) -> None:
        """Called inside popen_owned's setup/cleanup boundary before markers."""
        if sys.platform == "win32":
            raise RuntimeError("AnalysisPopen requires POSIX process ownership")
        initial_state = self._leader_state()
        if initial_state == "unknown":
            raise RuntimeError("Owned analysis direct-child observation unavailable")
        if initial_state == "zombie":
            self._early_direct_child_exit = True
        try:
            process = psutil.Process(self.pid)
            self._leader_token = f"psutil:{process.create_time():.9f}"
        except (psutil.Error, OSError):
            # Retained-child proof is independent of a zombie's live metadata.
            # Missing birth identity still cannot authorize descendant ancestry.
            if self._leader_state() != "zombie":
                self._uncertainties.add("leader birth unavailable for descendant acquisition")
        if self._leader_token is not None:
            self._captured.add((self.pid, self._leader_token))
        before_observation = self._leader_state()
        if before_observation == "unknown":
            raise RuntimeError("Owned analysis direct-child observation unavailable")
        if before_observation == "zombie":
            self._early_direct_child_exit = True
        self._capture(time.monotonic() + 0.5)

        def observe() -> None:
            while not self._stop_observer.wait(0.01):
                self._capture(time.monotonic() + 0.05)

        observer = threading.Thread(target=observe, name="analysis-owned-descendants", daemon=True)
        observer.start()
        self._observer = observer

    def _leader_state(self) -> str:
        """Observe our actual spawned direct child, never a replacement PID."""
        if sys.platform == "win32":
            return "unknown"
        if (
            self._reaped
            or os.getpid() != self._spawn_parent_pid
            or not self._spawn_succeeded
            or not self._session_established
            or self._wait_error is not None
        ):
            return "unknown"
        try:
            # Fresh waitid success verifies the direct parent/child relationship;
            # ECHILD or an inaccessible observation grants no PID authority.
            # WNOWAIT retains the PID even when live metadata is unavailable.
            exited = _exit_status(self.pid)
            if exited is not None:
                if self.returncode is not None and self.returncode != exited:
                    raise RuntimeError("Retained direct-child exit status changed")
                self.returncode = exited
                return "zombie"
            return "live"
        except (OSError, RuntimeError) as exc:
            if self._first_observation_failure is None:
                self._first_observation_failure = {
                    "operation": "retained direct-child waitid",
                    "pid": self.pid,
                    "error": type(exc).__name__,
                    "errno": getattr(exc, "errno", None),
                }
            self._wait_error = str(exc)
            return "unknown"

    def _anchor_matches(self) -> bool:
        # Successful setsid made this actual direct child an immutable session
        # leader. Fresh retained-child observation prevents recycled PID/PGID
        # authority without requiring live-only getpgid/getsid or image data.
        return self._leader_state() in {"live", "zombie"}

    def _note_incomplete_observation(self, operation: str) -> None:
        self._incomplete_observation_count += 1
        self._last_incomplete_observation = operation

    def _capture(self, deadline: float) -> bool:
        if sys.platform == "win32":
            return False
        if not self._ownership_guard.acquire(timeout=max(0, deadline - time.monotonic())):
            self._note_incomplete_observation("ownership observer exceeded its bounded phase")
            return False
        candidates: set[tuple[int, str]] = set()
        try:
            if not self._anchor_matches():
                self._uncertainties.add("retained group anchor unavailable")
                return False
            group_candidates = (
                _darwin_group_members(self.pid, deadline)
                if sys.platform == "darwin"
                else psutil.pids()
            )
            for pid in group_candidates:
                if time.monotonic() >= deadline:
                    self._note_incomplete_observation(
                        "group observation exceeded its bounded phase"
                    )
                    return False
                try:
                    if pid == self.pid:
                        continue  # Direct-child ownership comes from spawn/waitid.
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
                except (psutil.AccessDenied, OSError) as exc:
                    self._uncertainties.add("process group membership unavailable")
                    if self._first_observation_failure is None:
                        self._first_observation_failure = {
                            "operation": "enumerated process group/birth/liveness",
                            "pid": pid,
                            "error": type(exc).__name__,
                            "errno": getattr(exc, "errno", None),
                        }
            for pid, token in tuple(self._captured):
                if time.monotonic() >= deadline:
                    self._note_incomplete_observation(
                        "descendant observation exceeded its bounded phase"
                    )
                    return False
                state = self._leader_state() if pid == self.pid else _process_state(pid, token)
                if state == "unknown":
                    self._uncertainties.add("owned process liveness unavailable")
                if state != "live":
                    continue
                try:
                    parent = psutil.Process(pid)
                    if f"psutil:{parent.create_time():.9f}" != token:
                        continue
                    for child in parent.children(recursive=True):
                        # Each returned chain already rechecked every exact
                        # birth/PPID edge and its parent. Retain that acquisition
                        # even if a later proposal exceeds this phase.
                        candidates.update(_descendant_chain(child.pid, (pid, token), deadline))
                    if _process_state(pid, token) not in {"live", "zombie"}:
                        self._uncertainties.add(
                            "parent identity lost during descendant observation"
                        )
                except psutil.NoSuchProcess:
                    continue
                except _ObservationDeadline:
                    raise
                except (psutil.AccessDenied, OSError):
                    self._uncertainties.add("descendant identities unavailable")
            return True
        except _ObservationDeadline as exc:
            self._note_incomplete_observation(str(exc))
            return False
        except (psutil.Error, OSError, RuntimeError) as exc:
            self._uncertainties.add(f"ownership observation failed: {type(exc).__name__}")
            return False
        finally:
            # Preserve only acquisitions already completed with exact proof;
            # never replace their token after a partial snapshot or PID reuse.
            self._captured.update(candidates)
            self._ownership_guard.release()

    def _states(self, deadline: float) -> dict[tuple[int, str], str]:
        if not self._ownership_guard.acquire(timeout=max(0, deadline - time.monotonic())):
            self._note_incomplete_observation("owned states unavailable within bounded phase")
            return {(self.pid, self._leader_token or "unobserved"): "unknown"}
        try:
            states = {}
            for identity in self._captured:
                if identity[0] != self.pid:
                    if time.monotonic() < deadline:
                        states[identity] = _process_state(*identity)
                        if states[identity] == "unknown":
                            self._uncertainties.add("owned process liveness unavailable")
                    else:
                        states[identity] = "unknown"
                        self._note_incomplete_observation("owned state exceeded bounded phase")
            # A missing OS birth token is kept missing. This private key labels
            # actual direct-child spawn evidence; it is never a recovery token.
            if time.monotonic() < deadline:
                leader_state = self._leader_state()
            else:
                leader_state = "unknown"
                self._note_incomplete_observation("leader state exceeded bounded phase")
            states[(self.pid, self._leader_token or "retained-direct-child")] = leader_state
            return states
        finally:
            self._ownership_guard.release()

    def _darwin_terminal_group(self, deadline: float) -> list[dict[str, Any]] | None:
        """Positive exact membership/exit proof, independent of signal errno."""
        if (
            sys.platform != "darwin"
            or not self._anchor_matches()
            or self._uncertainties
            or self._first_observation_failure is not None
            or self._wait_error is not None
        ):
            return None
        try:
            if _exit_status(self.pid) is None:
                return None
            members = _darwin_group_members(self.pid, deadline)
            tokens: dict[int, str] = {}
            while time.monotonic() < deadline:
                terminal = []
                absent: set[int] = set()
                for pid in sorted(members):
                    if time.monotonic() >= deadline:
                        return None
                    if pid == self.pid:
                        if self._leader_state() != "zombie":
                            return None
                        terminal.append(
                            {
                                "pid": pid,
                                "start_token": self._leader_token,
                                "state": "zombie",
                                "identity_source": "retained-direct-child",
                            }
                        )
                    else:
                        if pid not in tokens:
                            tokens[pid] = f"psutil:{psutil.Process(pid).create_time():.9f}"
                        state = _process_state(pid, tokens[pid])
                        if state == "gone":
                            absent.add(pid)
                            continue
                        if state in {"unknown", "reused"}:
                            self._uncertainties.add(
                                "Darwin terminal member identity/liveness unavailable"
                            )
                        if state != "zombie":
                            return None
                        terminal.append({"pid": pid, "start_token": tokens[pid], "state": "zombie"})
                following = _darwin_group_members(self.pid, deadline)
                if not following.issubset(members):
                    self._uncertainties.add("Darwin terminal group gained unproven membership")
                    return None
                for member in terminal:
                    state = (
                        self._leader_state()
                        if member["pid"] == self.pid
                        else _process_state(member["pid"], member["start_token"])
                    )
                    if time.monotonic() >= deadline:
                        return None
                    if state == "gone":
                        absent.add(member["pid"])
                    elif state in {"unknown", "reused"}:
                        self._uncertainties.add(
                            "Darwin terminal member identity/liveness unavailable"
                        )
                        return None
                    elif state != "zombie":
                        return None
                if following != members or absent:
                    # A reaper can remove an exact terminal orphan. Reject this
                    # unstable proof, verify every removed identity freshly,
                    # then require a new complete stable snapshot pair.
                    previous = following
                    following = _darwin_group_members(self.pid, deadline)
                    if not following.issubset(previous) or absent.intersection(following):
                        self._uncertainties.add("Darwin terminal group membership inconsistent")
                        return None
                    for pid in members - following:
                        if time.monotonic() >= deadline:
                            return None
                        if pid == self.pid or _process_state(pid, tokens[pid]) not in {
                            "gone",
                            "zombie",
                        }:
                            self._uncertainties.add(
                                "Darwin removed terminal member retirement unavailable"
                            )
                            return None
                    self._note_incomplete_observation(
                        "positively terminal Darwin group shrink; retry"
                    )
                    members = following
                    continue
                if not self._anchor_matches() or time.monotonic() >= deadline:
                    return None
                return terminal
            return None
        except _ObservationDeadline as exc:
            self._note_incomplete_observation(str(exc))
            return None
        except (psutil.Error, OSError, RuntimeError) as exc:
            if self._first_observation_failure is None:
                self._first_observation_failure = {
                    "operation": "Darwin terminal group membership/identity",
                    "pid": self.pid,
                    "error": type(exc).__name__,
                    "errno": getattr(exc, "errno", None),
                }
            return None

    def _record_group_signal(
        self, selected_signal: int, result: str, error: int | None, *, operation: str = "killpg"
    ) -> None:
        self._group_signal_operations.append(
            {
                "operation": operation,
                "group": self.pid,
                "signal": int(selected_signal),
                "result": result,
                "errno": error,
            }
        )
        del self._group_signal_operations[:-8]  # Bound repeated cleanup diagnostics.

    def _signal_owned(self, selected_signal: int, deadline: float) -> None:
        if sys.platform == "win32":
            raise RuntimeError("AnalysisPopen cannot signal Windows processes")
        if time.monotonic() >= deadline:
            self._uncertainties.add("owned signal phase exceeded cleanup reserve")
            return
        if self._anchor_matches():
            terminal = self._darwin_terminal_group(deadline)
            if terminal is not None:
                # Darwin excludes zombies from group signalling and can return
                # EPERM for this exact terminal case. Avoid the unnecessary
                # signal only with complete positive native membership proof.
                self._terminal_group_receipt = terminal
                self._record_group_signal(selected_signal, "skipped-terminal-membership", None)
            else:
                try:
                    os.killpg(self.pid, selected_signal)
                    self._record_group_signal(selected_signal, "sent", None)
                except ProcessLookupError as exc:
                    self._record_group_signal(selected_signal, "group-absent", exc.errno)
                except OSError as exc:
                    self._record_group_signal(selected_signal, "unavailable", exc.errno)
                    # The exact child can exit after the pre-signal proof check.
                    # Darwin's EPERM then needs fresh positive terminal proof,
                    # inside this same deadline, never an errno-only waiver.
                    terminal = None
                    if sys.platform == "darwin" and exc.errno == errno.EPERM:
                        while (
                            time.monotonic() < deadline
                            and not self._uncertainties
                            and self._first_observation_failure is None
                            and self._wait_error is None
                            and self._anchor_matches()
                        ):
                            terminal = self._darwin_terminal_group(deadline)
                            if (
                                terminal is not None
                                or self._uncertainties
                                or self._first_observation_failure is not None
                            ):
                                break
                            time.sleep(min(0.005, max(0, deadline - time.monotonic())))
                    if terminal is not None and self._first_observation_failure is None:
                        self._terminal_group_receipt = terminal
                        self._record_group_signal(
                            selected_signal,
                            "terminal-membership-after-eperm",
                            exc.errno,
                            operation="terminal-group-proof",
                        )
                    else:
                        self._uncertainties.add("owned group signal unavailable")
        else:
            self._uncertainties.add("group signal refused without retained exact anchor")
            self._record_group_signal(selected_signal, "refused-anchor", None)
        for (pid, token), state in self._states(deadline).items():
            if time.monotonic() >= deadline:
                self._uncertainties.add("descendant signal phase exceeded cleanup reserve")
                return
            if state != "live" or pid == self.pid:
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
        if sys.platform == "win32":
            return False
        started = time.monotonic()
        deadline = started + 3 * grace_seconds
        if not self._cleanup_guard.acquire(timeout=max(0, deadline - time.monotonic())):
            return False
        try:
            if self._reaped:
                return analysis_cleanup_scope_confirmed(self.cleanup_receipt)
            self._capture(deadline)
            for selected_signal in (signal.SIGTERM, signal.SIGKILL):
                phase_end = min(deadline, time.monotonic() + grace_seconds)
                self._signal_owned(selected_signal, phase_end)
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
            if not complete_snapshot:
                self._uncertainties.add("final complete ownership snapshot unavailable")
            if any(state == "unknown" for state in states.values()):
                self._uncertainties.add("final owned process states unavailable")
            native_terminal = True
            if sys.platform == "darwin":
                self._terminal_group_receipt = self._darwin_terminal_group(deadline)
                native_terminal = self._terminal_group_receipt is not None
                if not native_terminal:
                    self._uncertainties.add("Darwin complete terminal group proof unavailable")
            retired = (
                complete_snapshot
                and native_terminal
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
            scope_confirmed = (
                retired and self._reaped and not self._uncertainties and self._wait_error is None
            )
            self.cleanup_receipt = {
                "pid": self.pid,
                "start_token": self._leader_token,
                "group": self.pid,
                "captured": [
                    {
                        "pid": pid,
                        "start_token": self._leader_token if pid == self.pid else token,
                        "state": state,
                        "identity_source": (
                            "retained-direct-child" if pid == self.pid else "observed-birth"
                        ),
                    }
                    for (pid, token), state in sorted(states.items())
                ],
                "leader_exit": self.returncode,
                "leader_reaped": self._reaped,
                "leader_identity_source": "successful-new-session-spawn-and-retained-waitid",
                "spawn_parent_pid": self._spawn_parent_pid,
                "new_session_spawn_succeeded": self._spawn_succeeded and self._session_established,
                "scope_confirmed": scope_confirmed,
                "confirmed": scope_confirmed,  # Compatibility alias for this scope only.
                "coverage": "retained_group_and_captured_descendants",
                "unobserved_detached_descendants": "not_proven",
                "early_direct_child_exit": self._early_direct_child_exit,
                "uncertainties": sorted(self._uncertainties.copy()),
                "wait_error": self._wait_error,
                "first_observation_failure": self._first_observation_failure,
                "native_terminal_group": self._terminal_group_receipt,
                "group_signal_operations": list(self._group_signal_operations),
                "incomplete_observation_count": self._incomplete_observation_count,
                "last_incomplete_observation": self._last_incomplete_observation,
                "elapsed_seconds": time.monotonic() - started,
                "budget_seconds": 3 * grace_seconds,
            }
            if scope_confirmed:
                with _RETAINED_GUARD:
                    _RETAINED.pop(self.pid, None)
            else:
                _LOG.warning("Analysis ownership cleanup unconfirmed: %s", self.cleanup_receipt)
            return scope_confirmed
        finally:
            self._cleanup_guard.release()
