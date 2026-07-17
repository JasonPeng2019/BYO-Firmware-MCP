#!/usr/bin/env python3
"""Run the Task 20 software acceptance sequence exactly once and record it."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pyocd_debug_mcp.kernel.processes import ProcessMarkerStore, run_owned


ROOT = Path(__file__).resolve().parents[1]


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-root", type=Path, required=True)
    return parser.parse_args()


def _command(
    name: str,
    argv: list[str],
    *,
    result_dir: Path,
    timeout: float,
    marker_store: ProcessMarkerStore,
) -> dict[str, object]:
    started_at = _timestamp()
    started = time.perf_counter()
    timed_out = False
    try:
        completed = run_owned(
            argv,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            marker_store=marker_store,
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
    elapsed = time.perf_counter() - started
    stdout_path = result_dir / f"{name}.stdout.log"
    stderr_path = result_dir / f"{name}.stderr.log"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return {
        "name": name,
        "argv": argv,
        "cwd": str(ROOT),
        "started_at": started_at,
        "elapsed_seconds": elapsed,
        "timeout_seconds": timeout,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "status": "pass" if exit_code == 0 and not timed_out else "fail",
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }


def main() -> None:
    args = _arguments()
    result_root = args.result_root.resolve()
    try:
        result_root.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise ValueError("--result-root must be outside the checkout")
    result_dir = result_root / "software"
    if result_dir.exists():
        raise FileExistsError(f"Software acceptance already ran: {result_dir}")
    result_dir.mkdir(parents=True)

    uv = shutil.which("uv")
    if uv is None:
        raise FileNotFoundError("uv")
    python = Path(sys.executable).resolve()
    pyright = python.parent / ("pyright.exe" if sys.platform == "win32" else "pyright")
    if not pyright.is_file():
        raise FileNotFoundError(pyright)
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
    import_code = """
import pyocd_debug_mcp
import pyocd_debug_mcp.server
import pyocd_debug_mcp.firmstore
import pyocd_debug_mcp.guardrails
import pyocd_debug_mcp.kernel
import pyocd_debug_mcp.safety
import pyocd_debug_mcp.setup_flow
import pyocd_debug_mcp.tools
print("import surface ok")
""".strip()
    dist_dir = result_dir / "dist"
    marker_store = ProcessMarkerStore(result_dir / "owned-processes")
    commands = [
        ("pytest", [uv, "run", "--locked", "pytest", "-q"], 900.0),
        ("ruff", [uv, "run", "--locked", "ruff", "check", "."], 180.0),
        ("pyright", [str(pyright)], 300.0),
        ("package_build", [uv, "build", "--out-dir", str(dist_dir)], 300.0),
        ("dependency_check", [uv, "pip", "check"], 120.0),
        ("import_check", [str(python), "-c", import_code], 60.0),
        ("stdio_boot_shutdown", [str(python), "-c", stdio_code], 60.0),
    ]
    results = [
        _command(
            name,
            argv,
            result_dir=result_dir,
            timeout=timeout,
            marker_store=marker_store,
        )
        for name, argv, timeout in commands
    ]
    status = "pass" if all(item["status"] == "pass" for item in results) else "fail"
    document = {
        "schema_version": 1,
        "phase": "software_once",
        "status": status,
        "recorded_at": _timestamp(),
        "host": {
            "platform": platform.platform(),
            "python": sys.version,
            "processor": platform.processor(),
        },
        "versions": {
            "package": importlib.metadata.version("pyocd-debug-mcp"),
            "mcp": importlib.metadata.version("mcp"),
            "pyocd": importlib.metadata.version("pyocd"),
        },
        "commands": results,
    }
    result_path = result_dir / "result.json"
    result_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": status, "result": str(result_path)}))
    if status != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
