"""Metadata-only observer calibration on a copy of genuine public project state."""

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    out = args.evidence.resolve()
    out.mkdir(exist_ok=False)
    clone = out / "project"
    shutil.copytree(args.project / ".firm", clone / ".firm")
    for key, path in (("BYO_MCP_ARTIFACT_ROOT", clone), ("BYO_MCP_MONITOR_ROOT", out / "monitor"),
                      ("BYO_MANAGED_TEST_EVIDENCE", out / "managed")):
        os.environ[key] = str(path)
    from pyocd_debug_mcp import server
    from pyocd_debug_mcp.adapters.swd_process import _WorkerClient
    from pyocd_debug_mcp.kernel.processes import popen_owned

    def forbidden(*args, **kwargs):
        raise AssertionError("Target/probe/subprocess use forbidden in metadata-only calibration")

    server.target_control.open_session = forbidden
    subprocess.Popen = forbidden
    helper = Path(__file__).with_name("b02_physical.py")
    tree = ast.parse(helper.read_text())
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "observe")
    namespace = dict(leases=forbidden, owned_code=popen_owned.__code__,
                     worker_codes={id(_WorkerClient.__init__.__code__), id(_WorkerClient.call.__code__), id(_WorkerClient.close.__code__)})
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(helper), "exec"), namespace)
    record = dict(at=datetime.now(timezone.utc).isoformat(), argv=sys.argv, hardware_IO=False, phases=[],
                  helper_sha256=hashlib.sha256(helper.read_bytes()).hexdigest(),
                  qualification="Copy of genuine public project; no public dispatch or physical proof. OS cache uncontrolled; corrected profile follows unprofiled.")
    for mode in ("unprofiled", "corrected_observer"):
        sys.setprofile(namespace["observe"] if mode == "corrected_observer" else None)
        start = time.perf_counter()
        try:
            reply = server._run_board_safety_refresh("b02_physical_stm32l476")
        finally:
            elapsed = time.perf_counter() - start
            sys.setprofile(None)
        assert reply["status"] == "safety_refresh_completed", reply
        record["phases"].append(dict(mode=mode, seconds=elapsed, status=reply["status"]))
    (out / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record))
    assert record["phases"][-1]["seconds"] < 30


if __name__ == "__main__":
    main()
