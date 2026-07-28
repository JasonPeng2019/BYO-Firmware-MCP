from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
RUNTIME = Path(__file__).resolve().parent
STATUS = RUNTIME / "change-loop.controller.json"
STDOUT = RUNTIME / "change-loop.stdout.log"
STDERR = RUNTIME / "change-loop.stderr.log"
WSL_ROOT = (
    "/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/"
    "Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP"
)
CODEX = (
    "/mnt/c/Users/Jason/.vscode/extensions/"
    "openai.chatgpt-26.715.61943-win32-x64/bin/windows-x86_64/codex.exe"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_status(payload: dict[str, object]) -> None:
    STATUS.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


command = f"""
set -euo pipefail
cd '{WSL_ROOT}'
export CL_RUNTIME_DIR=.change-loop/fresh-suite/H05-marker-unlink
export DOER_MODEL=gpt-5.6-terra
export SPEC_TESTER_MODEL=gpt-5.6-terra
export REGRESSION_TESTER_MODEL=gpt-5.6-terra
export CL_REASONING_EFFORT=medium
export CL_CODEX_BIN='{CODEX}'
export CL_CODEX_FLAGS='--sandbox danger-full-access --ignore-user-config --config service_tier=priority'
bash ../.codex/skills/change-loop/scripts/run_loop.sh
""".strip()

creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
with STDOUT.open("wb") as stdout, STDERR.open("wb") as stderr:
    process = subprocess.Popen(
        ["wsl.exe", "-d", "H00-POSIX", "--", "bash", "-lc", command],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        creationflags=creation_flags,
    )
    base = {
        "state": "running",
        "controller_pid": os.getpid(),
        "wsl_pid": process.pid,
        "started_utc": now(),
        "runtime": ".change-loop/fresh-suite/H05-marker-unlink",
        "models": {
            "doer": "gpt-5.6-terra",
            "spec_tester": "gpt-5.6-terra",
            "regression_tester": "gpt-5.6-terra",
        },
        "reasoning_effort": "medium",
        "service_tier": "priority",
        "sandbox": "danger-full-access",
        "sandbox_fallback_reason": (
            "The Windows workspace-write sandbox mounted the trusted repository read-only; "
            "the change-loop skill explicitly permits this fallback after that launch failure."
        ),
    }
    write_status(base)
    exit_code = process.wait()
    write_status(
        {
            **base,
            "state": "exited",
            "exit_code": exit_code,
            "ended_utc": now(),
        }
    )
