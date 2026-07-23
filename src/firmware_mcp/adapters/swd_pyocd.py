"""pyOCD-backed SWD adapter implementation."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

from firmware_mcp.kernel.processes import run_owned
from typing import Any, cast

from pyocd.core.exceptions import (  # type: ignore[import-untyped]
    CoreRegisterAccessError,
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

from firmware_mcp.adapters.debug_interface import (
    FlashVerification,
    PhysicalMemoryRegion,
    RecoveryCapability,
    RecoveryResult,
    DebugInterface,
    TargetSessionHandle,
    session_metadata,
)
from firmware_mcp.board_config import BoardConfig
from firmware_mcp.pack_provision import (
    PackProvisionError,
    VerifiedPack,
    read_pack_bytes,
    sha256_bytes,
    verified_pack_for_target,
)
from firmware_mcp.target_errors import (
    CleanupDiagnostic,
    LockedTargetError,
    ProbeNotFoundError,
    ResetLineUnavailableError,
    TargetConnectionCleanupError,
    TargetConnectionError,
    TargetControlError,
    TargetStateError,
    UnsupportedArtifactError,
)
from firmware_mcp.safety.linker import (
    LinkerEvidenceError,
    canonical_image_digest,
    parse_flash_image,
)
from firmware_mcp.services.live_identity import observe_live_identity
from firmware_mcp.timeouts import (
    subprocess_timeout_stream_text,
)

ROUTE_PYOCD_NATIVE = "pyocd-native"
SUPPORTED_FLASH_SUFFIXES = frozenset({".axf", ".elf", ".hex"})
# Transport buffering only, not a limit on an image or verification work: every
# parsed programmed byte is read back regardless of image size.
_FLASH_READBACK_CHUNK_BYTES = 1024
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
    timeout_seconds: float | None = None,
) -> tuple[int, str, str]:
    try:
        result = run_owned(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout_seconds=timeout_seconds,
        )
    except FileNotFoundError:
        executable = cmd[0] if cmd else "<unknown>"
        return 127, "", f"command not found: {executable}"
    except subprocess.TimeoutExpired as exc:
        return (
            124,
            subprocess_timeout_stream_text(exc.stdout),
            f"command timed out after {timeout_seconds}s: {' '.join(cmd)}",
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
    if isinstance(exc, TargetControlError):
        return exc
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


_CONNECTION_CLEANUP_RECOVERY = "Disconnect, power-cycle the target if needed, reconnect, and revalidate before further operations."


def _cleanup_diagnostic(stage: str, exc: Exception) -> CleanupDiagnostic:
    return CleanupDiagnostic(
        stage=stage,
        error_type=type(exc).__name__,
        error_message=str(exc),
        recovery=_CONNECTION_CLEANUP_RECOVERY,
    )


def _connection_cleanup_error(
    primary: Exception,
    diagnostics: list[CleanupDiagnostic],
) -> TargetConnectionCleanupError:
    """Keep the original provider failure and every unconfirmed cleanup step."""

    return TargetConnectionCleanupError(
        type(primary).__name__,
        str(primary),
        tuple(diagnostics),
    )


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
                f"raw instruction is 0x{observed:04X}, expected restored original 0x{expected:04X}"
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


def build_session_options(
    board: BoardConfig | None,
    target: str | None,
) -> dict[str, object] | None:
    """Build pyOCD session options from shared board facts."""

    options: dict[str, object] = {}
    if target:
        options["target_override"] = target
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


class PyOCDSWDInterface(DebugInterface):
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
        protocol: str | None = None,
        connect_mode: str | None = None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
        frequency_hz: int | None = None,
    ) -> TargetSessionHandle:
        probe_uid = unique_id
        target_override = target or (board.target if board else None)
        options = build_session_options(board, target_override)
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
            self._open_and_verify_session(session, board, target_override, pack_object, pdsc_device)
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            diagnostics: list[CleanupDiagnostic] = []
            try:
                _close_session(session)
            except Exception as cleanup:
                diagnostics.append(_cleanup_diagnostic("session_close", cleanup))
            # An explicit probe UID must stay explicit. Retrying without it can
            # select another physical probe and cannot improve correctness.
            if diagnostics:
                raise _connection_cleanup_error(exc, diagnostics) from exc
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

    def close(self, handle: TargetSessionHandle) -> None:
        _close_session(handle.session)

    def connect_under_reset(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
    ) -> TargetSessionHandle:
        probe_uid = unique_id
        target_override = target or (board.target if board else None)
        options = dict(build_session_options(board, target_override) or {})
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
            primary = ResetLineUnavailableError(
                "The selected probe does not expose wired reset-line control; "
                "connect_under_reset cannot degrade to an ordinary attach."
            )
            try:
                _close_session(session)
            except Exception as cleanup:
                raise _connection_cleanup_error(
                    primary,
                    [_cleanup_diagnostic("session_close", cleanup)],
                ) from primary
            raise primary
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
                # returning. Do not invent a retry window: the provider's one
                # halt command and observed state are the complete postcondition.
                session.open()
                self._verify_session_pack_source(session, target_override, pack_object, pdsc_device)
                session.target.halt()
                state = str(session.target.get_state().name).casefold()
                if state != "halted":
                    raise TargetConnectionError(
                        "connect_under_reset released reset but observed target state "
                        f"{state!r}, not halted"
                    )
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            # If initialisation failed after pyOCD asserted nRESET, release it
            # before closing the partially opened session. Both actions are
            # correctness postconditions, so preserve any failures explicitly.
            diagnostics: list[CleanupDiagnostic] = []
            try:
                with _backend_stdout_to_stderr():
                    assert_reset(False)
            except Exception as cleanup:
                diagnostics.append(_cleanup_diagnostic("reset_release", cleanup))
            try:
                _close_session(session)
            except Exception as cleanup:
                diagnostics.append(_cleanup_diagnostic("session_close", cleanup))
            if diagnostics:
                raise _connection_cleanup_error(exc, diagnostics) from exc
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
    ) -> int:
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

    def physical_memory_regions(
        self, handle: TargetSessionHandle
    ) -> tuple[PhysicalMemoryRegion, ...]:
        """Translate the current pyOCD map into serializable physical facts."""

        try:
            with _backend_stdout_to_stderr():
                memory_map = getattr(handle.session.target, "memory_map", None)
                raw_regions = getattr(memory_map, "regions", None)
                if memory_map is None or raw_regions is None:
                    raise TargetStateError(
                        "Live provider memory-map facts are unavailable; reconnect and validate the target."
                    )
                token = session_metadata(handle).runtime_token
                result: list[PhysicalMemoryRegion] = []
                for index, region in enumerate(raw_regions):
                    start = getattr(region, "start", None)
                    # pyOCD regions use an inclusive end address.
                    inclusive_end = getattr(region, "end", None)
                    flags = tuple(
                        getattr(region, name, None)
                        for name in ("is_readable", "is_writable", "is_executable")
                    )
                    if (
                        not isinstance(start, int)
                        or isinstance(start, bool)
                        or not isinstance(inclusive_end, int)
                        or isinstance(inclusive_end, bool)
                        or inclusive_end < start
                        or any(not isinstance(flag, bool) for flag in flags)
                    ):
                        raise TargetStateError(
                            "Live provider returned malformed memory-map facts; reconnect and validate the target."
                        )
                    result.append(
                        PhysicalMemoryRegion(
                            start=start,
                            end=inclusive_end + 1,
                            readable=bool(flags[0]),
                            writable=bool(flags[1]),
                            executable=bool(flags[2]),
                            kind=str(getattr(region, "type", type(region).__name__)),
                            name=str(getattr(region, "name", f"region-{index}")),
                            provenance="current_live_pyocd_provider_session",
                            session_token=token,
                        )
                    )
                return tuple(result)
        except TargetControlError:
            raise
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
    ) -> FlashVerification:
        if firmware.suffix.lower() not in SUPPORTED_FLASH_SUFFIXES:
            raise UnsupportedArtifactError(
                f"Unsupported artifact type '{firmware.suffix}' - use one of: "
                f"{', '.join(sorted(SUPPORTED_FLASH_SUFFIXES))}"
            )

        try:
            image = parse_flash_image(firmware)
        except LinkerEvidenceError as exc:
            raise UnsupportedArtifactError(f"Malformed flash image [{exc.code}]: {exc}") from exc

        target = handle.session.target
        # This is deliberately the same capability-aware observation used by
        # the guard and physical-map layers. A compatible proof remains
        # compatible and missing proof is unavailable.  A verified
        # contradiction or a configured proof that cannot currently be read
        # both block provider mutation before programming begins.
        try:
            observe_live_identity(
                handle,
                read_memory=lambda _handle, address, width: target.read_memory(address, width),
            )
        except TargetStateError:
            raise
        except Exception as exc:  # noqa: BLE001 - preserve provider diagnosis
            raise _typed_backend_error(exc) from exc
        memory_map = getattr(target, "memory_map", None)
        if memory_map is None or not callable(getattr(memory_map, "get_region_for_address", None)):
            raise TargetStateError(
                "Live provider memory-map facts are unavailable; reconnect with a target that exposes "
                "writable flash regions before programming."
            )
        for start, end in image.ranges:
            address = start
            while address < end:
                region = memory_map.get_region_for_address(address)
                if (
                    region is None
                    or not bool(getattr(region, "is_flash", False))
                    or not bool(getattr(region, "is_writable", False))
                ):
                    raise TargetStateError(
                        f"Flash image first lacks live writable-flash authority at 0x{address:016X}. "
                        "Reconnect/validate the exact target or use a provider recipe that reports "
                        "its own verification."
                    )
                region_end = int(getattr(region, "end", -1)) + 1
                if region_end <= address:
                    raise TargetStateError(
                        f"Live provider returned an invalid flash region at 0x{address:016X}. "
                        "Reconnect and retry target discovery."
                    )
                address = min(end, region_end)
        # Match `pyocd load`'s proven pre-reset sequence. On STM32/ST-Link, skipping
        # this can make the Python API flash path fail even though the CLI succeeds.
        try:
            with _backend_stdout_to_stderr():
                target.reset_and_halt()
                # Containment pre-computes and validates every implied erase sector.
                # Force pyOCD to honor that sector scope even when a host/session option
                # would otherwise select auto or whole-chip erase.
                FileProgrammer(handle.session, chip_erase="sector").program(str(firmware))
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc

        observed: dict[int, int] = {}
        try:
            with _backend_stdout_to_stderr():
                for start, end in image.ranges:
                    address = start
                    while address < end:
                        length = min(_FLASH_READBACK_CHUNK_BYTES, end - address)
                        values = list(target.read_memory_block8(address, length))
                        if len(values) != length or any(
                            not isinstance(value, int) or not 0 <= value <= 0xFF for value in values
                        ):
                            raise TargetStateError(
                                f"Flash readback at 0x{address:016X} did not return {length} byte(s). "
                                "Reconnect and retry the complete flash operation."
                            )
                        for offset, value in enumerate(values):
                            expected = image.bytes_by_address[address + offset]
                            if value != expected:
                                raise TargetStateError(
                                    f"Flash readback mismatch at 0x{address + offset:016X}: expected "
                                    f"0x{expected:02X}, observed 0x{value:02X}. Reconnect and retry the "
                                    "complete flash operation."
                                )
                            observed[address + offset] = value
                        address += length
        except TargetStateError:
            raise
        except Exception as exc:  # noqa: BLE001 - failed readback is never a successful flash
            raise _typed_backend_error(exc) from exc

        observed_digest = canonical_image_digest(observed)
        if observed_digest != image.sha256:
            raise TargetStateError(
                "Flash readback digest mismatch after byte comparison; reconnect and retry the complete "
                "flash operation."
            )
        try:
            with _backend_stdout_to_stderr():
                if halt_after_reset:
                    target.reset_and_halt()
                    final_state = str(target.get_state().name)
                    if final_state.casefold() != "halted":
                        # Programming and byte readback have already completed. This is a
                        # final-reset postcondition failure, not a pre-verification target-state
                        # error, so preserve the verified-write evidence for the process parent.
                        message = (
                            "Final reset postcondition failed; halt_after_reset=true; "
                            f"observed_state={final_state}; expected_state=HALTED. Reconnect and retry."
                        )
                        return FlashVerification(
                            str(image.path),
                            len(image.bytes_by_address),
                            image.ranges,
                            image.sha256,
                            observed_digest,
                            "failed",
                            TargetStateError.__name__,
                            message,
                        )
                else:
                    target.reset()
                    # A target can legitimately re-halt at a breakpoint or fault immediately after reset.
                    final_state = str(target.get_state().name)
        except TargetStateError:
            raise
        except Exception as exc:  # noqa: BLE001 - worker transports verified-write evidence to its parent
            return FlashVerification(
                str(image.path),
                len(image.bytes_by_address),
                image.ranges,
                image.sha256,
                observed_digest,
                "unknown",
                type(exc).__name__,
                str(exc),
            )
        return FlashVerification(
            str(image.path),
            len(image.bytes_by_address),
            image.ranges,
            image.sha256,
            observed_digest,
            final_state,
        )

    def recovery_capabilities(self, handle: TargetSessionHandle) -> tuple[RecoveryCapability, ...]:
        if getattr(handle.session, "target", None) is None:
            return ()
        return (
            RecoveryCapability(
                mechanism="backend_mass_erase",
                effect="erase",
                coverage={"kind": "all_matching", "physical_kinds": ["physical_flash"]},
                effect_verification="unavailable",
                session_postcondition="unknown",
            ),
        )

    def recover(self, handle: TargetSessionHandle, mechanism: str) -> RecoveryResult:
        if mechanism not in {item.mechanism for item in self.recovery_capabilities(handle)}:
            raise RuntimeError(
                f"Recovery mechanism '{mechanism}' is not currently exposed by this provider."
            )
        try:
            with _backend_stdout_to_stderr():
                FlashEraser(handle.session, FlashEraser.Mode.MASS).erase()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc
        return RecoveryResult(mechanism, True, "unavailable", "unknown")

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
                    rollback_failures.append(
                        f"breakpoint manager flush: {type(exc).__name__}: {exc}"
                    )
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
                    verification_failures.append(f"provider-level removal: {provider_failure}")

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
