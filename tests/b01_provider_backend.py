"""B01 controlled hardware boundary; the child runs the production worker loop."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle


class JournalBackend:
    def record(self, operation, **facts):
        with Path(sys.argv[1]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"operation": operation, **facts}) + "\n")

    def open(self, **kwargs):
        self.record("open")
        session = SimpleNamespace(
            board=SimpleNamespace(name="B01 controlled target"),
            probe=SimpleNamespace(unique_id=kwargs["unique_id"], description="B01 peer"),
            target=SimpleNamespace(part_number="controlled-target"),
        )
        return TargetSessionHandle(
            session, kwargs["board"], kwargs["unique_id"], "b01-controlled-worker", kwargs["target"]
        )

    def close(self, handle):
        self.record("close")

    def get_state(self, handle):
        return "halted"

    def release_reset(self, handle):
        pass

    def read_core_register(self, handle, name):
        self.record("read_core_register", name=name)
        return 0x12345678

    def supported_core_registers(self, handle):
        return ("r0", "xpsr", "d0")

    def write_core_register(self, handle, name, value):
        self.record("write_core_register", name=name, value=value)
        if name == "backend_failure":
            raise ValueError("B01 actual backend dispatch failure")


if __name__ == "__main__":
    from pyocd_debug_mcp.adapters import provider_worker, provider_worker_runtime

    provider_worker_runtime.PyOCDSWDInterface = JournalBackend
    provider_worker.main()
