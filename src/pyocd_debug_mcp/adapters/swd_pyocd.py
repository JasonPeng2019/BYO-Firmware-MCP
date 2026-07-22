"""pyOCD-backed SWD adapter implementation."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from pyocd_debug_mcp.kernel.processes import run_owned
from typing import Any, cast

from pyocd.core.exceptions import (  # type: ignore[import-untyped]
    CoreRegisterAccessError,
    TransferError,
)
from pyocd.core.helpers import ConnectHelper  # type: ignore[import-untyped]
from pyocd.flash.eraser import FlashEraser  # type: ignore[import-untyped]
from pyocd.flash.file_programmer import FileProgrammer  # type: ignore[import-untyped]
from pyocd.target.pack.cmsis_pack import CmsisPack  # type: ignore[import-untyped]
from pyocd.target.pack.pack_target import (  # type: ignore[import-untyped]
    TARGET,
    PackTargets,
    normalise_target_type_name,
)

from pyocd_debug_mcp.adapters.swd_interface import SWDInterface, TargetSessionHandle
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.pack_provision import (
    PackProvisionError,
    VerifiedPack,
    read_pack_bytes,
    sha256_bytes,
    verified_pack_for_target,
)
from pyocd_debug_mcp.probe_inventory import (
    _probe_info_from_pyocd_probe,
    list_connected_probes_cli,
)
from pyocd_debug_mcp.target_errors import (
    LockedTargetError,
    ProbeNotFoundError,
    ResetLineUnavailableError,
    TargetConnectionError,
    TargetControlError,
    TargetStateError,
    UnsupportedArtifactError,
)
from pyocd_debug_mcp.timeouts import (
    DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
    ServerTimeoutConfig,
    default_server_timeout_config,
    subprocess_timeout_stream_text,
)

ROUTE_PYOCD_NATIVE = "pyocd-native"
SUPPORTED_FLASH_SUFFIXES = frozenset({".axf", ".elf", ".hex"})
_PACK_OBJECTS: dict[str, tuple[bytes, CmsisPack]] = {}
_PACK_TARGET_LOCK = threading.RLock()
_MISSING_TARGET = object()


def _cmsis_pack_for(selected: VerifiedPack) -> CmsisPack:
    """Return the stable in-memory pack object used by pyOCD's global registry."""

    digest = selected.spec.sha256
    cached = _PACK_OBJECTS.get(digest)
    if cached is not None:
        payload, pack = cached
        if payload != selected.payload:
            raise TargetConnectionError("Pinned CMSIS-Pack digest collision detected.")
        return pack
    pack = CmsisPack(io.BytesIO(selected.payload))
    _PACK_OBJECTS[digest] = (selected.payload, pack)
    return pack


def _quarantined_cmsis_pack(path: Path, expected_sha256: str) -> CmsisPack:
    """Load one setup candidate without retaining unpromoted bytes globally."""

    try:
        payload = read_pack_bytes(path)
    except PackProvisionError as exc:
        raise TargetConnectionError(f"Quarantined CMSIS-Pack cannot be read: {exc}") from exc
    if sha256_bytes(payload) != expected_sha256:
        raise TargetConnectionError("Quarantined CMSIS-Pack changed before the live attach.")
    try:
        return CmsisPack(io.BytesIO(payload))
    except Exception as exc:  # noqa: BLE001 - normalize third-party parser failures
        raise TargetConnectionError(f"Quarantined CMSIS-Pack could not be loaded: {exc}") from exc


@contextlib.contextmanager
def _pack_target_scope(
    pack: CmsisPack | None,
    target: str | None,
    pdsc_device: str | None = None,
) -> Iterator[None]:
    """Temporarily bind one normalized pyOCD target name to the selected pack leaf."""

    if pack is None or target is None:
        yield
        return
    normalized = normalise_target_type_name(target)
    matches = tuple(
        device
        for device in pack.devices
        if (
            device.part_number.casefold() == pdsc_device.casefold()
            if pdsc_device is not None
            else normalise_target_type_name(device.part_number) == normalized
        )
    )
    if len(matches) != 1:
        raise TargetConnectionError(
            "Selected CMSIS-Pack must expose exactly one matching PDSC device."
        )
    if normalise_target_type_name(matches[0].part_number) != normalized:
        raise TargetConnectionError("Selected PDSC device does not match the canonical target.")
    pack_target_names = {normalise_target_type_name(device.part_number) for device in pack.devices}
    with _PACK_TARGET_LOCK:
        previous = {name: TARGET.get(name, _MISSING_TARGET) for name in pack_target_names}
        for name in pack_target_names:
            TARGET.pop(name, None)
        try:
            PackTargets.populate_device(matches[0])
            if normalized not in TARGET:
                raise TargetConnectionError(
                    f"pyOCD could not instantiate selected pack target {target!r}."
                )
            yield
        finally:
            for name, prior in previous.items():
                if prior is _MISSING_TARGET:
                    TARGET.pop(name, None)
                else:
                    TARGET[name] = prior


def _run_cmd(
    cmd: list[str],
    timeout_seconds: float = DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    try:
        result = run_owned(
            cmd,
            stdin=subprocess.DEVNULL,
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
def _backend_stdout_to_stderr() -> Iterator[None]:
    """Keep worker stdout protocol-only while preserving all backend diagnostics."""

    saved_stdout_fd: int | None = None
    stdout_fd: int | None = None
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            current_stdout_fd = sys.stdout.fileno()
            stderr_fd = sys.stderr.fileno()
            saved_stdout_fd = os.dup(current_stdout_fd)
            os.dup2(stderr_fd, current_stdout_fd)
            stdout_fd = current_stdout_fd
        except (AttributeError, io.UnsupportedOperation, OSError):
            if saved_stdout_fd is not None:
                os.close(saved_stdout_fd)
                saved_stdout_fd = None
            stdout_fd = None
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        try:
            sys.stdout.flush()
        finally:
            if saved_stdout_fd is not None and stdout_fd is not None:
                try:
                    os.dup2(saved_stdout_fd, stdout_fd)
                finally:
                    os.close(saved_stdout_fd)


def _typed_backend_error(exc: Exception) -> TargetControlError:
    if isinstance(exc, CoreRegisterAccessError):
        return TargetStateError(f"{type(exc).__name__}: {exc}")
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


def _breakpoint_kind(breakpoint: object) -> str:
    """Return the stable pyOCD breakpoint type name without binding to its enum class."""

    return str(getattr(getattr(breakpoint, "type", None), "name", "")).casefold()


def _breakpoint_recovery_instruction(breakpoint: object | None) -> str:
    kind = _breakpoint_kind(breakpoint) if breakpoint is not None else ""
    if kind == "sw":
        return (
            "Disconnect, restore/reflash code memory, reconnect/revalidate, and do not resume "
            "execution until restoration is proven."
        )
    if kind == "hw":
        return (
            "Disconnect, power-cycle the target, reconnect, and revalidate before further "
            "target control."
        )
    return (
        "Disconnect, power-cycle the target, reconnect/revalidate, and reflash/restore code "
        "memory before further target control."
    )


def _read_unfiltered_instruction16(target: Any, address: int) -> tuple[int | None, str | None]:
    """Read physical code memory through the selected core AP, bypassing BP filtering."""

    try:
        value = target.selected_core_or_raise.ap.read_memory(address, 16, now=True)
    except Exception as exc:  # noqa: BLE001 - returned as explicit provider-state evidence
        return None, f"raw instruction read failed: {type(exc).__name__}: {exc}"
    if type(value) is not int:
        return None, f"raw instruction read returned {type(value).__name__}, not an integer"
    return value & 0xFFFF, None


def _removed_breakpoint_provider_failure(
    target: Any,
    breakpoint: object | None,
    address: int,
) -> str | None:
    """Return why provider-level removal is unproven, or None when it is proven."""

    if breakpoint is None:
        return None
    kind = _breakpoint_kind(breakpoint)
    if kind == "sw":
        original = getattr(breakpoint, "original_instr", None)
        if type(original) is not int:
            return "software breakpoint has no valid saved original instruction"
        observed, error = _read_unfiltered_instruction16(target, address)
        if error is not None:
            return error
        expected = original & 0xFFFF
        if observed != expected:
            return (
                f"raw instruction is 0x{observed:04X}, expected restored original "
                f"0x{expected:04X}"
            )
        return None
    if kind == "hw":
        comparator = getattr(breakpoint, "comp_register_addr", None)
        if type(comparator) is not int:
            return "hardware breakpoint has no valid comparator register address"
        try:
            value = target.selected_core_or_raise.ap.read_memory(comparator, 32, now=True)
        except Exception as exc:  # noqa: BLE001 - provider state is uncertain
            return f"raw hardware comparator read failed: {type(exc).__name__}: {exc}"
        if type(value) is not int:
            return f"raw hardware comparator read returned {type(value).__name__}, not an integer"
        if value & 0xFFFFFFFF:
            return f"hardware comparator remains programmed with 0x{value & 0xFFFFFFFF:08X}"
        return None
    return f"breakpoint provider type {kind or '<unknown>'} cannot be verified"


def _looks_like_jlink_serial_open_failure(exc: Exception) -> bool:
    lowered = f"{type(exc).__name__}: {exc}".lower()
    return "no emulator with serial number" in lowered


def _same_probe_uid(expected: str | None, observed: str | None) -> bool:
    """Compare exact probe identities, allowing decimal zero padding only."""

    if not expected or not observed:
        return False
    left = expected.strip().casefold()
    right = observed.strip().casefold()
    if left == right:
        return True
    return (
        left.isdecimal()
        and right.isdecimal()
        and (left.lstrip("0") or "0") == (right.lstrip("0") or "0")
    )


def _single_matching_probe_visible_for_board_family(board: BoardConfig) -> bool:
    probes = []
    try:
        with _backend_stdout_to_stderr():
            for probe in ConnectHelper.get_all_connected_probes(
                blocking=False,
                print_wait_message=False,
            ):
                parsed = _probe_info_from_pyocd_probe(probe)
                if parsed is not None:
                    probes.append(parsed)
    except Exception:
        probes = []
    if not probes:
        probes = list_connected_probes_cli(_run_cmd)
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
        # This MCP server is headless: never wait for a J-Link provider dialog or control panel.
        options["jlink.non_interactive"] = True
    if board and board.debug_protocol:
        options["dap_protocol"] = board.debug_protocol
    if board and board.debug_connect_mode:
        options["connect_mode"] = board.debug_connect_mode
    if board and board.debug_clock_hz:
        options["frequency"] = board.debug_clock_hz
    return options or None


def _close_session(session: object) -> None:
    """Ask pyOCD to close; process death remains the native cleanup authority."""

    close = getattr(session, "close", None)
    if callable(close):
        with _backend_stdout_to_stderr():
            close()


class PyOCDSWDInterface(SWDInterface):
    """Child-local native pyOCD route for one provider-owned session."""

    @staticmethod
    def _choose_session(
        *,
        probe_uid: str | None,
        options: dict[str, object] | None,
    ) -> Any:
        with _backend_stdout_to_stderr():
            return ConnectHelper.session_with_chosen_probe(
                blocking=False,
                return_first=True,
                unique_id=probe_uid,
                auto_open=False,
                options=options,
            )

    def _open_and_verify_session(
        self,
        session: object,
        board: BoardConfig | None,
        target: str | None,
        pack: CmsisPack | None,
        pdsc_device: str | None,
    ) -> None:
        """Open and verify one selected native session."""

        with _backend_stdout_to_stderr():
            self._verify_session_pack_source(session, target, pack, pdsc_device)
            getattr(session, "open")()
            self._verify_session_pack_source(session, target, pack, pdsc_device)

    @staticmethod
    def _verify_session_pack_source(
        session: object,
        target: str | None,
        pack: CmsisPack | None,
        pdsc_device: str | None = None,
    ) -> None:
        """Prove the instantiated target came from the selected pack object."""

        if pack is None:
            return
        session_target = getattr(session, "target", None)
        source = getattr(type(session_target), "_pack_device", None)
        if source is None or not any(source is device for device in pack.devices):
            raise TargetConnectionError(
                f"pyOCD target {target!r} was already registered by a different device pack; "
                "restart with one unambiguous pinned provider."
            )
        if pdsc_device is not None and source.part_number.casefold() != pdsc_device.casefold():
            raise TargetConnectionError(
                f"pyOCD target {target!r} did not instantiate the persisted PDSC device."
            )

    def open(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        server_timeouts: ServerTimeoutConfig | None = None,
        protocol: str | None = None,
        connect_mode: str | None = None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
        frequency_hz: int | None = None,
        operation_timeout_seconds: float | None = None,
    ) -> TargetSessionHandle:
        del operation_timeout_seconds
        probe_uid = unique_id or os.environ.get("PYOCD_PROBE_UID") or None
        target_override = (
            target
            or (board.pyocd_target if board else None)
            or os.environ.get("PYOCD_TARGET")
            or None
        )
        options = build_session_options(board, target_override, server_timeouts)
        if protocol is not None:
            if protocol not in {"default", "swd", "jtag"}:
                raise ValueError(f"Unsupported pyOCD debug protocol: {protocol}")
            options = dict(options or {})
            options["dap_protocol"] = protocol
        if connect_mode is not None:
            if connect_mode not in {"attach", "halt", "pre-reset", "under-reset"}:
                raise ValueError(f"Unsupported pyOCD connect mode: {connect_mode}")
            options = dict(options or {})
            options["connect_mode"] = connect_mode
        if frequency_hz is not None:
            if isinstance(frequency_hz, bool) or frequency_hz <= 0:
                raise ValueError("frequency_hz must be a positive integer")
            options = dict(options or {})
            options["frequency"] = frequency_hz
        # Give pyOCD only the manifest-selected pack for this exact target. Passing
        # every local pack lets an unrelated provider win target resolution.
        if pack_path is not None:
            if pack_sha256 is None:
                raise ValueError("pack_sha256 is required with a quarantined pack_path")
            pack_object = _quarantined_cmsis_pack(pack_path.expanduser().resolve(), pack_sha256)
        elif pack_sha256 is not None:
            raise ValueError("pack_sha256 is valid only with a quarantined pack_path")
        else:
            selected_pack = (
                verified_pack_for_target(target_override) if target_override is not None else None
            )
            pack_object = _cmsis_pack_for(selected_pack) if selected_pack is not None else None
        if pack_object is not None:
            options = dict(options or {})
            options["pack"] = [pack_object]
        with _pack_target_scope(pack_object, target_override, pdsc_device):
            session = self._choose_session(probe_uid=probe_uid, options=options)
        if session is None:
            raise ProbeNotFoundError("No matching debug probe found.")
        try:
            self._open_and_verify_session(
                session, board, target_override, pack_object, pdsc_device
            )
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            try:
                _close_session(session)
            except Exception:
                pass
            if not _should_retry_without_uid(board, probe_uid, exc):
                raise _typed_backend_error(exc) from exc
            with _pack_target_scope(pack_object, target_override, pdsc_device):
                retry_session = self._choose_session(probe_uid=None, options=options)
            if retry_session is None:
                raise ProbeNotFoundError("No matching debug probe found.") from exc
            retry_uid = getattr(getattr(retry_session, "probe", None), "unique_id", None)
            if not _same_probe_uid(probe_uid, retry_uid):
                try:
                    _close_session(retry_session)
                except Exception:
                    pass
                raise ProbeNotFoundError(
                    "The selected J-Link was not the sole probe returned by UID-less retry; "
                    "refusing to open a different physical probe."
                ) from exc
            try:
                self._open_and_verify_session(
                    retry_session, board, target_override, pack_object, pdsc_device
                )
            except Exception as retry_exc:  # noqa: BLE001 - preserve backend context
                try:
                    _close_session(retry_session)
                except Exception:
                    pass
                raise _typed_backend_error(retry_exc) from retry_exc
            session = retry_session
        with _backend_stdout_to_stderr():
            observed_probe_uid = session.probe.unique_id or probe_uid
        return TargetSessionHandle(
            session=session,
            board=board,
            probe_uid=observed_probe_uid,
            route_used=ROUTE_PYOCD_NATIVE,
            target_override=target_override,
        )

    def close(self, handle: TargetSessionHandle) -> None:
        _close_session(handle.session)

    def connect_under_reset(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        server_timeouts: ServerTimeoutConfig | None = None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
        operation_timeout_seconds: float | None = None,
    ) -> TargetSessionHandle:
        del operation_timeout_seconds
        probe_uid = unique_id or os.environ.get("PYOCD_PROBE_UID") or None
        target_override = (
            target
            or (board.pyocd_target if board else None)
            or os.environ.get("PYOCD_TARGET")
            or None
        )
        options = dict(build_session_options(board, target_override, server_timeouts) or {})
        options["connect_mode"] = "under-reset"
        if pack_path is not None:
            if pack_sha256 is None:
                raise ValueError("pack_sha256 is required with a quarantined pack_path")
            pack_object = _quarantined_cmsis_pack(pack_path.expanduser().resolve(), pack_sha256)
        elif pack_sha256 is not None:
            raise ValueError("pack_sha256 is valid only with a quarantined pack_path")
        else:
            selected_pack = (
                verified_pack_for_target(target_override) if target_override is not None else None
            )
            pack_object = _cmsis_pack_for(selected_pack) if selected_pack is not None else None
        if pack_object is not None:
            options["pack"] = [pack_object]
        with _pack_target_scope(pack_object, target_override, pdsc_device):
            session = self._choose_session(probe_uid=probe_uid, options=options)
        if session is None:
            raise ProbeNotFoundError("No matching debug probe found.")
        assert_reset = getattr(session.probe, "assert_reset", None)
        if not callable(assert_reset):
            try:
                _close_session(session)
            except Exception:
                pass
            raise ResetLineUnavailableError(
                "The selected probe does not expose wired reset-line control; "
                "connect_under_reset cannot degrade to an ordinary attach."
            )
        try:
            with _backend_stdout_to_stderr():
                self._verify_session_pack_source(
                    session,
                    target_override,
                    pack_object,
                    pdsc_device,
                )
                # pyOCD's under-reset init sequence owns assertion, reset catch,
                # halt, and release. Calling assert_reset() before Session.open()
                # is invalid for probes that have not been opened yet and would
                # also duplicate pyOCD's reset sequence. Some real targets start
                # running again as nRESET is released, so explicitly halt once
                # more after open and verify the observable postcondition before
                # returning. The bounded retry covers probes where the first halt
                # command races reset release.
                session.open()
                self._verify_session_pack_source(
                    session, target_override, pack_object, pdsc_device
                )
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
                with _backend_stdout_to_stderr():
                    assert_reset(False)
            except Exception:
                pass
            try:
                _close_session(session)
            except Exception:
                pass
            if isinstance(exc, ResetLineUnavailableError):
                raise
            if isinstance(exc, NotImplementedError):
                raise ResetLineUnavailableError(
                    "The selected probe does not support wired reset-line control; "
                    "connect_under_reset cannot degrade to an ordinary attach."
                ) from exc
            raise _typed_backend_error(exc) from exc
        with _backend_stdout_to_stderr():
            observed_probe_uid = session.probe.unique_id or probe_uid
        return TargetSessionHandle(
            session=session,
            board=board,
            probe_uid=observed_probe_uid,
            route_used=ROUTE_PYOCD_NATIVE,
            target_override=target_override,
        )

    def get_state(self, handle: TargetSessionHandle) -> str:
        try:
            with _backend_stdout_to_stderr():
                return cast(str, handle.session.target.get_state().name)
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def read_memory(
        self,
        handle: TargetSessionHandle,
        address: int,
        width_bits: int,
        *,
        operation_timeout_seconds: float | None = None,
    ) -> int:
        del operation_timeout_seconds
        try:
            with _backend_stdout_to_stderr():
                return cast(int, handle.session.target.read_memory(address, width_bits))
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def read_memory_block(
        self, handle: TargetSessionHandle, address: int, length: int
    ) -> list[int]:
        try:
            with _backend_stdout_to_stderr():
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
            with _backend_stdout_to_stderr():
                handle.session.target.write_memory(address, value, width_bits)
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def read_core_register(self, handle: TargetSessionHandle, name: str) -> int:
        try:
            with _backend_stdout_to_stderr():
                return cast(int, handle.session.target.read_core_register(name))
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def write_core_register(self, handle: TargetSessionHandle, name: str, value: int) -> None:
        try:
            with _backend_stdout_to_stderr():
                handle.session.target.write_core_register(name, value)
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def supported_core_registers(self, handle: TargetSessionHandle) -> tuple[str, ...]:
        try:
            with _backend_stdout_to_stderr():
                registers = handle.session.target.core_registers.by_name
                return tuple(sorted(str(name).casefold() for name in registers))
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def halt(self, handle: TargetSessionHandle) -> None:
        try:
            with _backend_stdout_to_stderr():
                handle.session.target.halt()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def resume(self, handle: TargetSessionHandle) -> None:
        try:
            with _backend_stdout_to_stderr():
                handle.session.target.resume()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def step(self, handle: TargetSessionHandle) -> None:
        try:
            with _backend_stdout_to_stderr():
                handle.session.target.step()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def reset(self, handle: TargetSessionHandle) -> None:
        try:
            with _backend_stdout_to_stderr():
                handle.session.target.reset()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def reset_and_halt(self, handle: TargetSessionHandle) -> None:
        try:
            with _backend_stdout_to_stderr():
                handle.session.target.reset_and_halt()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def release_reset(self, handle: TargetSessionHandle) -> None:
        assert_reset = getattr(getattr(handle.session, "probe", None), "assert_reset", None)
        if not callable(assert_reset):
            raise ResetLineUnavailableError(
                "The selected probe does not expose wired reset-line control."
            )
        try:
            with _backend_stdout_to_stderr():
                assert_reset(False)
        except NotImplementedError as exc:
            raise ResetLineUnavailableError(
                "The selected probe does not support wired reset-line control."
            ) from exc
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
            with _backend_stdout_to_stderr():
                target.reset_and_halt()
                # Containment pre-computes and validates every implied erase sector.
                # Force pyOCD to honor that sector scope even when a host/session option
                # would otherwise select auto or whole-chip erase.
                FileProgrammer(handle.session, chip_erase="sector").program(str(firmware))
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
            with _backend_stdout_to_stderr():
                FlashEraser(handle.session, FlashEraser.Mode.MASS).erase()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

    def supports_recovery(self, handle: TargetSessionHandle, mechanism: str) -> bool:
        with _backend_stdout_to_stderr():
            return (
                mechanism == "backend_mass_erase"
                and getattr(handle.session, "target", None) is not None
            )

    def set_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        with _backend_stdout_to_stderr():
            address &= ~1
            target = handle.session.target
            manager_flush_succeeded = False
            try:
                existing = target.find_breakpoint(address)
                if existing is not None:
                    # find_breakpoint() also returns queued UnrealizedBreakpoint
                    # entries. Only an enabled provider object proves an already
                    # installed breakpoint. Flush a pre-existing queued request,
                    # but never claim ownership of or roll back that request.
                    if bool(getattr(existing, "enabled", False)):
                        return
                    try:
                        target.selected_core_or_raise.bp_manager.flush()
                        target.flush()
                        realized = target.find_breakpoint(address)
                    except Exception as exc:  # noqa: BLE001 - pre-existing state is unowned
                        raise TargetStateError(
                            f"Pre-existing breakpoint state at 0x{address:08X} could not be "
                            f"realized or verified. {_breakpoint_recovery_instruction(existing)}"
                        ) from exc
                    if realized is not None and bool(getattr(realized, "enabled", False)):
                        return
                    raise TargetStateError(
                        f"Pre-existing breakpoint state at 0x{address:08X} remains unrealized "
                        "or unverifiable; it was not removed because this call does not own it. "
                        f"{_breakpoint_recovery_instruction(existing)}"
                    )
                accepted = target.set_breakpoint(address)
            except TargetControlError:
                raise
            except Exception as exc:  # noqa: BLE001 - preserve backend context
                raise _typed_backend_error(exc) from exc
            if not accepted:
                raise TargetControlError(
                    f"pyOCD could not allocate a breakpoint at 0x{address:08X}. "
                    "Remove an existing breakpoint or choose a supported executable address, "
                    "then retry."
                )

            realized: object | None = None
            try:
                # Target.set_breakpoint() queues an UnrealizedBreakpoint. Match
                # pyOCD's own breakpoint command by flushing the selected core's
                # manager before flushing pending target transfers.
                target.selected_core_or_raise.bp_manager.flush()
                manager_flush_succeeded = True
                realized = target.find_breakpoint(address)
                if realized is None:
                    raise TargetStateError(
                        f"pyOCD did not return a realized breakpoint at 0x{address:08X}; "
                        "a suppressed software-provider write failure may have changed code "
                        f"memory. {_breakpoint_recovery_instruction(None)}"
                    )
                target.flush()
                if _breakpoint_kind(realized) == "sw":
                    observed, error = _read_unfiltered_instruction16(target, address)
                    if error is not None or observed != 0xBE00:
                        detail = error or f"raw instruction is 0x{observed:04X}, expected 0xBE00"
                        raise TargetStateError(
                            f"Software breakpoint installation at 0x{address:08X} could not be "
                            f"proven in physical code memory ({detail}). "
                            f"{_breakpoint_recovery_instruction(realized)}"
                        )
            except Exception as primary:  # noqa: BLE001 - transaction rollback is best effort
                rollback_failures: list[str] = []
                try:
                    target.remove_breakpoint(address)
                except Exception as exc:  # noqa: BLE001 - collect every cleanup failure
                    rollback_failures.append(f"remove_breakpoint: {type(exc).__name__}: {exc}")
                try:
                    target.selected_core_or_raise.bp_manager.flush()
                except Exception as exc:  # noqa: BLE001 - collect every cleanup failure
                    rollback_failures.append(f"breakpoint manager flush: {type(exc).__name__}: {exc}")
                try:
                    target.flush()
                except Exception as exc:  # noqa: BLE001 - collect every cleanup failure
                    rollback_failures.append(f"target flush: {type(exc).__name__}: {exc}")
                try:
                    remaining = target.find_breakpoint(address)
                except Exception as exc:  # noqa: BLE001 - absence could not be verified
                    rollback_failures.append(f"absence verification: {type(exc).__name__}: {exc}")
                else:
                    if remaining is not None:
                        rollback_failures.append("breakpoint remains present after rollback")
                provider_failure = _removed_breakpoint_provider_failure(
                    target,
                    realized,
                    address,
                )
                if provider_failure is not None:
                    rollback_failures.append(f"provider-level rollback: {provider_failure}")

                # A provider can program an FPB comparator and then throw before
                # the breakpoint manager records the realized object. In that
                # case manager absence after cleanup is not physical absence proof.
                if not manager_flush_succeeded:
                    rollback_failures.append(
                        "initial breakpoint manager/provider flush failed before physical "
                        "comparator state could be proven"
                    )

                if rollback_failures:
                    primary_detail = f"{type(primary).__name__}: {primary}"
                    rollback_detail = "; ".join(rollback_failures)
                    raise TargetStateError(
                        f"Breakpoint state at 0x{address:08X} is uncertain after a failed "
                        f"installation ({primary_detail}); rollback could not be proven "
                        f"complete ({rollback_detail}). "
                        f"{_breakpoint_recovery_instruction(realized)}"
                    ) from primary
                if isinstance(primary, TargetControlError):
                    raise
                raise _typed_backend_error(primary) from primary

    def remove_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        with _backend_stdout_to_stderr():
            address &= ~1
            target = handle.session.target
            try:
                realized = target.find_breakpoint(address)
            except Exception as exc:  # noqa: BLE001 - no removal was requested yet
                raise _typed_backend_error(exc) from exc
            try:
                target.remove_breakpoint(address)
                target.selected_core_or_raise.bp_manager.flush()
                target.flush()
                if target.find_breakpoint(address) is not None:
                    raise TargetControlError(
                        f"pyOCD did not remove the breakpoint at 0x{address:08X}; "
                        "the breakpoint remains installed."
                    )
                provider_failure = _removed_breakpoint_provider_failure(
                    target,
                    realized,
                    address,
                )
                if provider_failure is not None:
                    raise TargetStateError(
                        f"Breakpoint removal at 0x{address:08X} is uncertain "
                        f"({provider_failure}). {_breakpoint_recovery_instruction(realized)}"
                    )
                return
            except TargetControlError:
                raise
            except Exception as primary:  # noqa: BLE001 - establish provider state if possible
                verification_failures: list[str] = []
                try:
                    target.selected_core_or_raise.bp_manager.flush()
                except Exception as exc:  # noqa: BLE001 - collect every verification failure
                    verification_failures.append(
                        f"breakpoint manager flush: {type(exc).__name__}: {exc}"
                    )
                try:
                    target.flush()
                except Exception as exc:  # noqa: BLE001 - collect every verification failure
                    verification_failures.append(f"target flush: {type(exc).__name__}: {exc}")
                try:
                    remaining = target.find_breakpoint(address)
                except Exception as exc:  # noqa: BLE001 - provider state is unknown
                    verification_failures.append(
                        f"absence verification: {type(exc).__name__}: {exc}"
                    )
                    remaining = None
                provider_failure = _removed_breakpoint_provider_failure(
                    target,
                    realized,
                    address,
                )
                if provider_failure is not None:
                    verification_failures.append(
                        f"provider-level removal: {provider_failure}"
                    )

                if not verification_failures and remaining is None:
                    return
                if not verification_failures:
                    raise TargetControlError(
                        f"pyOCD did not remove the breakpoint at 0x{address:08X}; "
                        "the breakpoint remains installed."
                    ) from primary

                primary_detail = f"{type(primary).__name__}: {primary}"
                verification_detail = "; ".join(verification_failures)
                raise TargetStateError(
                    f"Breakpoint state at 0x{address:08X} is uncertain after a failed "
                    f"removal ({primary_detail}); provider state could not be verified "
                    f"({verification_detail}). {_breakpoint_recovery_instruction(realized)}"
                ) from primary
