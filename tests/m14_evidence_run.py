"""Finite M14 command capture; seals executed inputs and preserves raw outcomes."""

from __future__ import annotations

import argparse
import hashlib
import io
from importlib import metadata as distribution_metadata
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--old-commit")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    product = Path(__file__).resolve().parents[1]
    source = (args.source_root or product / "src").resolve()
    output = args.output.resolve() / args.label
    output.mkdir(parents=True, exist_ok=False)
    export_record = None
    if args.old_commit:
        assert args.source_root is None
        prefix = "MCP_Server_Dev/BYO-Firmware-MCP/src/"
        argv = ["git", "archive", args.old_commit, prefix[:-1]]
        exported = subprocess.run(argv, cwd=product.parents[1], capture_output=True, check=False)
        assert exported.returncode == 0, exported.stderr.decode(errors="replace")
        (output / "old-source.tar").write_bytes(exported.stdout)
        source = output / "old-source"
        source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(exported.stdout)) as archive:
            for member in archive.getmembers():
                if member.isfile():
                    assert member.name.startswith(prefix)
                    destination = source / member.name[len(prefix) :]
                    assert destination.resolve().is_relative_to(source.resolve())
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.extractfile(member).read())
        export_record = {
            "argv": argv,
            "raw_exit": exported.returncode,
            "stderr": exported.stderr.decode(errors="replace"),
            "tar_sha256": hashlib.sha256(exported.stdout).hexdigest(),
            "selected_source_commit": args.old_commit,
        }
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    env = dict(os.environ)
    overrides = {
        "BYO_MCP_ARTIFACT_ROOT": str(output / "project"),
        "BYO_MCP_MONITOR_ROOT": str(output / "monitor"),
        "BYO_MANAGED_TEST_EVIDENCE": str(output / "managed"),
        "PYTHONPATH": str(source) + os.pathsep + str(product),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(overrides)
    rows = []
    with zipfile.ZipFile(output / "inputs.zip", "w", zipfile.ZIP_DEFLATED) as archive:

        def static_inputs(directory):
            return (
                path
                for path in directory.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix in {".py", ".json", ".yaml", ".yml", ".elf", ".bin", ".hex"}
            )

        groups = [
            ("source", static_inputs(source)),
            ("tests", static_inputs(product / "tests")),
            ("environment", (product / name for name in ("pyproject.toml", "uv.lock"))),
        ]
        for group, paths in groups:
            for path in sorted(paths):
                data = path.read_bytes()
                relative = path.relative_to(source if group == "source" else product)
                entry = group + "/" + relative.as_posix()
                archive.writestr(entry, data)
                rows.append(
                    {
                        "path": str(path),
                        "entry": entry,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
    metadata = {
        "started_utc": now(),
        "argv": command,
        "cwd": str(product),
        "python": sys.executable,
        "python_version": sys.version,
        "distribution_versions": {
            name: distribution_metadata.version(name)
            for name in ("pyocd", "mcp", "ruff", "pyright")
        },
        "environment_overrides": overrides,
        "runner": str(Path(__file__).resolve()),
        "input_rows": rows,
    }
    metadata["source_export"] = export_record
    for field, argv in (
        ("head", ["git", "rev-parse", "HEAD"]),
        ("status", ["git", "status", "--short"]),
        (
            "trees",
            [
                "git",
                "rev-parse",
                "HEAD:MCP_Server_Dev/BYO-Firmware-MCP/src",
                "HEAD:MCP_Server_Dev/BYO-Firmware-MCP/tests",
            ],
        ),
    ):
        result = subprocess.run(argv, cwd=product, capture_output=True, text=True, check=False)
        metadata[field] = {
            "argv": argv,
            "exit": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    (output / "command.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"started_utc": metadata["started_utc"], "argv": command, "output": str(output)}
        ),
        flush=True,
    )
    with (output / "stdout.log").open("wb") as stdout, (output / "stderr.log").open("wb") as stderr:
        result = subprocess.run(
            command, cwd=product, env=env, stdout=stdout, stderr=stderr, check=False
        )
    metadata.update(finished_utc=now(), raw_exit=result.returncode)
    metadata["sealed_outputs"] = [
        {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in (output / "inputs.zip", output / "stdout.log", output / "stderr.log")
    ]
    (output / "result.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps({"finished_utc": metadata["finished_utc"], "raw_exit": result.returncode}))
    print((output / "stdout.log").read_text(encoding="utf-8", errors="replace")[-10000:])
    print((output / "stderr.log").read_text(encoding="utf-8", errors="replace")[-10000:])
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
