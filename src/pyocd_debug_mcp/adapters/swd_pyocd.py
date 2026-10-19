"""pyOCD-backed SWD adapter implementation."""

from __future__ import annotations

import contextlib
import io
import logging
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
    ProbeError,
    TransferError,
)
from pyocd.core.helpers import ConnectHelper  # type: ignore[import-untyped]
from pyocd.core.target import Target  # type: ignore[import-untyped]
from pyocd.coresight.cortex_m import CortexM  # type: ignore[import-untyped]
from pyocd.flash.file_programmer import FileProgrammer  # type: ignore[import-untyped]
from pyocd.target.pack.cmsis_pack import CmsisPack  # type: ignore[import-untyped]
from pyocd.target.pack.pack_target import (  # type: ignore[import-untyped]
    TARGET,
    PackTargets,
    normalise_target_type_name,
)

from pyocd_debug_mcp.adapters.swd_interface import (
    SWDInterface,
    TargetSessionHandle,
    session_metadata,
)
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.flash_image import (
    FlashImage,
    FlashResult,
    derive_flash_image,
    snapshot_flash_artifact,
    verify_flash_image,
)
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
    registered_provider_ids,
)
from pyocd_debug_mcp.target_errors import (
    FlashReadbackConnectionError,
    FlashTargetSupportError,
    FlashVerificationError,
    LockedTargetError,
    ProbeNotFoundError,
    ResetLineUnavailableError,
    TargetConnectionError,
    TargetControlError,
    TargetStateError,
)
from pyocd_debug_mcp.timeouts import (
    DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
    ServerTimeoutConfig,
    default_server_timeout_config,
    subprocess_timeout_stream_text,
)

ROUTE_PYOCD_NATIVE = "pyocd-native"
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
    if isinstance(exc, LockedTargetError):
        # Already a typed protected-target refusal; keep its remedy text intact.
        return LockedTargetError(str(exc))
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


def _require_flash_support(target: Any, image: FlashImage) -> None:
    """Refuse unless every addressed byte lies in live flash with a usable algorithm.

    pyOCD's FileProgrammer logs and skips ELF/HEX data it has no flash region for,
    then returns normally, so this mirrors FlashLoader.add_data's region walk before
    any reset or programming. RAM-addressed bytes are refused: flash means flash.
    """

    try:
        memory_map = target.memory_map
        core_name = getattr(getattr(target, "selected_core", None), "node_name", None)
        for start, end in image.ranges:
            address = start
            while address < end:
                region = memory_map.get_region_for_address(address, core_name)
                flash = getattr(region, "flash", None) if region is not None else None
                if (
                    region is None
                    or not region.is_flash
                    or flash is None
                    or getattr(flash, "is_valid", False) is not True
                    or not getattr(flash, "has_required_sequences", True)
                ):
                    part = getattr(target, "part_number", None) or "unknown"
                    raise FlashTargetSupportError(
                        f"The live target '{part}' has no programmable flash algorithm for "
                        f"addressed artifact bytes at 0x{address:08X} (range 0x{start:08X}-"
                        f"0x{end - 1:08X}); nothing was reset, erased or programmed. Connect "
                        "with the exact pyOCD or CMSIS-pack target for this device rather than "
                        "a generic target such as cortex_m, and flash only addresses in its "
                        "flash regions."
                    )
                address = int(region.end) + 1
    except FlashTargetSupportError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed when support is unprovable
        raise FlashTargetSupportError(
            f"Live flash support could not be confirmed ({type(exc).__name__}: {exc}); "
            "nothing was reset, erased or programmed."
        ) from exc


def _breakpoint_kind(breakpoint: object) -> str:
    """Return the stable pyOCD breakpoint type name without binding to its enum class."""

    return str(getattr(getattr(breakpoint, "type", None), "name", "")).casefold()


def _breakpoint_recovery_instruction(
    breakpoint: object | None, *, hardware_only: bool = False
) -> str:
    kind = _breakpoint_kind(breakpoint) if breakpoint is not None else ""
    if kind == "sw":
        return (
            "Disconnect, restore/reflash code memory, reconnect/revalidate, and do not resume "
            "execution until restoration is proven."
        )
    if kind == "hw" or (not kind and hardware_only):
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
    left_provider, left_separator, left_uid = left.partition(":")
    right_provider, right_separator, right_uid = right.partition(":")
    providers = registered_provider_ids()
    if left_separator and left_provider in providers:
        if right_separator and right_provider in providers and left_provider != right_provider:
            return False
        left = left_uid
    if right_separator and right_provider in providers:
        right = right_uid
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
) -> dict[str, object]:
    """Build pyOCD session options from shared board facts."""

    # pyOCD defaults auto_unlock on, which mass-erases a protected target (and may
    # write Nordic UICR) just to attach. Every session opened from these options
    # must refuse instead; erasing needs the separate one-use $mass-erase grant.
    options: dict[str, object] = {"auto_unlock": False}
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
    return options


# pyOCD logs this when auto_unlock is off and it finds a protected target.
_AUTO_UNLOCK_DECLINED = "not automatically unlocking"


class _AutoUnlockDeclineRecorder(logging.Handler):
    """Notice pyOCD declining to mass-erase a protected target during one open."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.notice: str | None = None
        # Adding this handler hides pyOCD warnings from logging.lastResort, so
        # forward them there when nothing else would have printed them.
        self._forward = None if logging.getLogger("pyocd").hasHandlers() else logging.lastResort

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if _AUTO_UNLOCK_DECLINED in message:
            self.notice = message
        if self._forward is not None and record.levelno >= self._forward.level:
            self._forward.handle(record)


def _protected_target_refusal(notice: str) -> LockedTargetError:
    return LockedTargetError(
        f"Target debug access is protected (pyOCD: {notice}). Connecting never erases "
        "a protected target, so no session was opened and the target was not erased or "
        "unlocked. pyOCD can remove this protection only by erasing the whole device, "
        "and target_unlock and target_unlock_raw need a connected session, so this "
        "server cannot recover it. If the device owner accepts losing its contents, use "
        "the vendor's recovery procedure outside this server, then connect again."
    )


def _open_refusing_protected_target(session: object) -> None:
    """Open a session built without auto-unlock; refuse a target pyOCD found protected.

    pyOCD may still fail later (no cores) or even finish opening after declining,
    so the decline itself decides the outcome. The caller closes the session.
    """

    recorder = _AutoUnlockDeclineRecorder()
    pyocd_logger = logging.getLogger("pyocd")
    pyocd_logger.addHandler(recorder)
    try:
        getattr(session, "open")()
    except Exception as exc:
        if recorder.notice is not None:
            raise _protected_target_refusal(recorder.notice) from exc
        raise
    finally:
        pyocd_logger.removeHandler(recorder)
    if recorder.notice is not None:
        raise _protected_target_refusal(recorder.notice)


def _close_session(session: object) -> None:
    """Ask pyOCD to close; process death remains the native cleanup authority."""

    close = getattr(session, "close", None)
    if callable(close):
        with _backend_stdout_to_stderr():
            close()


@contextlib.contextmanager
def _suspend_step_breakpoint(target: Any, breakpoint: Any) -> Iterator[None]:
    """Suspend one owned provider slot without reallocating it or changing its owner.

    Match pyOCD FPB's comparator-clear operation, retaining its exact register
    value and provider allocation. For a software BP, expose its saved RAM
    instruction and then restore the physical patch, bypassing read filtering.
    The manager/provider objects stay installed, so PRE_RUN cannot re-add them.
    """
    if breakpoint is None:
        yield
        return
    core = target.selected_core_or_raise
    address = breakpoint.addr
    kind = _breakpoint_kind(breakpoint)
    if kind == "hw":
        location, width, suspended = breakpoint.comp_register_addr, 32, 0
    elif kind == "sw":
        region = core.memory_map.get_region_for_address(address)
        if not (region and region.is_ram and not region.is_flash and region.is_writable
                and region.contains_range(address, length=2)):
            raise TargetStateError("Cannot suspend a software breakpoint outside writable RAM.")
        location, width, suspended = address, 16, breakpoint.original_instr
    else:
        raise TargetStateError("Cannot step over an unrealized or unsupported breakpoint.")
    saved = core.ap.read_memory(location, width, now=True)
    if type(saved) is not int or (kind == "hw" and not saved & 1) or (
        kind == "sw" and saved != 0xBE00
    ):
        raise TargetStateError("Cannot verify the installed breakpoint before stepping.")

    def write_and_verify(value: int) -> None:
        core.ap.write_memory(location, value, width)
        if kind == "sw":
            core.invalidate_instruction_cache(address)
        target.flush()
        if core.ap.read_memory(location, width, now=True) != value:
            raise TargetStateError("Physical breakpoint state did not match readback.")

    try:
        write_and_verify(suspended)
        yield
    finally:
        primary = sys.exc_info()[1]
        try:
            write_and_verify(saved)
            if target.find_breakpoint(address) is not breakpoint or not breakpoint.enabled:
                raise TargetStateError("Breakpoint manager/provider identity changed during step.")
        except Exception as exc:  # noqa: BLE001 - never hide an uncertain restoration
            raise TargetStateError(
                f"Step breakpoint restoration is uncertain: {exc}. "
                f"{_breakpoint_recovery_instruction(breakpoint)}"
                + (f" Original step error: {primary}" if primary else "")
            ) from exc


def _restore_step_control(target: Any, saved: int) -> None:
    """pyOCD restores C_MASKINTS/C_PMOV on normal return; also restore on errors."""
    core = target.selected_core_or_raise
    mask = CortexM.C_MASKINTS | CortexM.C_PMOV
    observed = core.read32(CortexM.DHCSR)
    if not observed & CortexM.S_HALT:
        target.halt()
        observed = core.read32(CortexM.DHCSR)
    if not observed & CortexM.S_HALT:
        raise TargetStateError("Step cleanup could not halt the core; control restoration is uncertain.")
    if observed & mask != saved & mask:
        core.write32(CortexM.DHCSR, CortexM.DBGKEY | CortexM.C_DEBUGEN | CortexM.C_HALT
                     | (saved & mask))
        target.flush()
        if core.read32(CortexM.DHCSR) & mask != saved & mask:
            raise TargetStateError("Step interrupt/debug control restoration is uncertain.")


class PyOCDSWDInterface(SWDInterface):
    """Child-local native pyOCD route for one provider-owned session."""

    def __init__(self) -> None:
        self._owned_breakpoints: dict[tuple[str, int], object] = {}

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
            _open_refusing_protected_target(session)
            self._verify_session_pack_source(session, target, pack, pdsc_device)

    @staticmethod
    def _verify_selected_probe_and_target(
        session: object, probe_uid: str | None, target: str | None
    ) -> None:
        """Check available probe/catalog evidence before target init can write.

        A probe's associated board target is catalog evidence, not a live silicon
        or package proof. Only a contradictory vendor is decisive here; aliases,
        generic targets and uncertain device variants retain unverified access.
        """

        probe = getattr(session, "probe", None)
        observed_uid = getattr(probe, "unique_id", None)
        if probe_uid is not None and not _same_probe_uid(probe_uid, observed_uid):
            raise ProbeNotFoundError(
                "The selected probe does not match the requested UID; refusing a different probe."
            )
        observed_target = getattr(getattr(probe, "associated_board_info", None), "target", None)
        if not isinstance(observed_target, str) or not target:
            return
        observed_class = TARGET.get(normalise_target_type_name(observed_target))
        requested_class = TARGET.get(normalise_target_type_name(target)) or type(
            getattr(session, "target", None)
        )
        observed_vendor = getattr(observed_class, "VENDOR", None)
        requested_vendor = getattr(requested_class, "VENDOR", None)
        generic_vendor = getattr(TARGET.get("cortex_m"), "VENDOR", None)
        if (
            isinstance(observed_vendor, str)
            and isinstance(requested_vendor, str)
            and observed_vendor
            and requested_vendor
            and observed_vendor != generic_vendor
            and requested_vendor != generic_vendor
            and observed_vendor.casefold() != requested_vendor.casefold()
        ):
            raise TargetConnectionError(
                f"Target selection mismatch: probe board catalog identifies {observed_target!r} "
                f"({observed_vendor}), but requested {target!r} uses {requested_vendor}. "
                "Select the intended probe/compatible target; target initialization was refused. "
                "This catalog contradiction is not an exact live package identity proof."
            )

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
            self._verify_selected_probe_and_target(session, probe_uid, target_override)
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
                self._verify_selected_probe_and_target(retry_session, probe_uid, target_override)
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
        token = session_metadata(handle).runtime_token
        failures: list[Exception] = []
        try:
            # pyOCD disconnect removes realized BPs but retains its pending map;
            # resume/PRE_RUN can then reinstall them. Use the ordinary verified
            # removal path first, which updates both maps before native close.
            for (owner, address), breakpoint in tuple(self._owned_breakpoints.items()):
                if owner != token:
                    continue
                try:
                    with _backend_stdout_to_stderr():
                        current = handle.session.target.find_breakpoint(address)
                    if current is breakpoint:
                        self.remove_breakpoint(handle, address)
                except Exception as exc:  # noqa: BLE001 - still close every host resource
                    failures.append(exc)
            try:
                _close_session(handle.session)
            except Exception as exc:  # noqa: BLE001 - retain removal and native close failures
                if not failures:
                    raise
                failures.append(exc)
            if failures:
                raise TargetStateError(
                    "Session-owned breakpoint cleanup is uncertain: "
                    + "; ".join(f"{type(exc).__name__}: {exc}" for exc in failures)
                    + ". Host session close was attempted; target restoration was not proven. "
                    "Reconnect/revalidate and verify breakpoint/code state before target control."
                ) from failures[0]
        finally:
            self._owned_breakpoints = {
                key: value for key, value in self._owned_breakpoints.items() if key[0] != token
            }

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
        # Reset-connect is action/profile-bound. Unlike ordinary open(), it
        # must not inherit ambient launch overrides when an input is omitted.
        probe_uid = unique_id
        target_override = target or (board.pyocd_target if board else None)
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
        try:
            self._verify_selected_probe_and_target(session, probe_uid, target_override)
        except Exception:
            _close_session(session)
            raise
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
                _open_refusing_protected_target(session)
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

    def step(self, handle: TargetSessionHandle) -> int:
        try:
            with _backend_stdout_to_stderr():
                target = handle.session.target
                core = target.selected_core_or_raise
                saved = core.read32(CortexM.DHCSR)
                if not (saved & CortexM.C_DEBUGEN and saved & CortexM.S_HALT):
                    raise TargetStateError("Incomplete step: core must be halted with debug enabled.")
                pc = target.read_core_register("pc") & ~1
                breakpoint = target.find_breakpoint(pc)
                key = (session_metadata(handle).runtime_token, pc)
                if breakpoint is not None and (
                    self._owned_breakpoints.get(key) is not breakpoint or not breakpoint.enabled
                ):
                    raise TargetStateError(
                        f"Cannot step at 0x{pc:08X}: this session does not own the installed "
                        "breakpoint. Have its owner remove it before retrying."
                    )
                with _suspend_step_breakpoint(target, breakpoint):
                    try:
                        # Establish pyOCD's default interrupt mask ourselves. Its
                        # step() then has no mask change to undo by forcing C_HALT
                        # on timeout. We can observe the actual step stop before
                        # restoring control in finally. S_RETIRE_ST is not a
                        # portable per-instruction oracle (Arm DDI0403 C1.6.2).
                        core.write32(
                            CortexM.DHCSR, CortexM.DBGKEY | CortexM.C_DEBUGEN
                            | CortexM.C_HALT | CortexM.C_MASKINTS | (saved & CortexM.C_PMOV),
                        )
                        target.flush()
                        if not core.read32(CortexM.DHCSR) & CortexM.C_MASKINTS:
                            raise TargetStateError("Incomplete step: interrupt masking was not established.")
                        core.clear_debug_cause_bits()
                        target.flush()
                        if core.read32(CortexM.DFSR) != 0:
                            raise TargetStateError("Incomplete step: stale debug causes could not be cleared.")
                        target.step()
                        state = target.get_state()
                        cause = core.read32(CortexM.DFSR)
                        pc_after = target.read_core_register("pc") if state == Target.State.HALTED else None
                        if not (state == Target.State.HALTED and cause == CortexM.DFSR_HALTED):
                            pc_text = f"0x{pc_after:08X}" if pc_after is not None else "unavailable"
                            raise TargetStateError(
                                f"Incomplete step: state={state.name} pc={pc_text} "
                                f"step_halt_confirmed=false dfsr=0x{cause:X}. "
                                "Inspect target state before retrying."
                            )
                    finally:
                        primary = sys.exc_info()[1]
                        try:
                            _restore_step_control(target, saved)
                        except Exception as exc:  # noqa: BLE001 - retain both failures
                            raise TargetStateError(
                                f"Step control restoration is uncertain: {exc}."
                                + (f" Original step error: {primary}" if primary else "")
                            ) from exc
                return cast(int, pc_after)
        except TargetControlError:
            raise
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

    def check_flash_support(
        self,
        handle: TargetSessionHandle,
        firmware: Path,
        *,
        base_address: int | None = None,
        artifact_sha256: str | None = None,
    ) -> None:
        with snapshot_flash_artifact(firmware, expected_sha256=artifact_sha256) as snapshot:
            image = derive_flash_image(snapshot, base_address=base_address)
        with _backend_stdout_to_stderr():
            _require_flash_support(handle.session.target, image)

    def flash(
        self,
        handle: TargetSessionHandle,
        firmware: Path,
        *,
        halt_after_reset: bool,
        base_address: int | None = None,
        artifact_sha256: str | None = None,
    ) -> FlashResult:
        # One snapshot supplies the expected bytes and is the file programmed, so a
        # rebuild of ``firmware`` mid-flash cannot add bytes that are never checked.
        with snapshot_flash_artifact(firmware, expected_sha256=artifact_sha256) as snapshot:
            # Derive the expected bytes and refuse any addressed byte without a live
            # flash algorithm before the first target effect. These typed refusals
            # deliberately stay outside the backend error mapping below.
            image = derive_flash_image(snapshot, base_address=base_address)
            target = handle.session.target
            with _backend_stdout_to_stderr():
                _require_flash_support(target, image)

            # Match `pyocd load`'s proven pre-reset sequence. On STM32/ST-Link, skipping
            # this can make the Python API flash path fail even though the CLI succeeds.
            try:
                with _backend_stdout_to_stderr():
                    target.reset_and_halt()
                    # Containment pre-computes and validates every implied erase sector.
                    # Force pyOCD to honor that sector scope even when a host/session
                    # option would otherwise select auto or whole-chip erase.
                    programmer = FileProgrammer(handle.session, chip_erase="sector")
                    if base_address is None:
                        programmer.program(str(snapshot))
                    else:
                        # A raw .bin is programmed only at its explicit, validated base.
                        programmer.program(str(snapshot), base_address=base_address)
            except TransferError as exc:
                # A pre-program reset or programming transfer failure is a real failure.
                raise _typed_backend_error(exc) from exc
            except Exception as exc:  # noqa: BLE001 - preserve backend context
                raise _typed_backend_error(exc) from exc
        # FileProgrammer returns normally even when it skipped data, so only matching
        # readback of every addressed byte, before the final reset, proves programming.
        with _backend_stdout_to_stderr():
            try:
                verify_flash_image(image, target.read_memory_block8)
            except FlashVerificationError as exc:
                # Only the oracle's direct native read cause establishes transport
                # loss. Mismatch, short bytes and nontransport refusals stay live.
                if isinstance(
                    exc.__cause__, (TransferError, ProbeError, OSError, TargetConnectionError)
                ):
                    raise FlashReadbackConnectionError(str(exc)) from exc
                raise
        try:
            with _backend_stdout_to_stderr():
                if halt_after_reset:
                    target.reset_and_halt()
                else:
                    target.reset()
        except TransferError:
            # `pyocd load` tolerates a transient transport drop during final reset,
            # but it cannot prove the target's resulting execution state.
            return FlashResult.for_image(image, "reset_state_unconfirmed")
        except Exception:
            # Programming is already verified. A final-reset failure leaves only
            # execution state unknown, irrespective of backend exception type.
            return FlashResult.for_image(image, "reset_state_unconfirmed")
        try:
            state = str(target.get_state().name).casefold()
        except Exception:  # final reset completed but its state cannot be observed
            return FlashResult.for_image(image, "reset_state_unconfirmed")
        if state in {"running", "halted"}:
            return FlashResult.for_image(image, state)
        return FlashResult.for_image(image, "reset_state_unconfirmed")

    def recover(self, handle: TargetSessionHandle) -> None:
        try:
            with _backend_stdout_to_stderr():
                result = handle.session.target.mass_erase()
        except Exception as exc:  # noqa: BLE001 - preserve backend context
            raise _typed_backend_error(exc) from exc
        # FlashEraser's MASS wrapper discards this result. Some targets return
        # None on success, while others explicitly return False on failure.
        if result is False:
            raise TargetControlError(
                "pyOCD target mass erase reported failure; completion is unconfirmed. "
                "Reconnect and run board_validate before further use."
            )

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
                if existing is not None and bool(getattr(existing, "enabled", False)):
                    return
                core = target.selected_core_or_raise
                memory_map = getattr(core, "memory_map", None)
                region = memory_map.get_region_for_address(address) if memory_map else None
                # Only positively identified writable RAM permits software patches.
                # Flash may be marked writable by pyOCD; unknown geometry is not RAM.
                hardware_only = not (
                    region is not None
                    and region.is_ram
                    and not region.is_flash
                    and region.is_writable
                    and region.contains_range(address, length=2)
                )
                if existing is not None:
                    # find_breakpoint() also returns queued UnrealizedBreakpoint
                    # entries. Only an enabled provider object proves an already
                    # installed breakpoint. Flush a pre-existing queued request,
                    # but never claim ownership of or roll back that request.
                    if hardware_only and _breakpoint_kind(existing) != "hw":
                        raise TargetStateError(
                            f"Pre-existing queued breakpoint at 0x{address:08X} is not "
                            "explicitly hardware; it was not flushed or removed because this "
                            "call does not own it. Have its owner cancel the request before "
                            "retrying; no code-memory write was requested by this call."
                        )
                if hardware_only:
                    fpb = getattr(core, "fpb", None)
                    # Explicit HW alone is insufficient: pyOCD can still select SW
                    # outside the FPB's supported range, or when no FPB is present.
                    if fpb is None or not fpb.can_support_address(address):
                        raise TargetControlError(
                            f"Hardware breakpoints are unavailable or unsupported at "
                            f"0x{address:08X}. No code-memory write was attempted; choose an "
                            "address supported by the target's hardware breakpoint unit."
                        )
                    if fpb.available_breakpoints <= 0:
                        raise TargetControlError(
                            f"Hardware breakpoint capacity exhausted at 0x{address:08X}. "
                            "No code-memory write was attempted; remove an owned hardware "
                            "breakpoint before retrying."
                        )
                if existing is not None:
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
                accepted = target.set_breakpoint(
                    address, Target.BreakpointType.HW if hardware_only else Target.BreakpointType.AUTO
                )
            except TargetControlError:
                raise
            except Exception as exc:  # noqa: BLE001 - preserve backend context
                raise _typed_backend_error(exc) from exc
            if not accepted:
                raise TargetControlError(
                    f"pyOCD could not allocate a {'hardware ' if hardware_only else ''}"
                    f"breakpoint at 0x{address:08X}. "
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
                        "provider state could not be proven."
                    )
                target.flush()
                if _breakpoint_kind(realized) == "sw":
                    observed, error = _read_unfiltered_instruction16(target, address)
                    if error is not None or observed != 0xBE00:
                        detail = error or f"raw instruction is 0x{observed:04X}, expected 0xBE00"
                        raise TargetStateError(
                            f"Software breakpoint installation at 0x{address:08X} could not be "
                            f"proven in physical code memory ({detail})."
                        )
                self._owned_breakpoints[(session_metadata(handle).runtime_token, address)] = realized
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
                if realized is None:
                    rollback_failures.append(
                        "no realized provider object is available for physical cleanup verification"
                    )

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
                        f"{_breakpoint_recovery_instruction(realized, hardware_only=hardware_only)}"
                    ) from primary
                restored = (
                    "the saved original instruction was read back physically"
                    if _breakpoint_kind(realized) == "sw"
                    else "the hardware comparator was read back clear"
                )
                error = (
                    primary if isinstance(primary, TargetControlError) else _typed_backend_error(primary)
                )
                raise type(error)(
                    f"{error} Rollback verified: breakpoint absent and {restored}. "
                    "This failure does not require reflashing."
                ) from primary

    def remove_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        with _backend_stdout_to_stderr():
            address &= ~1
            target = handle.session.target
            try:
                realized = target.find_breakpoint(address)
            except Exception as exc:  # noqa: BLE001 - no removal was requested yet
                raise _typed_backend_error(exc) from exc
            try:
                self._owned_breakpoints.pop((session_metadata(handle).runtime_token, address), None)
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
