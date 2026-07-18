"""pyOCD-backed SWD adapter implementation."""

from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from pyocd_debug_mcp.kernel.processes import run_owned
from typing import Any, BinaryIO, TextIO, cast

from pyocd.core.exceptions import TransferError  # type: ignore[import-untyped]
from pyocd.core.helpers import ConnectHelper  # type: ignore[import-untyped]
from pyocd.flash.eraser import FlashEraser  # type: ignore[import-untyped]
from pyocd.flash.file_programmer import FileProgrammer  # type: ignore[import-untyped]

from pyocd_debug_mcp.adapters.target_backend import (
    BackendProbe,
    MemoryAccessCapabilities,
    RegisterClass,
    RegisterDescriptor,
    TargetBackend,
    TargetSessionDescription,
    TargetSessionHandle,
)
from pyocd_debug_mcp.artifact_formats import FirmwareFormat, detect_firmware_format
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.pack_provision import discover_local_packs
from pyocd_debug_mcp.probe_inventory import list_connected_probes
from pyocd_debug_mcp.target_errors import (
    LockedTargetError,
    ProbeNotFoundError,
    ResetLineUnavailableError,
    TargetConnectionError,
    UnsupportedArtifactError,
)
from pyocd_debug_mcp.timeouts import (
    DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
    ServerTimeoutConfig,
    default_server_timeout_config,
    subprocess_timeout_stream_text,
)

ROUTE_PYOCD_NATIVE = "pyocd-native"

_ARM_ORDINARY_REGISTER = re.compile(r"(?:r(?:[0-9]|1[0-2])|[sdq][0-9]+)", re.IGNORECASE)
_ARM_EXECUTION_REGISTERS = frozenset(
    {
        "r13",
        "r14",
        "r15",
        "sp",
        "lr",
        "pc",
        "xpsr",
        "apsr",
        "ipsr",
        "epsr",
        "msp",
        "psp",
        "msplim",
        "psplim",
        "primask",
        "basepri",
        "basepri_max",
        "faultmask",
        "control",
        "cfbp",
        "fpscr",
    }
)


def _run_cmd(
    cmd: list[str],
    timeout_seconds: float = DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    try:
        result = run_owned(
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


@contextlib.contextmanager
def _quiet_backend_streams() -> Iterator[None]:
    """Keep backend chatter off the MCP stdio transport.

    On Windows, the pyOCD J-Link path can misbehave when the process stdout/stderr
    are pipe-backed handles, which is exactly how MCP stdio launches the server.
    Swapping the process-level descriptors to temp files during backend calls
    avoids both protocol corruption and the attach hang/failure seen under stdio.
    """

    redirected: list[tuple[TextIO, int]] = []
    temp_files: list[BinaryIO] = []
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
                stream_fd = stream.fileno()
                saved_fd = os.dup(stream_fd)
                temp_file = tempfile.TemporaryFile(mode="w+b")
                os.dup2(temp_file.fileno(), stream_fd)
            except (AttributeError, io.UnsupportedOperation, OSError):
                continue
            redirected.append((stream, saved_fd))
            temp_files.append(cast(BinaryIO, temp_file))

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            yield
    finally:
        for stream, saved_fd in reversed(redirected):
            try:
                stream.flush()
            except Exception:
                pass
            try:
                os.dup2(saved_fd, stream.fileno())
            finally:
                os.close(saved_fd)
        for opened_file in temp_files:
            opened_file.close()


def _typed_backend_error(exc: Exception) -> TargetConnectionError:
    if isinstance(exc, KeyError) and exc.args == (1,):
        return TargetConnectionError(
            "pyOCD target initialization could not reach expected access port AP#1. "
            "Possible causes include target lock, reset or attach state, probe connectivity, "
            "or an incompatible target selection. Follow the exact setup or validation remedy; "
            "use typed target recovery only when the server identifies it."
        )
    message = f"{type(exc).__name__}: {exc}"
    lowered = message.lower()
    if any(
        term in lowered for term in ("approtect", "access port", "locked target", "device locked")
    ):
        return LockedTargetError(message)
    return TargetConnectionError(message)


def _looks_like_jlink_serial_open_failure(exc: Exception) -> bool:
    lowered = f"{type(exc).__name__}: {exc}".lower()
    return "no emulator with serial number" in lowered


def _single_matching_probe_visible_for_board_family(board: BoardConfig) -> bool:
    probes = list_connected_probes(_run_cmd)
    matching = [probe for probe in probes if probe.family == board.probe_family]
    return len(matching) == 1


def _should_retry_without_uid(
    board: BoardConfig | None,
    unique_id: str | None,
    exc: Exception,
) -> bool:
    if not unique_id or board is None:
        return False
    if board.probe_family != "jlink":
        return False
    if not _looks_like_jlink_serial_open_failure(exc):
        return False
    return _single_matching_probe_visible_for_board_family(board)


def build_session_options(
    board: BoardConfig | None,
    target: str | None,
    server_timeouts: ServerTimeoutConfig | None = None,
) -> dict[str, object] | None:
    """Build pyOCD session options from shared board facts."""

    options: dict[str, object] = {}
    if target:
        options["target_override"] = target
    options.update((server_timeouts or default_server_timeout_config()).pyocd_options())
    if board and board.probe_family == "jlink":
        # Match the Stage 0/J-Link open-by-serial workaround proven on hardware.
        options["jlink.non_interactive"] = False
    if board and board.debug_connect_mode:
        options["connect_mode"] = board.debug_connect_mode
    if board and board.debug_clock_hz:
        options["frequency"] = board.debug_clock_hz
    return options or None


class PyOCDSWDInterface(TargetBackend):
    """Single native pyOCD route used during the early shared-service phase."""

    backend_name = "pyocd"

    def discover_targets(self) -> tuple[str, ...]:
        from pyocd.target.builtin import BUILTIN_TARGETS

        return tuple(sorted(str(name).casefold() for name in BUILTIN_TARGETS))

    def discover_probes(self) -> tuple[BackendProbe, ...]:
        return tuple(
            BackendProbe(probe.uid, probe.description or probe.raw, probe.family)
            for probe in list_connected_probes(_run_cmd)
        )

    def build_session_options(
        self,
        board: BoardConfig | None,
        target: str | None,
        server_timeouts: ServerTimeoutConfig | None = None,
    ) -> dict[str, object] | None:
        return build_session_options(board, target, server_timeouts)

    @staticmethod
    def _choose_session(
        *,
        probe_uid: str | None,
        options: dict[str, object] | None,
    ) -> Any:
        return ConnectHelper.session_with_chosen_probe(
            blocking=False,
            return_first=True,
            unique_id=probe_uid,
            auto_open=False,
            options=options,
        )

    @staticmethod
    def _close_quietly(session: object) -> None:
        try:
            close = getattr(session, "close", None)
            if callable(close):
                with _quiet_backend_streams():
                    close()
        except Exception:  # noqa: BLE001 - do not hide the original open failure
            pass

    def open(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        server_timeouts: ServerTimeoutConfig | None = None,
        connect_mode: str | None = None,
    ) -> TargetSessionHandle:
        probe_uid = unique_id or os.environ.get("PYOCD_PROBE_UID") or None
        target_override = (
            target
            or (board.target_identity if board else None)
            or os.environ.get("PYOCD_TARGET")
            or None
        )
        options = build_session_options(board, target_override, server_timeouts)
        if connect_mode is not None:
            if connect_mode not in {"attach", "halt", "pre-reset", "under-reset"}:
                raise ValueError(f"Unsupported pyOCD connect mode: {connect_mode}")
            options = dict(options or {})
            options["connect_mode"] = connect_mode
        # Load any locally-provisioned CMSIS-Packs (pinned + sha256-verified) so the
        # exact target resolves without depending on the live pyOCD pack index. This
        # is a runtime/filesystem concern, kept out of the pure build_session_options.
        local_packs = discover_local_packs()
        if local_packs:
            options = dict(options or {})
            options["pack"] = [str(p) for p in local_packs]
        session = self._choose_session(probe_uid=probe_uid, options=options)
        if session is None:
            raise ProbeNotFoundError("No matching debug probe found.")

        try:
            with _quiet_backend_streams():
                session.open()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            self._close_quietly(session)
            if _should_retry_without_uid(board, probe_uid, exc):
                retry_session = self._choose_session(probe_uid=None, options=options)
                if retry_session is None:
                    raise ProbeNotFoundError("No matching debug probe found.") from exc
                try:
                    with _quiet_backend_streams():
                        retry_session.open()
                except Exception as retry_exc:  # noqa: BLE001 - preserve backend context
                    self._close_quietly(retry_session)
                    raise _typed_backend_error(retry_exc) from retry_exc
                session = retry_session
            else:
                raise _typed_backend_error(exc) from exc
        return TargetSessionHandle(
            session=session,
            board=board,
            probe_uid=session.probe.unique_id or probe_uid,
            route_used=ROUTE_PYOCD_NATIVE,
            target_override=target_override,
        )

    def close(self, handle: TargetSessionHandle) -> None:
        with _quiet_backend_streams():
            handle.session.close()

    def connect_under_reset(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        server_timeouts: ServerTimeoutConfig | None = None,
    ) -> TargetSessionHandle:
        probe_uid = unique_id or os.environ.get("PYOCD_PROBE_UID") or None
        target_override = (
            target
            or (board.target_identity if board else None)
            or os.environ.get("PYOCD_TARGET")
            or None
        )
        options = dict(build_session_options(board, target_override, server_timeouts) or {})
        options["connect_mode"] = "under-reset"
        local_packs = discover_local_packs()
        if local_packs:
            options["pack"] = [str(path) for path in local_packs]
        session = self._choose_session(probe_uid=probe_uid, options=options)
        if session is None:
            raise ProbeNotFoundError("No matching debug probe found.")
        assert_reset = getattr(session.probe, "assert_reset", None)
        if not callable(assert_reset):
            self._close_quietly(session)
            raise ResetLineUnavailableError(
                "The selected probe does not expose wired reset-line control; "
                "connect_under_reset cannot degrade to an ordinary attach."
            )
        try:
            with _quiet_backend_streams():
                # pyOCD's under-reset init sequence owns assertion, reset catch,
                # halt, and release. Calling assert_reset() before Session.open()
                # is invalid for probes that have not been opened yet and would
                # also duplicate pyOCD's reset sequence. Some real targets start
                # running again as nRESET is released, so explicitly halt once
                # more after open and verify the observable postcondition before
                # returning. The bounded retry covers probes where the first halt
                # command races reset release.
                session.open()
                halt_deadline = time.monotonic() + 0.5
                while True:
                    session.target.halt()
                    state = str(session.target.get_state().name).casefold()
                    if state == "halted":
                        break
                    if time.monotonic() >= halt_deadline:
                        raise TargetConnectionError(
                            "connect_under_reset released reset but could not leave "
                            "the target halted"
                        )
                    time.sleep(0.01)
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            # If initialisation failed after pyOCD asserted nRESET, release it
            # best-effort before closing the partially opened session.
            try:
                with _quiet_backend_streams():
                    assert_reset(False)
            except Exception:
                pass
            self._close_quietly(session)
            if isinstance(exc, ResetLineUnavailableError):
                raise
            if isinstance(exc, NotImplementedError):
                raise ResetLineUnavailableError(
                    "The selected probe does not support wired reset-line control; "
                    "connect_under_reset cannot degrade to an ordinary attach."
                ) from exc
            raise _typed_backend_error(exc) from exc
        return TargetSessionHandle(
            session=session,
            board=board,
            probe_uid=session.probe.unique_id or probe_uid,
            route_used=ROUTE_PYOCD_NATIVE,
            target_override=target_override,
        )

    def get_state(self, handle: TargetSessionHandle) -> str:
        try:
            with _quiet_backend_streams():
                return cast(str, handle.session.target.get_state().name)
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def describe_session(self, handle: TargetSessionHandle) -> TargetSessionDescription:
        board = getattr(handle.session, "board", None)
        target = getattr(handle.session, "target", None)
        probe = getattr(handle.session, "probe", None)
        return TargetSessionDescription(
            board_name=str(getattr(board, "name", "") or "<unknown>"),
            live_target_part=str(getattr(target, "part_number", "") or "").strip(),
            probe_description=str(getattr(probe, "description", "") or "").strip(),
        )

    def read_memory(self, handle: TargetSessionHandle, address: int, width_bits: int) -> int:
        try:
            with _quiet_backend_streams():
                return cast(int, handle.session.target.read_memory(address, width_bits))
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def read_memory_block(
        self, handle: TargetSessionHandle, address: int, length: int
    ) -> list[int]:
        try:
            with _quiet_backend_streams():
                return list(
                    cast(list[int], handle.session.target.read_memory_block8(address, length))
                )
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def write_memory(
        self,
        handle: TargetSessionHandle,
        address: int,
        value: int,
        width_bits: int,
    ) -> None:
        try:
            with _quiet_backend_streams():
                handle.session.target.write_memory(address, value, width_bits)
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def memory_access_capabilities(
        self, handle: TargetSessionHandle
    ) -> MemoryAccessCapabilities:
        del handle
        return MemoryAccessCapabilities(
            read_width_bits=(8, 16, 32),
            write_width_bits=(8, 16, 32),
            address_bits=32,
            peripheral_width_bits=32,
            peripheral_alignment_bytes=4,
        )

    def read_core_register(self, handle: TargetSessionHandle, name: str) -> int:
        try:
            with _quiet_backend_streams():
                return cast(int, handle.session.target.read_core_register(name))
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def write_core_register(self, handle: TargetSessionHandle, name: str, value: int) -> None:
        try:
            with _quiet_backend_streams():
                handle.session.target.write_core_register(name, value)
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def supported_core_registers(self, handle: TargetSessionHandle) -> tuple[str, ...]:
        try:
            registers = handle.session.target.core_registers.by_name
            return tuple(sorted(str(name).casefold() for name in registers))
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def describe_core_register(
        self, handle: TargetSessionHandle, name: str
    ) -> RegisterDescriptor | None:
        normalized = name.strip().casefold()
        supported = {item.casefold() for item in self.supported_core_registers(handle)}
        if normalized not in supported:
            return None
        if normalized in _ARM_EXECUTION_REGISTERS:
            return RegisterDescriptor(normalized, RegisterClass.EXECUTION_STATE, 32)
        if _ARM_ORDINARY_REGISTER.fullmatch(normalized) is None:
            return None
        if normalized.startswith("q"):
            width_bits = 128
        elif normalized.startswith("d"):
            width_bits = 64
        else:
            width_bits = 32
        return RegisterDescriptor(normalized, RegisterClass.ORDINARY, width_bits)

    def halt(self, handle: TargetSessionHandle) -> None:
        try:
            with _quiet_backend_streams():
                handle.session.target.halt()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def resume(self, handle: TargetSessionHandle) -> None:
        try:
            with _quiet_backend_streams():
                handle.session.target.resume()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def step(self, handle: TargetSessionHandle) -> int:
        try:
            with _quiet_backend_streams():
                handle.session.target.step()
                return cast(int, handle.session.target.read_core_register("pc"))
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def reset(self, handle: TargetSessionHandle) -> None:
        try:
            with _quiet_backend_streams():
                handle.session.target.reset()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def reset_and_halt(self, handle: TargetSessionHandle) -> None:
        try:
            with _quiet_backend_streams():
                handle.session.target.reset_and_halt()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def release_reset(self, handle: TargetSessionHandle) -> None:
        try:
            probe = getattr(handle.session, "probe", None)
            assert_reset = getattr(probe, "assert_reset", None)
            if callable(assert_reset):
                assert_reset(False)
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def flash(
        self,
        handle: TargetSessionHandle,
        firmware: Path,
        *,
        halt_after_reset: bool,
    ) -> None:
        artifact_format = detect_firmware_format(firmware)
        if artifact_format not in {FirmwareFormat.ELF, FirmwareFormat.INTEL_HEX}:
            raise UnsupportedArtifactError(
                "Unsupported artifact bytes; pyOCD requires a self-addressing ELF or Intel HEX "
                "image. Raw caller-defined load addresses are forbidden."
            )

        target = handle.session.target
        # Match `pyocd load`'s proven pre-reset sequence. On STM32/ST-Link, skipping
        # this can make the Python API flash path fail even though the CLI succeeds.
        try:
            with _quiet_backend_streams():
                target.reset_and_halt()
                # M7 containment pre-computes and validates every implied erase sector.
                # Force pyOCD to honor that sector scope even when a host/session option
                # would otherwise select auto or whole-chip erase.
                FileProgrammer(handle.session, chip_erase="sector").program(
                    str(firmware), file_format=artifact_format.value
                )
                if halt_after_reset:
                    target.reset_and_halt()
                else:
                    target.reset()
        except TransferError as exc:
            # `pyocd load` tolerates a transient transfer drop during the final reset.
            if halt_after_reset:
                raise _typed_backend_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def recover(self, handle: TargetSessionHandle) -> None:
        try:
            with _quiet_backend_streams():
                FlashEraser(handle.session, FlashEraser.Mode.MASS).erase()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def supports_recovery(self, handle: TargetSessionHandle, mechanism: str) -> bool:
        return (
            mechanism == "backend_mass_erase"
            and getattr(handle.session, "target", None) is not None
        )

    def breakpoint_memory_span_bytes(
        self, handle: TargetSessionHandle, address: int
    ) -> int:
        """Conservatively cover the widest instruction pyOCD may patch on Arm targets."""

        del handle, address
        return 4

    def set_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        try:
            with _quiet_backend_streams():
                handle.session.target.set_breakpoint(address)
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def remove_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        try:
            with _quiet_backend_streams():
                handle.session.target.remove_breakpoint(address)
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc



