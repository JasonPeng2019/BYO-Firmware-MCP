"""Real identity-writing processes for nonbuilding local and Actions controls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil


def receipt(root: Path, role: str) -> None:
    process = psutil.Process()
    value = {
        "role": role,
        "pid": process.pid,
        "birth": process.create_time(),
        "ppid": process.ppid(),
        "exe": process.exe(),
        "argv": process.cmdline(),
        "state": process.status(),
    }
    if os.name == "posix":
        value.update(pgid=os.getpgid(0), sid=os.getsid(0))
    (root / (role + ".json")).write_text(json.dumps(value), encoding="utf-8")


def main() -> None:
    root = Path(sys.argv[2])
    role = sys.argv[1]
    receipt(root, role)
    if role in {"peer", "leaf"}:
        while True:
            time.sleep(0.02)
    mode = sys.argv[3]
    if mode != "terminal":
        spawned = subprocess.Popen(
            [sys.executable, __file__, "leaf", str(root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child_root = psutil.Process(spawned.pid)
        (root / "leaf-root.json").write_text(
            json.dumps(
                {
                    "role": "leaf-root",
                    "pid": child_root.pid,
                    "birth": child_root.create_time(),
                    "ppid": child_root.ppid(),
                    "exe": child_root.exe(),
                    "argv": child_root.cmdline(),
                    "creator_pid": os.getpid(),
                    "creator_birth": psutil.Process().create_time(),
                }
            ),
            encoding="utf-8",
        )
        deadline = time.monotonic() + 10
        while not (root / "leaf.json").is_file() and time.monotonic() < deadline:
            time.sleep(0.002)
        if not (root / "leaf.json").is_file():
            raise RuntimeError("fixture leaf readiness unavailable")
    deadline = time.monotonic() + 10
    while not (root / "release").is_file() and time.monotonic() < deadline:
        time.sleep(0.002)
    if not (root / "release").is_file():
        raise RuntimeError("fixture parent release unavailable")
    print("validator-normal-output", flush=True)
    print("validator-normal-error", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
