"""Actions-native ordinary fork/wait workload, with explicit identity receipts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil


def receipt(root: Path, role: str) -> None:
    process = psutil.Process()
    data = {
        "pid": process.pid,
        "birth": process.create_time(),
        "ppid": process.ppid(),
        "pgid": os.getpgid(0),
        "sid": os.getsid(0),
        "role": role,
        "argv": process.cmdline(),
    }
    path = root / (role + ".json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


def wait_file(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.002)
    if not path.exists():
        raise RuntimeError("fixture gate unavailable: " + str(path))


def main() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.name != "posix":
        raise SystemExit("native POSIX fixtures execute in Actions only")
    root = Path(sys.argv[2])
    role, mode = sys.argv[1], sys.argv[3]
    receipt(root, role)
    if role == "branch":
        wait_file(root / "fork-now")
        while True:
            pid = os.fork()
            if pid == 0:
                receipt(root, "leaf")
                if mode == "member-exit":
                    wait_file(root / "leaf-exit")
                elif mode == "directed":
                    time.sleep(60)
                else:
                    time.sleep(0.02)
                os._exit(0)
            os.waitpid(pid, 0)
            if mode == "member-exit":
                (root / "leaf-reaped").touch()
                time.sleep(60)
    else:
        subprocess.Popen(
            [sys.executable, __file__, "branch", str(root), mode],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_file(root / "branch.json")
        if mode not in {"directed", "member-exit"}:
            (root / "fork-now").touch()
            wait_file(root / "leaf.json")
        print("churn-output", flush=True)
        print("churn-error", file=sys.stderr, flush=True)
        if sys.argv[4] == "linger":
            time.sleep(60)
        raise SystemExit(int(sys.argv[4]))


if __name__ == "__main__":
    main()
