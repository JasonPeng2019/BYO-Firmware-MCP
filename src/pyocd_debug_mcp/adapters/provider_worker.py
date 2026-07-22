"""Minimal stdout-safe bootstrap for one native-provider worker."""

from __future__ import annotations

import os
import sys


def main() -> None:
    """Reserve the protocol stream before any provider imports can emit output."""

    protocol_fd = os.dup(sys.stdout.fileno())
    protocol = os.fdopen(protocol_fd, "wb", buffering=0)
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        sys.stdout = sys.stderr

        # Provider imports are deliberately lazy. Native libraries, pyOCD, and
        # their destructors may write to either Python stdout or fd 1 at any
        # time; both now permanently route to inherited stderr. Only the saved
        # duplicate above can carry newline-framed JSON replies.
        from pyocd_debug_mcp.adapters.provider_worker_runtime import main as run

        run(protocol)
    finally:
        try:
            protocol.flush()
        finally:
            protocol.close()


if __name__ == "__main__":
    main()
