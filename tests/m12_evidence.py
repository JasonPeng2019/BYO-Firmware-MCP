"""Finite M12 command capture: frozen inputs, isolated roots and passive imports."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile


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
    output = (args.output / args.label).resolve()
    output.mkdir(parents=True, exist_ok=False)
    product = Path.cwd().resolve()
    paths = sorted(
        p
        for directory in ("src", "tests")
        for p in (product / directory).rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".json", ".yaml", ".toml"}
        and "__pycache__" not in p.parts
    )
    paths += [product / name for name in ("pyproject.toml", "uv.lock")]
    inputs = []
    with zipfile.ZipFile(output / "inputs.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            relative = path.relative_to(product).as_posix()
            data = path.read_bytes()
            archive.writestr(relative, data)
            inputs.append(
                dict(
                    path=str(path),
                    member=relative,
                    bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )
    environment = dict(os.environ)
    for variable, directory in (
        ("BYO_MCP_ARTIFACT_ROOT", "project"),
        ("BYO_MCP_MONITOR_ROOT", "monitor"),
        ("BYO_MANAGED_TEST_EVIDENCE", "managed"),
        ("PYOCD_MCP_RUNS_ROOT", "runs"),
    ):
        environment[variable] = str(output / directory)
        Path(environment[variable]).mkdir()
    environment.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    observer = output / "observer"
    observer.mkdir()
    observer_source = (
        "import atexit, hashlib, json, os, sys\nfrom pathlib import Path\n"
        "def capture():\n"
        " rows=[]\n"
        " for name,module in sorted(sys.modules.items()):\n"
        "  file=getattr(module,'__file__',None)\n"
        "  if name.startswith(('pyocd_debug_mcp','tests.','pyocd.')) and file and Path(file).is_file():\n"
        "   p=Path(file).resolve(); data=p.read_bytes()\n"
        "   rows.append(dict(module=name,path=str(p),bytes=len(data),sha256=hashlib.sha256(data).hexdigest()))\n"
        f" destination=Path({str(output / 'imports')!r}); destination.mkdir(exist_ok=True)\n"
        " (destination/(str(os.getpid())+'.json')).write_text(json.dumps(rows,indent=2),encoding='utf-8')\n"
        "atexit.register(capture)\n"
    )
    (observer / "sitecustomize.py").write_text(observer_source, encoding="utf-8")
    environment["PYTHONPATH"] = os.pathsep.join((str(observer), str(product / "src"), str(product)))
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    identity = subprocess.run(
        ["git", "rev-parse", "HEAD", "HEAD:MCP_Server_Dev/BYO-Firmware-MCP/src"],
        capture_output=True,
        text=True,
        check=True,
    )
    record = dict(
        started_at=now(),
        argv=command,
        cwd=str(product),
        inputs=inputs,
        inputs_zip_sha256=sha(output / "inputs.zip"),
        git_identity=identity.stdout.splitlines(),
        environment={
            k: environment[k]
            for k in (
                "PYTHONPATH",
                "UV_PROJECT_ENVIRONMENT",
                "BYO_MCP_ARTIFACT_ROOT",
                "BYO_MCP_MONITOR_ROOT",
                "BYO_MANAGED_TEST_EVIDENCE",
                "PYOCD_MCP_RUNS_ROOT",
                "PYTHONIOENCODING",
                "PYTHONDONTWRITEBYTECODE",
            )
        },
    )
    (output / "before.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    with (output / "stdout.log").open("wb") as stdout, (output / "stderr.log").open("wb") as stderr:
        result = subprocess.run(command, env=environment, stdout=stdout, stderr=stderr, check=False)
    record.update(
        finished_at=now(),
        raw_exit=result.returncode,
        stdout_sha256=sha(output / "stdout.log"),
        stderr_sha256=sha(output / "stderr.log"),
    )
    (output / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps({k: record[k] for k in ("argv", "started_at", "finished_at", "raw_exit")}))
    for name in ("stdout.log", "stderr.log"):
        print((output / name).read_text(encoding="utf-8", errors="replace")[-9000:])
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
