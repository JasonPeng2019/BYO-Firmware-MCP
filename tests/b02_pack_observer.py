"""Read-only pack-selection observations inside actual production workers."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys


def install():
    if "pyocd_debug_mcp.adapters.provider_worker" not in getattr(sys, "orig_argv", sys.argv):
        return
    directory = Path(os.environ["B02_PACK_SELECTION_EVIDENCE"])
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{os.getpid()}.jsonl"

    def observe(frame, event, value):
        name = frame.f_code.co_name
        if event != "return" or value is None or name not in {
            "verified_pack_for_target", "_cmsis_pack_for", "_quarantined_cmsis_pack"
        }:
            return
        module = frame.f_globals.get("__name__")
        if module not in {"pyocd_debug_mcp.pack_provision", "pyocd_debug_mcp.adapters.swd_pyocd"}:
            return
        if name == "_quarantined_cmsis_pack":
            path = frame.f_locals["path"]
            expected = frame.f_locals["expected_sha256"]
            payload = frame.f_locals["payload"]
        else:
            selected = value if name == "verified_pack_for_target" else frame.f_locals["selected"]
            path, expected, payload = selected.path, selected.spec.sha256, selected.payload
        from pyocd_debug_mcp.kernel.processes import _start_token
        row = dict(at=datetime.now(timezone.utc).isoformat(), pid=os.getpid(), function=name,
                   token=_start_token(os.getpid()),
                   parent_pid=os.getppid(), parent_token=_start_token(os.getppid()),
                   source=frame.f_code.co_filename, path=str(path), expected_sha256=expected,
                   payload_sha256=hashlib.sha256(payload).hexdigest(),
                   artifact_root=os.environ.get("BYO_MCP_ARTIFACT_ROOT"))
        with destination.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    assert sys.getprofile() is None
    sys.setprofile(observe)
