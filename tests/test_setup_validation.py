from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.setup_flow.validate import (
    BoardValidator,
    SafetyMapSnapshot,
    ValidationBackend,
    ValidationHooks,
    ValidationInventory,
    ValidationProbe,
    ValidationRequest,
)


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.inventory_value = ValidationInventory(
            probes=(ValidationProbe("probe-a", "ST-Link", "stlink", "PROBE-001"),)
        )
        self.support: bool | None = True
        self.values: dict[int, int] = {0xE0042000: 0x10016415}

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

    def close(self, connection: object) -> None:
        del connection
        self.calls.append(("close",))

    def services(self) -> ValidationBackend:
        return ValidationBackend(
            self.inventory,
            self.target_supported,
            self.connect,
            self.read_memory,
            lambda _serial, _baud, _duration, _bytes: "",
            self.close,
        )


def repository(tmp_path: Path, *, identity: bool = True) -> ProfileRepository:
    store = FirmStore(tmp_path)
    legacy = tmp_path / "boards"
    legacy.mkdir(exist_ok=True)
    profiles = ProfileRepository(store, legacy_board_dir=legacy)
    profiles.commit_core(
        profiles.stage_core(
            {
                "board_id": "bench_board",
                "display_name": "Bench Board",
                "mcu_part_number": "STM32L476RGT6",
                "mcu_family": "stm32l4",
                "probe_family": "stlink",
                "pyocd_target": "stm32l476rgtx",
            }
        )
    )
    optional: dict[str, object] = {}
    if identity:
        optional.update(
            {
                "silicon_id_address": 0xE0042000,
                "silicon_id_expected": 0x415,
                "silicon_id_mask": 0xFFF,
                "silicon_id_width_bits": 32,
                "silicon_id_label": "STM32 device-family identity",
            }
        )
    if optional:
        profiles.commit_optional(profiles.stage_optional("bench_board", optional))
    return profiles


def open_hooks(
    events: list[tuple[object, ...]] | None = None,
    *,
    map_present: bool = True,
    map_consistent: bool = True,
) -> ValidationHooks:
    def load(_profile):
        if events is not None:
            events.append(("load_safety_map",))
        return SafetyMapSnapshot(
            map_present,
            map_consistent,
            "map-1" if map_present and map_consistent else None,
            "map unavailable" if not map_present else "",
        )

    def stamp(
        board: str,
        validation_run: str,
        probe_id: str,
        probe: str | None,
        observed_mcu: str,
        map_digest: str,
    ) -> bool:
        assert board == "bench_board"
        assert validation_run.startswith("validation-")
        assert probe_id == "probe-a"
        assert probe is not None
        assert (observed_mcu, map_digest) == (
            "STM32 device-family identity 0x10016415",
            "map-1",
        )
        if events is not None:
            events.append(("stamp", validation_run))
        return True

    def mismatch(
        board: str,
        validation_run: str,
        probe_id: str,
        probe: str | None,
        expected_mcu: str,
        observed_mcu: str,
    ) -> bool:
        if events is not None:
            events.append(
                (
                    "mismatch",
                    board,
                    validation_run,
                    probe_id,
                    probe,
                    expected_mcu,
                    observed_mcu,
                )
            )
        return True

    return ValidationHooks(load, stamp, mismatch)


def validator(
    tmp_path: Path,
    profiles: ProfileRepository,
    backend: FakeBackend,
    *,
    hooks: ValidationHooks | None = None,
) -> BoardValidator:
    return BoardValidator(
        profiles,
        ReportWriter(FirmStore(tmp_path)),
        backend.services(),
        hooks=hooks,
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("passed", "validation_passed"),
        ("needs_input", "validation_needs_user_input"),
        ("research", "validation_research_required"),
        ("blocked", "validation_blocked"),
        ("failed", "validation_failed"),
        ("incomplete", "validation_incomplete"),
    ],
)
def test_validation_status_vocabulary(tmp_path: Path, case: str, expected: str) -> None:
    case_root = tmp_path / case
    case_root.mkdir()
    profiles = repository(case_root)
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


def test_validation_backend_call_order_is_lean_bounded_and_non_destructive(
    tmp_path: Path,
) -> None:
    profiles = repository(tmp_path)
    backend = FakeBackend()
    hook_events: list[tuple[object, ...]] = []

    result = validator(
        tmp_path, profiles, backend, hooks=open_hooks(hook_events)
    ).validate(ValidationRequest("bench_board"))

    assert result.status == "validation_passed"
    assert [call[0] for call in backend.calls] == [
        "inventory",
        "target_supported",
        "connect",
        "read_memory",
        "close",
    ]
    assert hook_events[0] == ("load_safety_map",)
    assert hook_events[1][0] == "stamp"
    assert all(
        call[0] not in {"capture_serial", "write", "flash", "erase", "reset", "recover"}
        for call in backend.calls
    )
    assert "UART readiness" in result.agent_prompt


def test_missing_live_identity_evidence_is_stamp_ineligible(tmp_path: Path) -> None:
    profiles = repository(tmp_path, identity=False)
    backend = FakeBackend()
    events: list[tuple[object, ...]] = []

    result = validator(tmp_path, profiles, backend, hooks=open_hooks(events)).validate(
        ValidationRequest("bench_board")
    )

    assert result.status == "validation_blocked"
    assert result.code == "validation/live-identity-evidence-missing"
    assert "maintainers" in result.agent_prompt
    assert "board_setup" not in result.agent_prompt
    assert not any(call[0] == "connect" for call in backend.calls)
    assert all(event[0] != "stamp" for event in events)


def test_silicon_mismatch_is_neutral_records_allowance_and_never_mutates_profile(
    tmp_path: Path,
) -> None:
    profiles = repository(tmp_path)
    path = profiles.store.layout.board_profile("bench_board")
    before = path.read_bytes()
    backend = FakeBackend()
    backend.values[0xE0042000] = 0xFFFFFFFF
    events: list[tuple[object, ...]] = []

    result = validator(tmp_path, profiles, backend, hooks=open_hooks(events)).validate(
        ValidationRequest("bench_board")
    )

    assert result.status == "validation_failed"
    assert result.code == "validation/silicon-mismatch"
    assert path.read_bytes() == before
    assert "Expected MCU STM32L476RGT6" in result.agent_prompt
    assert "observed STM32 device-family identity 0xFFFFFFFF" in result.agent_prompt
    assert "ask what they want to do" in result.agent_prompt
    assert "board_setup" not in result.agent_prompt
    mismatch = next(event for event in events if event[0] == "mismatch")
    assert mismatch[5:] == (
        "STM32L476RGT6",
        "STM32 device-family identity 0xFFFFFFFF",
    )


def test_missing_safety_map_runs_identity_check_but_never_stamps(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    backend = FakeBackend()
    events: list[tuple[object, ...]] = []

    result = validator(
        tmp_path,
        profiles,
        backend,
        hooks=open_hooks(events, map_present=False),
    ).validate(ValidationRequest("bench_board"))

    assert result.status == "validation_incomplete"
    assert result.code == "validation/safety-missing"
    assert any(call[0] == "read_memory" for call in backend.calls)
    assert all(event[0] != "stamp" for event in events)


def test_probe_choice_retry_preserves_only_probe_selector(tmp_path: Path) -> None:
    profiles = repository(tmp_path)
    backend = FakeBackend()
    backend.inventory_value = ValidationInventory(
        probes=(
            ValidationProbe("probe-a", "First probe", "stlink", "PROBE-A"),
            ValidationProbe("probe-b", "Second probe", "stlink", "PROBE-B"),
        )
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
    passed = service.validate(ValidationRequest("bench_board", probe_id="probe-a"))
    assert passed.status == "validation_passed"
    assert passed.to_payload()["accepted_response"] is None
