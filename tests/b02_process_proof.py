"""Observe exact live worker descendants, then verify their absence; no target I/O."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import psutil

from pyocd_debug_mcp.kernel.processes import _start_token, identity_matches
from tests.m02_physical_flash import creation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = args.evidence.resolve()
    invocation = json.loads(args.invocation.read_text())
    status = json.loads(Path(invocation["paths"]["controller_status"]).read_text())
    assert status["run_id"] == invocation["run_id"] and status["controller_state"] == "running"
    for identity in (status["controller_identity"], status["provider_state"]):
        assert creation(identity["pid"]) == identity["creation_time"]
    original = Path(json.loads((Path(invocation["cwd"]) / "harness/harness-config.json").read_text())["root_workspace"])
    leases = [json.loads(path.read_text()) for path in (original / ".harness-runtime/resources/leases").glob("*.lease")]
    assert {row["resource_id"] for row in leases} == set(invocation["exclusive_resources"])
    for row in leases:
        assert row["run_id"] == invocation["run_id"] and row["lane_id"] == invocation["lane_id"]
        assert row["pid"] == status["controller_identity"]["pid"] and row["creation_time"] == status["controller_identity"]["creation_time"]
    result = dict(at=datetime.now(timezone.utc).isoformat(), run_id=invocation["run_id"], leases=leases, processes=[])
    capture = root / "live-worker-descendants.json"
    destination = root / ("worker-descendants-cleanup.json" if args.check else capture.name)
    assert not destination.exists()
    if args.check:
        observed = json.loads(capture.read_text())
        assert observed["run_id"] == invocation["run_id"]
        for row in observed["processes"]:
            result["processes"].append(row | dict(exact_identity_absent=not identity_matches(row["pid"], row["token"])))
        assert result["processes"] and all(row["exact_identity_absent"] for row in result["processes"])
        selected = [json.loads(line) for path in root.glob("*-pack-selection/*.jsonl") for line in path.read_text().splitlines()]
        result["selected_worker_pids"] = []
        for pid in sorted({row["pid"] for row in selected}):
            absent = not psutil.pid_exists(pid)
            result["selected_worker_pids"].append(dict(pid=pid, pid_absent=absent,
                creation_token=next((row["token"] for row in observed["processes"] if row["pid"] == pid), None),
                qualification="No process has this observed PID; missing historical creation tokens remain unavailable"))
        assert all(row["pid_absent"] for row in result["selected_worker_pids"])
    else:
        full = json.loads((root / "full-retry-4.json").read_text())
        selected = [json.loads(line) for path in (root / "full-retry-4-pack-selection").glob("*.jsonl") for line in path.read_text().splitlines()]
        for pid in sorted({row["pid"] for row in selected}):
            process = psutil.Process(pid)
            ancestors = process.parents()
            bindings = [row for row in ancestors if str(row.pid) in full["known_children"]
                        and identity_matches(row.pid, full["known_children"][str(row.pid)]["token"])]
            assert bindings, "Selection child must descend from an exact observed owned launcher"
            token = _start_token(pid)
            assert token
            result["processes"].append(dict(pid=pid, token=token, argv=process.cmdline(),
                                            owned_ancestor_pids=[row.pid for row in bindings]))
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(dict(path=str(destination), processes=result["processes"])))


if __name__ == "__main__":
    main()
