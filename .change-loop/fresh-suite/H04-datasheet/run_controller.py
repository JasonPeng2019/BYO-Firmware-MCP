from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
RUNTIME = REPO / ".change-loop" / "fresh-suite" / "H04-datasheet"
STATUS = RUNTIME / "controller.status.json"
STDOUT = RUNTIME / "run_loop.stdout.log"
STDERR = RUNTIME / "run_loop.stderr.log"


def write_status(**updates: object) -> None:
    current: dict[str, object] = {}
    if STATUS.exists():
        try:
            current = json.loads(STATUS.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    current.update(updates)
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, STATUS)


def main() -> int:
    shell = (
        "export CL_RUNTIME_DIR=.change-loop/fresh-suite/H04-datasheet; "
        "export DOER_MODEL=gpt-5.6-terra; "
        "export SPEC_TESTER_MODEL=gpt-5.6-terra; "
        "export REGRESSION_TESTER_MODEL=gpt-5.6-terra; "
        "export CL_REASONING_EFFORT=medium; "
        "export CL_CODEX_BIN=codex.exe; "
        'export CL_CODEX_FLAGS="--sandbox danger-full-access --ignore-user-config"; '
        "bash ../.codex/skills/change-loop/scripts/run_loop.sh"
    )
    with STDOUT.open("a", encoding="utf-8") as stdout, STDERR.open(
        "a", encoding="utf-8"
    ) as stderr:
        proc = subprocess.Popen(
            ["bash", "-lc", shell],
            cwd=REPO,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
        write_status(
            state="running",
            controller_pid=os.getpid(),
            run_loop_pid=proc.pid,
            started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            runtime_dir=".change-loop/fresh-suite/H04-datasheet",
            models="gpt-5.6-terra",
            reasoning_effort="medium",
            sandbox="danger-full-access",
            reason="Windows workspace sandbox is known unable to launch role commands",
        )
        code = proc.wait()
    write_status(
        state="exited",
        exit_code=code,
        ended_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    return int(code)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        write_status(
            state="error",
            error=repr(exc),
            ended_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        raise
