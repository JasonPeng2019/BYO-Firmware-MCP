#!/usr/bin/env python3
"""
Host-level readiness checks that should run before board-level Stage 0 validation.

This script does not claim a board fully works. It only checks whether the host can:
- run pyOCD
- enumerate probes
- enumerate serial ports
- load board configs
- check/install target packs referenced by board configs

With `--board-id`, it also checks whether the selected board's matching probe and
serial endpoint are visible enough to start `stage0_check.py`.

It may optionally reconcile the canonical Python environment and install missing
pyOCD packs. By design, it does not install OS drivers or proprietary vendor
probe software; those remain part of the short developer bootstrap this repo
expects before post-bootstrap automation takes over.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SRC_DIR = Path(__file__).resolve().parent / "src"
if SRC_DIR.is_dir():
    sys.path.insert(0, str(SRC_DIR))

from pyocd_debug_mcp.board_config import (  # noqa: E402
    DEFAULT_BOARD_CONFIG_DIR,
    BoardConfig,
    ConfigError,
    load_selected_board_configs,
    preview_board_config_paths,
)
from pyocd_debug_mcp.local_env import load_local_env  # noqa: E402
from pyocd_debug_mcp.pack_provision import (  # noqa: E402
    PackProvisionError,
    discover_local_packs,
    ensure_all,
)
from pyocd_debug_mcp.probe_inventory import (  # noqa: E402
    list_connected_probes,
    ProbeResolution,
    resolve_probe_for_board,
)
from pyocd_debug_mcp.serial_resolver import (  # noqa: E402
    SerialPortInfo,
    SerialResolution,
    command_exists,
    list_serial_ports,
    resolve_serial_port,
)
from pyocd_debug_mcp.timeouts import (  # noqa: E402
    DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
    SETUP_COMMAND_TIMEOUT_SECONDS,
    subprocess_timeout_stream_text,
)

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
INFO = "INFO"

load_local_env()


@dataclass(frozen=True)
class DependencySpec:
    package_name: str
    import_name: str
    required: bool
    reason: str


DEPENDENCIES = (
    DependencySpec(
        package_name="pyocd",
        import_name="pyocd",
        required=True,
        reason="required to enumerate probes, targets, and packs",
    ),
    DependencySpec(
        package_name="pyserial",
        import_name="serial",
        required=True,
        reason="required to enumerate serial ports",
    ),
    DependencySpec(
        package_name="pyyaml",
        import_name="yaml",
        required=False,
        reason="required to load YAML board configs",
    ),
    DependencySpec(
        package_name="python-dotenv",
        import_name="dotenv",
        required=True,
        reason="required to auto-load repo-local .env defaults",
    ),
)


@dataclass(frozen=True)
class BoardAttachmentStatus:
    board: BoardConfig
    probe_status: str
    probe_message: str
    serial_status: str
    serial_message: str
    ready: bool


def run(
    cmd: list[str],
    capture: bool = True,
    cwd: Path | None = None,
    timeout_seconds: float = DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        executable = cmd[0] if cmd else "<unknown>"
        return 127, "", f"command not found: {executable}"
    except subprocess.TimeoutExpired as exc:
        return (
            124,
            subprocess_timeout_stream_text(exc.stdout),
            f"command timed out after {timeout_seconds:.0f}s: {' '.join(cmd)}",
        )
    return result.returncode, result.stdout or "", result.stderr or ""


def header(text: str):
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print("=" * 60)


def log(status: str, message: str):
    print(f"  [{status}] {message}")


def package_installed(import_name: str) -> bool:
    return importlib.util.find_spec(import_name) is not None


def reconcile_canonical_env(package_names: list[str]) -> bool:
    if not package_names:
        return True
    repo_root = Path(__file__).resolve().parent
    cmd = ["uv", "sync", "--locked"]
    print(f"  Attempting: {' '.join(cmd)}")
    rc, _, _ = run(
        cmd,
        capture=False,
        cwd=repo_root,
        timeout_seconds=SETUP_COMMAND_TIMEOUT_SECONDS,
    )
    return rc == 0


def dependency_summary(require_yaml: bool, install_missing: bool) -> dict[str, bool]:
    header("Python dependencies")

    results: dict[str, bool] = {}
    missing_required: list[str] = []
    missing_optional: list[str] = []

    for dep in DEPENDENCIES:
        installed = package_installed(dep.import_name)
        results[dep.package_name] = installed
        if installed:
            log(PASS, f"{dep.package_name} installed")
            continue

        if dep.required or (dep.package_name == "pyyaml" and require_yaml):
            log(FAIL, f"{dep.package_name} missing - {dep.reason}")
            missing_required.append(dep.package_name)
        else:
            log(WARN, f"{dep.package_name} missing - {dep.reason}")
            missing_optional.append(dep.package_name)

    if install_missing:
        install_list = missing_required + missing_optional
        if install_list:
            ok = reconcile_canonical_env(install_list)
            if ok:
                log(PASS, "Reconciled the canonical repo environment with 'uv sync --locked'")
            else:
                log(
                    FAIL,
                    "Failed to reconcile the canonical repo environment with 'uv sync --locked'",
                )
            for dep in DEPENDENCIES:
                results[dep.package_name] = package_installed(dep.import_name)

    return results


def serial_summary(pyserial_ok: bool) -> int | None:
    header("Serial ports")
    if not pyserial_ok:
        log(FAIL, "pyserial missing - cannot enumerate serial ports")
        return None

    ports = list_serial_ports()
    if ports is None:
        log(FAIL, "pyserial import failed during serial enumeration")
        return None
    if not ports:
        log(WARN, "No serial ports detected")
        return 0

    log(PASS, f"Detected {len(ports)} serial port(s)")
    for port in ports:
        print(f"    - {port.device}")
    return len(ports)


def list_target_names(pack_args: list[str] | None = None) -> set[str]:
    cmd = ["pyocd", "list", "--targets"]
    if pack_args:
        cmd.extend(pack_args)
    _, out, _ = run(cmd)
    names: set[str] = set()
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or re.fullmatch(r"-+", stripped):
            continue
        names.add(stripped.split()[0].lower())
    return names


def pyocd_summary(pyocd_ok: bool) -> int | None:
    header("pyOCD host visibility")
    if not pyocd_ok:
        log(FAIL, "pyocd missing - cannot enumerate probes or targets")
        return None

    rc, out, err = run(["pyocd", "--version"])
    if rc == 0:
        log(PASS, f"pyOCD found: {out.strip()}")
    else:
        log(FAIL, f"pyOCD command failed: {(err or out).strip()[:300]}")
        return None

    probes = list_connected_probes(lambda cmd: run(cmd))
    if probes:
        log(PASS, f"Detected {len(probes)} probe(s)")
        for probe in probes:
            desc = probe.description or probe.raw
            suffix = f" [{probe.state}]" if probe.state else ""
            print(f"    - {probe.uid or '(no uid)'} :: {desc}{suffix}")
        return len(probes)
    else:
        log(WARN, "No debug probes detected by pyOCD")
        print(
            "      This usually means an OS driver / vendor tooling / USB enumeration issue, not a board-config issue."
        )
        return 0


def board_config_summary(boards: list[BoardConfig]):
    header("Board configs")
    if not boards:
        log(WARN, "No board configs selected")
        return

    log(PASS, f"Loaded {len(boards)} board config(s)")
    for board in boards:
        print(
            f"    - {board.board_id} :: {board.display_name} :: target={board.pyocd_target} :: pack={board.pack_name}"
        )


def target_pack_summary(
    boards: list[BoardConfig], pyocd_ok: bool, install_packs: bool
) -> dict[str, bool]:
    header("Target packs")
    if not boards:
        log(WARN, "No board configs selected - skipping pack checks")
        return {}
    if not pyocd_ok:
        log(FAIL, "pyocd missing - cannot check target packs")
        return {board.board_id: False for board in boards}

    # Provision pinned packs first (deterministic; does NOT depend on the live
    # pyOCD pack index, which fetches ~1500 vendor descriptors and silently drops
    # whole families on restrictive networks). See packs/manifest.yaml.
    if install_packs:
        try:
            provisioned = ensure_all()
            if provisioned:
                log(PASS, f"Provisioned {len(provisioned)} pinned pack(s) into packs/")
        except PackProvisionError as exc:
            log(FAIL, f"Pinned pack provisioning failed: {exc}")

    local_packs = discover_local_packs()
    pack_args: list[str] = []
    for pack in local_packs:
        pack_args.extend(["--pack", str(pack)])

    # Pack-aware availability: targets from local .pack files only appear when the
    # listing is given the same --pack args the runtime uses.
    installed_targets = list_target_names(pack_args)
    results: dict[str, bool] = {}

    for board in boards:
        if board.pyocd_target.lower() in installed_targets:
            via = " (via pinned local pack)" if local_packs else ""
            log(PASS, f"{board.board_id}: target '{board.pyocd_target}' available{via}")
            results[board.board_id] = True
            continue

        log(WARN, f"{board.board_id}: target '{board.pyocd_target}' not found")
        if install_packs:
            log(
                FAIL,
                f"{board.board_id}: no built-in target and no pinned pack provides "
                f"'{board.pyocd_target}'",
            )
            print(
                f"      Fix: add a pinned entry for {board.pack_name} to packs/manifest.yaml "
                "(id/url/version/sha256), then rerun with --install-packs"
            )
        else:
            print("      Fix: rerun with --install-packs to provision pinned packs")
        results[board.board_id] = False

    return results


def vendor_serial_tool_summary(boards: list[BoardConfig]):
    header("Serial auto-detect helpers")
    if not boards:
        log(WARN, "No board configs selected - skipping vendor serial-tool hints")
        return

    needs_nrfjprog = any(
        board.mcu_family.lower().startswith("nrf") and board.probe_family == "jlink"
        for board in boards
    )
    needs_stm32_cli = any(board.probe_family == "stlink" for board in boards)

    if not needs_nrfjprog and not needs_stm32_cli:
        log(INFO, "Selected boards do not require vendor-specific serial auto-detect helpers")
        return

    if needs_nrfjprog:
        if command_exists("nrfjprog"):
            log(
                PASS,
                "nrfjprog found - Nordic J-Link serial auto-detect can use 'nrfjprog --com'",
            )
        else:
            log(
                WARN,
                "nrfjprog not found - Nordic J-Link serial auto-detect helper is unavailable; falling back to generic matching or manual --port",
            )

    if needs_stm32_cli:
        if command_exists("STM32_Programmer_CLI"):
            log(
                PASS,
                "STM32_Programmer_CLI found - ST-LINK serial auto-detect can use 'STM32_Programmer_CLI -l'",
            )
        else:
            log(
                WARN,
                "STM32_Programmer_CLI not found - ST-LINK serial auto-detect helper is unavailable; falling back to generic matching or manual --port",
            )


def _serial_status_from_resolution(resolution: SerialResolution) -> str:
    if resolution.port is not None:
        return PASS
    if resolution.note.startswith("multiple matching serial ports found"):
        return WARN
    return FAIL


def _format_probe_message(board: BoardConfig, resolution: ProbeResolution) -> tuple[str, str]:
    if resolution.probe is None:
        return FAIL, f"{board.board_id}: {resolution.note}"
    note = f" ({resolution.note})" if resolution.note else ""
    return (
        PASS,
        f"{board.board_id}: matched probe {resolution.probe.uid} :: {resolution.probe.description}{note}",
    )


def _format_serial_message(board: BoardConfig, resolution: SerialResolution) -> tuple[str, str]:
    if resolution.port is not None:
        note = f" ({resolution.note})" if resolution.note else ""
        return PASS, f"{board.board_id}: matched serial port {resolution.port.device}{note}"
    return _serial_status_from_resolution(resolution), f"{board.board_id}: {resolution.note}"


def assess_board_attachment_statuses(
    boards: list[BoardConfig],
    *,
    pyserial_ok: bool,
    run_cmd: Callable[[list[str]], tuple[int, str, str]],
) -> tuple[BoardAttachmentStatus, ...]:
    ports: list[SerialPortInfo] | None
    if not pyserial_ok:
        ports = None
    else:
        ports = list_serial_ports()

    statuses: list[BoardAttachmentStatus] = []
    for board in boards:
        probe_resolution = resolve_probe_for_board(
            board,
            run_cmd=run_cmd,
            allow_single_fallback=True,
        )
        probe_status, probe_message = _format_probe_message(board, probe_resolution)

        if probe_resolution.probe is None:
            statuses.append(
                BoardAttachmentStatus(
                    board=board,
                    probe_status=probe_status,
                    probe_message=probe_message,
                    serial_status=INFO,
                    serial_message=f"{board.board_id}: serial check skipped until a matching probe is resolved",
                    ready=False,
                )
            )
            continue

        if not pyserial_ok:
            serial_resolution = SerialResolution(
                None, "pyserial missing - cannot resolve board-specific serial ports"
            )
        elif ports is None:
            serial_resolution = SerialResolution(
                None, "pyserial import failed during serial enumeration"
            )
        elif not ports:
            serial_resolution = SerialResolution(None, "no serial ports detected")
        else:
            serial_resolution = resolve_serial_port(
                board,
                ports,
                probe_resolution.probe,
                override=None,
                allow_single_fallback=False,
                run_cmd=run_cmd,
                interactive=False,
            )

        serial_status, serial_message = _format_serial_message(board, serial_resolution)
        statuses.append(
            BoardAttachmentStatus(
                board=board,
                probe_status=probe_status,
                probe_message=probe_message,
                serial_status=serial_status,
                serial_message=serial_message,
                ready=probe_status == PASS and serial_status == PASS,
            )
        )

    return tuple(statuses)


def board_attachment_summary(
    boards: list[BoardConfig],
    *,
    pyserial_ok: bool,
    run_cmd: Callable[[list[str]], tuple[int, str, str]],
) -> bool:
    header("Selected-board attachment readiness")
    if not boards:
        log(WARN, "No board configs selected - skipping board attachment checks")
        return False

    statuses = assess_board_attachment_statuses(
        boards,
        pyserial_ok=pyserial_ok,
        run_cmd=run_cmd,
    )
    for status in statuses:
        log(status.probe_status, status.probe_message)
        log(status.serial_status, status.serial_message)
    return all(status.ready for status in statuses)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Host bootstrap before Stage 0 board checks")
    parser.add_argument(
        "--board-config-dir",
        default=str(DEFAULT_BOARD_CONFIG_DIR),
        help="Directory containing board config files. Defaults to the repo's boards/ directory.",
    )
    parser.add_argument(
        "--board-config",
        action="append",
        default=[],
        help="Additional board config file (.json, .yaml, .yml). Repeat for multiple extra boards.",
    )
    parser.add_argument(
        "--board-id",
        action="append",
        default=[],
        help="Board id to inspect. Repeat to select multiple boards. Defaults to all non-example board configs in the board-config-dir plus any extra board-config files.",
    )
    parser.add_argument(
        "--install-missing",
        action="store_true",
        help="Reconcile the canonical repo environment with 'uv sync --locked' if required Python packages are missing.",
    )
    parser.add_argument(
        "--install-packs",
        action="store_true",
        help="Install missing pyOCD target packs referenced by the selected board configs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    board_config_dir = Path(args.board_config_dir).expanduser().resolve()
    extra_paths = [Path(raw_path).expanduser().resolve() for raw_path in args.board_config]
    require_yaml = any(
        path.suffix.lower() in {".yaml", ".yml"}
        for path in [*extra_paths, *preview_board_config_paths(board_config_dir)]
    )

    dependency_results = dependency_summary(
        require_yaml=require_yaml, install_missing=args.install_missing
    )
    pyocd_ok = dependency_results.get("pyocd", False)
    pyserial_ok = dependency_results.get("pyserial", False)

    probe_count = pyocd_summary(pyocd_ok)
    serial_count = serial_summary(pyserial_ok)

    boards: list[BoardConfig] = []
    board_config_ok = True
    try:
        boards = load_selected_board_configs(
            board_config_dir,
            extra_paths=extra_paths,
            requested_ids=args.board_id,
        )
    except ConfigError as exc:
        board_config_ok = False
        header("Board configs")
        log(FAIL, str(exc))

    board_config_summary(boards)
    pack_results = target_pack_summary(boards, pyocd_ok=pyocd_ok, install_packs=args.install_packs)
    vendor_serial_tool_summary(boards)
    selected_board_ids = [
        board_id.strip().lower() for board_id in args.board_id if board_id.strip()
    ]
    board_attachment_ready: bool | None = None
    if selected_board_ids:
        board_attachment_ready = board_attachment_summary(
            boards,
            pyserial_ok=pyserial_ok,
            run_cmd=run,
        )
    packs_ready = bool(boards) and all(pack_results.get(board.board_id, False) for board in boards)
    host_prereqs_ready = (
        pyocd_ok
        and pyserial_ok
        and board_config_ok
        and packs_ready
        and (probe_count or 0) > 0
        and (serial_count or 0) > 0
    )
    overall_ready = (
        host_prereqs_ready
        if board_attachment_ready is None
        else host_prereqs_ready and board_attachment_ready
    )

    header("Summary")
    if board_attachment_ready is None:
        if overall_ready:
            log(INFO, "Host prerequisites and board-target support are ready for stage0_check.py")
        elif not pyocd_ok or not pyserial_ok or not board_config_ok or not packs_ready:
            log(WARN, "Host is not fully ready for stage0_check.py yet")
        else:
            log(
                WARN,
                "Canonical env and board-target support are present, but attached hardware is not fully visible yet",
            )
    elif overall_ready:
        log(INFO, "Selected board attachment and host prerequisites are ready for stage0_check.py")
    elif not host_prereqs_ready:
        log(
            WARN,
            "Host bootstrap is not sufficient yet for the selected board(s): host prerequisites are incomplete",
        )
    else:
        log(
            WARN,
            "Host bootstrap is not sufficient yet for the selected board(s): board attachment is not uniquely ready",
        )
    print("  This script does not verify flashing, UART behavior, or recovery on real hardware.")
    return 0 if overall_ready else 1


if __name__ == "__main__":
    sys.exit(main())
