"""Regression coverage for reviewed-policy acceptance of connected deployment HEX data."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pyocd_debug_mcp.safety.enforce import LoadedSafetyMap, SafetyPolicy
from pyocd_debug_mcp.safety.linker import (
    BuildArtifactSelection,
    BuildRole,
    LoadableSegment,
    extract_build_evidence,
)
from pyocd_debug_mcp.safety.regions import ActionCategory, AddressRange, Allowed


_BASE = 0x4000
_ELF_IMAGE = bytes(
    (0x00, 0x10, 0x00, 0x20, 0x09, 0x40, 0x00, 0x00, 0x41, 0x42, 0x00, 0x44, 0x45, 0x46, 0x47, 0x48)
)


def _hex_record(address: int, data: bytes) -> str:
    payload = bytes((len(data),)) + address.to_bytes(2, "big") + b"\x00" + data
    return ":" + (payload + bytes([(-sum(payload)) & 0xFF])).hex().upper()


def _parsed_elf(_path: Path):
    load_range = AddressRange(_BASE, _BASE + len(_ELF_IMAGE))
    return (
        {"__app_partition_start": _BASE, "__app_partition_end": _BASE + len(_ELF_IMAGE)},
        (LoadableSegment(0, load_range, load_range, len(_ELF_IMAGE), len(_ELF_IMAGE), True, False, True),),
        _BASE + 9,
        dict(zip(range(_BASE, _BASE + len(_ELF_IMAGE)), _ELF_IMAGE)),
    )


class _ReviewedMap:
    def __init__(self, application: AddressRange) -> None:
        self.application = application
        self.checked: list[tuple[AddressRange, ...]] = []

    def check(self, action: ActionCategory, ranges: tuple[AddressRange, ...]):
        if action is ActionCategory.FLASH_APPLICATION:
            self.checked.append(ranges)
            assert all(self.application.contains(item) for item in ranges)
        return Allowed(action, ranges, ())


def test_connected_wrapper_reaches_reviewed_policy_when_exact_content_and_erase_range_are_authorized(
    monkeypatch, tmp_path: Path
) -> None:
    elf_path = tmp_path / "deployment.elf"
    hex_path = tmp_path / "deployment.hex"
    elf_path.write_bytes(b"parsed by test fixture")
    prefix = b"\xa5"
    suffix = b"\x5a"
    hex_path.write_text(
        "\n".join(
            (
                _hex_record(_BASE - len(prefix), prefix),
                _hex_record(_BASE, _ELF_IMAGE),
                _hex_record(_BASE + len(_ELF_IMAGE), suffix),
                ":00000001FF",
            )
        ),
        encoding="ascii",
    )
    monkeypatch.setattr("pyocd_debug_mcp.safety.linker._read_elf", _parsed_elf)
    evidence = extract_build_evidence(
        BuildArtifactSelection("a23-regression", BuildRole.APPLICATION, elf_path, None, hex_path)
    )
    wrapper_range = AddressRange(_BASE - len(prefix), _BASE + len(_ELF_IMAGE) + len(suffix))
    assert evidence.hex_ranges == (wrapper_range,)

    reviewed_map = _ReviewedMap(wrapper_range)
    document = SimpleNamespace(
        identity=SimpleNamespace(pyocd_target="test-target"),
        partitions=SimpleNamespace(application=wrapper_range, bootloader=None),
        geometry=SimpleNamespace(
            erase_available=True,
            erase_sectors=(),
            erase_origin=wrapper_range.start,
            erase_size=wrapper_range.size,
        ),
    )
    policy = SafetyPolicy(SimpleNamespace())
    monkeypatch.setattr(policy, "load", lambda _board_id: LoadedSafetyMap(document, reviewed_map))
    monkeypatch.setattr(policy, "_extract_runtime_evidence", lambda _role, _path: evidence)

    assert (
        policy.check_flash(
            "test-board",
            BuildRole.APPLICATION,
            hex_path,
            current_target="test-target",
        )
        is evidence
    )
    assert any(wrapper_range in checked for checked in reviewed_map.checked)
