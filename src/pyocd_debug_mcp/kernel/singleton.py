"""Cross-process lease for the one physical-hardware Server B owner."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import IO


class ServerBAlreadyRunningError(RuntimeError):
    """Another process already owns the Server B hardware-manager lease."""


class ServerBLease:
    """Hold one non-blocking OS file lock for the complete server lifetime."""

    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("PYOCD_MCP_SERVER_B_LOCK", "").strip()
        self.path = (
            path
            or (Path(configured).expanduser() if configured else None)
            or Path(tempfile.gettempdir()) / "pyocd-debug-mcp" / "server-b.lock"
        ).resolve()
        self._stream: IO[bytes] | None = None

    def __enter__(self) -> ServerBLease:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        if stream.seek(0, 2) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise ServerBAlreadyRunningError(
                "Another Server B process already owns the shared hardware manager. Connect to "
                "that endpoint instead of starting a second board owner."
            ) from exc
        self._stream = stream
        return self

    def __exit__(self, *_exc: object) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
