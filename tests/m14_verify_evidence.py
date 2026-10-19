"""Finite read-only integrity audit of M14 captures, including retained failures."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checks", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    prefix = "MCP_Server_Dev/BYO-Firmware-MCP/src/"
    exports = {}
    errors, captures, physical, imports = [], [], [], []
    sealed = set()
    process_observations = {}
    from pyocd_debug_mcp.kernel.processes import _start_token

    counters = dict(
        input_rows=0,
        input_bytes=0,
        source_git_comparisons=0,
        external_rows=0,
        imported_sealed_files=0,
        vendor_current_comparisons=0,
    )

    def require(condition, message):
        if not condition:
            errors.append(message)

    def export(commit):
        if commit not in exports:
            argv = ["git", "archive", commit, prefix[:-1]]
            result = subprocess.run(argv, cwd=root, capture_output=True, check=False)
            require(result.returncode == 0, f"Git export {commit}: {result.stderr!r}")
            files = {}
            if result.returncode == 0:
                with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
                    files = {
                        m.name[len(prefix) :]: archive.extractfile(m).read()
                        for m in archive.getmembers()
                        if m.isfile()
                    }
            exports[commit] = files
        return exports[commit]

    for path in sorted(args.checks.glob("*/result.json")):
        record = json.loads(path.read_text())
        captures.append(
            dict(
                path=str(path),
                sha256=digest(path.read_bytes()),
                raw_exit=record["raw_exit"],
                started_utc=record["started_utc"],
                finished_utc=record["finished_utc"],
                argv=record["argv"],
            )
        )
        for item in record["sealed_outputs"]:
            file = path.parent / item["path"]
            require(digest(file.read_bytes()) == item["sha256"], f"Changed output {file}")
        commit = (record.get("source_export") or {}).get("selected_source_commit") or args.candidate
        source = export(commit)
        with zipfile.ZipFile(path.parent / "inputs.zip") as archive:
            for item in record["input_rows"]:
                data = archive.read(item["entry"])
                require(
                    digest(data) == item["sha256"] and len(data) == item["bytes"],
                    f"Bad sealed input {path}:{item['entry']}",
                )
                counters["input_rows"] += 1
                counters["input_bytes"] += len(data)
                sealed.add((str(Path(item["path"]).resolve()).casefold(), item["sha256"]))
                if item["entry"].startswith("source/"):
                    relative = item["entry"][len("source/") :]
                    require(
                        source.get(relative) == data, f"Source differs from {commit}: {relative}"
                    )
                    counters["source_git_comparisons"] += 1

    for pattern in ("**/physical.json", "**/continuation.json"):
        for path in sorted(args.checks.glob(pattern)):
            record = json.loads(path.read_text())
            physical.append(
                dict(
                    path=str(path),
                    sha256=digest(path.read_bytes()),
                    verdict=record["verdict"],
                    phase=record.get("phase", record.get("stage")),
                    workers=record["workers"],
                    cleanup_errors=record["cleanup_errors"],
                )
            )
            for row in record.get("imports", []):
                imports.append(row)
            external = path.parent / "external-inputs.zip"
            if record.get("external_inputs"):
                with zipfile.ZipFile(external) as archive:
                    for item in record["external_inputs"]:
                        data = archive.read(item["entry"])
                        require(
                            digest(data) == item["sha256"],
                            f"Bad external input {path}:{item['entry']}",
                        )
                        counters["external_rows"] += 1
            observer = record.get("executed_observer")
            if observer:
                require(
                    digest(Path(observer["path"]).read_bytes()) == observer["sha256"],
                    f"Changed executed observer {path}",
                )
            if record["verdict"] == "PASS":
                for worker in record["workers"].values():
                    key = f"{worker['pid']}:{worker.get('token')}"
                    current = _start_token(worker["pid"])
                    absent = bool(worker.get("token") and current != worker["token"])
                    process_observations[key] = dict(
                        pid=worker["pid"],
                        expected_token=worker.get("token"),
                        current_token=current,
                        exact_identity_absent=absent,
                        raw_exit=worker.get("raw_exit", "unavailable"),
                        argv=worker["argv"],
                        observed_in_record=str(path),
                    )
                    require(absent, f"Exact observed process still live/unproven {path}:{key}")
                    if "raw_exit" in worker:
                        require(
                            worker["raw_exit"] is not None
                            and worker["cleanup_confirmed"]
                            and worker["exact_identity_absent"],
                            f"Unproven provider cleanup {path}:{key}",
                        )
                    # Inventory observations carry only pid/token/argv. Current
                    # OS absence is proof of absence, never a reconstructed exit.

    for pattern in ("**/imports.json", "**/child-imports/*.json"):
        for path in args.checks.glob(pattern):
            imports += json.loads(path.read_text())
    for row in imports:
        path = Path(row["path"]).resolve()
        if (str(path).casefold(), row["sha256"]) in sealed:
            counters["imported_sealed_files"] += 1
        else:
            # External installed dependencies have actual recorded fingerprints,
            # but their source was not archived by the command recorder.
            require(
                path.is_file() and digest(path.read_bytes()) == row["sha256"],
                f"Available external import no longer matches {row['module']}:{path}",
            )
            counters["vendor_current_comparisons"] += 1

    report = dict(
        observed_utc=datetime.now(timezone.utc).isoformat(),
        candidate=args.candidate,
        scope="Capture integrity only; raw failures remain failures, no module acceptance",
        captures=captures,
        physical=physical,
        process_observations=process_observations,
        counters=counters,
        errors=errors,
        qualification="Empty pre-m14-behavior directory came from an early recorder export failure before tests. No missing execution-time metadata is reconstructed. Historical failed nrf-u04-01 has one unavailable creation token; it is not a passing cleanup proof. Installed dependency fingerprints are compared with current unchanged files, not claimed archived source.",
        verdict="PASS" if not errors else "FAIL",
    )
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            dict(
                verdict=report["verdict"],
                counters=counters,
                captures=len(captures),
                physical=len(physical),
                errors=errors[:20],
            )
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
