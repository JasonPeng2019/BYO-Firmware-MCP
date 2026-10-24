"""Real source server command seam with child-local import identity receipts.

This helper does not configure a backend, intercept replies, or write stdout.
It is used only by source tests; installed tests invoke the shipped launcher.
"""

import atexit
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

import psutil
from pyocd_debug_mcp import server


def record(stage):
    destination = os.environ.get("BYO_TEST_STDIO_IDENTITIES")
    if not destination:
        return
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    modules = []
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if filename and name.startswith(
            ("pyocd_debug_mcp", "pyocd.", "mcp.", "anyio.", "elftools.", "psutil")
        ):
            path = Path(filename)
            if path.is_file():
                modules.append(
                    {
                        "name": name,
                        "path": str(path.resolve()),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
    receipt = {
        "stage": stage,
        "pid": os.getpid(),
        "creation_time": psutil.Process().create_time(),
        "python": sys.executable,
        "argv": sys.argv,
        "cwd": str(Path.cwd()),
        "modules": modules,
        "versions": {
            n: importlib.metadata.version(n)
            for n in ("pyocd", "mcp", "anyio", "pyelftools", "psutil")
        },
    }
    (root / f"{os.getpid()}-{stage}.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    record("before-server")
    atexit.register(record, "after-server")
    server.main()
