"""Host-only fixture around the real provider bootstrap, dispatcher and adapter.

Only open/close are replaced: no native session/probe can be created. Recovery
uses production PyOCDSWDInterface.recover and the installed Nordic target method.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.m14_return_boundary import ReturnBoundary


def main():
    from pyocd_debug_mcp.adapters.provider_worker import main as worker_main
    from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
    from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface
    from tests.m14_independent_support import imports

    output = Path(os.environ["M14_RETURN_WORKER_OUTPUT"])
    output.mkdir(parents=True, exist_ok=True)
    boundary = ReturnBoundary("timeout")

    class DurableEvents(list):
        def append(self, event):
            super().append(event)
            (output / "boundary.json").write_text(
                json.dumps({"pid": os.getpid(), "events": self}), encoding="utf-8"
            )

    boundary.events = DurableEvents()

    def open_host_only(self, **kwargs):
        imports(output / "imports.json")
        return TargetSessionHandle(
            session=SimpleNamespace(target=boundary.target),
            board=kwargs["board"],
            probe_uid=kwargs["unique_id"],
            route_used="m14-host-only",
            target_override=kwargs["target"],
        )

    # All observations are durable before the RPC response: production close may
    # terminate the process immediately after its close reply, before finally.
    with (
        patch.object(PyOCDSWDInterface, "open", open_host_only),
        patch.object(PyOCDSWDInterface, "close", lambda self, handle: None),
        boundary.controlled_clock(),
    ):
        worker_main()


if __name__ == "__main__":
    main()
