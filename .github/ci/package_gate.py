"""Bind an Actions-built source wheel to its actual no-sync console selection.

Only metadata and package bytes are read; no MCP deployment or model runs here.
The proof environment installs locked dependencies and the Actions-built wheel.
Dependency compilation is allowed here in Actions; LOCAL installs require wheels.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Package construction/qualification is an Actions gate")
    packages = args.packages.resolve(strict=True)
    receipt = packages / "PACKAGE-RECEIPT.json"
    if receipt.exists():
        raise RuntimeError("Never overwrite a package qualification")
    wheels = list(packages.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Exactly one candidate wheel is required")
    wheel = wheels[0]
    source = {
        p.relative_to(ROOT / "src").as_posix(): sha(p)
        for p in (ROOT / "src/pyocd_debug_mcp").rglob("*.py")
    }
    with zipfile.ZipFile(wheel) as archive:
        payload = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name.startswith("pyocd_debug_mcp/") and name.endswith(".py")
        }
    if source != payload:
        raise RuntimeError("Wheel Python inventory/bytes differ from the exact candidate")
    proof = packages.parent / "server-package-proof"
    proof.mkdir(exist_ok=False)
    runtime = proof / "runtime"
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    environment = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(runtime)}
    environment.pop("PYTHONPATH", None)
    steps = []

    def run(command):
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        index = len(steps)
        (proof / f"{index}.stdout").write_text(result.stdout, encoding="utf-8")
        (proof / f"{index}.stderr").write_text(result.stderr, encoding="utf-8")
        steps.append(
            {
                "argv": command,
                "exit": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if result.returncode:
            raise RuntimeError(f"Package proof command exited {result.returncode}")
        return result.stdout

    record = {
        "schema": "actions-prebuilt-source-console/v1",
        "steps": steps,
        "github_run_id": os.environ["GITHUB_RUN_ID"],
        "github_run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
        "wheel": {"name": wheel.name, "sha256": sha(wheel), "bytes": wheel.stat().st_size},
        "uv_lock_sha256": sha(ROOT / "uv.lock"),
        "pyproject_sha256": sha(ROOT / "pyproject.toml"),
        "python_payload": payload,
        "model_requests": 0,
        "mcp_deployment": False,
        "dependency_install_scope": "Actions-only locked uv sync; dependency compilation allowed here, never in the LOCAL qualifier",
        "outcome": "ERROR",
    }
    try:
        run(["uv", "venv", "--python", sys.executable, str(runtime)])
        requirements = proof / "runtime-requirements.txt"
        run(
            [
                "uv",
                "export",
                "--locked",
                "--no-default-groups",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ]
        )
        run(
            [
                "uv",
                "sync",
                "--locked",
                "--no-default-groups",
                "--no-install-project",
                "--python",
                sys.executable,
            ]
        )
        run(
            ["uv", "pip", "install", "--python", str(python), "--no-deps", "--no-build", str(wheel)]
        )
        probe = run(
            [
                "uv",
                "run",
                "--no-sync",
                "--no-build",
                "--project",
                str(ROOT),
                "python",
                "-I",
                "-B",
                "-c",
                "import importlib.metadata as m,json,shutil,sys,pyocd_debug_mcp as p; "
                "print(json.dumps({'python':sys.executable,'package':p.__file__,"
                "'version':m.version('pyocd-debug-mcp'),"
                "'console':shutil.which('pyocd-debug-mcp'),"
                "'entrypoints':{e.name:e.value for e in m.distribution('pyocd-debug-mcp').entry_points}}))",
            ]
        )
        selected = json.loads(probe)
        expected_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
            "version"
        ]
        if (
            Path(selected["python"]).resolve() != python.resolve()
            or not Path(selected["console"]).resolve().is_relative_to(runtime)
            or not Path(selected["package"]).resolve().is_relative_to(runtime)
            or selected["version"] != expected_version
            or selected["entrypoints"].get("pyocd-debug-mcp") != "pyocd_debug_mcp.server:main"
        ):
            raise RuntimeError("uv selected a stale/global console or package")
        record.update(
            outcome="PASS", actual_selection=selected, requirements_sha256=sha(requirements)
        )
    finally:
        receipt.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"outcome": record["outcome"], "wheel": record["wheel"]}))


if __name__ == "__main__":
    main()
