from __future__ import annotations

from pathlib import Path

import pytest

from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import BoardConfig, RECOVER_MODE_MANUAL_ONLY
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.target_errors import (
    ProbeNotFoundError,
    TargetConnectionError,
    UnsupportedArtifactError,
)
from pyocd_debug_mcp.timeouts import (
    PYOCD_CORE_RECOVER_TIMEOUT_SECONDS,
    PYOCD_DAP_RECOVER_TIMEOUT_SECONDS,
    PYOCD_FLASH_ANALYZER_TIMEOUT_SECONDS,
    PYOCD_FLASH_ERASE_ALL_TIMEOUT_SECONDS,
    PYOCD_FLASH_ERASE_SECTOR_TIMEOUT_SECONDS,
    PYOCD_FLASH_INIT_TIMEOUT_SECONDS,
    PYOCD_FLASH_PROGRAM_TIMEOUT_SECONDS,
    PYOCD_RESET_HALT_TIMEOUT_SECONDS,
    PYOCD_STEP_TIMEOUT_SECONDS,
)


class FakeTarget:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def reset_and_halt(self) -> None:
        self._calls.append("reset_and_halt")

    def reset(self) -> None:
        self._calls.append("reset")


class FakeSession:
    def __init__(self, target: FakeTarget) -> None:
        self.target = target


def default_pyocd_timeout_options() -> dict[str, object]:
    return {
        "cpu.step.instruction.timeout": PYOCD_STEP_TIMEOUT_SECONDS,
        "reset.halt_timeout": PYOCD_RESET_HALT_TIMEOUT_SECONDS,
        "reset.dap_recover.timeout": PYOCD_DAP_RECOVER_TIMEOUT_SECONDS,
        "reset.core_recover.timeout": PYOCD_CORE_RECOVER_TIMEOUT_SECONDS,
        "flash.timeout.init": PYOCD_FLASH_INIT_TIMEOUT_SECONDS,
        "flash.timeout.program": PYOCD_FLASH_PROGRAM_TIMEOUT_SECONDS,
        "flash.timeout.erase_sector": PYOCD_FLASH_ERASE_SECTOR_TIMEOUT_SECONDS,
        "flash.timeout.erase_all": PYOCD_FLASH_ERASE_ALL_TIMEOUT_SECONDS,
        "flash.timeout.analyzer": PYOCD_FLASH_ANALYZER_TIMEOUT_SECONDS,
    }


def test_build_session_options_keeps_jlink_workaround() -> None:
    jlink_board = BoardConfig(
        board_id="tmp_jlink",
        display_name="Tmp J-Link",
        mcu_family="nrf52840",
        probe_family="jlink",
        pyocd_target="nrf52840",
        pack_name="nrf52840",
        probe_type="SEGGER J-Link",
        probe_hint_terms=("segger",),
        serial_hint_terms=("segger",),
        test_addr=0x10000000,
    )

    options = target_control.build_session_options(jlink_board, jlink_board.pyocd_target)

    assert options == default_pyocd_timeout_options() | {
        "target_override": "nrf52840",
        "jlink.non_interactive": False,
    }


def test_typed_backend_error_explains_missing_ap_keyerror() -> None:
    error = swd_pyocd._typed_backend_error(KeyError(1))

    assert isinstance(error, TargetConnectionError)
    assert "AP#1" in str(error)
    assert "nRF52" in str(error)


def test_build_session_options_adds_nucleo_under_reset_workaround() -> None:
    stlink_board = BoardConfig(
        board_id="nucleo_l476rg",
        display_name="Nucleo-L476RG",
        mcu_family="stm32l476",
        probe_family="stlink",
        pyocd_target="stm32l476rgtx",
        pack_name="stm32l476",
        probe_type="ST-Link",
        probe_hint_terms=("stlink",),
        serial_hint_terms=("stlink",),
        test_addr=0x08000000,
    )

    options = target_control.build_session_options(stlink_board, stlink_board.pyocd_target)

    assert options == default_pyocd_timeout_options() | {
        "target_override": "stm32l476rgtx",
        "connect_mode": "under-reset",
        "frequency": 1_000_000,
    }


def test_pyocd_flash_matches_cli_reset_sequence(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    target = FakeTarget(calls)
    session = FakeSession(target)
    handle = TargetSessionHandle(
        session=session,  # type: ignore[arg-type]
        board=None,
        probe_uid="probe-1",
        route_used=swd_pyocd.ROUTE_PYOCD_NATIVE,
        target_override="stm32l476rgtx",
    )
    firmware = tmp_path / "firmware.elf"
    firmware.write_text("placeholder", encoding="utf-8")

    class FakeProgrammer:
        def __init__(self, provided_session) -> None:
            assert provided_session is session

        def program(self, path: str) -> None:
            calls.append(f"program:{Path(path).name}")

    monkeypatch.setattr(swd_pyocd, "FileProgrammer", FakeProgrammer)

    adapter = swd_pyocd.PyOCDSWDInterface()
    adapter.flash(handle, firmware, halt_after_reset=False)

    assert calls == [
        "reset_and_halt",
        "program:firmware.elf",
        "reset",
    ]


def test_pyocd_flash_can_leave_target_halted(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    target = FakeTarget(calls)
    session = FakeSession(target)
    handle = TargetSessionHandle(
        session=session,  # type: ignore[arg-type]
        board=None,
        probe_uid="probe-1",
        route_used=swd_pyocd.ROUTE_PYOCD_NATIVE,
        target_override="stm32l476rgtx",
    )
    firmware = tmp_path / "firmware.hex"
    firmware.write_text("placeholder", encoding="utf-8")

    class FakeProgrammer:
        def __init__(self, provided_session) -> None:
            assert provided_session is session

        def program(self, path: str) -> None:
            calls.append(f"program:{Path(path).name}")

    monkeypatch.setattr(swd_pyocd, "FileProgrammer", FakeProgrammer)

    adapter = swd_pyocd.PyOCDSWDInterface()
    adapter.flash(handle, firmware, halt_after_reset=True)

    assert calls == [
        "reset_and_halt",
        "program:firmware.hex",
        "reset_and_halt",
    ]


def test_pyocd_flash_suppresses_backend_stdout_progress(
    monkeypatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    target = FakeTarget(calls)
    session = FakeSession(target)
    handle = TargetSessionHandle(
        session=session,  # type: ignore[arg-type]
        board=None,
        probe_uid="probe-1",
        route_used=swd_pyocd.ROUTE_PYOCD_NATIVE,
        target_override="stm32l476rgtx",
    )
    firmware = tmp_path / "firmware.elf"
    firmware.write_text("placeholder", encoding="utf-8")

    class FakeProgrammer:
        def __init__(self, provided_session) -> None:
            assert provided_session is session

        def program(self, path: str) -> None:
            print("[---|---|---|---|---|---|---|---|---|----]")
            print("[========================================]")
            calls.append(f"program:{Path(path).name}")

    monkeypatch.setattr(swd_pyocd, "FileProgrammer", FakeProgrammer)

    adapter = swd_pyocd.PyOCDSWDInterface()
    adapter.flash(handle, firmware, halt_after_reset=False)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert calls == [
        "reset_and_halt",
        "program:firmware.elf",
        "reset",
    ]


def test_recover_target_rejects_manual_only_board() -> None:
    manual_only_board = BoardConfig(
        board_id="tmp_manual",
        display_name="Tmp Manual",
        mcu_family="stm32f4",
        probe_family="stlink",
        pyocd_target="stm32f4x",
        pack_name="stm32f4x",
        probe_type="ST-Link",
        probe_hint_terms=("stlink",),
        serial_hint_terms=("stlink",),
        test_addr=0x08000000,
        requires_recover_validation=True,
        recover_mode=RECOVER_MODE_MANUAL_ONLY,
    )
    handle = TargetSessionHandle(
        session=object(),  # type: ignore[arg-type]
        board=manual_only_board,
        probe_uid="probe-1",
        route_used="pyocd-native",
        target_override="stm32f4x",
    )

    with pytest.raises(RuntimeError, match="manual_only"):
        target_control.recover_target(handle)


def test_adapter_open_raises_probe_not_found_when_no_probe_matches(monkeypatch) -> None:
    monkeypatch.setattr(
        swd_pyocd.ConnectHelper,
        "session_with_chosen_probe",
        staticmethod(lambda **kwargs: None),
    )

    adapter = swd_pyocd.PyOCDSWDInterface()
    with pytest.raises(ProbeNotFoundError, match="No matching debug probe found"):
        adapter.open(board=None, unique_id="missing", target="stm32l476rgtx")


def test_adapter_open_retries_jlink_uidless_after_known_serial_open_failure(monkeypatch) -> None:
    board = BoardConfig(
        board_id="nrf52840dk",
        display_name="nRF52840-DK",
        mcu_family="nrf52840",
        probe_family="jlink",
        pyocd_target="nrf52840",
        pack_name="nrf52840",
        probe_type="SEGGER J-Link",
        probe_hint_terms=("segger",),
        serial_hint_terms=("segger",),
        test_addr=0x10000000,
    )

    class FakeProbe:
        unique_id = "1050263657"

    class FakeBoard:
        name = "Nordic nRF52840 DK"

    class FailingSession:
        def __init__(self) -> None:
            self.probe = FakeProbe()
            self.board = FakeBoard()

        def open(self) -> None:
            raise RuntimeError("No emulator with serial number 1050263657 found.")

        def close(self) -> None:
            return None

    class PassingSession:
        def __init__(self) -> None:
            self.probe = FakeProbe()
            self.board = FakeBoard()

        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

    calls: list[dict[str, object]] = []
    sessions = [FailingSession(), PassingSession()]

    def fake_choose_probe(**kwargs):
        calls.append(dict(kwargs))
        return sessions.pop(0)

    monkeypatch.setattr(
        swd_pyocd.ConnectHelper,
        "session_with_chosen_probe",
        staticmethod(fake_choose_probe),
    )
    # Keep this test hermetic: neutralize locally-provisioned pack discovery so the
    # asserted backend options don't depend on what's in the repo packs/ dir.
    monkeypatch.setattr(swd_pyocd, "discover_local_packs", lambda *a, **k: [])
    monkeypatch.setattr(
        swd_pyocd,
        "list_connected_probes",
        lambda run_cmd: [
            type(
                "ProbeRow",
                (),
                {
                    "uid": "1050263657",
                    "description": "SEGGER J-Link",
                    "raw": "0  SEGGER J-Link  1050263657  ok",
                    "searchable_text": "1050263657 segger j-link",
                },
            )(),
            type(
                "ProbeRow",
                (),
                {
                    "uid": "0668FF514988525067213913",
                    "description": "ST-Link",
                    "raw": "1  ST-Link  0668FF514988525067213913  ok",
                    "searchable_text": "0668ff514988525067213913 st-link stm32",
                },
            )(),
        ],
    )

    adapter = swd_pyocd.PyOCDSWDInterface()
    handle = adapter.open(board=board, unique_id="1050263657", target="nrf52840")

    assert handle.probe_uid == "1050263657"
    assert calls == [
        {
            "blocking": False,
            "return_first": True,
            "unique_id": "1050263657",
            "auto_open": False,
            "options": default_pyocd_timeout_options()
            | {
                "target_override": "nrf52840",
                "jlink.non_interactive": False,
            },
        },
        {
            "blocking": False,
            "return_first": True,
            "unique_id": None,
            "auto_open": False,
            "options": default_pyocd_timeout_options()
            | {
                "target_override": "nrf52840",
                "jlink.non_interactive": False,
            },
        },
    ]


def test_run_cmd_returns_timeout_code(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise swd_pyocd.subprocess.TimeoutExpired(cmd=kwargs.get("args", "pyocd"), timeout=3)

    monkeypatch.setattr(swd_pyocd.subprocess, "run", fake_run)

    rc, out, err = swd_pyocd._run_cmd(["pyocd", "list"], timeout_seconds=3)

    assert rc == 124
    assert out == ""
    assert "command timed out after 3s" in err


def test_adapter_read_memory_raises_typed_connection_error() -> None:
    class FailingTarget:
        def read_memory(self, address: int, width_bits: int) -> int:
            raise RuntimeError("unable to connect")

    handle = TargetSessionHandle(
        session=FakeSession(FailingTarget()),  # type: ignore[arg-type]
        board=None,
        probe_uid="probe-1",
        route_used=swd_pyocd.ROUTE_PYOCD_NATIVE,
        target_override="stm32l476rgtx",
    )

    adapter = swd_pyocd.PyOCDSWDInterface()
    with pytest.raises(TargetConnectionError, match="unable to connect"):
        adapter.read_memory(handle, 0x08000000, 32)


def test_pyocd_flash_rejects_unsupported_artifact_suffix(tmp_path: Path) -> None:
    calls: list[str] = []
    target = FakeTarget(calls)
    session = FakeSession(target)
    handle = TargetSessionHandle(
        session=session,  # type: ignore[arg-type]
        board=None,
        probe_uid="probe-1",
        route_used=swd_pyocd.ROUTE_PYOCD_NATIVE,
        target_override="stm32l476rgtx",
    )
    firmware = tmp_path / "firmware.bin"
    firmware.write_text("placeholder", encoding="utf-8")

    adapter = swd_pyocd.PyOCDSWDInterface()
    with pytest.raises(UnsupportedArtifactError, match="Unsupported artifact type"):
        adapter.flash(handle, firmware, halt_after_reset=False)
