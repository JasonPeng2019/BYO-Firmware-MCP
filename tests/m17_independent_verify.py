"""Read-only seals and raw outcome audit for this independent M17 assignment."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import zipfile

from tests.m15_independent_verify import git_blobs


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    lane = Path(__file__).resolve().parents[3]
    source = git_blobs(lane, "b01f6ead86e41a4759d8df1ad641d94d5287834f")
    tests = Path(__file__).resolve().parent
    current_tests = ast.parse((tests / "test_m17_independent_deployment.py").read_text())
    current_methods = {
        node.name: ast.dump(node)
        for cls in current_tests.body
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }
    helper_ast = ast.dump(ast.parse((tests / "m17_independent_support.py").read_text()))
    report = dict(
        captures=[],
        snapshots=0,
        imports=0,
        source_bindings=0,
        physical=[],
        method_bindings={},
        executed_images=[],
    )
    for path in sorted(args.evidence.glob("*/result.json")):
        root = path.parent
        record = json.loads(path.read_text())
        assert record["child_exact_identity_absent"]
        assert digest((root / "inputs.zip").read_bytes()) == record["inputs_zip_sha256"]
        for name, seal in record["streams"].items():
            data = (root / name).read_bytes()
            assert len(data) == seal["bytes"] and digest(data) == seal["sha256"]
        with zipfile.ZipFile(root / "inputs.zip") as archive:
            source_differences = []
            for row in record["inputs"]:
                data = archive.read(row["member"])
                assert len(data) == row["bytes"] and digest(data) == row["sha256"]
                report["snapshots"] += 1
                if row["member"].startswith("source/"):
                    expected = source[row["member"][len("source/") :]]
                    if data.replace(b"\r\n", b"\n") != expected.replace(b"\r\n", b"\n"):
                        source_differences.append(row["member"])
                    report["source_bindings"] += 1
            if "sensitivity" not in root.name:
                assert source_differences == [], source_differences
            else:
                assert len(source_differences) == 1, source_differences
            archived_tests = ast.parse(archive.read("tests/test_m17_independent_deployment.py"))
            archived_methods = {
                node.name: ast.dump(node)
                for cls in archived_tests.body
                if isinstance(cls, ast.ClassDef)
                for node in cls.body
                if isinstance(node, ast.FunctionDef)
            }
            archived_helper = ast.dump(ast.parse(archive.read("tests/m17_independent_support.py")))
            if "sensitivity" not in root.name and archived_helper == helper_ast:
                for name in re.findall(
                    r"^(test_\w+) \(tests\.test_m17_independent_deployment\.[^\n]+\) \.\.\. ok$",
                    (root / "stderr.log").read_text(),
                    re.MULTILINE,
                ):
                    if archived_methods[name] == current_methods[name]:
                        report["method_bindings"].setdefault(name, []).append(root.name)
        for table in (root / "imports").glob("*.jsonl"):
            for line in table.read_text().splitlines():
                row = json.loads(line)
                data = (root / "imports/blobs" / row["sha256"]).read_bytes()
                assert digest(data) == row["sha256"] and len(data) == row["bytes"]
                if row["module"].startswith("pyocd_debug_mcp"):
                    assert Path(row["path"]).is_relative_to(Path(record["source_root"]))
                report["imports"] += 1
        report["captures"].append(
            dict(label=root.name, raw_exit=record["raw_exit"], unittest=record["unittest"])
        )
    for path in sorted((args.evidence / "physical").glob("*/*/physical.json")):
        row = json.loads(path.read_text())
        for operation in row.get("programming", []):
            image = Path(operation["artifact"])
            assert digest(image.read_bytes()) == operation["sha256"], image
            report["executed_images"].append(
                dict(
                    path=str(image),
                    sha256=operation["sha256"],
                    physical_record=str(path),
                    qualification="Current retained bytes match the digest recorded for the actual executed image.",
                )
            )
            for block in operation["independent_addressed_readback"]:
                readback = path.parent / f"readback-{operation['role']}-{block['start']:x}.bin"
                data = readback.read_bytes()
                assert digest(data) == block["sha256"] and len(data) == block["bytes"]
        for archive_name, rows_name in (
            ("external-inputs.zip", "external_inputs"),
            ("project-state.zip", "project_state"),
        ):
            archive_path = path.parent / archive_name
            if archive_path.exists():
                with zipfile.ZipFile(archive_path) as archive:
                    for item in row[rows_name]:
                        data = archive.read(item["member"])
                        assert digest(data) == item["sha256"]
        for phase_name in (
            "backup",
            "before",
            "post-deploy",
            "restored-unrelated",
            "final-unrelated",
        ):
            for item in row.get(phase_name, {}).values():
                assert digest(Path(item["path"]).read_bytes()) == item["sha256"]
        from tests.m02_physical_flash import creation

        observed = list(row["workers"].values()) + row.get("observed_children", [])
        independent_absence = []
        for process in observed:
            if not process.get("token"):
                independent_absence.append(
                    dict(pid=process["pid"], qualification="initial creation unavailable")
                )
                continue
            expected = int(process["token"].split(":", 1)[1], 16)
            try:
                actual = int(creation(process["pid"]).split(":", 1)[1])
            except OSError:
                import psutil

                assert not psutil.pid_exists(process["pid"]), process
                actual = None
            assert actual != expected, process
            independent_absence.append(
                dict(
                    pid=process["pid"],
                    expected_filetime=expected,
                    current_filetime=actual,
                    exact_identity_absent=True,
                )
            )
        report["physical"].append(
            dict(
                path=str(path),
                verdict=row["verdict"],
                cleanup_errors=row["cleanup_errors"],
                workers=row["workers"],
                observed_children=row.get("observed_children"),
                independent_os_absence=independent_absence,
            )
        )
    assert set(report["method_bindings"]) == set(current_methods), report["method_bindings"]
    from tests.m02_physical_flash import creation

    invocation = json.loads((lane / ".agent-workspace/invocation.json").read_text())
    status = json.loads((lane / ".agent-workspace/controller.status.json").read_text())
    assert status["run_id"] == invocation["run_id"]
    for process in (status["controller_identity"], status["provider_state"]):
        assert creation(process["pid"]) == process["creation_time"]
    transcript = lane / ".agent-workspace/provider-transcript.jsonl"
    with transcript.open(encoding="utf-8") as stream:
        thread = json.loads(stream.readline())
    assert thread["type"] == "thread.started"
    assert thread["thread_id"] == os.environ["CODEX_SESSION_ID"]
    report["native"] = dict(
        invocation=invocation,
        current_status=status,
        session=thread,
        task_card_sha256=digest((lane / ".agent-workspace/task-card.json").read_bytes()),
    )
    with zipfile.ZipFile(
        args.output.with_suffix(".images.zip"), "w", zipfile.ZIP_DEFLATED
    ) as archive:
        seen = set()
        for image in report["executed_images"]:
            image["member"] = image["sha256"] + ".elf"
            if image["member"] not in seen:
                archive.write(image["path"], image["member"])
                seen.add(image["member"])
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                k: v if k in ("snapshots", "imports", "source_bindings") else len(v)
                for k, v in report.items()
            }
        )
    )


if __name__ == "__main__":
    main()
