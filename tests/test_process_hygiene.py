from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pyocd_debug_mcp.kernel import processes as process_module
from pyocd_debug_mcp.kernel.hygiene import (
    cleanup_stale_owned_processes,
    require_clean_startup,
)
from pyocd_debug_mcp.kernel.processes import (
    ProcessMarkerStore,
    ProcessIdentityUnavailable,
    _posix_start_token,
    _start_token,
    _windows_start_token,
    popen_owned,
    process_group_options,
    run_owned,
    terminate_process_group,
    validate_argv,
)


@pytest.mark.parametrize("argv", [[], "echo hello", ["ok", ""], ["ok", "bad\x00arg"]])
def test_explicit_argv_validation_rejects_hostile_shapes(argv: object) -> None:
    with pytest.raises(ValueError):
        validate_argv(argv)  # type: ignore[arg-type]


def test_platform_process_group_abstraction() -> None:
    assert process_group_options("nt") == {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
        | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
    }
    assert process_group_options("posix") == {"start_new_session": True}


def test_posix_start_token_does_not_depend_on_procfs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        @staticmethod
        def is_running() -> bool:
            return True

        @staticmethod
        def status() -> str:
            return "sleeping"

        @staticmethod
        def create_time() -> float:
            return 1234.5

    monkeypatch.setattr(process_module.psutil, "Process", lambda _pid: FakeProcess())

    assert _posix_start_token(123) == "psutil:1234.500000000"


def test_posix_start_token_records_unreaped_zombie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = SimpleNamespace(create_time=lambda: 1234.5)
    monkeypatch.setattr(process_module.psutil, "Process", lambda _pid: fake)

    assert _posix_start_token(123) == "psutil:1234.500000000"


def test_posix_start_token_rejects_stale_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_pid: int) -> object:
        raise process_module.psutil.NoSuchProcess(123)

    monkeypatch.setattr(process_module.psutil, "Process", missing)

    assert _posix_start_token(123) is None


def test_posix_start_token_distinguishes_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def inaccessible(_pid: int) -> object:
        raise process_module.psutil.AccessDenied(123)

    monkeypatch.setattr(process_module.psutil, "Process", inaccessible)

    with pytest.raises(ProcessIdentityUnavailable, match="Access denied"):
        _posix_start_token(123)


@pytest.mark.parametrize("error", [0, 5, 6])
def test_windows_start_token_treats_unclassified_open_failure_as_indeterminate(
    error: int,
) -> None:
    fake = SimpleNamespace(
        OpenProcess=lambda *_args: 0,
        GetLastError=lambda: error,
    )

    with pytest.raises(ProcessIdentityUnavailable, match="Windows error"):
        _windows_start_token(123, fake)


@pytest.mark.parametrize("error", [87, 1168])
def test_windows_start_token_recognizes_confirmed_absence(error: int) -> None:
    fake = SimpleNamespace(
        OpenProcess=lambda *_args: 0,
        GetLastError=lambda: error,
    )

    assert _windows_start_token(123, fake) is None


def test_marked_posix_group_cleanup_does_not_claim_permission_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_module, "_start_token", lambda _pid: None)
    monkeypatch.setattr(
        process_module,
        "os",
        SimpleNamespace(
            name="posix",
            killpg=lambda *_args: (_ for _ in ()).throw(PermissionError("denied")),
        ),
    )

    assert process_module.terminate_marked_group(123, "psutil:1.000000000") is False


def test_run_owned_has_finite_timeout_and_removes_marker(tmp_path: Path) -> None:
    store = ProcessMarkerStore(tmp_path / "markers")
    with pytest.raises(subprocess.TimeoutExpired):
        run_owned(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.05,
            marker_store=store,
        )
    assert list(store.root.glob("*.json")) == []


def test_run_owned_terminates_before_removing_marker_on_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "owned.json"
    marker.write_text("owned", encoding="utf-8")
    events: list[str] = []

    class InterruptedProcess:
        returncode = None

        @staticmethod
        def communicate(*, timeout: float) -> tuple[None, None]:
            assert timeout == 1.0
            raise KeyboardInterrupt

    process = cast(subprocess.Popen[Any], InterruptedProcess())

    def fake_popen_owned(*_args: object, **_kwargs: object) -> tuple[subprocess.Popen[Any], Path]:
        return process, marker

    def fake_terminate(candidate: subprocess.Popen[Any]) -> bool:
        assert candidate is process
        assert marker.exists()
        events.append("terminated")
        return True

    monkeypatch.setattr(process_module, "popen_owned", fake_popen_owned)
    monkeypatch.setattr(process_module, "terminate_process_group", fake_terminate)

    with pytest.raises(KeyboardInterrupt):
        run_owned([sys.executable, "-c", "pass"], timeout=1.0)

    assert events == ["terminated"]
    assert not marker.exists()


def test_run_owned_retains_marker_when_interrupt_cleanup_is_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "owned.json"
    marker.write_text("owned", encoding="utf-8")

    class InterruptedProcess:
        returncode = None

        @staticmethod
        def communicate(*, timeout: float) -> tuple[None, None]:
            raise KeyboardInterrupt

    process = cast(subprocess.Popen[Any], InterruptedProcess())
    monkeypatch.setattr(
        process_module, "popen_owned", lambda *_args, **_kwargs: (process, marker)
    )
    monkeypatch.setattr(process_module, "terminate_process_group", lambda _process: False)

    with pytest.raises(KeyboardInterrupt):
        run_owned([sys.executable, "-c", "pass"], timeout=1.0)

    assert marker.exists()


def test_run_owned_timeout_terminates_real_descendant_tree(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    parent_script = (
        "import pathlib, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(30)"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_owned(
            [sys.executable, "-c", parent_script, str(child_pid)],
            timeout=0.5,
            marker_store=ProcessMarkerStore(tmp_path / "markers"),
        )

    pid = int(child_pid.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2.0
    while _start_token(pid) is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _start_token(pid) is None


def test_run_owned_normal_exit_clears_background_descendant(tmp_path: Path) -> None:
    child_pid = tmp_path / "background-child.pid"
    parent_script = (
        "import pathlib, subprocess, sys; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
    )

    completed = run_owned(
        [sys.executable, "-c", parent_script, str(child_pid)],
        timeout=2.0,
        marker_store=ProcessMarkerStore(tmp_path / "normal-markers"),
    )

    assert completed.returncode == 0
    pid = int(child_pid.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2.0
    while _start_token(pid) is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _start_token(pid) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group recovery regression")
def test_leaderless_posix_marker_is_retained_without_signaling_group(tmp_path: Path) -> None:
    child_pid = tmp_path / "fast-background-child.pid"
    store = ProcessMarkerStore(tmp_path / "fast-markers")
    parent_script = (
        "import pathlib, subprocess, sys; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
    )

    process, marker = popen_owned(
        [sys.executable, "-c", parent_script, str(child_pid)],
        marker_store=store,
    )
    process.wait(timeout=2.0)

    assert marker is not None and marker.exists()
    result = cleanup_stale_owned_processes(store.root)
    assert result.refused == 1
    assert marker.exists()
    pid = int(child_pid.read_text(encoding="utf-8"))
    getattr(os, "killpg")(process.pid, getattr(signal, "SIGKILL"))
    deadline = time.monotonic() + 2.0
    while _start_token(pid) is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    marker.unlink()


def test_seeded_live_marker_is_identity_checked_and_terminated(tmp_path: Path) -> None:
    store = ProcessMarkerStore(tmp_path / "markers")
    process, marker = popen_owned(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        marker_store=store,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert marker is not None and marker.exists()
    seeded = json.loads(marker.read_text(encoding="utf-8"))
    seeded["owner_pid"] = 999_999_999
    seeded["owner_start_token"] = "orphaned-owner"
    marker.write_text(json.dumps(seeded), encoding="utf-8")
    result = cleanup_stale_owned_processes(store.root)
    assert result.terminated == 1
    process.wait(timeout=2.0)
    assert not marker.exists()


def test_stale_reused_pid_marker_is_removed_without_termination(tmp_path: Path) -> None:
    root = tmp_path / "markers"
    root.mkdir()
    marker = root / "stale.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "marker_id": "seeded",
                "owner_pid": 999_999_999,
                "owner_start_token": "orphaned-owner",
                "pid": os.getpid(),
                "start_token": "definitely-not-this-process",
                "argv_sha256": "0" * 64,
                "executable": sys.executable,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    result = cleanup_stale_owned_processes(root)
    assert result.stale_removed == 1
    assert not marker.exists()


def test_inaccessible_marker_identity_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "markers"
    root.mkdir()
    marker = root / "inaccessible.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "marker_id": "inaccessible",
                "owner_pid": 999_999_999,
                "owner_start_token": "orphaned-owner",
                "pid": 123,
                "start_token": "psutil:1.000000000",
                "argv_sha256": "0" * 64,
                "executable": sys.executable,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "pyocd_debug_mcp.kernel.hygiene._start_token",
        lambda _pid: (_ for _ in ()).throw(ProcessIdentityUnavailable("denied")),
    )

    result = cleanup_stale_owned_processes(root)

    assert result.refused == 1
    assert marker.exists()


def test_owned_launch_fails_and_cleans_up_when_identity_is_inaccessible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        process_module,
        "_start_token",
        lambda _pid: (_ for _ in ()).throw(ProcessIdentityUnavailable("denied")),
    )

    with pytest.raises(ProcessIdentityUnavailable, match="denied"):
        popen_owned(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            marker_store=ProcessMarkerStore(tmp_path / "markers"),
        )

    assert list((tmp_path / "markers").glob("*.json")) == []


def test_malformed_marker_fails_closed_and_is_preserved(tmp_path: Path) -> None:
    root = tmp_path / "markers"
    root.mkdir()
    hostile = root / "hostile.json"
    hostile.write_text('{"pid": 1, "start_token": "guessed"}', encoding="utf-8")
    started = time.monotonic()
    result = cleanup_stale_owned_processes(root, timeout_seconds=0.2)
    assert time.monotonic() - started < 1.0
    assert result.refused == 1
    assert result.unresolved == 1
    assert hostile.exists()

    with pytest.raises(RuntimeError, match="unresolved marker"):
        require_clean_startup(root)


def test_seeded_stale_lock_marker_is_removed_but_live_lock_is_not_stolen(
    tmp_path: Path,
) -> None:
    store = ProcessMarkerStore(tmp_path / "markers")
    process, marker = popen_owned(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        marker_store=store,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert marker is not None
    live_lock = marker.with_name(f"live-{process.pid}.lock.json")
    marker.replace(live_lock)
    seeded = json.loads(live_lock.read_text(encoding="utf-8"))
    seeded["start_token"] = "stale-token"
    stale_lock = store.root / "stale.lock.json"
    stale_lock.write_text(json.dumps(seeded), encoding="utf-8")

    result = cleanup_stale_owned_processes(store.root)
    assert result.stale_removed == 0
    assert result.refused == 2
    assert live_lock.exists()
    assert stale_lock.exists()
    assert terminate_process_group(process)
    live_lock.unlink()
    stale_lock.unlink()


def test_new_stdio_server_does_not_kill_another_live_owners_helper(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    store = ProcessMarkerStore(runs_root / "owned-processes")
    helper, marker = popen_owned(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        marker_store=store,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert marker is not None
    environment = dict(os.environ)
    environment["PYOCD_MCP_RUNS_ROOT"] = str(runs_root)
    server = subprocess.Popen(
        [sys.executable, "-m", "pyocd_debug_mcp.server"],
        cwd=Path(__file__).parents[1],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        assert server.stdin is not None and server.stdout is not None
        server.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "restart-test", "version": "1"},
                    },
                }
            )
            + "\n"
        )
        server.stdin.flush()
        messages: queue.Queue[str] = queue.Queue()
        server_stdout = server.stdout
        reader = threading.Thread(
            target=lambda: messages.put(server_stdout.readline()), daemon=True
        )
        reader.start()
        response = json.loads(messages.get(timeout=8.0))
        assert response["id"] == 1 and "result" in response
        server.stdin.close()
        assert server.wait(timeout=8.0) == 0
        assert helper.poll() is None
        assert marker.exists()
        assert cleanup_stale_owned_processes(store.root).live_owner_skipped == 1
    finally:
        if server.poll() is None:
            server.kill()
        if helper.poll() is None:
            assert terminate_process_group(helper)
        store.remove(marker)
