"""Finite check capture with isolated roots, raw Git exports and passive import seals."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def capture_imports():
    """Call at a test/server exit boundary, after every guarded operation."""
    selected = os.environ.get("M15_IMPORT_CAPTURE_ROOT")
    if not selected:
        return
    destination = Path(selected)
    blobs = destination / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, module in sorted(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if not name.startswith(("pyocd_debug_mcp", "pyocd.", "tests.")) or not file:
            continue
        path = Path(file).resolve()
        if path.is_file():
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if not (blobs / digest).exists():
                (blobs / digest).write_bytes(data)
            rows.append(dict(module=name, path=str(path), bytes=len(data), sha256=digest))
    with (destination / f"{os.getpid()}-boundaries.jsonl").open("a", encoding="utf-8") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)


def export_source(product, commit, destination):
    """Export cat-file bytes, without a checkout or Git newline conversion."""
    prefix = "MCP_Server_Dev/BYO-Firmware-MCP/src/"
    listing = subprocess.check_output(
        ["git", "ls-tree", "-rz", commit, prefix], cwd=product.parents[1]
    )
    rows = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        fields, name = entry.split(b"\t", 1)
        _, kind, oid = fields.decode().split()
        assert kind == "blob"
        relative = name.decode()[len(prefix) :]
        target = destination / relative
        assert target.resolve().is_relative_to(destination.resolve())
        payload = subprocess.check_output(["git", "cat-file", "blob", oid], cwd=product)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        rows.append(dict(path=name.decode(), blob=oid, bytes=len(payload), sha256=sha(target)))
    assert rows and (destination / "pyocd_debug_mcp/server.py").is_file(), "Empty source export"
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--old-commit")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    product = Path(__file__).resolve().parents[1]
    lane = product.parents[1]
    output = (args.output / args.label).resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = (args.source_root or product / "src").resolve()
    old = None
    if args.old_commit:
        assert args.source_root is None
        source = output / "old-source"
        old = export_source(product, args.old_commit, source)
    environment = dict(os.environ)
    overrides = {}
    for key, directory in (
        ("BYO_MCP_ARTIFACT_ROOT", "project"),
        ("PYOCD_PROJECT_DIR", "project"),
        ("BYO_MCP_MONITOR_ROOT", "monitor"),
        ("BYO_MANAGED_TEST_EVIDENCE", "managed"),
        ("PYOCD_MCP_RUNS_ROOT", "runs"),
        ("TEMP", "temp"),
        ("TMP", "temp"),
    ):
        overrides[key] = str(output / directory)
        Path(overrides[key]).mkdir(exist_ok=True)
    observer = output / "observer"
    observer.mkdir()
    # Hash after public operations. Per-import disk writes inside a validation
    # worker's five-second startup budget can intrude on that budget. Terminated
    # children may have no exit import record; do not reconstruct that metadata.
    observer_text = (
        "import atexit, hashlib, importlib._bootstrap, json, os, sys\nfrom pathlib import Path\n"
        f"root=Path({str(output / 'imports')!r}); root.mkdir(exist_ok=True)\n"
        "blobs=root/'blobs'; blobs.mkdir(exist_ok=True)\n"
        "seen=set()\n"
        "def capture(module):\n"
        " name=getattr(module,'__name__',''); file=getattr(module,'__file__',None)\n"
        " if not name.startswith(('pyocd_debug_mcp','pyocd.','tests.')) or not file: return\n"
        " p=Path(file).resolve()\n"
        " if not p.is_file(): return\n"
        " data=p.read_bytes(); digest=hashlib.sha256(data).hexdigest(); key=(name,str(p),digest)\n"
        " if key in seen: return\n"
        " seen.add(key); (blobs/digest).write_bytes(data)\n"
        " with (root/(str(os.getpid())+'.jsonl')).open('a',encoding='utf-8') as stream:\n"
        "  stream.write(json.dumps(dict(module=name,path=str(p),bytes=len(data),sha256=digest))+'\\n')\n"
        "def finish():\n"
        " for module in tuple(sys.modules.values()):\n"
        "  if module is not None: capture(module)\n"
        "atexit.register(finish)\n"
    )
    observer_text += (
        "import importlib.util\n"
        "spec=importlib.util.find_spec('pyocd_debug_mcp')\n"
        f"expected=Path({str(source / 'pyocd_debug_mcp/__init__.py')!r}).resolve()\n"
        "actual=Path(spec.origin).resolve() if spec and spec.origin else None\n"
        "if actual != expected:\n"
        " (root/(str(os.getpid())+'-wrong-source.json')).write_text(json.dumps(dict(expected=str(expected),actual=str(actual))))\n"
        " os._exit(87)\n"
    )
    (observer / "sitecustomize.py").write_text(observer_text, encoding="utf-8")
    overrides.update(
        PYTHONPATH=os.pathsep.join((str(observer), str(source), str(product))),
        PYTHONIOENCODING="utf-8",
        PYTHONDONTWRITEBYTECODE="1",
    )
    environment.update(overrides)
    environment["M15_IMPORT_CAPTURE_ROOT"] = str(output / "imports")
    overrides["M15_IMPORT_CAPTURE_ROOT"] = environment["M15_IMPORT_CAPTURE_ROOT"]
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    rows = []
    with zipfile.ZipFile(output / "inputs.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for group, base in (("source", source), ("tests", product / "tests")):
            for path in sorted(base.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                data = path.read_bytes()
                member = group + "/" + path.relative_to(base).as_posix()
                archive.writestr(member, data)
                rows.append(dict(path=str(path), member=member, bytes=len(data), sha256=sha(path)))
        for path in (
            Path(__file__),
            product / "pyproject.toml",
            product / "uv.lock",
            lane / ".agent-workspace/invocation.json",
            observer / "sitecustomize.py",
        ):
            member = "environment/" + path.name
            archive.write(path, member)
            rows.append(
                dict(path=str(path), member=member, bytes=path.stat().st_size, sha256=sha(path))
            )
    record = dict(
        started_utc=now(),
        argv=command,
        cwd=str(product),
        source_root=str(source),
        runner=dict(path=str(Path(__file__).resolve()), sha256=sha(__file__)),
        python=dict(executable=sys.executable, version=sys.version),
        versions={name: metadata.version(name) for name in ("pyocd", "mcp", "ruff", "pyright")},
        environment=overrides,
        inputs=rows,
        inputs_zip_sha256=sha(output / "inputs.zip"),
        old_source=dict(commit=args.old_commit, raw_git_blobs=old) if old else None,
    )
    for label, argv in (
        (
            "git",
            [
                "git",
                "rev-parse",
                "HEAD",
                "HEAD:MCP_Server_Dev/BYO-Firmware-MCP/src",
                "HEAD:MCP_Server_Dev/BYO-Firmware-MCP/tests",
            ],
        ),
        ("git_status", ["git", "status", "--short"]),
    ):
        result = subprocess.run(argv, cwd=product, capture_output=True, text=True, check=True)
        record[label] = dict(
            argv=argv, raw_exit=result.returncode, stdout=result.stdout, stderr=result.stderr
        )
    (output / "start.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(
        json.dumps(dict(started_utc=record["started_utc"], argv=command, output=str(output))),
        flush=True,
    )
    import psutil

    with (output / "stdout.log").open("wb") as stdout, (output / "stderr.log").open("wb") as stderr:
        process = subprocess.Popen(
            command, cwd=product, env=environment, stdout=stdout, stderr=stderr
        )
        info = psutil.Process(process.pid)
        token = info.create_time()
        record["child"] = dict(
            pid=process.pid, creation_time_unix=token, actual_argv=info.cmdline()
        )
        (output / "child.json").write_text(json.dumps(record["child"], indent=2), encoding="utf-8")
        raw_exit = process.wait()
    try:
        absent = psutil.Process(process.pid).create_time() != token
    except psutil.NoSuchProcess:
        absent = True
    stderr_text = (output / "stderr.log").read_text(encoding="utf-8", errors="replace")
    counts = re.findall(r"Ran (\d+) tests? in ([\d.]+)s", stderr_text)
    details = re.findall(r"\b(failures|errors|skipped)=(\d+)", stderr_text)
    record.update(
        finished_utc=now(),
        raw_exit=raw_exit,
        child_exact_identity_absent=absent,
        unittest=dict(summaries=counts, details=details),
        streams={
            name: dict(bytes=(output / name).stat().st_size, sha256=sha(output / name))
            for name in ("stdout.log", "stderr.log")
        },
    )
    (output / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: record[key]
                for key in ("finished_utc", "raw_exit", "unittest", "child_exact_identity_absent")
            }
        )
    )
    print((output / "stdout.log").read_text(encoding="utf-8", errors="replace")[-5000:])
    print(stderr_text[-10000:])
    return raw_exit


if __name__ == "__main__":
    sys.exit(main())
