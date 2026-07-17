#!/usr/bin/env python3
"""Rerun only Task 20 checks whose uv wrapper returned a contradictory code."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pyocd_debug_mcp.kernel.processes import ProcessMarkerStore, run_owned


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(name: str, argv: list[str], output_dir: Path, timeout: float) -> dict[str, object]:
    started_at = _timestamp()
    started = time.perf_counter()
    completed = run_owned(
        argv,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=timeout,
        marker_store=ProcessMarkerStore(output_dir / "owned-processes"),
    )
    elapsed = time.perf_counter() - started
    stdout_path = output_dir / f"{name}.stdout.log"
    stderr_path = output_dir / f"{name}.stderr.log"
    stdout_path.write_text(completed.stdout or "", encoding="utf-8")
    stderr_path.write_text(completed.stderr or "", encoding="utf-8")
    return {
        "name": name,
        "argv": argv,
        "started_at": started_at,
        "elapsed_seconds": elapsed,
        "timeout_seconds": timeout,
        "exit_code": completed.returncode,
        "status": "pass" if completed.returncode == 0 else "fail",
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--stdio-only", action="store_true")
    args = parser.parse_args()
    software_dir = args.result_root.resolve() / "software"
    original = software_dir / "result.json"
    if not original.is_file():
        raise FileNotFoundError(original)
    output_dir = software_dir / (
        "affected-rerun-2" if args.stdio_only else "affected-rerun"
    )
    if output_dir.exists():
        raise FileExistsError(output_dir)
    output_dir.mkdir()

    python = Path(sys.executable).resolve()
    pyright = python.parent / ("pyright.exe" if sys.platform == "win32" else "pyright")
    stdio_code = """
import asyncio
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pyocd_debug_mcp.server"],
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            names = {tool.name for tool in tools}
            assert "initialization_handshake" in names
            print(f"stdio tool count: {len(names)}")

asyncio.run(main())
""".strip()
    results = []
    prior_pyright_pass = False
    prior_result: Path | None = None
    if args.stdio_only:
        candidate = software_dir / "affected-rerun/result.json"
        prior_result = candidate
        prior = json.loads(candidate.read_text(encoding="utf-8"))
        prior_pyright_pass = any(
            item["name"] == "pyright_direct" and item["status"] == "pass"
            for item in prior["affected_checks"]
        )
        if not prior_pyright_pass:
            raise RuntimeError("The prior direct Pyright check did not pass")
    else:
        results.append(_run("pyright_direct", [str(pyright)], output_dir, 300.0))
    results.append(_run("stdio_direct", [str(python), "-c", stdio_code], output_dir, 60.0))
    source = json.loads(original.read_text(encoding="utf-8"))
    original_passes = {
        item["name"]: item["status"]
        for item in source["commands"]
        if item["name"] not in {"pyright", "stdio_boot_shutdown"}
    }
    status = (
        "pass"
        if all(value == "pass" for value in original_passes.values())
        and (not args.stdio_only or prior_pyright_pass)
        and all(item["status"] == "pass" for item in results)
        else "fail"
    )
    document = {
        "schema_version": 1,
        "phase": "software_once_reconciliation",
        "status": status,
        "recorded_at": _timestamp(),
        "reason": (
            "The original uv wrappers returned exit 1 while their logs showed Pyright "
            "0 errors and a successful 35-tool stdio session; only those two checks were "
            "rerun directly from the locked environment. The complete pytest suite was not rerun."
        ),
        "original_result": str(original),
        "original_result_sha256": _sha256(original),
        "prior_affected_result": str(prior_result) if prior_result else None,
        "prior_affected_result_sha256": _sha256(prior_result) if prior_result else None,
        "retained_original_passes": original_passes,
        "affected_checks": results,
    }
    output = output_dir / "result.json"
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": status, "result": str(output)}))
    if status != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"affected check timed out: {exc.cmd}") from exc
