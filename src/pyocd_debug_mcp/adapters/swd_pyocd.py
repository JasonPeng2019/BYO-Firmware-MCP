"""pyOCD-backed SWD adapter implementation."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO, TextIO, cast

from pyocd.core.exceptions import TransferError  # type: ignore[import-untyped]
from pyocd.core.helpers import ConnectHelper  # type: ignore[import-untyped]
from pyocd.flash.eraser import FlashEraser  # type: ignore[import-untyped]
from pyocd.flash.file_programmer import FileProgrammer  # type: ignore[import-untyped]

from pyocd_debug_mcp.adapters.swd_interface import SWDInterface, TargetSessionHandle
from pyocd_debug_mcp.board_config import BoardConfig, PROBE_FAMILY_HINTS
from pyocd_debug_mcp.pack_provision import discover_local_packs
from pyocd_debug_mcp.probe_inventory import list_connected_probes
from pyocd_debug_mcp.target_errors import (
    LockedTargetError,
    ProbeNotFoundError,
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
SUPPORTED_FLASH_SUFFIXES = frozenset({".elf", ".hex"})


def _run_cmd(
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
            "pyOCD target init failed while probing AP#1. "
            "On nRF52 targets this usually means the debug path is not fully reachable "
            "(for example: stale probe state, target lock/recover needed, or a transient "
            "J-Link attach failure)."
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
    family_terms = PROBE_FAMILY_HINTS.get(board.probe_family, set())
    if not family_terms:
        return False
    matching = [
        probe for probe in probes if any(term in probe.searchable_text for term in family_terms)
    ]
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
    if board and board.board_id == "nucleo_l476rg":
        # Current STM32 bench truth on this host requires connecting under reset
        # at a lower SWD clock to avoid STLink "DP wait" attach failures.
        options["connect_mode"] = "under-reset"
        options["frequency"] = 1_000_000
    return options or None


class PyOCDSWDInterface(SWDInterface):
    """Single native pyOCD route used during the early shared-service phase."""

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
    ) -> TargetSessionHandle:
        probe_uid = unique_id or os.environ.get("PYOCD_PROBE_UID") or None
        target_override = (
            target
            or (board.pyocd_target if board else None)
            or os.environ.get("PYOCD_TARGET")
            or None
        )
        options = build_session_options(board, target_override, server_timeouts)
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

    def get_state(self, handle: TargetSessionHandle) -> str:
        try:
            with _quiet_backend_streams():
                return cast(str, handle.session.target.get_state().name)
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

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

    def step(self, handle: TargetSessionHandle) -> None:
        try:
            with _quiet_backend_streams():
                handle.session.target.step()
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

    def flash(
        self,
        handle: TargetSessionHandle,
        firmware: Path,
        *,
        halt_after_reset: bool,
    ) -> None:
        if firmware.suffix.lower() not in SUPPORTED_FLASH_SUFFIXES:
            raise UnsupportedArtifactError(
                f"Unsupported artifact type '{firmware.suffix}' - use one of: "
                f"{', '.join(sorted(SUPPORTED_FLASH_SUFFIXES))}"
            )

        target = handle.session.target
        # Match `pyocd load`'s proven pre-reset sequence. On STM32/ST-Link, skipping
        # this can make the Python API flash path fail even though the CLI succeeds.
        try:
            with _quiet_backend_streams():
                target.reset_and_halt()
                FileProgrammer(handle.session).program(str(firmware))
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
