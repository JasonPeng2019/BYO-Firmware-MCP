"""Read-only verification of the single B02-R01 attempt and fresh restoration."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import yaml


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--attempt", default="physical-stm-r01")
    parser.add_argument("--prefix", default="r01")
    parser.add_argument("--retry", type=int, default=3)
    parser.add_argument("--expect", choices=("client-failure", "public-failure", "pass"), default="client-failure")
    parser.add_argument("--output", default="verification-r01.json")
    args = parser.parse_args()
    root = args.evidence.resolve()
    physical = root / args.attempt
    destination = root / args.output
    assert not destination.exists()
    invocation = load(args.invocation)
    summary = dict(at=datetime.now(timezone.utc).isoformat(), run_id=invocation["run_id"],
                   commands=[], physical=[], snapshots=0, imports=0, historical={})
    historical = root / "verification.json"
    assert sha(historical) == "a89552cb3735d3368b9d6ee80ac1794a4849ce875f30d3cd5f07517f57ef33e2"
    for relative in ("verification.json", "final-members-08/result.json", "final-controls-09/result.json",
                     "physical-nrf-02/full.json", "physical-stm-04/full.json"):
        path = root / relative
        summary["historical"][relative] = dict(path=str(path), sha256=sha(path))
    reports = {}
    required = set(invocation["exclusive_resources"])
    assert required == {"hardware:nrf52840:683854191", "hardware:stm32l476:066FFF514988525067233337"}
    for stage, label, filename, exit_code in (
        ("prepare", f"{args.prefix}-stm-prepare-01", "prepare.json", 0),
        ("full", f"{args.prefix}-stm-full-retry{args.retry}-01", f"full-retry-{args.retry}.json", 0 if args.expect == "pass" else 1),
        ("restore", f"{args.prefix}-stm-restore-01", "restore.json", 0),
    ):
        command = load(root / label / "result.json")
        assert command["exit_code"] == exit_code
        for stream in ("stdout", "stderr"):
            assert sha(root / label / f"{stream}.log") == command[f"{stream}_sha256"]
        inputs = {str(Path(row["path"]).resolve()).casefold(): row for row in command["inputs"]}
        for row in inputs.values():
            assert sha(Path(row["snapshot"])) == row["sha256"]
            summary["snapshots"] += 1
        observed = [row for path in (root / label / "imports").glob("*.json") for row in load(path)]
        assert any(row["module"] == "pyocd_debug_mcp.server" for row in observed)
        for row in observed:
            if row["module"].startswith(("pyocd_debug_mcp", "tests.")):
                assert inputs[str(Path(row["path"]).resolve()).casefold()]["sha256"] == row["sha256"]
                summary["imports"] += 1
        path = physical / filename
        report = reports[stage] = load(path)
        assert report["verdict"] == ("FAIL" if exit_code else "PASS")
        assert report["invocation"] == invocation and report["cleanup_errors"] == []
        helper = next(row for row in inputs.values() if Path(row["path"]).name == "b02_physical.py")
        assert helper["sha256"] == report["helper_sha256"]
        assert report["workers"] and report["known_children"]
        for pid, worker in report["workers"].items():
            assert worker["cleanup_confirmed"] and worker["exact_identity_absent"] and worker["raw_exit"] is not None
            actual = report["known_children"][pid]["actual_environment"]
            assert actual["BYO_MCP_ARTIFACT_ROOT"] == report["project"]
            assert actual["PYOCD_PROJECT_DIR"] == report["project"]
            assert actual["BYO_MCP_MONITOR_ROOT"] == str(Path(report["project"]) / "monitor")
            assert actual["BYO_MANAGED_TEST_EVIDENCE"] == str(physical / "managed")
        assert all(row["exact_identity_absent"] and row["raw_exit"] is not None for row in report["known_children"].values())
        assert "RUNNING" in report["cleanup_state"]
        assert report["calls"][-1]["tool"] == "disconnect" and "Disconnected" in report["calls"][-1]["reply"]
        assert report["leases"]
        for check in report["leases"]:
            assert {row["resource_id"] for row in check["leases"]} == required
            for row in check["leases"]:
                assert row["run_id"] == invocation["run_id"] and row["lane_id"] == invocation["lane_id"]
                assert row["pid"] == check["controller"]["pid"] and row["creation_time"] == check["controller"]["creation_time"]
        summary["commands"].append({key: command[key] for key in ("argv", "started_at", "finished_at", "exit_code")}
                                   | dict(label=label, unittest_counts=None, qualification="Physical helper, no unittest run"))
        summary["physical"].append(dict(stage=stage, path=str(path), sha256=sha(path), calls=len(report["calls"]),
                                        lease_checks=len(report["leases"]), workers=report["workers"], known_children=report["known_children"]))
    prepare, full, restore = (reports[key] for key in ("prepare", "full", "restore"))
    assert prepare["finished_at"] < full["started_at"] < full["finished_at"] < restore["started_at"]
    assert list(physical.glob("full*.json")) == [physical / f"full-retry-{args.retry}.json"]
    assert prepare["physical_identity"] == restore["physical_identity"]
    original, controlled = (physical / name for name in ("sector-original.bin", "sector-controlled.bin"))
    reads = [row for row in prepare["calls"] if row["tool"] == "read_memory_raw" and row["arguments"]["address"] == 0x08080000]
    assert bytes.fromhex(json.loads(reads[0]["reply"].splitlines()[0])["result"]) == original.read_bytes()
    assert len(original.read_bytes()) == len(controlled.read_bytes()) == 2048
    assert original.read_bytes() != controlled.read_bytes() != b"\xff" * 2048
    assert prepare["outside_sector"]["actual_sha256"] == sha(controlled)
    assert restore["outside_sector"]["actual_sha256"] == sha(original)
    summary["fresh_preparation"] = {name: dict(path=str(path), sha256=sha(path)) for name, path in (
        ("report", physical / "prepare.json"), ("saved_original", original), ("controlled", controlled))}
    connect = next(row for row in full["calls"] if row["tool"] == "connect")
    assert "Connected" in connect["reply"]
    overview = json.loads(next(row["reply"] for row in full["calls"] if row["tool"] == "setup_overview"))
    route = overview["routes"][0]
    if route["route"] == "validate":
        assert connect["arguments"] == dict(board_id="b02_physical_stm32l476")
    else:
        assert route["route"] == "no-setup" and "plan_action_parameters_template" not in route
        assert connect["arguments"] == dict(board_id="b02_physical_stm32l476", probe_uid="066FFF514988525067233337", target="stm32l476rgtx")
    if args.expect == "client-failure":
        assert "KeyError: 'plan_action_parameters_template'" in full["traceback"]
        assert not any(row["tool"] in {"board_validate", "board_setup", "board_fix_setup", "read_memory_symbol", "flash_application", "set_breakpoint"} for row in full["calls"])
    else:
        if route["route"] == "validate":
            assert any(row["tool"] == route["next_call"]["tool"] and row["arguments"] == route["next_call"]["arguments"] for row in full["calls"])
        else:
            populated = next(row for row in full["calls"] if row["tool"] == "board_setup-plan" and row["arguments"]["board_id"])
            accepted = json.loads(populated["reply"])
            assert accepted["status"] == "plan_accepted"
            action = populated["arguments"]["action_parameters"]
            assert action["mode"] == "setup" and action["target_tier"] == "setup-full"
            assert action["connection_id"] in {row["connection_id"] for row in overview["connections"]}
            assert any(row["tool"] == accepted["stable_client_fallback"]["tool_name"]
                       and row["arguments"] == accepted["stable_client_fallback"]["arguments"] for row in full["calls"])
            summary["accepted_setup_plan"] = accepted
        assert full["validation"]["status"] == "validation_passed"
        assert full["raw_identity_source"]["sha256"] == sha(physical / "prepare.json")
        summary["validation"] = full["validation"]
        refusal = next(row for row in full["calls"] if row["tool"] == "action_batch"
                       and "has no application deployment allocation yet" in row.get("reply", ""))
        assert not any(refusal["started_at"] <= row["at"] <= refusal["finished_at"] for row in full["rpc"])
        summary["initial_no_allocation_refusal"] = refusal
        if args.expect == "public-failure":
            failure = next(row for row in full["calls"] if row["tool"] == "board_safety_refresh" and row.get("error"))
            assert "exceeded its 30s operation timeout" in failure["error"]
            assert not any(row["tool"] in {"flash_application", "read_memory_symbol", "set_breakpoint"} for row in full["calls"])
            summary["public_failure"] = failure
    project = Path(full["project"])
    assert full["retry_source"]["sha256"] == sha(Path(full["retry_source"]["previous_report"]))
    pack_manifest = project / ".firm/packs/manifest.yaml"
    spec = next(row for row in yaml.safe_load(pack_manifest.read_text())["packs"] if "stm32l476rgtx" in row["provides_targets"])
    pack = project / ".firm/packs/files" / spec["filename"]
    assert sha(pack) == spec["sha256"] == prepare["pack"]["actual_sha256"]
    summary["pinned_pack"] = dict(path=str(pack), sha256=sha(pack), manifest_sha256=sha(pack_manifest),
                                   selected_inside_worker="Unavailable: no selection observation was captured; null open pack arguments delegate selection to the child")
    if args.expect != "client-failure":
        if "selected_worker_cleanup" in full:
            cleanups = [row for report in reports.values() for row in report["selected_worker_cleanup"]]
            assert cleanups and all(row["token"] and row["exact_identity_absent"] for row in cleanups)
            observed_pids = {row["pid"] for row in cleanups}
            summary["worker_descendants_cleanup"] = cleanups
        else:
            cleanup = load(physical / "worker-descendants-cleanup.json")
            assert cleanup["run_id"] == invocation["run_id"]
            assert all(row["exact_identity_absent"] for row in cleanup["processes"])
            assert all(row["pid_absent"] for row in cleanup["selected_worker_pids"])
            observed_pids = {row["pid"] for row in cleanup["selected_worker_pids"]}
            summary["worker_descendants_cleanup"] = dict(path=str(physical / "worker-descendants-cleanup.json"),
                                                         sha256=sha(physical / "worker-descendants-cleanup.json"), proof=cleanup)
        selections = []
        for stage, report in reports.items():
            phase = f"full-retry-{args.retry}" if stage == "full" else stage
            rows = [json.loads(line) for path in (physical / (phase + "-pack-selection")).glob("*.jsonl")
                    for line in path.read_text().splitlines()]
            # Configured FULL connect passes the qualified pinned package to
            # the loader; raw explicit-target connect selects its manifest pack.
            assert any(row["function"] in {"_cmsis_pack_for", "_quarantined_cmsis_pack"} for row in rows)
            for row in rows:
                assert row["pid"] in observed_pids
                if "token" in row:
                    assert report["workers"][str(row["parent_pid"])]["token"] == row["parent_token"]
                assert row["artifact_root"] == report["project"]
                assert row["payload_sha256"] == row["expected_sha256"] == sha(pack)
                assert sha(Path(row["path"])) == sha(pack)
            selections.extend(rows)
        summary["pinned_pack"]["selected_inside_worker"] = selections
    if args.expect == "pass":
        assert full["validation"]["status"] == "validation_passed"
        assert full["raw_identity_source"]["sha256"] == sha(physical / "prepare.json")
        assert len(full["symbol_equality"]) == 2
        assert all(row["address_value"] == row["direct_value"] == row["batch_value"] for row in full["symbol_equality"])
        assert full["persisted_allocation"]["deployment_policy"]["kind"] != "none"
        assert full["breakpoint_hit"] and full["outside_preserved"]["before_sha256"] == full["outside_preserved"]["after_sha256"] == sha(controlled)
        plans = [row["arguments"]["action_parameters"] for row in full["calls"]
                 if row["tool"] == "set_breakpoint-plan" and row["arguments"]["board_id"]]
        assert len(plans) >= 3 and all(row == plans[0] for row in plans)
        assert plans[0]["symbol_or_address"] == "repair_step_probe"
        deployed = next(row for row in full["calls"] if row["tool"] == "flash_application")
        assert f"read back {full['artifact']['addressed_bytes']} matching addressed bytes" in deployed["reply"]
        refresh = next(row for row in full["calls"] if row["tool"] == "board_safety_refresh")
        assert json.loads(refresh["reply"])["status"] == "safety_refresh_completed"
        assert any(row["tool"] == "action_batch" and "has no application deployment allocation yet" in row.get("reply", "")
                   and refresh["finished_at"] < row["started_at"] < deployed["started_at"] for row in full["calls"])
        summary["m06"] = dict(verdict="PASS", symbol_equality=full["symbol_equality"])
        summary["m13"] = dict(verdict="PASS", breakpoint_hit=full["breakpoint_hit"], allocation=full["persisted_allocation"])
    summary["route"] = route
    summary["limitation"] = ("Explicit connect succeeded; client assumed the fresh-setup template for an existing no-setup route. No populated repair/validation or member FULL workflow was executed. No new production defect is established."
                             if args.expect == "client-failure" else full.get("traceback", full["limitation"]))
    summary["verdict"] = "EVIDENCE_VERIFIED"
    summary["member_verdicts"] = (dict(M06="PASS / ROOT acceptance pending", M13="PASS / ROOT acceptance pending")
        if args.expect == "pass" else dict(M06="FAIL / AWAITING STM direct/batch/planned-address symbol equality",
                                           M13="FAIL / AWAITING STM refresh/deployment/same-breakpoint-hit workflow"))
    destination.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(dict(report=str(destination), verdict=summary["verdict"], snapshots=summary["snapshots"], imports=summary["imports"])))


if __name__ == "__main__":
    main()
