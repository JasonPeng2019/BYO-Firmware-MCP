"""Read-only B02 evidence verification; no hardware, grants or native mutations."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    root = args.evidence.resolve()
    source = Path.cwd().resolve()
    summary = {"at": datetime.now(timezone.utc).isoformat(), "commands": [],
               "verified_imports": 0, "verified_snapshots": 0, "physical": [],
               "historical_import_snapshot_mismatches": []}
    commands = {}
    for path in sorted(root.glob("*/result.json")):
        record = json.loads(path.read_text())
        commands[path.parent.name] = record
        stderr = (path.parent / "stderr.log").read_text(errors="replace")
        count = re.search(r"Ran (\d+) tests? in ([\d.]+)s", stderr)
        summary["commands"].append(dict(label=path.parent.name, exit_code=record["exit_code"],
                                       started_at=record["started_at"], finished_at=record["finished_at"],
                                       unittest_tests=int(count[1]) if count else None,
                                       unittest_seconds=count[2] if count else None))
        assert sha(path.parent / "stdout.log") == record["stdout_sha256"]
        assert sha(path.parent / "stderr.log") == record["stderr_sha256"]
        inputs_by_path = {str(Path(r["path"]).resolve()).casefold(): r for r in record["inputs"]}
        for observed in (path.parent / "imports").glob("*.json"):
            for row in json.loads(observed.read_text()):
                retained = inputs_by_path.get(str(Path(row["path"]).resolve()).casefold())
                if retained and retained["sha256"] != row["sha256"]:
                    summary["historical_import_snapshot_mismatches"].append(dict(
                        command=path.parent.name, module=row["module"], path=row["path"],
                        before_command_sha256=retained["sha256"], observed_at_exit_sha256=row["sha256"]))
    for label, count in (("fixture-controls-01", 26), ("final-controls-09", 26), ("final-members-08", 13)):
        record = commands[label]
        assert record["exit_code"] == 0
        log = (root / label / "stderr.log").read_text()
        assert f"Ran {count} tests" in log and "\nOK\n" in log
        assert not re.search(r"\b(?:skipped|failures=|errors=)", log)
    for label in ("fixture-controls-01", "final-controls-09", "final-members-08",
                  "physical-nrf-prepare-02", "physical-nrf-full-02", "physical-nrf-restore-02",
                  "physical-stm-prepare-04", "physical-stm-full-retry2-04", "physical-stm-restore-04"):
        record = commands[label]
        assert record["exit_code"] == (1 if label == "physical-stm-full-retry2-04" else 0)
        inputs = {str(Path(row["path"]).resolve()).casefold(): row for row in record["inputs"]}
        for row in inputs.values():
            assert sha(Path(row["snapshot"])) == row["sha256"]
            summary["verified_snapshots"] += 1
        imported = []
        for path in (root / label / "imports").glob("*.json"):
            imported.extend(json.loads(path.read_text()))
        assert any(row["module"].startswith("pyocd_debug_mcp.") for row in imported)
        for row in imported:
            if not row["module"].startswith(("pyocd_debug_mcp", "tests.")):
                continue
            imported_path = Path(row["path"]).resolve()
            assert imported_path.is_relative_to(source), (label, row)
            retained = inputs[str(imported_path).casefold()]
            assert retained["sha256"] == row["sha256"], (label, row)
            summary["verified_imports"] += 1
    for member, label in (("m06", "m06-sensitivity-02"), ("m13", "m13-sensitivity-01")):
        record = commands[label]
        assert record["exit_code"] == 1
        mutation = json.loads((root / label / "managed" / member / "result.json").read_text())
        assert mutation["exit_code"] == 1 and "... ok" in mutation["stderr"]
        assert "FAILED (failures=" in mutation["stderr"]
        if member == "m06":
            assert "Required public no-plan symbol read failed" in mutation["stderr"]
        else:
            assert "has no application deployment allocation yet" in mutation["stderr"]
        mutant_source = Path(mutation["source"])
        assert sha(mutant_source) == mutation["source_sha256"]
        rows = [r for p in (root / label / "imports").glob("*.json") for r in json.loads(p.read_text())]
        if member == "m06":
            # tiered_test_server intentionally unloads server before atexit.
            # Preserve the actual executed-code filename in the failing public
            # traceback; do not invent an unavailable exit-time import row.
            assert str(mutant_source) in mutation["stderr"]
            summary["m06_mutant_identity"] = "Executed traceback filename plus retained pre-execution mutant bytes; server is unloaded before exit observation."
        else:
            assert any(Path(r["path"]) == mutant_source and r["sha256"] == mutation["source_sha256"] for r in rows)
    for board in ("physical-nrf-02", "physical-stm-04"):
        reports = {stage: json.loads((root / board / ("full-retry-2.json" if stage == "full" and "stm" in board else f"{stage}.json")).read_text()) for stage in ("prepare", "full", "restore")}
        for report in reports.values():
            expected = "FAIL" if "stm" in board and report["stage"] == "full" else "PASS"
            assert report["verdict"] == expected and report["cleanup_errors"] == []
            assert report["workers"] and report["known_children"]
            assert all(r["exact_identity_absent"] and r["raw_exit"] is not None and r["cleanup_confirmed"] for r in report["workers"].values())
            assert all(r["exact_identity_absent"] and r["raw_exit"] is not None for r in report["known_children"].values())
            assert report["leases"]
            stem, attempt = board.rsplit("-", 1)
            retry = f"-retry{report['retry']}" if report.get("retry") else ""
            label = f"{stem}-{report['stage']}{retry}-{attempt}"
            retained = next(row for row in commands[label]["inputs"] if Path(row["path"]).name == "b02_physical.py")
            assert retained["sha256"] == report["helper_sha256"]
            for check in report["leases"]:
                assert len(check["leases"]) == 2
                for lease in check["leases"]:
                    assert lease["run_id"] == report["invocation"]["run_id"]
                    assert lease["pid"] == check["controller"]["pid"] and lease["creation_time"] == check["controller"]["creation_time"]
        full = reports["full"]
        assert reports["restore"]["outside_sector"]["actual_sha256"] == sha(root / board / "sector-original.bin")
        if "stm" in board:
            initial = json.loads((root / board / "full.json").read_text())
            assert initial["verdict"] == "FAIL" and "worker did not reply before the deadline" in initial["traceback"]
            assert "not recognized" in full["traceback"]
            summary["physical"].append(dict(board=board, verdict="FAIL / AWAITING FULL prerequisite",
                                             initial_failure=initial["traceback"], reconnect_failure=full["traceback"],
                                             restored_sha256=reports["restore"]["outside_sector"]["actual_sha256"],
                                             calls=sum(len(r["calls"]) for r in reports.values()),
                                             lease_checks=sum(len(r["leases"]) for r in reports.values())))
            continue
        assert len(full["symbol_equality"]) == 2
        assert all(r["direct_value"] == r["batch_value"] == r["address_value"] for r in full["symbol_equality"])
        assert full["persisted_allocation"]["deployment_policy"]["kind"] != "none"
        assert full["breakpoint_hit"] and "RUNNING" in full["final_state"]
        assert full["outside_preserved"]["before_sha256"] == full["outside_preserved"]["after_sha256"]
        assert reports["restore"]["outside_sector"]["actual_sha256"] == sha(root / board / "sector-original.bin")
        summary["physical"].append(dict(board=board, symbol_equality=full["symbol_equality"],
                                         breakpoint_hit=full["breakpoint_hit"], outside=full["outside_preserved"],
                                         restored_sha256=reports["restore"]["outside_sector"]["actual_sha256"],
                                         calls=sum(len(r["calls"]) for r in reports.values()),
                                         lease_checks=sum(len(r["leases"]) for r in reports.values())))
    summary["verdict"] = "EVIDENCE_VERIFIED"
    summary["member_verdicts"] = dict(M06="FAIL / AWAITING STM FULL physical proof",
                                        M13="FAIL / AWAITING STM FULL physical proof")
    destination = root / "verification.json"
    assert not destination.exists()
    destination.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("verdict", "verified_imports", "verified_snapshots")}))


if __name__ == "__main__":
    main()
