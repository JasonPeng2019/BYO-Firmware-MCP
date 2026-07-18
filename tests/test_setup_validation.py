from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyocd_debug_mcp.firmstore.cache import AttachmentCache
from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.setup_flow.validate import (
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


def repository(tmp_path: Path, *, uart: bool = True, silicon: bool = True) -> ProfileRepository:
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
    optional: dict[str, object] = {"test_read_address": 0x08000000}
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
        observed_identity: str,
        fingerprint: str,
    ) -> bool:
        assert board == "bench_board"
        assert hardware_result in {
            "validation_passed",
            "validation_passed_uart_not_configured",
        }
        assert (probe_id, probe, observed_identity, fingerprint) == (
            "probe-a",
            "PROBE-001",
            "0x10016400",
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
        ("uart_not_configured", "validation_passed"),
        ("needs_input", "validation_needs_user_input"),
        ("research", "validation_passed"),
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
        silicon=True,
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
        "connect",
        "read_memory",
        "close",
    ]
    assert hook_events == ["load_layer0", "stamp"]
    assert all(
        call[0] not in {"write", "flash", "erase", "reset", "recover"}
        for call in backend.calls
    )


def test_missing_generic_test_read_does_not_block_identity_validation(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    current = profiles.load("bench_board", include_legacy=False)
    document = current.to_document()
    document.pop("test_read_address", None)
    profiles.store.atomic_write_yaml(
        profiles.store.layout.board_profile("bench_board"), document
    )
    backend = FakeBackend()

    result = validator(tmp_path, profiles, backend, hooks=open_hooks()).validate(
        ValidationRequest("bench_board")
    )

    assert result.status == "validation_passed"
    reads = [call for call in backend.calls if call[0] == "read_memory"]
    assert reads == [("read_memory", 0xE0042000, 32, 5.0)]


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
    assert "ask whether the intended board is attached" in result.agent_prompt


def test_missing_safety_map_runs_hardware_but_never_stamps(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    backend = FakeBackend()

    result = validator(tmp_path, profiles, backend).validate(ValidationRequest("bench_board"))

    assert result.status == "validation_incomplete"
    assert result.code == "validation/safety-missing"
    assert result.observed["hardware_result"] == "validation_passed"
    assert all(step.number != 9 for step in result.steps)


def test_lean_validation_does_not_mutate_attachment_cache(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    backend = FakeBackend()
    cache = AttachmentCache(FirmStore(tmp_path))

    validator(tmp_path, profiles, backend, hooks=open_hooks(), cache=cache).validate(
        ValidationRequest("bench_board")
    )

    records = cache.load_records()
    assert records == []


def test_validation_choice_retry_preserves_prior_selector_through_both_ambiguities(
    tmp_path: Path,
) -> None:
    profiles = repository(tmp_path)
    backend = FakeBackend()
    backend.inventory_value = ValidationInventory(
        probes=(
            ValidationProbe("probe-a", "First probe", "stlink", "PROBE-001"),
            ValidationProbe("probe-b", "Second probe", "stlink", "PROBE-B"),
        ),
        serial_ports=(
            ValidationSerial("serial-a", "COM7", "First UART", "UART-A"),
            ValidationSerial("serial-b", "COM8", "Second UART", "UART-B"),
        ),
    )
    service = validator(tmp_path, profiles, backend, hooks=open_hooks())

    choose_probe = service.validate(ValidationRequest("bench_board"))
    assert choose_probe.status == "validation_needs_user_input"
    assert choose_probe.to_payload()["accepted_response"] == {
        "tool": "board_validate",
        "arguments": {
            "board_id": "bench_board",
            "probe_id": "<one choice_id from choices>",
        },
    }

    choose_serial = service.validate(ValidationRequest("bench_board", probe_id="probe-a"))
    assert choose_serial.status == "validation_passed"
    assert choose_serial.to_payload()["accepted_response"] is None

    passed = service.validate(
        ValidationRequest("bench_board", probe_id="probe-a", serial_id="serial-a")
    )
    assert passed.status == "validation_passed"
    assert passed.to_payload()["accepted_response"] is None


def test_probe_retry_preserves_a_preselected_serial_identity(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    backend = FakeBackend()
    backend.inventory_value = ValidationInventory(
        probes=(
            ValidationProbe("probe-a", "First probe", "stlink", "PROBE-A"),
            ValidationProbe("probe-b", "Second probe", "stlink", "PROBE-B"),
        ),
        serial_ports=(ValidationSerial("serial-a", "COM7", "UART", "UART-A"),),
    )

    result = validator(tmp_path, profiles, backend, hooks=open_hooks()).validate(
        ValidationRequest("bench_board", serial_id="serial-a")
    )

    assert result.to_payload()["accepted_response"] == {
        "tool": "board_validate",
        "arguments": {
            "board_id": "bench_board",
            "serial_id": "serial-a",
            "probe_id": "<one choice_id from choices>",
        },
    }
