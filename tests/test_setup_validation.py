from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyocd_debug_mcp.firmstore.cache import AttachmentCache
from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.setup_flow.validate import (
    MAX_SERIAL_CAPTURE_BYTES,
    BoardValidator,
    Layer0Snapshot,
    ValidationBackend,
    ValidationHooks,
    ValidationInventory,
    ValidationProbe,
    ValidationRequest,
    ValidationSerial,
)


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.inventory_value = ValidationInventory(
            probes=(ValidationProbe("probe-a", "ST-Link", "stlink", "PROBE-001"),),
            serial_ports=(
                ValidationSerial(
                    "serial-a",
                    "COM7",
                    "ST-Link UART",
                    "UART-001",
                    0x0483,
                    0x5740,
                ),
            ),
        )
        self.support: bool | None = True
        self.values: dict[int, int] = {0x08000000: 0x12345678, 0xE0042000: 0x10016415}
        self.serial_text = "boot ready\n"

    def inventory(self) -> ValidationInventory:
        self.calls.append(("inventory",))
        return self.inventory_value

    def target_supported(self, target: str) -> bool | None:
        self.calls.append(("target_supported", target))
        return self.support

    def connect(self, profile, probe: ValidationProbe, timeout: float) -> object:
        self.calls.append(("connect", profile.board_id, probe.probe_id, timeout))
        return object()

    def read_memory(self, connection: object, address: int, width: int, timeout: float) -> int:
        del connection
        self.calls.append(("read_memory", address, width, timeout))
        return self.values[address]

    def capture_serial(
        self, serial: ValidationSerial, baudrate: int, duration: float, max_bytes: int
    ) -> str:
        self.calls.append(
            ("capture_serial", serial.serial_id, baudrate, duration, max_bytes)
        )
        return self.serial_text

    def close(self, connection: object) -> None:
        del connection
        self.calls.append(("close",))

    def services(self) -> ValidationBackend:
        return ValidationBackend(
            self.inventory,
            self.target_supported,
            self.connect,
            self.read_memory,
            self.capture_serial,
            self.close,
        )


def repository(tmp_path: Path, *, uart: bool = True, silicon: bool = False) -> ProfileRepository:
    store = FirmStore(tmp_path)
    legacy = tmp_path / "boards"
    legacy.mkdir(exist_ok=True)
    profiles = ProfileRepository(store, legacy_board_dir=legacy)
    profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": "bench_board",
                "display_name": "Bench Board",
                "mcu_part_number": "STM32L476RGT6-Exact",
                "mcu_family": "stm32l4",
                "probe_family": "stlink",
                "pyocd_target": "stm32l476rgtx",
            }
        )
    )
    optional: dict[str, object] = {}
    if uart:
        optional["expected_uart_substring"] = "boot ready"
    if silicon:
        optional.update(
            {
                "silicon_id_address": 0xE0042000,
                "silicon_id_expected": 0x10016400,
                "silicon_id_mask": 0xFFFFFF00,
                "silicon_id_width_bits": 32,
                "silicon_id_label": "device id",
            }
        )
    if optional:
        profiles.commit_optional(profiles.stage_optional("bench_board", optional))
    return profiles


def open_hooks(events: list[str] | None = None) -> ValidationHooks:
    def load(_profile):
        if events is not None:
            events.append("load_layer0")
        return Layer0Snapshot(True, True, "fingerprint-1")

    def stamp(
        board: str,
        hardware_result: str,
        probe_id: str,
        probe: str | None,
        fingerprint: str,
    ) -> bool:
        assert board == "bench_board"
        assert hardware_result in {
            "validation_passed",
            "validation_passed_uart_not_configured",
        }
        assert (probe_id, probe, fingerprint) == (
            "probe-a",
            "PROBE-001",
            "fingerprint-1",
        )
        if events is not None:
            events.append("stamp")
        return True

    return ValidationHooks(load, stamp)


def validator(
    tmp_path: Path,
    profiles: ProfileRepository,
    backend: FakeBackend,
    *,
    hooks: ValidationHooks | None = None,
    cache: AttachmentCache | None = None,
) -> BoardValidator:
    return BoardValidator(
        profiles,
        ReportWriter(FirmStore(tmp_path)),
        backend.services(),
        hooks=hooks,
        cache=cache,
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("passed", "validation_passed"),
        ("uart_not_configured", "validation_passed_uart_not_configured"),
        ("needs_input", "validation_needs_user_input"),
        ("research", "validation_research_required"),
        ("blocked", "validation_blocked"),
        ("failed", "validation_failed"),
        ("incomplete", "validation_incomplete"),
    ],
)
def test_exact_seven_status_vocabulary(tmp_path: Path, case: str, expected: str) -> None:
    case_root = tmp_path / case
    case_root.mkdir()
    profiles = repository(
        case_root,
        uart=case != "uart_not_configured",
        silicon=case == "failed",
    )
    backend = FakeBackend()
    hooks = open_hooks()
    if case == "needs_input":
        backend.inventory_value = ValidationInventory(
            probes=(
                ValidationProbe("probe-a", "First", "stlink", "A"),
                ValidationProbe("probe-b", "Second", "stlink", "B"),
            )
        )
    elif case == "research":
        backend.support = None
    elif case == "blocked":
        backend.inventory_value = ValidationInventory()
    elif case == "failed":
        backend.values[0xE0042000] = 0xDEADBEEF
    elif case == "incomplete":
        hooks = ValidationHooks.closed_placeholders()

    result = validator(case_root, profiles, backend, hooks=hooks).validate(
        ValidationRequest("bench_board")
    )

    assert result.status == expected
    assert result.to_payload()["code"] == result.code
    assert result.status in {
        "validation_passed",
        "validation_passed_uart_not_configured",
        "validation_needs_user_input",
        "validation_research_required",
        "validation_blocked",
        "validation_failed",
        "validation_incomplete",
    }
    assert result.report_paths.report.exists()
    assert result.report_paths.events.exists()
    report = json.loads(result.report_paths.report.read_text(encoding="utf-8"))
    assert report["terminal_status"] == expected


def test_validation_backend_call_order_is_bounded_and_non_destructive(tmp_path: Path) -> None:
    profiles = repository(tmp_path, silicon=True)
    backend = FakeBackend()
    hook_events: list[str] = []

    result = validator(
        tmp_path, profiles, backend, hooks=open_hooks(hook_events)
    ).validate(ValidationRequest("bench_board"))

    assert result.status == "validation_passed"
    assert [call[0] for call in backend.calls] == [
        "inventory",
        "target_supported",
        "connect",
        "read_memory",
        "read_memory",
        "capture_serial",
        "close",
    ]
    capture = next(call for call in backend.calls if call[0] == "capture_serial")
    assert capture[3:] == (3.0, MAX_SERIAL_CAPTURE_BYTES)
    assert hook_events == ["load_layer0", "stamp"]
    assert all(call[0] not in {"write", "flash", "erase", "reset", "recover"} for call in backend.calls)


def test_silicon_mismatch_does_not_mutate_profile_and_only_offers_assignment_remedy(
    tmp_path: Path,
) -> None:
    profiles = repository(tmp_path, silicon=True)
    path = profiles.store.layout.board_profile("bench_board")
    before = path.read_bytes()
    backend = FakeBackend()
    backend.values[0xE0042000] = 0xFFFFFFFF

    result = validator(tmp_path, profiles, backend, hooks=open_hooks()).validate(
        ValidationRequest("bench_board")
    )

    assert result.status == "validation_failed"
    assert result.code == "validation/silicon-mismatch"
    assert path.read_bytes() == before
    assert "Correct the physical assignment or attach the intended board" in result.agent_prompt


def test_missing_safety_map_runs_hardware_but_never_stamps(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    backend = FakeBackend()

    result = validator(tmp_path, profiles, backend).validate(ValidationRequest("bench_board"))

    assert result.status == "validation_incomplete"
    assert result.code == "validation/safety-missing"
    assert result.observed["hardware_result"] == "validation_passed"
    assert all(step.number != 9 for step in result.steps)


def test_successful_hardware_validation_confirms_stable_attachment_cache(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    backend = FakeBackend()
    cache = AttachmentCache(FirmStore(tmp_path))

    validator(tmp_path, profiles, backend, hooks=open_hooks(), cache=cache).validate(
        ValidationRequest("bench_board")
    )

    records = cache.load_records()
    assert len(records) == 1
    assert records[0].board_id == "bench_board"
    assert records[0].probe_usb_serial == "PROBE-001"
