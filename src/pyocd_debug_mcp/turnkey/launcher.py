"""Start or reuse the singleton Server B, then serve Server A on stdio."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pyocd_debug_mcp.kernel.processes import (
    ProcessMarkerStore,
    popen_owned,
    terminate_process_group,
)
from pyocd_debug_mcp.turnkey.server import main as run_server_a
from pyocd_debug_mcp.turnkey.server_b_probe import verify_server_b


def _server_b_ready(url: str) -> bool:
    return verify_server_b(url) is not None


def _bounded_tail(path: Path, limit: int = 4096) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace").strip()


def _wait_for_server(
    process: subprocess.Popen[bytes], url: str, log_path: Path, timeout: float = 15.0
) -> None:
    deadline = time.monotonic() + timeout
    exit_code: int | None = None
    retry_delay = 0.1
    while time.monotonic() < deadline:
        if _server_b_ready(url):
            return
        exit_code = process.poll()
        if exit_code is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(retry_delay, remaining))
        retry_delay = min(retry_delay * 2, 1.0)
    raise RuntimeError(
        f"Server B did not become a verified MCP endpoint within {timeout:g}s; "
        f"child_exit={exit_code}; "
        f"log={log_path}; tail={_bounded_tail(log_path)!r}"
    )


def main() -> None:
    """Reuse one shared hardware owner; optionally start it before Server A."""

    try:
        port = int(os.environ.get("BYO_SERVER_B_PORT", "8765"))
    except ValueError as exc:
        raise RuntimeError("BYO_SERVER_B_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("BYO_SERVER_B_PORT must be in 1..65535")
    url = f"http://127.0.0.1:{port}/mcp"
    process: subprocess.Popen[bytes] | None = None
    marker = None
    marker_store = ProcessMarkerStore(
        Path(tempfile.mkdtemp(prefix="byo-server-b-parent-markers-"))
    )
    log_path = Path(tempfile.gettempdir()) / "pyocd-debug-mcp" / "server-b.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    persist = os.environ.get("BYO_SERVER_B_PERSIST", "").strip() == "1"
    try:
        if not _server_b_ready(url):
            env = dict(os.environ)
            env["BYO_SERVER_B_HOST"] = "127.0.0.1"
            env["BYO_SERVER_B_PORT"] = str(port)
            env["PYOCD_MCP_RUNS_ROOT"] = tempfile.mkdtemp(
                prefix="byo-server-b-child-markers-"
            )
            with log_path.open("ab") as log:
                process, marker = popen_owned(
                    [sys.executable, "-m", "pyocd_debug_mcp.server_b_http"],
                    marker_store=marker_store,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            _wait_for_server(process, url, log_path)
            marker_store.remove(marker)
            marker = None
        os.environ["BYO_SERVER_B_URL"] = url
        run_server_a()
    finally:
        marker_store.remove(marker)
        if process is not None and not persist:
            terminate_process_group(process)
        shutil.rmtree(marker_store.root, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    main()
