"""B02 finite check evidence: isolated roots, exact inputs, raw exits and imports."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    output = (args.output / args.label).resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = Path.cwd().resolve()
    inputs = []
    for directory in ("src", "tests"):
        for path in sorted((root / directory).rglob("*.py")):
            relative = path.relative_to(root)
            destination = output / "inputs" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            inputs.append({"path": str(path), "snapshot": str(destination),
                           "sha256": digest(path), "bytes": path.stat().st_size})
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(root / name, output / "inputs" / name)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    env = dict(os.environ)
    for name, directory in (("BYO_MCP_ARTIFACT_ROOT", "project"),
                            ("BYO_MCP_MONITOR_ROOT", "monitor"),
                            ("BYO_MANAGED_TEST_EVIDENCE", "managed")):
        env[name] = str(output / directory)
        Path(env[name]).mkdir()
    observer = output / "observer"
    observer.mkdir()
    (observer / "sitecustomize.py").write_text(
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
        "atexit.register(capture)\n", encoding="utf-8")
    env["PYTHONPATH"] = os.pathsep.join((str(observer), str(root / "src"), str(root)))
    record = dict(started_at=now(), argv=command, cwd=str(root), inputs=inputs,
                  environment={k: env[k] for k in ("PYTHONPATH", "BYO_MCP_ARTIFACT_ROOT",
                  "BYO_MCP_MONITOR_ROOT", "BYO_MANAGED_TEST_EVIDENCE")})
    (output / "command.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    with (output / "stdout.log").open("wb") as stdout, (output / "stderr.log").open("wb") as stderr:
        result = subprocess.run(command, env=env, stdout=stdout, stderr=stderr, check=False)
    record.update(finished_at=now(), exit_code=result.returncode,
                  stdout_sha256=digest(output / "stdout.log"),
                  stderr_sha256=digest(output / "stderr.log"))
    (output / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps({k: record[k] for k in ("started_at", "finished_at", "exit_code", "argv")}))
    print((output / "stdout.log").read_text(errors="replace")[-8000:])
    print((output / "stderr.log").read_text(errors="replace")[-8000:])
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
