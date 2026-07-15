#!/usr/bin/env python3
"""
Stage 0 board and toolchain validation.

Automates the Stage 0 checks that are safe and observable from the host:
- pyOCD installed
- board probe visible
- target pack available
- SWD connect + register read
- virtual COM port visible
- optional reference firmware flash
- optional UART smoke test

Board definitions are data-driven. Board facts come from board config files,
not hardcoded board entries in this script.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SRC_DIR = Path(__file__).resolve().parent / "src"
if SRC_DIR.is_dir():
    sys.path.insert(0, str(SRC_DIR))

from pyocd_debug_mcp.board_config import (  # noqa: E402
    DEFAULT_BOARD_CONFIG_DIR,
    RECOVER_MODE_MANUAL_ONLY,
    RECOVER_MODE_NRF_PYOCD_UNLOCK,
    BoardConfig,
    ConfigError,
    load_selected_board_configs,
    parse_int,
    validate_width_bits,
)
from pyocd_debug_mcp.guardrails.flash_gate import resolve_flash_request  # noqa: E402
from pyocd_debug_mcp.guardrails.recover_gate import authorize_recover  # noqa: E402
from pyocd_debug_mcp.local_env import load_local_env  # noqa: E402
from pyocd_debug_mcp.pack_provision import (  # noqa: E402
    PackProvisionError,
    discover_local_packs,
    ensure_all,
)
from pyocd_debug_mcp.probe_inventory import (  # noqa: E402
    ProbeInfo,
    list_connected_probes,
    pick_probe_for_board,
)
from pyocd_debug_mcp.serial_resolver import (  # noqa: E402
    SerialPortInfo,
    command_exists,
    is_interactive_terminal,
    list_serial_ports,
    resolve_serial_port,
)
from pyocd_debug_mcp.services.session_runtime import ActionContext, PolicyRefusal  # noqa: E402
from pyocd_debug_mcp.services import target_control  # noqa: E402
from pyocd_debug_mcp.services.uart_capture import capture_uart_output  # noqa: E402
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle  # noqa: E402
from pyocd_debug_mcp.target_errors import (  # noqa: E402
    LockedTargetError,
    TargetConnectionError,
    UnsupportedArtifactError,
)
from pyocd_debug_mcp.timeouts import (  # noqa: E402
    DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
    subprocess_timeout_stream_text,
)

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
INFO = "INFO"

SUMMARY_PASS = "pass"
SUMMARY_FAIL = "fail"
SUMMARY_MANUAL = "manual"

load_local_env()


@dataclass(frozen=True)
class SummaryItem:
    label: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class ReadResult:
    rc: int
    stdout: str
    stderr: str
    value: int | None = None
    error: Exception | None = None

    @property
    def combined(self) -> str:
        return f"{self.stdout}\n{self.stderr}".lower()


@dataclass(frozen=True)
class RecoverResult:
    completed: bool
    reconnect_ok: bool | None = None
    post_identity_ok: bool | None = None


@dataclass
class FlashResult:
    status: bool | None
    session_handle: TargetSessionHandle | None = None


def run(
    cmd: list[str],
    capture: bool = True,
    timeout_seconds: float = DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
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


def check(ok: bool, message: str) -> bool:
    log(PASS if ok else FAIL, message)
    return ok


def board_cli_label(board: BoardConfig) -> str:
    return board.board_id


def build_recover_attempts(
    board: BoardConfig, probe: ProbeInfo | None
) -> list[tuple[str, list[str]]]:
    if board.recover_mode == RECOVER_MODE_NRF_PYOCD_UNLOCK:
        # VENDOR-FIXED, BENCH-VERIFIED (pyOCD 0.44.1 built-in recover path for Nordic APPROTECT).
        unlock_cmd = pyocd_base("cmd", board, probe)
        unlock_cmd.extend(["-c", "unlock"])

        # VENDOR-FIXED, BENCH-VERIFIED (pyOCD 0.44.1 mass erase fallback when Commander unlock fails).
        mass_erase_cmd = pyocd_base("erase", board, probe)
        mass_erase_cmd.append("--mass")

        return [
            ("pyOCD built-in unlock", unlock_cmd),
            ("pyOCD built-in mass erase fallback", mass_erase_cmd),
        ]

    return []


def pyocd_base(subcommand: str, board: BoardConfig, probe: ProbeInfo | None) -> list[str]:
    cmd = ["pyocd", subcommand, "-t", board.pyocd_target]
    if probe and probe.uid:
        cmd.extend(["-u", probe.uid])
    if board.probe_family == "jlink":
        # VENDOR-FIXED, UNVERIFIED ACROSS VERSIONS (pyOCD session option; behaviour depends on
        # the pyOCD + pylink + J-Link DLL combo). pyOCD calls pylink's disable_dialog_boxes()
        # when jlink.non_interactive is True (its default). Verified here on pyOCD 0.44.1 +
        # pylink 1.7.0 + J-Link DLL V9.50: that call clears the USB emulator selection, so the
        # subsequent open-by-serial fails with "No emulator with serial number <sn> found" even
        # though the probe is present and enumerable. Turning the option off skips the call and
        # lets the open succeed. Tradeoff: a J-Link dialog (e.g. a firmware-update prompt) could
        # appear during an automated run. NOTE: the plan standardizes the shipped product on
        # CMSIS-DAP, not this SEGGER-DLL path (see surfaced conflict).
        cmd.extend(["-O", "jlink.non_interactive=false"])
    # Load any locally-provisioned CMSIS-Packs (pinned, sha256-verified) so the
    # exact target resolves without depending on the live pyOCD pack index.
    for pack in discover_local_packs():
        cmd.extend(["--pack", str(pack)])
    return cmd


def load_pyserial():
    try:
        import serial as serial_mod
        from serial.tools import list_ports
    except ImportError:
        return None, None
    return serial_mod, list_ports


def parse_key_value(items: list[str], option_name: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ConfigError(f"{option_name} requires BOARD_ID=VALUE, got '{item}'")
        key, value = item.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if not key or not value:
            raise ConfigError(f"{option_name} requires BOARD_ID=VALUE, got '{item}'")
        parsed[key] = value
    return parsed


def resolve_firmware_path(path_value: str | None, base_dir: Path | None = None) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path.resolve()


def resolve_required_firmware_path(path_value: str) -> Path:
    path = resolve_firmware_path(path_value)
    if path is None:
        raise ConfigError("reference firmware override path cannot be empty")
    return path


def action_context(action_name: str) -> ActionContext:
    return ActionContext(source="stage0_check", action_name=action_name, session_id=None)


def check_pyocd_installed() -> bool:
    header("pyOCD installation")
    rc, out, _ = run(["pyocd", "--version"])
    ok = rc == 0
    if ok:
        check(True, f"pyOCD found: {out.strip()}")
    else:
        check(False, "pyOCD not found - run: uv sync")
    return ok


def check_pyserial_installed() -> bool:
    header("pyserial installation")
    serial_mod, _ = load_pyserial()
    ok = serial_mod is not None
    if ok:
        version = getattr(serial_mod, "__version__", "unknown version")
        check(True, f"pyserial found: {version}")
    else:
        check(False, "pyserial not found - run: uv sync")
    return ok


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


def read_memory_value(
    board: BoardConfig,
    probe: ProbeInfo | None,
    address: int,
    width_bits: int,
) -> ReadResult:
    if probe is None:
        return ReadResult(rc=1, stdout="", stderr="probe not detected")

    width_bits = validate_width_bits(width_bits, "width_bits")
    last_result = ReadResult(rc=1, stdout="", stderr="unknown read failure")
    for attempt in range(3):
        handle = None
        try:
            handle = target_control.open_session(
                board=board,
                unique_id=probe.uid or None,
                target=board.pyocd_target,
            )
            value = target_control.read_memory(handle, address, width_bits)
            width_nibbles = width_bits // 4
            out = f"{address:08X}:  {value:0{width_nibbles}X}"
            return ReadResult(rc=0, stdout=out, stderr="", value=value)
        except Exception as exc:  # noqa: BLE001 - keep the raw backend failure text
            last_result = ReadResult(
                rc=1,
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
                value=None,
                error=exc,
            )
        finally:
            if handle is not None:
                try:
                    target_control.close_session(handle)
                except Exception:  # noqa: BLE001 - close failure should not mask the read error
                    pass

        combined = last_result.combined
        if (
            attempt < 2
            and isinstance(last_result.error, TargetConnectionError)
            and (
                "j-link is already open" in combined
                or "could not read j-link capabilities" in combined
            )
        ):
            time.sleep(0.25)
            continue
        break
    return last_result


def check_probes(boards_to_check: list[BoardConfig]) -> dict[str, ProbeInfo | None]:
    header("Connected probes")
    probes = list_connected_probes(lambda cmd: run(cmd))

    if not probes:
        _, out, _ = run(["pyocd", "list"])
        print(f"  Raw output:\n{out or '  (none)'}")
        results = {}
        for board in boards_to_check:
            check(False, f"{board.display_name} ({board.probe_type}) - no probes detected")
            results[board.board_id] = None
        return results

    print(f"  Found {len(probes)} probe(s):")
    for probe in probes:
        desc = probe.description or probe.raw
        suffix = f" [{probe.state}]" if probe.state else ""
        print(f"    - {probe.uid or '(no uid)'} :: {desc}{suffix}")

    results: dict[str, ProbeInfo | None] = {}
    allow_single_fallback = len(probes) == 1 and len(boards_to_check) == 1
    for board in boards_to_check:
        resolution = pick_probe_for_board(
            board,
            probes,
            allow_single_fallback=allow_single_fallback,
        )
        probe = resolution.probe
        note = resolution.note
        if probe:
            detail = f"{board.display_name} ({board.probe_type}) probe visible"
            if note:
                detail += f" - {note}"
            check(True, detail)
        else:
            check(
                False,
                f"{board.display_name} ({board.probe_type}) probe not uniquely identifiable: {note}",
            )
        results[board.board_id] = probe

    return results


def check_target_packs(boards_to_check: list[BoardConfig], auto_install: bool) -> dict[str, bool]:
    header("CMSIS-Pack / target availability")
    local_packs = discover_local_packs()
    pack_args: list[str] = []
    for pack in local_packs:
        pack_args.extend(["--pack", str(pack)])
    installed_targets = list_target_names(pack_args)

    results: dict[str, bool] = {}
    for board in boards_to_check:
        target_present = board.pyocd_target.lower() in installed_targets
        if target_present:
            check(True, f"{board.display_name}: target '{board.pyocd_target}' available")
            results[board.board_id] = True
            continue

        check(False, f"{board.display_name}: target '{board.pyocd_target}' not found")
        if auto_install:
            log(INFO, f"Provisioning pinned packs for {board.pack_name}...")
            try:
                provisioned = ensure_all()
            except PackProvisionError as exc:
                check(False, f"Pinned pack provisioning failed: {exc}")
                results[board.board_id] = False
                continue
            if provisioned:
                log(INFO, f"Provisioned {len(provisioned)} pinned pack(s)")
            local_packs = discover_local_packs()
            pack_args = []
            for pack in local_packs:
                pack_args.extend(["--pack", str(pack)])
            ok = board.pyocd_target.lower() in list_target_names(pack_args)
            via = " via pinned local pack" if ok and local_packs else ""
            check(
                ok,
                f"Target '{board.pyocd_target}' now {'available' if ok else 'still missing'}{via}",
            )
            results[board.board_id] = ok
        else:
            print("      Fix: re-run with --install-packs to provision pinned packs")
            print(
                f"      If this board is not yet covered, add a pinned entry for {board.pack_name} to packs/manifest.yaml"
            )
            results[board.board_id] = False

    return results


def check_connection(board: BoardConfig, probe: ProbeInfo | None, target_ok: bool) -> bool:
    header(f"Connection test - {board.display_name}")

    if probe is None:
        check(False, "Skipped - probe not detected")
        return False
    if not target_ok:
        check(False, "Skipped - target pack not installed")
        return False

    print(
        "  Attempting: "
        f"pyOCD API read32 {hex(board.test_addr)} via {probe.uid or 'auto-selected probe'}"
    )
    result = read_memory_value(board, probe, board.test_addr, 32)

    if result.value is not None:
        check(True, f"Connected and read {hex(board.test_addr)}: 0x{result.value:08X}")
        return True

    if isinstance(result.error, LockedTargetError):
        log(WARN, f"{board.display_name} appears access-protected.")
        for _, recover_cmd in build_recover_attempts(board, probe):
            print(f"      Recover with: {' '.join(recover_cmd)}")
        if board.requires_recover_validation and board.recover_mode == RECOVER_MODE_MANUAL_ONLY:
            print(
                "      Recover mode for this board is manual_only; this repo treats recover as a manual bench step for this family."
            )
        return False

    if isinstance(result.error, TargetConnectionError):
        if (
            "no emulator" in result.combined
            or "could not read j-link capabilities" in result.combined
            or "no j-link" in result.combined
            or "unable to open" in result.combined
            or "could not open" in result.combined
            or "failed to open" in result.combined
            or "access denied" in result.combined
            or "no backend available" in result.combined
            or "no usb backend" in result.combined
            or "libusb" in result.combined
        ):
            check(
                False,
                f"{board.display_name} probe is visible but the {board.probe_type} backend could not claim it",
            )
            print("      The probe enumerates on the host but the debug channel cannot be opened.")
            print(
                "      This is almost always a USB driver-binding or exclusive-access issue, not the board:"
            )
            print(
                "      - Close anything else holding the probe (other pyOCD/JLink/IDE sessions, an MCP server) and replug."
            )
            if board.probe_family == "jlink":
                print(
                    "      - The J-Link debug interface must be bound to the SEGGER J-Link driver, not generic WinUSB."
                )
                print(
                    "        Reinstall the SEGGER J-Link Software pack (it rebinds the driver), then replug."
                )
            elif board.probe_family == "stlink":
                print("      - Install/repair the ST-Link USB driver, then replug.")
            else:
                print(
                    "      - Ensure the debug interface is bound to the driver pyOCD expects for this probe family."
                )
            return False

        check(False, "pyOCD found the probe but could not connect to the target MCU")
        print("      - Is the board powered?")
        print("      - Is the USB cable a data cable (not charge-only)?")
        if board.probe_family == "jlink":
            print("      - J-Link firmware or drivers may need updating")
        return False

    check(False, f"Unexpected error (rc={result.rc})")
    if result.stdout.strip():
        print(f"      stdout: {result.stdout.strip()[:300]}")
    if result.stderr.strip():
        print(f"      stderr: {result.stderr.strip()[:300]}")
    return False


def check_silicon_identity(
    board: BoardConfig,
    probe: ProbeInfo | None,
    target_ok: bool,
) -> bool | None:
    if board.silicon_id_addr is None or board.silicon_id_expected is None:
        return None

    header(f"Silicon identity - {board.display_name}")

    if probe is None:
        check(False, "Skipped - probe not detected")
        return False
    if not target_ok:
        check(False, "Skipped - target pack not installed")
        return False

    label = board.silicon_id_label or "silicon identity"
    print(
        "  Attempting: "
        f"pyOCD API read{board.silicon_id_width_bits} {hex(board.silicon_id_addr)} "
        f"via {probe.uid or 'auto-selected probe'}"
    )
    result = read_memory_value(board, probe, board.silicon_id_addr, board.silicon_id_width_bits)
    if result.value is None:
        check(False, f"Unable to read {label}")
        if result.stdout.strip():
            print(f"      stdout: {result.stdout.strip()[:300]}")
        if result.stderr.strip():
            print(f"      stderr: {result.stderr.strip()[:300]}")
        return False

    mask = (
        board.silicon_id_mask
        if board.silicon_id_mask is not None
        else (1 << board.silicon_id_width_bits) - 1
    )
    actual_masked = result.value & mask
    expected_masked = board.silicon_id_expected & mask
    ok = actual_masked == expected_masked
    check(
        ok,
        f"{label} {'matched' if ok else 'did not match'} "
        f"(actual=0x{actual_masked:X}, expected=0x{expected_masked:X}, mask=0x{mask:X})",
    )
    if result.stdout.strip():
        print(f"      Raw: {result.stdout.strip()[:300]}")
    return ok


def check_virtual_com_ports(
    boards_to_check: list[BoardConfig],
    probes_by_board: dict[str, ProbeInfo | None],
    port_overrides: dict[str, str],
    pyserial_ok: bool,
) -> dict[str, SerialPortInfo | None]:
    header("Virtual COM / serial ports")

    if not pyserial_ok:
        for board in boards_to_check:
            check(False, f"{board.display_name} COM-port check skipped - pyserial is not installed")
        return {board.board_id: None for board in boards_to_check}

    ports = list_serial_ports()
    if ports is None:
        for board in boards_to_check:
            check(False, f"{board.display_name} COM-port check unavailable")
        return {board.board_id: None for board in boards_to_check}

    if not ports:
        check(False, "No serial ports detected")
        return {board.board_id: None for board in boards_to_check}

    print(f"  Found {len(ports)} serial port(s):")
    for port in ports:
        desc = port.description or "(no description)"
        extras = " ".join(
            part
            for part in [
                port.manufacturer,
                port.product,
                port.interface,
                port.serial_number,
                port.location,
            ]
            if part
        )
        if extras:
            desc = f"{desc} :: {extras}"
        print(f"    - {port.device} :: {desc}")

    results: dict[str, SerialPortInfo | None] = {}
    allow_single_fallback = len(ports) == 1 and len(boards_to_check) == 1
    interactive = is_interactive_terminal()
    for board in boards_to_check:
        resolution = resolve_serial_port(
            board=board,
            ports=ports,
            probe=probes_by_board.get(board.board_id),
            override=port_overrides.get(board.board_id),
            allow_single_fallback=allow_single_fallback,
            run_cmd=run,
            interactive=interactive,
        )
        port = resolution.port
        note = resolution.note
        if port:
            detail = f"{board.display_name} virtual COM port visible on {port.device}"
            if note:
                detail += f" - {note}"
            check(True, detail)
        else:
            check(False, f"{board.display_name} COM port not uniquely identifiable: {note}")
            if board.uart_note:
                print(f"      Note: {board.uart_note}")
        results[board.board_id] = port

    return results


def flash_reference_firmware(
    board: BoardConfig,
    probe: ProbeInfo | None,
    target_ok: bool,
    identity_ok: bool | None,
    reference_firmware_path: Path | None,
) -> FlashResult:
    header(f"Reference firmware flash - {board.display_name}")

    if reference_firmware_path is None:
        log(WARN, "No reference firmware path supplied - leaving flash validation as a manual step")
        return FlashResult(None)
    if not reference_firmware_path.exists():
        check(False, f"Reference firmware file does not exist: {reference_firmware_path}")
        return FlashResult(False)
    if probe is None:
        check(False, "Skipped - probe not detected")
        return FlashResult(False)
    if not target_ok:
        check(False, "Skipped - target pack not installed")
        return FlashResult(False)
    if board.silicon_id_expected is not None and identity_ok is False:
        check(False, "Skipped - connected silicon identity does not match this board config")
        return FlashResult(False)

    def try_nrfjprog_fallback() -> bool:
        if not (board.mcu_family.startswith("nrf") and board.probe_family == "jlink"):
            return False
        if not command_exists("nrfjprog"):
            return False

        flash_image_path = reference_firmware_path
        if reference_firmware_path.suffix.lower() != ".hex":
            hex_candidate = reference_firmware_path.with_suffix(".hex")
            if not hex_candidate.exists():
                print(
                    "      pyOCD flash failed and no sibling .hex artifact exists for the nrfjprog fallback"
                )
                return False
            flash_image_path = hex_candidate

        flash_cmd = [
            "nrfjprog",
            "--program",
            str(flash_image_path),
            "--sectorerase",
            "--verify",
            "-f",
            "NRF52",
        ]
        reset_cmd = ["nrfjprog", "--reset", "-f", "NRF52"]
        print(f"  Attempting fallback: {' '.join(flash_cmd)}")
        flash_rc, flash_out, flash_err = run(flash_cmd)
        if flash_rc != 0:
            if flash_err.strip():
                print(f"      fallback stderr: {flash_err.strip()[:300]}")
            return False

        print(f"  Attempting fallback reset: {' '.join(reset_cmd)}")
        reset_rc, reset_out, reset_err = run(reset_cmd)
        if reset_rc != 0:
            if reset_err.strip():
                print(f"      reset stderr: {reset_err.strip()[:300]}")
            return False

        check(
            True,
            f"Flashed reference firmware with nrfjprog fallback: {reference_firmware_path}",
        )
        trimmed_flash_out = flash_out.strip()[:300]
        trimmed_reset_out = reset_out.strip()[:300]
        if trimmed_flash_out:
            print(f"      flash output: {trimmed_flash_out}")
        if trimmed_reset_out:
            print(f"      reset output: {trimmed_reset_out}")
        return True

    print(
        "  Attempting: "
        f"pyOCD API flash {reference_firmware_path} via {probe.uid or 'auto-selected probe'}"
    )
    handle = None
    keep_session_open = False
    try:
        handle = target_control.open_session(
            board=board,
            unique_id=probe.uid or None,
            target=board.pyocd_target,
        )
        request = resolve_flash_request(
            handle,
            explicit_path=reference_firmware_path,
            action_context=action_context("flash_reference_firmware"),
        )
        flashed_path = target_control.flash_firmware(
            handle,
            request.artifact_path,
            halt_after_reset=True,
        )
        check(True, f"Flashed reference firmware: {flashed_path}")
        keep_session_open = True
        return FlashResult(True, session_handle=handle)
    except PolicyRefusal as exc:
        check(False, f"Flash refused for {reference_firmware_path}")
        print(f"      refusal: [{exc.code}] {exc.message}")
        return FlashResult(False)
    except UnsupportedArtifactError as exc:
        check(False, f"Flash failed for {reference_firmware_path}")
        print(f"      stderr: {type(exc).__name__}: {str(exc)[:300]}")
        return FlashResult(False)
    except Exception as exc:  # noqa: BLE001 - surface the raw flash failure first
        check(False, f"Flash failed for {reference_firmware_path}")
        print(f"      stderr: {type(exc).__name__}: {str(exc)[:300]}")
        return FlashResult(True if try_nrfjprog_fallback() else False)
    finally:
        if handle is not None and not keep_session_open:
            try:
                target_control.close_session(handle)
            except Exception:  # noqa: BLE001 - do not hide the flash result
                pass


def read_uart_output(
    board: BoardConfig,
    port: SerialPortInfo | None,
    baudrate: int,
    read_seconds: float,
    expected_text: str | None,
    reset_handle: TargetSessionHandle | None = None,
) -> bool | None:
    header(f"UART output check - {board.display_name}")

    if port is None:
        check(False, "Skipped - COM port not detected")
        return False

    serial_mod, _ = load_pyserial()
    if serial_mod is None:
        check(False, "Skipped - pyserial is not installed")
        return False

    try:
        on_port_open: Callable[[], None] | None = None
        if reset_handle is not None:

            def reset_on_port_open() -> None:
                target_control.reset(reset_handle, halt_after=False)

            on_port_open = reset_on_port_open

        capture = capture_uart_output(
            port.device,
            baudrate,
            read_seconds,
            expected_text,
            on_port_open=on_port_open,
        )
    except Exception as exc:
        check(False, f"Unable to read {port.device} at {baudrate} baud")
        print(f"      stderr: {type(exc).__name__}: {str(exc)[:300]}")
        if expected_text:
            print(f"      Expected: {expected_text}")
        return False

    if expected_text:
        ok = capture.matched
        check(
            ok, f"UART output {'matched' if ok else 'did not match'} expected text on {port.device}"
        )
        if not ok:
            print(f"      Expected: {expected_text}")
            print(f"      Reopen count: {capture.reopen_count}")
            print(f"      Duration: {capture.duration_seconds:.2f}s")
        if capture.excerpt:
            print(f"      Captured: {capture.excerpt}")
        return ok

    ok = capture.has_output
    check(ok, f"UART produced {'some output' if ok else 'no output'} on {port.device}")
    if not ok:
        print(f"      Reopen count: {capture.reopen_count}")
        print(f"      Duration: {capture.duration_seconds:.2f}s")
    if capture.excerpt:
        print(f"      Captured: {capture.excerpt}")
    return ok


def run_recover_test(
    board: BoardConfig,
    probe: ProbeInfo | None,
    target_ok: bool,
    enabled: bool,
) -> RecoverResult | None:
    if not board.requires_recover_validation:
        return None

    header(f"Recover / unlock validation - {board.display_name}")

    if not enabled:
        log(WARN, "Recover test not run - this remains a required manual Stage 0 validation")
        return None
    if probe is None:
        check(False, "Skipped - probe not detected")
        return RecoverResult(False)
    if not target_ok:
        check(False, "Skipped - target pack not installed")
        return RecoverResult(False)
    print("  Running destructive recover command. This may erase flash on the target.")
    print(f"  Attempting: pyOCD API recover via {probe.uid or 'auto-selected probe'}")
    handle = None
    try:
        handle = target_control.open_session(
            board=board,
            unique_id=probe.uid or None,
            target=board.pyocd_target,
        )
        authorize_recover(
            handle,
            confirm=True,
            recover_already_completed=False,
            action_context=action_context("run_recover_test"),
        )
        completed_via = target_control.recover_target(handle)
    except PolicyRefusal as exc:
        check(False, "Recover policy refused this operation")
        print(f"      refusal: [{exc.code}] {exc.message}")
        return RecoverResult(False)
    except Exception as exc:  # noqa: BLE001 - preserve the backend failure
        check(False, "Recover flow failed")
        print(f"      stderr: {type(exc).__name__}: {str(exc)[:300]}")
        return RecoverResult(False)
    finally:
        if handle is not None:
            try:
                target_control.close_session(handle)
            except Exception:  # noqa: BLE001 - do not hide the recover result
                pass

    check(True, f"Recover completed via {completed_via}")
    reconnect_ok = check_connection(board, probe, target_ok)
    post_identity_ok = check_silicon_identity(board, probe, target_ok)
    return RecoverResult(True, reconnect_ok=reconnect_ok, post_identity_ok=post_identity_ok)


def summarise_status(status: str) -> str:
    if status == SUMMARY_PASS:
        return PASS
    if status == SUMMARY_FAIL:
        return FAIL
    return WARN


def print_summary(items: list[SummaryItem], manual_items: list[str]) -> bool:
    header("Summary")

    automated_failures = False
    pending_manual = False
    for item in items:
        symbol = summarise_status(item.status)
        suffix = f" - {item.detail}" if item.detail else ""
        print(f"  [{symbol}] {item.label}{suffix}")
        if item.status == SUMMARY_FAIL:
            automated_failures = True
        if item.status == SUMMARY_MANUAL:
            pending_manual = True

    if manual_items:
        header("Manual validation still required")
        seen = set()
        for item in manual_items:
            if item in seen:
                continue
            seen.add(item)
            print(f"  - {item}")

    print()
    if automated_failures:
        print("  Automated Stage 0 checks failed. Fix the items above and re-run.")
    elif pending_manual or manual_items:
        print(
            "  Automated checks passed, but full Stage 0 is not complete until the manual items are validated."
        )
    else:
        print("  Automated checks passed and this run covered the requested Stage 0 validations.")
    print()

    return not automated_failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 0 board validation")
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
        help="Board id to run. Repeat to select multiple boards. Defaults to all non-example board configs in the board-config-dir plus any extra board-config files.",
    )
    parser.add_argument(
        "--install-packs",
        action="store_true",
        help="Automatically provision missing pinned CMSIS-Packs from packs/manifest.yaml",
    )
    parser.add_argument(
        "--port",
        action="append",
        default=[],
        metavar="BOARD_ID=PORT",
        help="Override the detected virtual COM port for a board.",
    )
    parser.add_argument(
        "--reference-firmware",
        action="append",
        default=[],
        metavar="BOARD_ID=PATH",
        help="Reference firmware path for a board.",
    )
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="BOARD_ID=TEXT",
        help="Substring expected in the UART output for a board.",
    )
    parser.add_argument(
        "--baudrate",
        action="append",
        default=[],
        metavar="BOARD_ID=BAUD",
        help="Override the UART baud rate for a board.",
    )
    parser.add_argument(
        "--recover-test",
        action="append",
        default=[],
        metavar="BOARD_ID",
        help="Run the destructive recover validation for the given board_id. Repeat for multiple boards.",
    )
    parser.add_argument(
        "--confirm-shared-usb",
        action="append",
        default=[],
        metavar="BOARD_ID",
        help="Record that a human confirmed the visible debug probe and COM port come from the same physical board.",
    )
    parser.add_argument(
        "--serial-read-seconds",
        type=float,
        default=3.0,
        help="How long to listen for UART output after opening the port.",
    )
    return parser


def collect_overrides(
    args: argparse.Namespace,
) -> tuple[dict[str, str], dict[str, Path], dict[str, str], dict[str, int], set[str], set[str]]:
    port_overrides = parse_key_value(args.port, "--port")
    reference_firmware_overrides = {
        board_id: resolve_required_firmware_path(path_text)
        for board_id, path_text in parse_key_value(
            args.reference_firmware, "--reference-firmware"
        ).items()
    }
    expect_overrides = parse_key_value(args.expect, "--expect")
    baudrate_overrides = {
        board_id: parse_int(value, "--baudrate")
        for board_id, value in parse_key_value(args.baudrate, "--baudrate").items()
    }
    recover_tests: set[str] = {
        board_id.strip().lower() for board_id in args.recover_test if board_id.strip()
    }
    confirmed_shared_usb: set[str] = {
        board_id.strip().lower() for board_id in args.confirm_shared_usb if board_id.strip()
    }

    return (
        port_overrides,
        reference_firmware_overrides,
        expect_overrides,
        baudrate_overrides,
        recover_tests,
        confirmed_shared_usb,
    )


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        board_config_dir = Path(args.board_config_dir).expanduser().resolve()
        custom_board_paths = [
            Path(raw_path).expanduser().resolve() for raw_path in args.board_config
        ]
        boards_to_check = load_selected_board_configs(
            board_config_dir,
            extra_paths=custom_board_paths,
            requested_ids=args.board_id,
        )
        if not boards_to_check:
            raise ConfigError(
                "No boards selected. Add board configs or pass --board-id for an existing board."
            )
        (
            port_overrides,
            reference_firmware_overrides,
            expect_overrides,
            baudrate_overrides,
            recover_tests,
            confirmed_shared_usb,
        ) = collect_overrides(args)
    except ConfigError as exc:
        parser.error(str(exc))
        return

    print("\nStage 0 - Board and toolchain validation")
    print(f"Checking: {', '.join(board.display_name for board in boards_to_check)}")

    pyocd_ok = check_pyocd_installed()
    pyserial_ok = check_pyserial_installed()
    if not pyocd_ok:
        sys.exit(1)

    probes = check_probes(boards_to_check)
    target_ok = check_target_packs(boards_to_check, auto_install=args.install_packs)
    serial_ports = check_virtual_com_ports(boards_to_check, probes, port_overrides, pyserial_ok)

    summary_items: list[SummaryItem] = []
    manual_items: list[str] = []

    for board in boards_to_check:
        probe = probes.get(board.board_id)
        target_available = target_ok.get(board.board_id, False)
        port = serial_ports.get(board.board_id)
        reference_firmware_path = reference_firmware_overrides.get(board.board_id)
        expected_text = expect_overrides.get(board.board_id, board.expected_uart_substring)
        baudrate = baudrate_overrides.get(board.board_id, board.default_baudrate)

        conn_ok = check_connection(board, probe, target_available)
        identity_ok = check_silicon_identity(board, probe, target_available)
        flash_result = flash_reference_firmware(
            board,
            probe,
            target_available,
            identity_ok,
            reference_firmware_path,
        )
        flash_ok = flash_result.status
        uart_ok = None
        try:
            if flash_ok is True:
                uart_ok = read_uart_output(
                    board,
                    port,
                    baudrate,
                    args.serial_read_seconds,
                    expected_text,
                    reset_handle=flash_result.session_handle,
                )
        finally:
            if flash_result.session_handle is not None:
                try:
                    target_control.close_session(flash_result.session_handle)
                except Exception:  # noqa: BLE001 - preserve the Stage 0 result
                    pass
        recover_ok = run_recover_test(
            board,
            probe,
            target_available,
            board.board_id in recover_tests,
        )
        if recover_ok is not None and recover_ok.completed:
            if recover_ok.reconnect_ok is not None:
                conn_ok = recover_ok.reconnect_ok
            if recover_ok.post_identity_ok is not None:
                identity_ok = recover_ok.post_identity_ok

        summary_items.extend(
            [
                SummaryItem(
                    f"{board.display_name}: probe visible", SUMMARY_PASS if probe else SUMMARY_FAIL
                ),
                SummaryItem(
                    f"{board.display_name}: target '{board.pyocd_target}' available",
                    SUMMARY_PASS if target_available else SUMMARY_FAIL,
                ),
                SummaryItem(
                    f"{board.display_name}: connect + read register",
                    SUMMARY_PASS if conn_ok else SUMMARY_FAIL,
                ),
            ]
        )

        if board.silicon_id_expected is not None:
            summary_items.append(
                SummaryItem(
                    f"{board.display_name}: exact silicon identity matches board config",
                    SUMMARY_PASS if identity_ok else SUMMARY_FAIL,
                )
            )

        summary_items.extend(
            [
                SummaryItem(
                    f"{board.display_name}: virtual COM port visible",
                    SUMMARY_PASS if port else SUMMARY_FAIL,
                ),
            ]
        )

        if flash_ok is None:
            summary_items.append(
                SummaryItem(
                    f"{board.display_name}: flash known-good reference firmware",
                    SUMMARY_MANUAL,
                    f"provide --reference-firmware {board_cli_label(board)}=PATH from the host filesystem",
                )
            )
            manual_items.append(
                f"{board.display_name}: flash a known-good reference image and verify it is the intended baseline artifact."
            )
        else:
            summary_items.append(
                SummaryItem(
                    f"{board.display_name}: flash known-good reference firmware",
                    SUMMARY_PASS if flash_ok else SUMMARY_FAIL,
                )
            )

        if reference_firmware_path is None:
            summary_items.append(
                SummaryItem(
                    f"{board.display_name}: UART output observed",
                    SUMMARY_MANUAL,
                    f"requires flashed reference firmware and optional --expect {board_cli_label(board)}=TEXT",
                )
            )
            manual_items.append(
                f"{board.display_name}: confirm the reference firmware prints over UART on the expected port and baudrate."
            )
        elif flash_ok is False:
            summary_items.append(
                SummaryItem(
                    f"{board.display_name}: UART output observed",
                    SUMMARY_FAIL,
                )
            )
        else:
            summary_items.append(
                SummaryItem(
                    f"{board.display_name}: UART output observed",
                    SUMMARY_PASS if uart_ok else SUMMARY_FAIL,
                )
            )
            if expected_text is None:
                manual_items.append(
                    f"{board.display_name}: confirm the captured UART text is the expected reference-firmware output, not just arbitrary bytes."
                )

        if board.requires_recover_validation:
            if recover_ok is None:
                summary_items.append(
                    SummaryItem(
                        f"{board.display_name}: recover / unlock cycle proven",
                        SUMMARY_MANUAL,
                        f"run with --recover-test {board_cli_label(board)} to automate the destructive check",
                    )
                )
                manual_items.append(
                    f"{board.display_name}: prove a recover/unlock cycle on this machine. This may erase flash, so it is opt-in."
                )
            else:
                summary_items.append(
                    SummaryItem(
                        f"{board.display_name}: recover / unlock cycle proven",
                        SUMMARY_PASS if recover_ok.completed else SUMMARY_FAIL,
                    )
                )

        if board.board_id not in confirmed_shared_usb:
            manual_items.append(
                f"{board.display_name}: confirm the visible debug probe and COM port are exposed by the same physical USB connection."
            )

    automated_ok = print_summary(summary_items, manual_items)
    sys.exit(0 if automated_ok else 1)


if __name__ == "__main__":
    main()
