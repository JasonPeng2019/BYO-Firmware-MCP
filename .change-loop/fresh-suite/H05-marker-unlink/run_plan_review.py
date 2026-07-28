from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path


REPO = Path(
    r"C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual"
    r"\MCP-Trial-3\BYO-Firmware-MCP"
)
RUNTIME = REPO / ".change-loop" / "fresh-suite" / "H05-marker-unlink"
CODEX = Path(
    r"C:\Users\Jason\.vscode\extensions\openai.chatgpt-26.715.61943-win32-x64"
    r"\bin\windows-x86_64\codex.exe"
)
PROMPT = RUNTIME / "plan-review.prompt.md"
JSONL = RUNTIME / "plan-review.jsonl"
STDERR = RUNTIME / "plan-review.stderr.log"
LAST = RUNTIME / "plan-review.last-message.md"
STATUS = RUNTIME / "plan-review.controller.json"


def write_status(payload: dict[str, object]) -> None:
    STATUS.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    argv = [
        str(CODEX),
        "-a",
        "on-request",
        "-s",
        "read-only",
        "-C",
        str(REPO),
        "-m",
        "gpt-5.6-terra",
        "-c",
        'model_reasoning_effort="medium"',
        "-c",
        'service_tier="priority"',
        "-c",
        "notice.model_migrations={}",
        "exec",
        "--ignore-user-config",
        "--json",
        "--output-last-message",
        str(LAST),
        "-",
    ]
    with JSONL.open("w", encoding="utf-8") as stdout, STDERR.open(
        "w", encoding="utf-8"
    ) as stderr:
        process = subprocess.Popen(
            argv,
            cwd=REPO,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        write_status(
            {
                "state": "running",
                "controller_pid": __import__("os").getpid(),
                "codex_pid": process.pid,
                "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "model": "gpt-5.6-terra",
                "reasoning_effort": "medium",
                "service_tier": "priority",
                "sandbox": "read-only",
                "approval_policy": "on-request",
            }
        )
        assert process.stdin is not None
        process.stdin.write(PROMPT.read_text(encoding="utf-8"))
        process.stdin.close()
        code = process.wait()
    write_status(
        {
            "state": "exited",
            "controller_pid": __import__("os").getpid(),
            "codex_pid": process.pid,
            "exit_code": code,
            "ended_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model": "gpt-5.6-terra",
            "reasoning_effort": "medium",
            "service_tier": "priority",
            "sandbox": "read-only",
            "approval_policy": "on-request",
        }
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
