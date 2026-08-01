"""Manual hardware check for the `remote:<host>:<port>` probe route.

Exercises REMOTE_PROBE_PLAN.md section 6 end to end against a real, natively
discoverable ST-LINK: start `pyocd server`, register it through the same code the
`register_remote_probe` MCP tool uses, take a real `HardwareInventoryService.snapshot()`
and confirm the remote row, open a session through the server's own connect path and
read a real register back off the target, then unregister and confirm the row is gone.

This is deliberately NOT part of the automated suite: it needs a real debug probe and a
spare TCP port, both of which `python -m unittest discover -s tests` must never depend
on to stay green and hermetic. Its filename does not start with `test`, which is what
keeps unittest's default discovery pattern (`test*.py`) from ever picking it up
regardless of which directory it lives in.

Run it by hand:

    python tests/manual/manual_remote_probe_hardware_check.py
    python tests/manual/manual_remote_probe_hardware_check.py --probe-uid <uid> --port <port>

The default `--probe-uid` is the ST-LINK verified on the machine REMOTE_PROBE_PLAN.md
was written against (`0668FF514988525067213913`); pass your own with `--probe-uid` on
any other machine. `--port` defaults to an OS-assigned free port so repeat runs never
collide with a leftover listener.

Expect the PC read back in step 5 to be a real but essentially arbitrary code address
(the plan measured `0x80015de` on its reference board) -- the exact value depends on
whatever the target happens to be executing, and is not asserted here. A remote probe
reports no board identity, so pyOCD falls back to a generic `cortex_m` target; that is
expected, not a defect, and this script does not special-case it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from pyocd_debug_mcp.hardware_inventory import HardwareInventoryService  # noqa: E402
from pyocd_debug_mcp.probe_inventory import EMPTY_NATIVE_PROBE_LISTING  # noqa: E402
from pyocd_debug_mcp.remote_probes import load_remote_probes  # noqa: E402
from pyocd_debug_mcp.services import target_control  # noqa: E402
from pyocd_debug_mcp.tools.remote_probes import (  # noqa: E402
    RemoteProbeToolServices,
    build_remote_probe_handlers,
)

DEFAULT_PROBE_UID = "0668FF514988525067213913"
SERVER_START_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.2


def _free_tcp_port() -> int:
    """Ask the OS for an unused port rather than guessing one that might collide."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for_port_open(host: str, port: int, timeout_seconds: float) -> None:
    """Poll until the port accepts a connection. Never a single blind sleep-then-check."""

    deadline = time.monotonic() + timeout_seconds
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"pyocd server never opened {host}:{port} within {timeout_seconds}s"
    ) from last_error


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Always torn down: terminate, then kill if it does not exit promptly."""

    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-uid", default=DEFAULT_PROBE_UID)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    port = args.port if args.port is not None else _free_tcp_port()
    host = "localhost"
    selector = f"remote:{host}:{port}"

    env = dict(os.environ)
    # Required on Windows: pyocd server crashes with a charmap codec error while
    # printing its own probe table without this set. Observed, not hypothetical.
    env["PYTHONIOENCODING"] = "utf-8"

    server_process = subprocess.Popen(
        [sys.executable, "-m", "pyocd", "server", "-p", str(port), "-u", args.probe_uid],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        print(f"Waiting for pyocd server to open 127.0.0.1:{port} ...")
        _wait_for_port_open("127.0.0.1", port, SERVER_START_TIMEOUT_SECONDS)
        print("pyocd server is accepting connections.")

        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "remote_probes.json"
            handlers = build_remote_probe_handlers(
                RemoteProbeToolServices(registry_path=lambda: registry_path)
            )

            print(f"Registering {selector} ...")
            register_result = json.loads(
                handlers["register_remote_probe"](host, port, "manual hardware check")
            )
            print(register_result)
            assert register_result["reachable"] is True, (
                "the endpoint should be reachable: the server is already listening"
            )
            assert register_result["selector"] == selector

            entries = load_remote_probes(registry_path)
            service = HardwareInventoryService(
                native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
                native_uarts=lambda: [],
                remote_probes=lambda: entries,
            )
            snapshot = service.snapshot()
            remote_rows = [row for row in snapshot.probes if row.provider == "remote"]
            print(f"snapshot() probe rows: {[row.unique_id for row in snapshot.probes]}")
            assert len(remote_rows) == 1, "the remote row did not appear in a real snapshot"
            assert remote_rows[0].unique_id == selector

            print("Opening a session through the server's own connect path ...")
            handle = target_control.open_session(board=None, unique_id=selector)
            try:
                pc = target_control.read_core_register(handle, "pc")
                print(f"Read back PC = 0x{pc:08X}")
            finally:
                with contextlib.suppress(Exception):
                    target_control.close_session(handle)

            print(f"Unregistering {selector} ...")
            unregister_result = json.loads(handlers["unregister_remote_probe"](host, port))
            print(unregister_result)
            assert unregister_result["removed"] is True

            after = HardwareInventoryService(
                native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
                native_uarts=lambda: [],
                remote_probes=lambda: load_remote_probes(registry_path),
            ).snapshot()
            assert not any(
                row.provider == "remote" for row in after.probes
            ), "the remote row survived unregistration"

        print("PASS: the remote probe route works end to end against real hardware.")
        return 0
    finally:
        _terminate(server_process)


if __name__ == "__main__":
    raise SystemExit(main())
