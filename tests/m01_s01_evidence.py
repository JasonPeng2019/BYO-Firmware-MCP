"""Capture M01-S-1 finite checks outside the checkout; never wraps providers.

Usage: py -3 tests/m01_s01_evidence.py LABEL COMMAND [ARG ...]
The evidence destination comes from the installed root mapping, not cwd.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


SERVER = Path(__file__).resolve().parents[1]
WORKTREE = SERVER.parents[1]
ROOT = Path(json.loads((WORKTREE / "harness/harness-config.json").read_text())["root_workspace"])
EVIDENCE = ROOT / ".harness-runtime/campaigns/product-repair/checks/m01-validate-s01"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(label: str, command: list[str]) -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    log = EVIDENCE / f"{label}.log"
    record = EVIDENCE / f"{label}-command.json"
    if log.exists() or record.exists():
        raise FileExistsError(f"preserve previous evidence: {label}")
    snapshots = EVIDENCE / "snapshots"
    snapshots.mkdir(exist_ok=True)
    for path in (SERVER / "tests").glob("*m01*.py"):
        (snapshots / f"{sha(path)}.py").write_bytes(path.read_bytes())
    environment = dict(os.environ)
    environment.update(
        BYO_MANAGED_TEST_EVIDENCE=str(EVIDENCE / label / "managed"),
        BYO_MCP_ARTIFACT_ROOT=str(EVIDENCE / label / "project"),
        BYO_MCP_MONITOR_ROOT=str(EVIDENCE / label / "monitor"),
        M01_S01_JOURNAL=str(EVIDENCE / f"{label}-journal.jsonl"),
    )
    start = dt.datetime.now(dt.timezone.utc).isoformat()
    with log.open("wb") as output:
        child = subprocess.run(
            command,
            cwd=SERVER,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=False,
        )
    end = dt.datetime.now(dt.timezone.utc).isoformat()
    identities = {
        str(path.relative_to(SERVER)): sha(path)
        for pattern in ("src/**/*.py", "tests/*m01*.py", "uv.lock", "pyproject.toml")
        for path in SERVER.glob(pattern)
    }
    document = dict(
        argv=command,
        cwd=str(SERVER),
        started_utc=start,
        ended_utc=end,
        raw_exit=child.returncode,
        log_sha256=sha(log),
        inputs_sha256=identities,
        evidence_environment={
            key: value
            for key, value in environment.items()
            if key
            in {
                "BYO_MANAGED_TEST_EVIDENCE",
                "BYO_MCP_ARTIFACT_ROOT",
                "BYO_MCP_MONITOR_ROOT",
                "M01_S01_JOURNAL",
            }
        },
    )
    record.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(log.read_text(encoding="utf-8", errors="replace"))
    print(json.dumps({"record": str(record), "raw_exit": child.returncode}))
    return child.returncode


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1], sys.argv[2:]))
