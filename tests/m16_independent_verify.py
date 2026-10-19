"""Read-only M16-T capture audit; raw failures remain failures."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

from tests.m15_independent_verify import git_blobs


BASE = "8bfb8d04c6dd5f722c5ebcbe76ed116d859bf261"
PRIOR = "80f37fe342d04f9ebf94a234a25b76ef4830b5e5"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def syntax(data):
    return ast.dump(ast.parse(data))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    product = Path(__file__).resolve().parents[1]
    lane = product.parents[1]
    sources = {BASE: git_blobs(lane, BASE), PRIOR: git_blobs(lane, PRIOR)}
    report = dict(
        utc=datetime.now(timezone.utc).isoformat(),
        argv=__import__("sys").argv,
        cwd=str(Path.cwd()),
        source_base=BASE,
        captures=[],
        inputs=0,
        imports=0,
        source_rows=0,
        qualifications=[
            "All factory research/observations and target I/O are synthetic host fixtures; no physical or vendor identity proof.",
            "The unchanged M15 capture observes its direct finite child identity and exit; arbitrary product grandchildren can lack independent exit/import metadata.",
            "Absent child import records are unavailable, not reconstructed or treated as exit0. No real target worker is used by the synthetic backend.",
            "Normal maintenance may publish a new pointer to identical committed record bytes; exact recovery/restart compares the unchanged pointer as well.",
        ],
    )
    for result_path in sorted(evidence.glob("*/result.json")):
        directory = result_path.parent
        record = json.loads(result_path.read_bytes())
        if "inputs_zip_sha256" not in record:
            continue
        start = json.loads((directory / "start.json").read_bytes())
        assert start["argv"] == record["argv"]
        assert start["started_utc"] == record["started_utc"]
        assert digest((directory / "inputs.zip").read_bytes()) == record["inputs_zip_sha256"]
        for filename, seal in record["streams"].items():
            data = (directory / filename).read_bytes()
            assert len(data) == seal["bytes"] and digest(data) == seal["sha256"]
        old = record.get("old_source")
        expected = sources[old["commit"] if old else BASE]
        by_path = {}
        with zipfile.ZipFile(directory / "inputs.zip") as archive:
            for row in record["inputs"]:
                data = archive.read(row["member"])
                assert len(data) == row["bytes"] and digest(data) == row["sha256"], row
                by_path[str(Path(row["path"]).resolve())] = row
                if row["member"].startswith("source/"):
                    relative = row["member"][len("source/") :]
                    assert data.replace(b"\r\n", b"\n") == expected[relative].replace(
                        b"\r\n", b"\n"
                    ), row
                    report["source_rows"] += 1
                report["inputs"] += 1
            assert (
                digest(archive.read("environment/m15_independent_evidence.py"))
                == record["runner"]["sha256"]
            )
        if old:
            assert len(old["raw_git_blobs"]) == len(expected)
            for row in old["raw_git_blobs"]:
                relative = row["path"].split("/src/", 1)[1]
                assert digest(expected[relative]) == row["sha256"]
        for path in (directory / "imports").glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                data = (directory / "imports/blobs" / row["sha256"]).read_bytes()
                assert len(data) == row["bytes"] and digest(data) == row["sha256"]
                declared = by_path.get(str(Path(row["path"]).resolve()))
                if declared:
                    assert declared["sha256"] == row["sha256"], row
                if row["module"].startswith("pyocd_debug_mcp"):
                    assert (
                        Path(row["path"])
                        .resolve()
                        .is_relative_to(Path(record["source_root"]).resolve())
                    ), row
                report["imports"] += 1
        assert record["child_exact_identity_absent"]
        report["captures"].append(
            dict(
                label=directory.name,
                raw_exit=record["raw_exit"],
                unittest=record["unittest"],
                result_sha256=digest(result_path.read_bytes()),
                child=record["child"],
                child_exact_identity_absent=True,
            )
        )
    # Bind the exact final regression functions used on old source to current
    # oracles, without crediting a merely similar or reconstructed test.
    with zipfile.ZipFile(evidence / "prior-source-09/inputs.zip") as archive:
        old_oracle = archive.read("tests/test_m16_independent_partition.py")
    assert syntax(old_oracle) == syntax(
        (product / "tests/test_m16_independent_partition.py").read_bytes()
    )
    report["prior_oracle_ast_identical"] = True
    with zipfile.ZipFile(evidence / "independent-03/inputs.zip") as archive:
        old_recovery = archive.read("tests/test_m16_independent_recovery.py")
        old_helper = ast.parse(archive.read("tests/m16_independent_support.py"))

    def methods(data):
        tree = ast.parse(data)
        return {
            n.name: ast.dump(n)
            for cls in tree.body
            if isinstance(cls, ast.ClassDef)
            for n in cls.body
            if isinstance(n, ast.FunctionDef)
        }

    current_methods = methods((product / "tests/test_m16_independent_recovery.py").read_bytes())
    for name, node in methods(old_recovery).items():
        assert current_methods[name] == node
    current_helper = ast.parse((product / "tests/m16_independent_support.py").read_bytes())

    def corpus(tree):
        return next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "partitioned_recovery_corpus"
        )

    assert ast.dump(corpus(old_helper)) == ast.dump(corpus(current_helper))
    report["earlier_five_recovery_oracles_and_corpus_ast_identical"] = True
    report["abrupt_children"] = []
    for path in (evidence / "recovery-child-10/managed").rglob("events.json"):
        for row in json.loads(path.read_bytes()):
            if "abrupt" in row:
                assert row["raw_exit"] == 86 and row["exact_identity_absent"]
                assert isinstance(row["observed"]["pid"], int)
                # A Windows venv launcher can spawn the Python interpreter
                # that writes the proof. Only the launcher has an observed
                # creation token/exit here. Never equate those two identities
                # or reconstruct the inner interpreter's missing lifecycle.
                report["abrupt_children"].append(
                    dict(
                        row,
                        proof_pid_matches_launch_pid=row["observed"]["pid"] == row["pid"],
                        proof_process_lifecycle=(
                            "same observed process"
                            if row["observed"]["pid"] == row["pid"]
                            else "independent creation token/raw exit/cleanup unavailable; launch exit and absence observed separately"
                        ),
                    )
                )
    assert len(report["abrupt_children"]) == 2
    report["current_files"] = [
        dict(path=str(p), sha256=digest(p.read_bytes()))
        for p in (
            product / "tests/m16_independent_support.py",
            product / "tests/test_m16_independent_partition.py",
            product / "tests/test_m16_independent_recovery.py",
            Path(__file__).resolve(),
        )
    ]
    report["git"] = subprocess.check_output(
        [
            "git",
            "rev-parse",
            "HEAD",
            "HEAD:MCP_Server_Dev/BYO-Firmware-MCP/src",
            "HEAD:MCP_Server_Dev/BYO-Firmware-MCP/tests",
        ],
        cwd=lane,
        text=True,
    ).splitlines()
    report["audit_passed"] = True
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("captures", "inputs", "imports", "source_rows", "audit_passed")
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
