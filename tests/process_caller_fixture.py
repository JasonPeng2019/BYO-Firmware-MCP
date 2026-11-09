"""Nonbuilding independent fixture. Every role publishes its own native identity."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil


def publish(root: Path, role: str) -> None:
    process = psutil.Process()
    record = {
        "role": role,
        "pid": os.getpid(),
        "birth": process.create_time(),
        "parent": os.getppid(),
        "image": process.exe(),
        "argv": process.cmdline(),
        "state": process.status(),
        "monotonic": time.monotonic(),
        "pgid": os.getpgid(0) if os.name == "posix" else None,
        "sid": os.getsid(0) if os.name == "posix" else None,
    }
    temporary = root / (role + ".tmp")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    temporary.replace(root / (role + ".json"))


def await_gate(path: Path) -> None:
    deadline = time.monotonic() + 25
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError("fixture gate absent: " + str(path))
        time.sleep(0.002)


def spawn(root: Path, role: str, *arguments: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), role, str(root), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    role, directory, *arguments = sys.argv[1:]
    root = Path(directory)
    if role == "peer":
        publish(root, role)
        time.sleep(45)
    elif role == "leaf":
        if arguments == ["ignore-term"]:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        publish(root, role)
        time.sleep(45)
    elif role == "branch":
        if sys.platform == "win32":
            # Only the Linux orphan-race test uses this role; the guard also lets
            # the Windows type gate treat the os.fork path as unreachable.
            raise SystemExit("branch role is the POSIX-only directed fork race")
        publish(root, role)
        await_gate(root / "fork-now")
        # The directed membership race needs a real new birth after its gate,
        # without charging a fresh Python/psutil import to the cleanup phase.
        if os.fork() == 0:
            publish(root, "leaf")
            time.sleep(45)
            os._exit(0)
        await_gate(root / "leaf.json")
        os._exit(0)
    elif role == "leader":
        mode, status, payload = arguments
        if mode == "race":
            spawn(root, "branch")
            await_gate(root / "branch.json")
        else:
            spawn(root, "leaf", *(["ignore-term"] if mode == "kill" else []))
            await_gate(root / "leaf.json")
        publish(root, role)
        # Admission and exact identities precede the leader's natural exit.
        await_gate(root / "leader-exit")
        print(payload, flush=True)
        print("independent-stderr", file=sys.stderr, flush=True)
        if mode == "timeout":
            time.sleep(45)
        raise SystemExit(int(status))
    else:
        raise SystemExit("unknown fixture role: " + role)


if __name__ == "__main__":
    main()
