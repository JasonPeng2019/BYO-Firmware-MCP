#!/usr/bin/env python3
"""Run the production MCP server with non-secret M9 hardware lifecycle markers."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    return parser.parse_args()


class MarkerLog:
    """Append lifecycle facts without recording MCP arguments or captured UART data."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: str, **details: object) -> None:
        row = {"timestamp": _timestamp(), "event": event, **details}
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")


def main() -> None:
    args = _arguments()
    sys.argv = [sys.argv[0]]
    markers = MarkerLog(args.trace)

    # Import only after BYO_MCP_ARTIFACT_ROOT has been supplied by the parent.
    from pyocd_debug_mcp import server
    from pyocd_debug_mcp.adapters import swd_pyocd
    from pyocd_debug_mcp.services import target_control, uart_capture

    original_programmer: Any = swd_pyocd.FileProgrammer

    class RecordingFileProgrammer:
        def __init__(self, session: Any, *positional: Any, **options: Any) -> None:
            markers.write(
                "programmer-created",
                chip_erase=options.get("chip_erase"),
            )
            self._inner = original_programmer(session, *positional, **options)

        def program(self, path: str) -> object:
            artifact = Path(path)
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            markers.write(
                "flash-program-start",
                artifact_name=artifact.name,
                artifact_sha256=digest,
            )
            try:
                result = self._inner.program(path)
            except BaseException as exc:
                markers.write("flash-program-error", error_type=type(exc).__name__)
                raise
            markers.write(
                "flash-program-complete",
                artifact_name=artifact.name,
                artifact_sha256=digest,
            )
            return result

    swd_pyocd.FileProgrammer = RecordingFileProgrammer  # type: ignore[assignment]

    backend = uart_capture._BACKEND
    backend_type = type(backend)
    original_uart_open = backend_type.open
    original_uart_close = backend_type.close

    def recording_uart_open(self: Any, device: str, **options: Any) -> object:
        handle = original_uart_open(self, device, **options)
        markers.write("uart-open", device=device)
        return handle

    def recording_uart_close(self: Any, handle: object) -> None:
        try:
            cast(Any, original_uart_close)(self, handle)
        finally:
            markers.write("uart-close")

    backend_type.open = recording_uart_open  # type: ignore[assignment]
    backend_type.close = recording_uart_close  # type: ignore[assignment]

    original_close_session = target_control.close_session
    original_reset = target_control.reset

    def recording_close_session(handle: Any) -> None:
        markers.write("debug-close-start")
        try:
            original_close_session(handle)
        finally:
            markers.write("debug-close-complete")

    def recording_reset(handle: Any, halt_after: bool) -> None:
        markers.write("target-reset", halt_after=halt_after)
        original_reset(handle, halt_after=halt_after)

    target_control.close_session = recording_close_session
    target_control.reset = recording_reset

    original_open_session = target_control.open_session
    patched_probe_types: set[type[object]] = set()

    def instrument_probe(handle: Any) -> Any:
        probe = getattr(getattr(handle, "session", None), "probe", None)
        probe_type = type(probe)
        if probe is None or probe_type in patched_probe_types:
            return handle
        original_assert_reset = getattr(probe_type, "assert_reset", None)
        if not callable(original_assert_reset):
            return handle

        def recording_assert_reset(self: Any, asserted: bool) -> object:
            markers.write("probe-reset-line", asserted=bool(asserted))
            return cast(Any, original_assert_reset)(self, asserted)

        setattr(probe_type, "assert_reset", recording_assert_reset)
        patched_probe_types.add(probe_type)
        return handle

    def recording_open_session(*positional: Any, **options: Any) -> Any:
        return instrument_probe(original_open_session(*positional, **options))

    target_control.open_session = recording_open_session
    markers.write("server-start")
    try:
        server.main()
    finally:
        markers.write("server-stop")


if __name__ == "__main__":
    main()
