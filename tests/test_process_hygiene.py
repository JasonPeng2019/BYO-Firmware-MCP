from __future__ import annotations

import json
import os
import subprocess
import sys
import queue
import threading
import time
from pathlib import Path

import pytest

from pyocd_debug_mcp.kernel.hygiene import cleanup_stale_owned_processes
from pyocd_debug_mcp.kernel.processes import (
    ProcessMarkerStore,
    popen_owned,
    process_group_options,
    run_owned,
    validate_argv,
)


@pytest.mark.parametrize("argv", [[], "echo hello", ["ok", ""], ["ok", "bad\x00arg"]])
def test_explicit_argv_validation_rejects_hostile_shapes(argv: object) -> None:
    with pytest.raises(ValueError):
        validate_argv(argv)  # type: ignore[arg-type]


def test_platform_process_group_abstraction() -> None:
    assert process_group_options("nt") == {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
    }
    assert process_group_options("posix") == {"start_new_session": True}


def test_run_owned_has_finite_timeout_and_removes_marker(tmp_path: Path) -> None:
    store = ProcessMarkerStore(tmp_path / "markers")
    with pytest.raises(subprocess.TimeoutExpired):
        run_owned(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.05,
            marker_store=store,
        )
    assert list(store.root.glob("*.json")) == []


def test_seeded_live_marker_is_identity_checked_and_terminated(tmp_path: Path) -> None:
    store = ProcessMarkerStore(tmp_path / "markers")
    process, marker = popen_owned(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        marker_store=store,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert marker is not None and marker.exists()
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
                "schema_version": 1,
                "marker_id": "seeded",
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


def test_malformed_marker_fails_closed_and_is_preserved(tmp_path: Path) -> None:
    root = tmp_path / "markers"
    root.mkdir()
    hostile = root / "hostile.json"
    hostile.write_text('{"pid": 1, "start_token": "guessed"}', encoding="utf-8")
    started = time.monotonic()
    result = cleanup_stale_owned_processes(root, timeout_seconds=0.2)
    assert time.monotonic() - started < 1.0
    assert result.refused == 1
    assert hostile.exists()


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
    assert result.stale_removed == 1
    assert result.refused == 1
    assert live_lock.exists()
    assert not stale_lock.exists()
    process.terminate()
    process.wait(timeout=2.0)
    live_lock.unlink()


def test_new_stdio_server_run_cleans_seeded_helper_before_boot(tmp_path: Path) -> None:
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
    environment["PYOCD_MCP_SERVER_B_LOCK"] = str(tmp_path / "server-b.lock")
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
        helper.wait(timeout=2.0)
        assert not marker.exists()
    finally:
        if server.poll() is None:
            server.kill()
        if helper.poll() is None:
            helper.kill()
