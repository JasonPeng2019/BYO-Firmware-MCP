#!/usr/bin/env python3
"""Regenerate the four 0.1.8rc1 logs and their manifest-bound attestation."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.verify_release_evidence import (
        ATTESTATION,
        RELEASE_LOGS,
        pinned_release_directories,
        replace_release_file,
        unlink_release_file,
        mkdir_release_directory,
        _lstat,
        verify_release_logs,
        working_tree_manifest_sha256,
        write_attestation,
        write_release_text,
    )
except ModuleNotFoundError:  # Direct ``python scripts/generate_release_evidence.py``.
    from verify_release_evidence import (  # type: ignore[no-redef]
        ATTESTATION,
        RELEASE_LOGS,
        pinned_release_directories,
        replace_release_file,
        unlink_release_file,
        mkdir_release_directory,
        _lstat,
        verify_release_logs,
        working_tree_manifest_sha256,
        write_attestation,
        write_release_text,
    )


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DEFAULT_TESTS = 1438
# Default evidence excludes opt-in kernel vcan on both Linux and macOS.
# The native CI job provisions and qualifies that interface separately.
EXPECTED_DEFAULT_SKIPS = 30
EXPECTED_LIVE_TESTS = 22
RELEASE_TMPDIR = "/tmp"


def _sanitize(text: str, private_paths: tuple[str, ...] = ()) -> str:
    replacements = {str(ROOT): "<candidate-root>", str(Path.home()): "<operator-home>"}
    replacements.update({path: "<private-path>" for path in private_paths if path})
    for path in sorted(replacements, key=len, reverse=True):
        text = text.replace(path, replacements[path])
    return text


def _run(
    arguments: list[str],
    *,
    environment: dict[str, str] | None = None,
    private_paths: tuple[str, ...] = (),
) -> tuple[str, int]:
    print("running: " + " ".join(arguments), flush=True)
    completed = subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = _sanitize(completed.stdout or "", private_paths)
    if completed.returncode:
        print(output, file=sys.stderr, flush=True)
    return output, completed.returncode


def _release_test_environment(**overrides: str) -> dict[str, str]:
    """Return an environment that keeps disposable test trees off network storage."""

    environment = os.environ.copy()
    environment.update(
        {
            "TMPDIR": RELEASE_TMPDIR,
            "TEMP": RELEASE_TMPDIR,
            "TMP": RELEASE_TMPDIR,
            **overrides,
        }
    )
    return environment


def _require_green_unittest(output: str, expected: int, *, skipped: int | None = None) -> str:
    if re.search(rf"(?m)^Ran {expected} tests? in ", output) is None:
        raise SystemExit(f"suite output does not contain the expected {expected}-test run")
    marker = "\nOK\n" if skipped is None else f"\nOK (skipped={skipped})\n"
    if marker not in output:
        raise SystemExit("suite output does not contain the expected green unittest marker")
    return output


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _publish_with_rollback(staging: Path) -> None:
    """Publish the validated set, restoring the previous set if a final gate fails."""

    saved: list[tuple[Path, Path]] = []
    published: list[Path] = []
    rolled_back = False

    def rollback() -> None:
        nonlocal rolled_back
        if rolled_back:
            return
        rolled_back = True
        for destination in reversed(published):
            unlink_release_file(destination)
        for prior, destination in reversed(saved):
            replace_release_file(prior, destination)

    with pinned_release_directories(
        (staging, RELEASE_LOGS[0].parent),
        on_validation_failure=rollback,
    ) as (staging_root, results_root):
        staged = [staging_root / path.name for path in (*RELEASE_LOGS, ATTESTATION)]
        destinations = [results_root / path.name for path in (*RELEASE_LOGS, ATTESTATION)]
        backup = staging_root / "previous"
        mkdir_release_directory(backup)
        try:
            for destination in destinations:
                try:
                    mode = _lstat(destination).st_mode
                except FileNotFoundError:
                    continue
                if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                    raise OSError(f"refusing to replace non-file release path: {destination}")
                prior = backup / destination.name
                replace_release_file(destination, prior)
                saved.append((prior, destination))
            for source, destination in zip(staged, destinations, strict=True):
                replace_release_file(source, destination)
                published.append(destination)

            errors = verify_release_logs()
            if errors:
                raise RuntimeError(
                    "published release evidence is inconsistent: " + "; ".join(errors)
                )
            privacy_output, privacy_status = _run(
                [
                    "uv",
                    "run",
                    "--locked",
                    "--extra",
                    "can",
                    "python",
                    "scripts/verify_evidence_privacy.py",
                ]
            )
            if privacy_status:
                raise RuntimeError(
                    "published release evidence failed privacy verification: " + privacy_output
                )
        except BaseException:
            rollback()
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python-install-dir",
        type=Path,
        help="UV-managed interpreter directory containing CPython 3.10.20",
    )
    arguments = parser.parse_args(argv)

    private_paths: set[str] = set()
    if arguments.python_install_dir is not None:
        supplied = arguments.python_install_dir.expanduser()
        private_paths.add(str(supplied.absolute()))
        private_paths.add(str(supplied.resolve()))
    redactions = tuple(sorted(private_paths, key=len, reverse=True))

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    manifest = working_tree_manifest_sha256()
    lock = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    uv_version = subprocess.check_output(["uv", "--version"], text=True).strip()
    python_version = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )

    def header(title: str, *, runtime: str = python_version) -> str:
        return (
            f"BYO-Firmware-MCP 0.1.8rc1 {title}\n"
            f"generated_utc={now}\n"
            f"base_git_head={head}\n"
            f"working_tree_manifest_sha256={manifest}\n"
            f"uv_lock_sha256={lock}\n"
            f"uv_version={uv_version}\n"
            f"python_version={runtime}\n"
        )

    static_commands = (
        [
            "uv",
            "run",
            "--locked",
            "--extra",
            "can",
            "--group",
            "dev",
            "python",
            "scripts/verify_changed_python_format.py",
        ],
        [
            "uv",
            "run",
            "--locked",
            "--extra",
            "can",
            "--group",
            "dev",
            "ruff",
            "check",
            "src",
            "tests",
            "scripts",
            "client-workspace",
            "client-workspace/bin/attach-project",
        ],
        [
            "uv",
            "run",
            "--locked",
            "--extra",
            "can",
            "--group",
            "dev",
            "pyright",
            "src",
            "scripts",
            "client-workspace",
            "client-workspace/bin/attach-project",
        ],
        ["uv", "lock", "--check"],
        [
            "uv",
            "run",
            "--locked",
            "--extra",
            "can",
            "python",
            "scripts/verify_evidence_privacy.py",
        ],
        ["git", "diff", "--check"],
    )
    static = header("static verification") + "--- output ---\n"
    overall = 0
    for command in static_commands:
        output, status = _run(command, private_paths=redactions)
        static += "command=" + " ".join(command) + "\n" + output
        if output and not output.endswith("\n"):
            static += "\n"
        static += f"exit_status={status}\n"
        overall = overall or status
    static += f"overall_exit_status={overall}\n"
    if overall:
        raise SystemExit("static verification failed")
    parse_environment = os.environ.copy()
    command_prefix = ""
    if arguments.python_install_dir is not None:
        install_dir = str(arguments.python_install_dir.resolve())
        parse_environment["UV_PYTHON_INSTALL_DIR"] = install_dir
        command_prefix = "UV_PYTHON_INSTALL_DIR=<python-install-dir> "
    parse_command = [
        "uv",
        "run",
        "--python",
        "3.10.20",
        "--no-project",
        "python",
        "scripts/verify_python310_parse.py",
    ]
    parse_output, parse_status = _run(
        parse_command,
        environment=parse_environment,
        private_paths=redactions,
    )
    version_output, version_status = _run(
        ["uv", "run", "--python", "3.10.20", "--no-project", "python", "--version"],
        environment=parse_environment,
        private_paths=redactions,
    )
    if parse_status or version_status or "result=PASS" not in parse_output:
        raise SystemExit(
            "CPython 3.10 parse failed; install it first with `uv python install 3.10.20`"
        )
    runtime = version_output.strip()
    parse_log = (
        header("CPython 3.10 parse verification", runtime=runtime)
        + "command="
        + command_prefix
        + " ".join(parse_command)
        + "\n--- output ---\n"
        + parse_output
        + f"--- result ---\nexit_status={parse_status}\n"
    )
    full_environment = _release_test_environment()
    # The retained-log consistency test is self-referential while this generator
    # is replacing those logs. Defer that one case, then run the whole release-
    # evidence module against immutable staged bytes below. No other test is
    # skipped by this environment variable.
    full_environment["BYO_RELEASE_EVIDENCE_GENERATING"] = "1"
    full_command = [
        "uv",
        "run",
        "--locked",
        "--extra",
        "can",
        "python",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        "test_*.py",
        "-v",
    ]
    with tempfile.TemporaryDirectory(prefix="byo-release-processes-") as owned_runs:
        full_environment["PYOCD_MCP_RUNS_ROOT"] = owned_runs
        full_output, full_status = _run(
            full_command,
            environment=full_environment,
            private_paths=(*redactions, owned_runs),
        )
    if full_status:
        raise SystemExit("full software suite failed")
    full_output = _require_green_unittest(
        full_output, EXPECTED_DEFAULT_TESTS, skipped=EXPECTED_DEFAULT_SKIPS
    )
    if working_tree_manifest_sha256() != manifest:
        raise SystemExit("candidate source changed while the full software suite was running")
    software_log = (
        header("software verification") + "command=BYO_RELEASE_EVIDENCE_GENERATING=1 TMPDIR=/tmp "
        "uv run --locked --extra can python -m unittest discover "
        "-s tests -p 'test_*.py' -v\n"
        + "deferred_self_reference=tests.test_release_evidence run against immutable staged logs\n"
        + "output_sanitization=absolute candidate root replaced with <candidate-root>\n"
        + "--- output ---\n"
        + full_output
        + "--- result ---\nexit_status=0\n"
    )
    live_environment = _release_test_environment(BYO_RUN_CODEX_E2E="1")
    live_command = [
        "uv",
        "run",
        "--locked",
        "--extra",
        "can",
        "python",
        "-m",
        "unittest",
        "tests.test_monitor_codex_e2e",
        "-v",
    ]
    live_output, live_status = _run(
        live_command,
        environment=live_environment,
        private_paths=redactions,
    )
    if live_status:
        raise SystemExit("live Codex/MCP monitor suite failed")
    live_output = _require_green_unittest(live_output, EXPECTED_LIVE_TESTS)
    if working_tree_manifest_sha256() != manifest:
        raise SystemExit("candidate source changed while the live monitor suite was running")
    live_log = (
        header("opt-in live Codex/MCP monitor verification")
        + "command=BYO_RUN_CODEX_E2E=1 TMPDIR=/tmp uv run --locked --extra can python "
        "-m unittest tests.test_monitor_codex_e2e -v\n"
        + "output_sanitization=absolute candidate root replaced with <candidate-root>\n"
        + "--- output ---\n"
        + live_output
        + "--- result ---\nexit_status=0\n"
    )
    if working_tree_manifest_sha256() != manifest:
        raise SystemExit("candidate source changed while release evidence was generated")

    # Keep the final four logs immutable while their attestation, complete
    # release-evidence test module, and privacy scan inspect those exact bytes.
    # Nothing under the retained results directory changes until every staging
    # gate succeeds.
    static += (
        "post_generation_validation=complete release-evidence module and privacy scan "
        "run against immutable staged bytes before publication\n"
    )
    with tempfile.TemporaryDirectory(prefix=".byo-release-evidence-", dir=ROOT.parent) as temp:
        staging = Path(temp)
        staged_content = {
            "0.1.8rc1-static.log": static,
            "0.1.8rc1-python310-parse.log": parse_log,
            "0.1.8rc1-software-suite.log": software_log,
            "0.1.8rc1-monitor-codex-e2e.log": live_log,
        }
        for name, content in staged_content.items():
            write_release_text(staging / name, content)
        write_attestation(staging)

        errors = verify_release_logs(staging)
        if errors:
            raise SystemExit("staged release evidence is inconsistent: " + "; ".join(errors))

        evidence_environment = os.environ.copy()
        evidence_environment["BYO_RELEASE_EVIDENCE_RESULTS_DIR"] = str(staging)
        evidence_command = [
            "uv",
            "run",
            "--locked",
            "--extra",
            "can",
            "python",
            "-m",
            "unittest",
            "tests.test_release_evidence",
            "-v",
        ]
        evidence_output, evidence_status = _run(
            evidence_command,
            environment=evidence_environment,
            private_paths=(*redactions, str(staging)),
        )
        if evidence_status:
            raise SystemExit("final release-evidence tests failed:\n" + evidence_output)

        privacy_command = [
            "uv",
            "run",
            "--locked",
            "--extra",
            "can",
            "python",
            "scripts/verify_evidence_privacy.py",
        ]
        staged_paths = tuple(staging / path.name for path in RELEASE_LOGS)
        for path in (*staged_paths, staging / ATTESTATION.name):
            privacy_command.extend(["--additional-evidence", str(path)])
        privacy_output, privacy_status = _run(
            privacy_command,
            private_paths=(*redactions, str(staging)),
        )
        if privacy_status:
            raise SystemExit(
                "final staged evidence failed privacy verification:\n" + privacy_output
            )
        if working_tree_manifest_sha256() != manifest:
            raise SystemExit("candidate source changed during final evidence validation")

        try:
            _publish_with_rollback(staging)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
    print(f"wrote release logs and attestation for manifest {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
