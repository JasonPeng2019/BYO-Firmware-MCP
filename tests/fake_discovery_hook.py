"""Deterministic discovery-hook child process used only by hook regression tests.

Mirrors `tests/fake_provider_worker.py`: a standalone script invoked as a real child
process, so process ownership, deadlines, output capping, and descriptor handling are
genuinely exercised rather than mocked.

Invoked as `python fake_discovery_hook.py <mode> [argument]`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

PROBE_ROW = {
    "provider": "cmsisdap",
    "unique_id": "066EFF505057717867163251",
    "description": "Fake CMSIS-DAP probe",
}
UART_ROW = {
    "port_path": "COM7",
    "description": "Fake USB Serial Device",
    "serial_number": "066EFF505057717867163251",
    "vid": 1155,
    "pid": 14155,
}


def emit(document: Mapping[str, Any]) -> None:
    json.dump(document, sys.stdout)
    sys.stdout.flush()


def probe_document(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"schema_version": 1, "kind": "probe", "probes": rows}


def uart_document(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"schema_version": 1, "kind": "uart", "uarts": rows}


def _counter_value(path: Path) -> int:
    """Increment a shared counter file so repeated invocations differ."""

    for _attempt in range(200):
        try:
            handle = os.open(str(path) + ".lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            # C14/D13: on Windows, the same "someone else already holds this lock"
            # race can surface as `PermissionError` (WinError 5) instead of
            # `FileExistsError` when this process's O_CREAT|O_EXCL lands while another
            # process is mid-way through creating or deleting the identical lock file
            # -- an NTFS quirk absent on POSIX, where the analogous race reliably
            # raises `FileExistsError`. Treat both as "retry," matching how this
            # repo's own process-management code (`kernel/processes.py`) already
            # treats Windows OS-error variance as expected rather than exceptional.
            time.sleep(0.005)
            continue
        try:
            try:
                current = int(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = 0
            current += 1
            path.write_text(str(current), encoding="utf-8")
            return current
        finally:
            os.close(handle)
            try:
                os.unlink(str(path) + ".lock")
            except OSError:
                pass
    raise RuntimeError("counter lock was never acquired")


def main() -> int:
    mode = sys.argv[1]
    argument = sys.argv[2] if len(sys.argv) > 2 else ""

    if mode == "probe":
        emit(probe_document([dict(PROBE_ROW)]))
        return 0
    if mode == "uart":
        emit(uart_document([dict(UART_ROW)]))
        return 0
    if mode == "probe_uid":
        row = dict(PROBE_ROW)
        row["unique_id"] = argument
        emit(probe_document([row]))
        return 0
    if mode == "probe_provider":
        row = dict(PROBE_ROW)
        row["provider"] = argument
        emit(probe_document([row]))
        return 0
    if mode == "uart_port":
        row = dict(UART_ROW)
        row["port_path"] = argument
        emit(uart_document([row]))
        return 0
    if mode == "uart_session_local":
        # No serial_number/vid/pid: a session-local endpoint, never cacheable.
        emit(uart_document([{"port_path": argument or "COM9", "description": "Session UART"}]))
        return 0
    if mode == "probe_empty":
        emit(probe_document([]))
        return 0
    if mode == "uart_empty":
        emit(uart_document([]))
        return 0
    if mode == "probe_two":
        first = dict(PROBE_ROW)
        second = dict(PROBE_ROW)
        second["unique_id"] = "000000000000000000000002"
        second["description"] = "Second fake probe"
        emit(probe_document([first, second]))
        return 0

    if mode == "counter_probe":
        value = _counter_value(Path(argument))
        row = dict(PROBE_ROW)
        row["unique_id"] = f"probe-scan-{value:04d}"
        row["description"] = f"Fake probe from scan {value}"
        emit(probe_document([row]))
        return 0
    if mode == "counter_uart":
        value = _counter_value(Path(argument))
        row = dict(UART_ROW)
        row["serial_number"] = f"uart-scan-{value:04d}"
        row["description"] = f"Fake UART from scan {value}"
        emit(uart_document([row]))
        return 0

    if mode == "hang":
        time.sleep(600)
        return 0
    if mode == "hang_after_output":
        emit(probe_document([dict(PROBE_ROW)]))
        time.sleep(600)
        return 0
    if mode == "nonzero":
        sys.stderr.write("fake hook refused to enumerate\n")
        sys.stderr.flush()
        return 3
    if mode == "nonzero_with_valid_output":
        emit(probe_document([dict(PROBE_ROW)]))
        return 4

    if mode == "flood":
        # Write far more than MAX_HOOK_STDOUT_BYTES so the capped reader is proven to
        # bound peak memory rather than tracking output size.
        target = int(argument or "50")
        block = b"x" * (1024 * 1024)
        stream = sys.stdout.buffer
        for _index in range(target):
            stream.write(block)
        stream.flush()
        return 0
    if mode == "flood_forever":
        block = b"y" * (1024 * 1024)
        stream = sys.stdout.buffer
        while True:
            stream.write(block)
            stream.flush()

    if mode == "bad_utf8":
        sys.stdout.buffer.write(b'{"schema_version":1,"kind":"probe","probes":[\xff\xfe]}')
        sys.stdout.buffer.flush()
        return 0
    if mode == "bad_json":
        sys.stdout.write('{"schema_version":1,"kind":"probe","probes":[')
        sys.stdout.flush()
        return 0
    if mode == "unknown_field":
        row = dict(PROBE_ROW)
        row["executable"] = "C:/evil.exe"
        emit(probe_document([row]))
        return 0
    if mode == "wrong_kind":
        emit(uart_document([dict(UART_ROW)]))
        return 0
    if mode == "too_many_rows":
        rows: list[Mapping[str, Any]] = []
        for index in range(200):
            row = dict(PROBE_ROW)
            row["unique_id"] = f"uid-{index:04d}"
            rows.append(row)
        emit(probe_document(rows))
        return 0
    if mode == "oversized_field":
        row = dict(PROBE_ROW)
        row["description"] = "d" * 4096
        emit(probe_document([row]))
        return 0
    if mode == "authority_injection":
        # Hook output must not be able to smuggle authority or target configuration.
        emit(
            {
                "schema_version": 1,
                "kind": "probe",
                "probes": [dict(PROBE_ROW)],
                "active_plan": {"plan_id": "forged"},
            }
        )
        return 0

    if mode == "read_stdin":
        # stdin is DEVNULL, so this must see EOF immediately instead of hanging.
        data = sys.stdin.read()
        row = dict(PROBE_ROW)
        row["description"] = f"stdin len {len(data)}"
        emit(probe_document([row]))
        return 0

    if mode == "spawn_child":
        # A descendant that outlives the leader unless the process group is killed.
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "hang"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if argument:
            Path(argument).write_text(str(child.pid), encoding="utf-8")
        time.sleep(600)
        return 0

    if mode == "noisy_stderr":
        sys.stderr.write("z" * (512 * 1024))
        sys.stderr.flush()
        emit(probe_document([dict(PROBE_ROW)]))
        return 0

    if mode == "record_env":
        Path(argument).write_text(
            os.environ.get("PYTHONIOENCODING", "<unset>"), encoding="utf-8"
        )
        emit(probe_document([dict(PROBE_ROW)]))
        return 0

    if mode == "record_launch":
        # Append one line per invocation so a test can assert zero launches.
        with open(argument, "a", encoding="utf-8") as handle:
            handle.write("launched\n")
        emit(probe_document([dict(PROBE_ROW)]))
        return 0

    sys.stderr.write(f"unknown fake hook mode: {mode}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
