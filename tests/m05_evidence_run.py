"""Finite independent check recorder; evidence and generated projects stay external."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    output = args.output.resolve() / args.label
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / "executed-inputs"
    inputs.mkdir()
    for path in Path("tests").glob("*m05*.py"):
        shutil.copyfile(path, inputs / path.name)
    for name in (
        "tiered_acceptance_support.py",
        "test_tiered_acceptance_lite.py",
        "m02_independent_support.py",
    ):
        shutil.copyfile(Path("tests") / name, inputs / name)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    env = {
        **os.environ,
        **{
            name: str(output / sub)
            for name, sub in (
                ("BYO_MANAGED_TEST_EVIDENCE", "managed"),
                ("BYO_MCP_ARTIFACT_ROOT", "project"),
                ("BYO_MCP_MONITOR_ROOT", "monitor"),
            )
        },
    }
    # Read-only import observation in each child, including its fresh-process
    # clients. It changes no policy, dispatch, guard or hardware implementation.
    observer = output / "import-observer"
    observer.mkdir()
    (observer / "sitecustomize.py").write_bytes(
        (
            "import atexit, hashlib, json, os, sys\nfrom pathlib import Path\n"
            "def record_imports():\n"
            " rows=[]\n"
            " for name,module in sorted(sys.modules.items()):\n"
            "  file=getattr(module,'__file__',None)\n"
            "  if name.startswith(('pyocd_debug_mcp','tests.')) and file and Path(file).is_file():\n"
            "   p=Path(file).resolve(); data=p.read_bytes()\n"
            "   rows.append({'module':name,'path':str(p),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})\n"
            f" destination=Path({str(output / 'observed-imports')!r}); destination.mkdir(exist_ok=True)\n"
            " (destination/(str(os.getpid())+'.json')).write_bytes((json.dumps(rows,indent=2)+'\\n').encode())\n"
            "atexit.register(record_imports)\n"
        ).encode()
    )
    env["PYTHONPATH"] = str(observer) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    record = {
        "argv": command,
        "cwd": str(Path.cwd()),
        "started_at": now(),
        "environment_overrides": {
            key: env[key]
            for key in (
                "BYO_MANAGED_TEST_EVIDENCE",
                "BYO_MCP_ARTIFACT_ROOT",
                "BYO_MCP_MONITOR_ROOT",
                "PYTHONPATH",
            )
        },
        "runner": {"path": __file__, "sha256": sha(__file__)},
    }
    record["executed_helpers"] = [
        {"path": str(path), "sha256": sha(path)} for path in inputs.glob("*.py")
    ]
    (output / "command.json").write_text(json.dumps(record, indent=2) + "\n")
    with (output / "stdout.log").open("wb") as stdout, (output / "stderr.log").open("wb") as stderr:
        result = subprocess.run(command, env=env, stdout=stdout, stderr=stderr, check=False)
    record.update(
        finished_at=now(),
        exit_code=result.returncode,
        stdout_sha256=sha(output / "stdout.log"),
        stderr_sha256=sha(output / "stderr.log"),
    )
    (output / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    print(
        json.dumps({key: record[key] for key in ("argv", "started_at", "finished_at", "exit_code")})
    )
    print((output / "stdout.log").read_text(errors="replace")[-12000:])
    print((output / "stderr.log").read_text(errors="replace")[-12000:])
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
