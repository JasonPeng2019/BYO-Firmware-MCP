"""Read-only audit of M15 captures, exact source blobs and physical preservation."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git_blobs(lane, commit):
    prefix = "MCP_Server_Dev/BYO-Firmware-MCP/src/"
    listing = subprocess.check_output(["git", "ls-tree", "-rz", commit, prefix], cwd=lane)
    entries = []
    for row in listing.split(b"\0"):
        if row:
            fields, name = row.split(b"\t", 1)
            entries.append((name.decode()[len(prefix) :], fields.split()[2].decode()))
    assert entries
    result = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=lane,
        input="".join(oid + "\n" for _, oid in entries).encode(),
        capture_output=True,
        check=True,
    )
    blobs, offset = {}, 0
    for relative, oid in entries:
        end = result.stdout.index(b"\n", offset)
        actual_oid, kind, size = result.stdout[offset:end].split()
        assert actual_oid.decode() == oid and kind == b"blob"
        offset = end + 1
        data = result.stdout[offset : offset + int(size)]
        offset += int(size) + 1
        assert hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == oid
        blobs[relative] = data
    assert offset == len(result.stdout)
    return blobs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    product = Path(__file__).resolve().parents[1]
    lane = product.parents[1]
    expected_source = "659217848b2be690a59a5b44c689a9348c07e02a"
    old_source = "2f2ada86a380c058ebe4135e1a97abd069a70869"
    current, old = git_blobs(lane, expected_source), git_blobs(lane, old_source)
    report = dict(
        started_utc=datetime.now(timezone.utc).isoformat(),
        captures=[],
        snapshots=0,
        imports=0,
        source_exact=0,
        source_crlf_only=0,
        old_source_exact=0,
        physical={},
        worker_absence=[],
        qualifications=[
            "old-source-behavior-09 raw0 is invalid sensitivity: empty export selected installed integration source.",
            "Early per-import observer could consume validation startup budget; terminated-child exit imports can be unavailable.",
            "Generic control children lack independently observed process identities; parent success is not a child-exit observation.",
            "Before observer correction, inventory process raw exit fields can be unavailable. Never infer exit0.",
        ],
    )
    for result_path in sorted(evidence.glob("*/result.json")):
        directory = result_path.parent
        record = json.loads(result_path.read_text())
        start = json.loads((directory / "start.json").read_text())
        assert start["argv"] == record["argv"] and start["started_utc"] == record["started_utc"]
        assert digest((directory / "inputs.zip").read_bytes()) == record["inputs_zip_sha256"]
        for filename, seal in record["streams"].items():
            data = (directory / filename).read_bytes()
            assert len(data) == seal["bytes"] and digest(data) == seal["sha256"]
        by_path = {}
        with zipfile.ZipFile(directory / "inputs.zip") as archive:
            for row in record["inputs"]:
                data = archive.read(row["member"])
                assert len(data) == row["bytes"] and digest(data) == row["sha256"]
                by_path[str(Path(row["path"]).resolve())] = row
                report["snapshots"] += 1
                if row["member"].startswith("source/"):
                    relative = row["member"][len("source/") :]
                    expected = old[relative] if record.get("old_source") else current[relative]
                    if data == expected:
                        report[
                            "old_source_exact" if record.get("old_source") else "source_exact"
                        ] += 1
                    else:
                        assert (
                            not record.get("old_source")
                            and data.replace(b"\r\n", b"\n") == expected
                        )
                        report["source_crlf_only"] += 1
            assert (
                digest(archive.read("environment/m15_independent_evidence.py"))
                == record["runner"]["sha256"]
            )
        if record.get("old_source"):
            assert record["old_source"]["commit"] == old_source
            assert len(record["old_source"]["raw_git_blobs"]) == len(old)
            for row in record["old_source"]["raw_git_blobs"]:
                relative = row["path"].split("/src/", 1)[1]
                assert digest(old[relative]) == row["sha256"]
        for path in (directory / "imports").glob("*.jsonl"):
            for line in path.read_text().splitlines():
                row = json.loads(line)
                data = (directory / "imports/blobs" / row["sha256"]).read_bytes()
                assert len(data) == row["bytes"] and digest(data) == row["sha256"]
                declared = by_path.get(str(Path(row["path"]).resolve()))
                if declared:
                    assert declared["sha256"] == row["sha256"], row
                if record.get("old_source") and row["module"].startswith("pyocd_debug_mcp"):
                    assert (
                        Path(row["path"])
                        .resolve()
                        .is_relative_to(Path(record["source_root"]).resolve())
                    )
                report["imports"] += 1
        assert record["child_exact_identity_absent"]
        report["captures"].append(
            dict(
                label=directory.name,
                raw_exit=record["raw_exit"],
                unittest=record["unittest"],
                result_sha256=digest(result_path.read_bytes()),
            )
        )
    # The final assertion code is the same oracle actually run against old source.
    with zipfile.ZipFile(evidence / "old-source-behavior-10/inputs.zip") as archive:
        historical = archive.read("tests/test_m15_independent_recovery.py").decode()
    actual = (product / "tests/test_m15_independent_recovery.py").read_text()
    assert ast.dump(ast.parse(historical)) == ast.dump(ast.parse(actual))
    report["old_oracle_ast_identical"] = True
    from pyocd_debug_mcp.kernel.processes import identity_matches

    for board, setup_name, recovery_name, final_name in (
        ("nrf-u02", "setup-continuation", "recover-proof", "final-inspect"),
        ("stm-u02", "setup", "recover", "final-inspect-proof"),
    ):
        root = evidence / board
        phases = {
            name: json.loads((root / name / "physical.json").read_text())
            for name in ("inspect", setup_name, recovery_name, "validate", final_name)
        }
        assert all(
            row["verdict"] == "PASS" and not row["cleanup_errors"] for row in phases.values()
        )
        setup, recovery, validation = phases[setup_name], phases[recovery_name], phases["validate"]
        assert len({row["process"]["pid"] for row in (setup, recovery, validation)}) == 3
        assert len({row["server_run_id"] for row in (setup, recovery, validation)}) == 3
        selected = setup["selected"]
        generation, pointer, backup = (
            Path(selected["generation"]),
            Path(selected["pointer"]),
            Path(selected["backup"]),
        )
        assert generation.read_bytes() == backup.read_bytes()
        assert digest(backup.read_bytes()) == selected["generation_sha256"]
        assert digest(pointer.read_bytes()) == selected["pointer_sha256"]
        assert pointer.read_bytes() == (root / "original-pointer.json").read_bytes()
        assert json.loads(backup.read_bytes()) == selected["document"]
        assert recovery["worker_rpc"] == [] and not recovery.get("connection")
        controlled = {}
        for label in ("workload", "outside", "sram"):
            before = (root / setup_name / f"baseline-{label}.bin").read_bytes()
            after = (root / "validate" / f"after-{label}.bin").read_bytes()
            assert before == after
            assert digest(before) == setup["controlled"][label]["sha256"]
            assert digest(after) == validation["controlled"][label]["sha256"]
            controlled[label] = dict(
                bytes=len(before),
                sha256=digest(before),
                start=setup["controlled"][label]["start"],
                end=setup["controlled"][label]["end"],
            )
        for label in phases["inspect"]["settings"]:
            assert (root / "inspect" / (label + ".bin")).read_bytes() == (
                root / final_name / (label + ".bin")
            ).read_bytes()
        assert phases["inspect"]["physical_identity"] == phases[final_name]["physical_identity"]
        assert phases[final_name]["final_state"].splitlines()[0] == "RUNNING"
        for phase_name, row in phases.items():
            with zipfile.ZipFile(root / phase_name / "external-inputs.zip") as archive:
                for external in row["external_inputs"]:
                    payload = archive.read(external["member"])
                    assert (
                        len(payload) == external["bytes"] and digest(payload) == external["sha256"]
                    )
            assert row["leases"]
            for check in row["leases"]:
                assert {item["resource_id"] for item in check["owned"]} == {
                    "hardware:nrf52840:683854191",
                    "hardware:stm32l476:066FFF514988525067233337",
                }
                assert all(
                    item["run_id"] == row["invocation"]["run_id"]
                    and item["lane_id"] == row["invocation"]["lane_id"]
                    for item in check["owned"]
                )
            for process in row["workers"].values():
                assert process["token"] and not identity_matches(process["pid"], process["token"])
                report["worker_absence"].append(
                    dict(
                        board=board,
                        phase=phase_name,
                        pid=process["pid"],
                        token=process["token"],
                        actual_exit=process.get("raw_exit"),
                        cleanup_confirmed=process.get("cleanup_confirmed"),
                        original_identity_absent=True,
                    )
                )
        report["physical"][board] = dict(
            setup_process=setup["process"],
            recovery_process=recovery["process"],
            validation_process=validation["process"],
            controlled=controlled,
            pointer_sha256=selected["pointer_sha256"],
            generation_sha256=selected["generation_sha256"],
            identity=phases[final_name]["physical_identity"],
            settings=phases[final_name]["settings"],
            setup_validation=setup["validations"],
            fresh_validation=validation["validations"],
            safe_sram_value=validation["safe_sram_value"],
        )
    invocation = json.loads((lane / ".agent-workspace/invocation.json").read_text())
    transcript = (lane / ".agent-workspace/provider-transcript.jsonl").read_bytes().splitlines()[0]
    observed_thread = json.loads(transcript)
    assert observed_thread["type"] == "thread.started"
    status = json.loads(Path(invocation["paths"]["controller_status"]).read_text())
    native_live = {}
    for name in ("controller_identity", "provider_state"):
        identity = status[name]
        value = int(identity["creation_time"].split(":", 1)[1])
        assert identity_matches(identity["pid"], f"win:{value:016x}")
        native_live[name] = identity
    report["native"] = dict(
        invocation=invocation,
        observed_thread=observed_thread,
        first_transcript_line_sha256=digest(transcript),
        live_before_submission=native_live,
        qualification="Still-running provider exit and native retirement belong to ROOT after submission.",
    )
    report.update(finished_utc=datetime.now(timezone.utc).isoformat(), integrity_verdict="PASS")
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            dict(
                integrity_verdict="PASS",
                captures=len(report["captures"]),
                snapshots=report["snapshots"],
                imports=report["imports"],
                original_worker_absence_observations=len(report["worker_absence"]),
            )
        )
    )


if __name__ == "__main__":
    main()
