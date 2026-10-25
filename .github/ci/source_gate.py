"""Retain exact committed source and host bindings for native Actions gates."""

import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


def main() -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Source gates execute only in GitHub Actions")
    root = Path(__file__).resolve().parents[2]
    phase = sys.argv[1]

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=root)

    head = git("rev-parse", "HEAD").decode().strip()
    files = []
    mismatches = []
    for entry in git("ls-tree", "-r", "-z", "HEAD").split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        relative = name.decode()
        content = (root / relative).read_bytes()
        if content != git("cat-file", "blob", blob):
            mismatches.append(relative)
        files.append(
            {
                "path": relative,
                "mode": mode,
                "kind": kind,
                "blob": blob,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    output = Path(os.environ["RUNNER_TEMP"]) / "server-source"
    output.mkdir(exist_ok=True)
    record = {
        "phase": phase,
        "commit": head,
        "event_sha": os.environ["GITHUB_SHA"],
        "tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
        "files": files,
        "mismatches": mismatches,
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "confirmed": head == os.environ["GITHUB_SHA"] and not mismatches,
    }
    (output / (phase + ".json")).write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({key: value for key, value in record.items() if key != "files"}))
    return int(not record["confirmed"])


if __name__ == "__main__":
    raise SystemExit(main())
