from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUN = Path(__file__).resolve().parent
STATUS = RUN / "controller.status.json"
LOG = RUN / "controller.log"
COMMAND = (
    "CL_RUNTIME_DIR=.change-loop/fresh-suite/H04-pack-repair-zero-retries "
    "DOER_MODEL=gpt-5.6-terra "
    "SPEC_TESTER_MODEL=gpt-5.6-terra "
    "REGRESSION_TESTER_MODEL=gpt-5.6-terra "
    "CL_REASONING_EFFORT=medium "
    "CL_CODEX_BIN=/mnt/c/Users/Jason/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe "
    "CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config' "
    "../.codex/skills/change-loop/scripts/run_loop.sh"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_status(payload: dict[str, object]) -> None:
    STATUS.write_text(json.dumps(payload, indent=2), encoding="utf-8")


with LOG.open("w", encoding="utf-8", errors="replace") as output:
    process = subprocess.Popen(
        ["bash", "-lc", COMMAND],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        text=True,
    )
    state: dict[str, object] = {
        "state": "running",
        "controller_pid": os.getpid(),
        "bash_pid": process.pid,
        "started_utc": now(),
        "runtime": ".change-loop/fresh-suite/H04-pack-repair-zero-retries",
        "models": {
            "doer": "gpt-5.6-terra",
            "spec_tester": "gpt-5.6-terra",
            "regression_tester": "gpt-5.6-terra",
        },
        "reasoning": "medium",
        "sandbox": "danger-full-access",
        "sandbox_reason": (
            "The Windows workspace-write sandbox rejected the authorized plan-review artifact; "
            "use the documented change-loop fallback without bypassing approvals."
        ),
        "approval": "Codex exec noninteractive; hardware actions prohibited by plan and role prompts",
    }
    write_status(state)
    exit_code = process.wait()
    state.update({"state": "exited", "exit_code": exit_code, "ended_utc": now()})
    write_status(state)
