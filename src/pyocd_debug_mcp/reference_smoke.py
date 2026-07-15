"""Canonical Stage 1 smoke verifier for the shared SWD + UART substrate."""

from __future__ import annotations

import argparse
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pyocd_debug_mcp.board_config import (
    DEFAULT_BOARD_CONFIG_DIR,
    BoardConfig,
    load_selected_board_configs,
)
from pyocd_debug_mcp.local_env import load_local_env
from pyocd_debug_mcp.probe_inventory import resolve_probe_for_board
from pyocd_debug_mcp.reference_artifacts import resolve_reference_artifacts
from pyocd_debug_mcp.serial_resolver import (
    SerialPortInfo,
    is_interactive_terminal,
    list_serial_ports,
    resolve_serial_port,
)
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.services.symbols import ResolvedSymbol, read_symbol_u32
from pyocd_debug_mcp.services.uart_capture import capture_uart_output
from pyocd_debug_mcp.timeouts import (
    DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
    subprocess_timeout_stream_text,
)

PASS = "PASS"
FAIL = "FAIL"
INFO = "INFO"
KNOWN_SYMBOL_NAME = "stage1_known_value"
KNOWN_SYMBOL_VALUE = 0x1234ABCD


@dataclass(frozen=True)
class ProbeHint:
    uid: str


@dataclass
class BoardPortView:
    board_id: str
    display_name: str
    mcu_family: str
    probe_family: str
    serial_hint_terms: tuple[str, ...]


@dataclass
class ProbePortView:
    uid: str


@dataclass(frozen=True)
class Stage1SmokeResult:
    board: BoardConfig
    probe_uid: str
    route_used: str
    flash_artifact: Path
    symbol_artifact: Path
    serial_port: SerialPortInfo
    baudrate: int
    expected_text: str
    pc: int
    resolved_symbol: ResolvedSymbol
    capture_text: str
    capture_excerpt: str
    capture_reopen_count: int
    capture_duration_seconds: float


def log(status: str, message: str) -> None:
    print(f"  [{status}] {message}")


def header(text: str) -> None:
    print(f"\n{'=' * 60}\n  {text}\n{'=' * 60}")


def run_cmd(
    cmd: list[str],
    timeout_seconds: float = DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
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


def load_board(board_id: str) -> BoardConfig:
    boards = load_selected_board_configs(DEFAULT_BOARD_CONFIG_DIR, requested_ids=[board_id])
    if not boards:
        raise RuntimeError(f"Board not found: {board_id}")
    return boards[0]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-id", required=True, help="Tracked board id to validate.")
    parser.add_argument("--probe-uid", help="Probe unique id override.")
    parser.add_argument("--port", help="Serial port override for the board.")
    parser.add_argument("--flash-artifact", help="Override the flash artifact path.")
    parser.add_argument("--elf", help="Override the symbol-bearing ELF path.")
    parser.add_argument("--baudrate", type=int, help="UART baudrate override.")
    parser.add_argument(
        "--serial-read-seconds",
        type=float,
        default=3.0,
        help="How long to wait for UART output after reset.",
    )
    return parser


def resolve_port(
    board: BoardConfig,
    *,
    probe_uid: str | None,
    override: str | None,
) -> SerialPortInfo:
    ports = list_serial_ports()
    if ports is None:
        raise RuntimeError("pyserial is not installed")
    if not ports:
        raise RuntimeError("No serial ports detected")

    allow_single_fallback = len(ports) == 1
    board_view = BoardPortView(
        board_id=board.board_id,
        display_name=board.display_name,
        mcu_family=board.mcu_family,
        probe_family=board.probe_family,
        serial_hint_terms=board.serial_hint_terms,
    )
    probe = ProbePortView(probe_uid) if probe_uid else None
    resolution = resolve_serial_port(
        board=board_view,
        ports=ports,
        probe=probe,
        override=override,
        allow_single_fallback=allow_single_fallback,
        run_cmd=run_cmd,
        interactive=is_interactive_terminal(),
    )
    if resolution.port is None:
        raise RuntimeError(f"Serial port resolution failed: {resolution.note}")
    return resolution.port


def run_stage1_smoke(
    *,
    board_id: str,
    probe_uid: str | None = None,
    port: str | None = None,
    flash_artifact: str | Path | None = None,
    elf: str | Path | None = None,
    baudrate: int | None = None,
    serial_read_seconds: float = 3.0,
) -> Stage1SmokeResult:
    load_local_env()
    board = load_board(board_id.strip().lower())
    resolved_probe_uid = probe_uid or os.environ.get("PYOCD_PROBE_UID") or None
    if resolved_probe_uid is None:
        resolution = resolve_probe_for_board(
            board,
            run_cmd=run_cmd,
            allow_single_fallback=True,
        )
        if resolution.probe is None:
            raise RuntimeError(
                f"Probe resolution failed for {board.display_name}: {resolution.note}"
            )
        if not resolution.probe.uid:
            raise RuntimeError(
                f"Probe resolution for {board.display_name} did not yield a unique id. "
                "Rerun with --probe-uid."
            )
        resolved_probe_uid = resolution.probe.uid

    resolved_baudrate = baudrate or board.default_baudrate
    artifact_pair = resolve_reference_artifacts(
        board,
        flash_artifact=flash_artifact,
        elf_path=elf,
    )
    serial_port = resolve_port(board, probe_uid=resolved_probe_uid, override=port)
    expected_text = board.expected_uart_substring or "boot ok"
    handle = None
    try:
        handle = target_control.open_session(
            board=board,
            unique_id=resolved_probe_uid,
            target=board.pyocd_target,
        )
        target_control.flash_firmware(
            handle,
            artifact_pair.flash_artifact,
            halt_after_reset=True,
        )
        pc = target_control.read_core_register(handle, "pc")
        resolved_symbol = read_symbol_u32(
            handle,
            artifact_pair.symbol_artifact,
            KNOWN_SYMBOL_NAME,
        )
        if resolved_symbol.value_u32 != KNOWN_SYMBOL_VALUE:
            raise RuntimeError(
                f"{resolved_symbol.name} value mismatch: actual=0x{resolved_symbol.value_u32:08X} "
                f"expected=0x{KNOWN_SYMBOL_VALUE:08X}"
            )

        def on_port_open() -> None:
            target_control.reset(handle, halt_after=False)

        capture = capture_uart_output(
            serial_port.device,
            resolved_baudrate,
            serial_read_seconds,
            expected_text,
            on_port_open=on_port_open,
        )
        if not capture.matched:
            raise RuntimeError(
                f"UART output did not contain '{expected_text}'. Captured: {capture.excerpt or '(none)'}"
            )
        return Stage1SmokeResult(
            board=board,
            probe_uid=resolved_probe_uid,
            route_used=handle.route_used,
            flash_artifact=artifact_pair.flash_artifact,
            symbol_artifact=artifact_pair.symbol_artifact,
            serial_port=serial_port,
            baudrate=resolved_baudrate,
            expected_text=expected_text,
            pc=pc,
            resolved_symbol=resolved_symbol,
            capture_text=capture.text,
            capture_excerpt=capture.excerpt,
            capture_reopen_count=capture.reopen_count,
            capture_duration_seconds=capture.duration_seconds,
        )
    finally:
        if handle is not None:
            try:
                target_control.close_session(handle)
            except Exception:  # noqa: BLE001 - verification result already determined
                pass


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    requested_board_id = args.board_id.strip().lower()
    try:
        result = run_stage1_smoke(
            board_id=requested_board_id,
            probe_uid=args.probe_uid,
            port=args.port,
            flash_artifact=args.flash_artifact,
            elf=args.elf,
            baudrate=args.baudrate,
            serial_read_seconds=args.serial_read_seconds,
        )

        print(f"\nStage 1 smoke harness — {result.board.display_name} ({result.board.board_id})")
        print(f"flash artifact: {result.flash_artifact}")
        print(f"symbol artifact: {result.symbol_artifact}")
        print(f"serial port: {result.serial_port.device}")

        header("Flash and control")
        log(INFO, f"Opened session via {result.route_used} on {result.probe_uid}")
        log(PASS, f"Flashed {result.flash_artifact.name}")
        log(PASS, f"Read pc=0x{result.pc:08X}")
        log(
            PASS,
            "Resolved "
            f"{result.resolved_symbol.name} @0x{result.resolved_symbol.address:08X} "
            f"size={result.resolved_symbol.size} type={result.resolved_symbol.type}",
        )
        log(PASS, f"Read {result.resolved_symbol.name}=0x{result.resolved_symbol.value_u32:08X}")

        header("UART")
        log(PASS, "Reset and resumed target")
        log(PASS, f"UART matched '{result.expected_text}'")
        if result.capture_excerpt:
            print(f"      Captured: {result.capture_excerpt}")
    except Exception as exc:  # noqa: BLE001 - surface the concrete harness failure
        header("Summary")
        log(FAIL, f"{type(exc).__name__}: {exc}")
        flash_label = (
            str(Path(args.flash_artifact).expanduser().resolve())
            if args.flash_artifact
            else "(unresolved)"
        )
        symbol_label = str(Path(args.elf).expanduser().resolve()) if args.elf else "(unresolved)"
        serial_label = args.port or "(unresolved)"
        print(f"      board_id: {requested_board_id}")
        print(f"      flash_artifact: {flash_label}")
        print(f"      symbol_artifact: {symbol_label}")
        print(f"      serial_port: {serial_label}")
        print(f"      baudrate: {args.baudrate if args.baudrate is not None else '(unresolved)'}")
        print("      expected_text: (unresolved)")
        print("      route_used: (unavailable)")
        print("      reopen_count: (unavailable)")
        print("      capture_duration: (unavailable)")
        print("      excerpt: (unavailable)")
        return 1

    header("Summary")
    log(PASS, "Stage 1 smoke harness passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
