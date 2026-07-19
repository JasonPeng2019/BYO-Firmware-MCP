from __future__ import annotations

import pytest

from pyocd_debug_mcp.safety.regions import (
    ActionCategory,
    AddressRange,
    Allowed,
    MAX_ADDRESS_EXCLUSIVE,
    Provenance,
    Refusal,
    RegionError,
    RegionKind,
    SafetyMap,
    SafetyRegion,
    SourceAuthority,
)


PROVENANCE = (Provenance(SourceAuthority.RECONCILED, "pack+datasheet", "two-source agreement"),)


def region(
    name: str,
    kind: RegionKind,
    start: int,
    end: int,
    *,
    executable: bool = False,
) -> SafetyRegion:
    return SafetyRegion(name, kind, AddressRange(start, end), PROVENANCE, executable)


def test_half_open_range_boundaries_and_validation() -> None:
    value = AddressRange(0x1000, 0x1010)

    assert value.contains_address(0x1000)
    assert value.contains_address(0x100F)
    assert not value.contains_address(0x1010)
    assert not value.overlaps(AddressRange(0x1010, 0x1020))
    assert value.overlaps(AddressRange(0x100F, 0x1020))
    assert value.contains(AddressRange(0x1000, 0x1010))
    assert AddressRange.from_start_size(0x1000, 0x10) == value
    with pytest.raises(RegionError):
        AddressRange(1, 1)
    with pytest.raises(RegionError):
        AddressRange(-1, 1)
    with pytest.raises(RegionError):
        AddressRange.from_start_size(0, 0)
    with pytest.raises(RegionError):
        AddressRange(MAX_ADDRESS_EXCLUSIVE - 1, MAX_ADDRESS_EXCLUSIVE + 1)
    with pytest.raises(RegionError):
        AddressRange.from_start_size(MAX_ADDRESS_EXCLUSIVE - 1, 2)


def test_unknown_is_default_and_any_uncovered_byte_denies() -> None:
    safety = SafetyMap([region("RAM", RegionKind.RAM, 0x2000, 0x2100)])

    assert safety.classify(AddressRange(0x2000, 0x2100)) is RegionKind.RAM
    assert safety.classify(AddressRange(0x1FFF, 0x2001)) is RegionKind.UNKNOWN
    assert safety.classify(AddressRange(0x20FF, 0x2101)) is RegionKind.UNKNOWN
    refused = safety.check(ActionCategory.MEMORY_WRITE, [AddressRange(0x20FF, 0x2101)])

    assert isinstance(refused, Refusal)
    assert refused.code == "safety/unknown"


def test_adjacent_same_kind_regions_form_gapless_full_containment() -> None:
    safety = SafetyMap(
        [
            region("RAM A", RegionKind.RAM, 0x2000, 0x2080),
            region("RAM B", RegionKind.RAM, 0x2080, 0x2100),
        ]
    )
    assert safety.classify(AddressRange(0x2040, 0x20C0)) is RegionKind.RAM
    assert isinstance(
        safety.check(ActionCategory.MEMORY_WRITE, [AddressRange(0x2040, 0x20C0)]),
        Allowed,
    )


def test_adjacent_bank_boundary_is_half_open_and_cross_bank_union_is_complete() -> None:
    safety = SafetyMap(
        [
            region("RAM bank 1", RegionKind.RAM, 0x20000000, 0x20001000),
            region("RAM bank 0", RegionKind.RAM, 0x1FFFF000, 0x20000000),
        ]
    )

    assert safety.classify(AddressRange(0x1FFFFFFF, 0x20000001)) is RegionKind.RAM
    assert safety.classify(AddressRange(0x20000000, 0x20000001)) is RegionKind.RAM
    assert safety.classify(AddressRange(0x20000FFF, 0x20001001)) is RegionKind.UNKNOWN


def test_overlapping_equal_specificity_kinds_are_unknown_but_prohibited_wins() -> None:
    ambiguous = SafetyMap(
        [
            region("RAM", RegionKind.RAM, 0x1000, 0x2000),
            region("Peripheral alias", RegionKind.PERIPHERAL, 0x1800, 0x2800),
        ]
    )
    assert ambiguous.classify(AddressRange(0x1800, 0x1900)) is RegionKind.UNKNOWN

    prohibited = SafetyMap(
        [
            *ambiguous.regions,
            region("prohibited subrange", RegionKind.PROHIBITED, 0x1880, 0x1890),
        ]
    )
    assert prohibited.classify(AddressRange(0x1800, 0x1900)) is RegionKind.PROHIBITED


def test_prohibited_overrides_all_for_every_overlap_shape() -> None:
    safety = SafetyMap(
        [
            region("Peripheral", RegionKind.PERIPHERAL, 0x4000, 0x5000),
            region("Security", RegionKind.PROHIBITED, 0x4400, 0x4500),
        ]
    )

    for requested in (
        AddressRange(0x4400, 0x4401),
        AddressRange(0x43FF, 0x4401),
        AddressRange(0x44FF, 0x4501),
        AddressRange(0x4000, 0x5000),
    ):
        assert safety.classify(requested) is RegionKind.PROHIBITED
        for action in ActionCategory:
            result = safety.check(action, [requested])
            assert isinstance(result, Refusal)
            assert result.code == "safety/prohibited"
    assert safety.classify(AddressRange(0x4500, 0x4501)) is RegionKind.PERIPHERAL


def test_build_partition_is_more_specific_than_physical_flash() -> None:
    safety = SafetyMap(
        [
            region("Physical flash", RegionKind.PHYSICAL_FLASH, 0x0000, 0x10000),
            region("Application", RegionKind.APPLICATION_FLASH, 0x2000, 0x8000),
        ]
    )

    assert safety.classify(AddressRange(0x3000, 0x4000)) is RegionKind.APPLICATION_FLASH
    assert safety.classify(AddressRange(0x8000, 0x9000)) is RegionKind.PHYSICAL_FLASH


@pytest.mark.parametrize(
    ("action", "kind", "allowed"),
    [
        (ActionCategory.MEMORY_READ, RegionKind.RAM, True),
        (ActionCategory.MEMORY_READ, RegionKind.ROM_BOOTLOADER, True),
        (ActionCategory.MEMORY_READ, RegionKind.PERIPHERAL_READ_ONLY, True),
        (ActionCategory.MEMORY_READ, RegionKind.PERIPHERAL_WRITE_ONLY, False),
        (ActionCategory.MEMORY_WRITE, RegionKind.RAM, True),
        (ActionCategory.MEMORY_WRITE, RegionKind.PHYSICAL_RAM, False),
        (ActionCategory.REGISTER_WRITE, RegionKind.PERIPHERAL, True),
        (ActionCategory.REGISTER_WRITE, RegionKind.PERIPHERAL_READ_ONLY, False),
        (ActionCategory.REGISTER_WRITE, RegionKind.PERIPHERAL_WRITE_ONLY, True),
        (ActionCategory.REGISTER_WRITE, RegionKind.CPU_SYSTEM, False),
        (ActionCategory.FLASH_APPLICATION, RegionKind.APPLICATION_FLASH, True),
        (ActionCategory.FLASH_APPLICATION, RegionKind.BOOTLOADER_FLASH, False),
        (ActionCategory.FLASH_BOOTLOADER, RegionKind.BOOTLOADER_FLASH, True),
    ],
)
def test_action_category_matrix(action: ActionCategory, kind: RegionKind, allowed: bool) -> None:
    safety = SafetyMap([region(kind.value, kind, 0x1000, 0x2000)])
    result = safety.check(action, [AddressRange(0x1100, 0x1200)])

    assert isinstance(result, Allowed) is allowed
    assert isinstance(result, Refusal) is not allowed


def test_memory_read_denies_unknown_and_prohibited_but_allows_mapped_kinds() -> None:
    safety = SafetyMap(
        [
            region("Application", RegionKind.APPLICATION_FLASH, 0x1000, 0x2000),
            region("RAM", RegionKind.RAM, 0x2000, 0x3000),
            region("Peripheral", RegionKind.PERIPHERAL, 0x4000, 0x5000),
            region("Security", RegionKind.PROHIBITED, 0x4800, 0x4810),
        ]
    )

    for requested in (
        AddressRange(0x1000, 0x1004),
        AddressRange(0x2000, 0x2004),
        AddressRange(0x4000, 0x4004),
    ):
        assert isinstance(safety.check(ActionCategory.MEMORY_READ, [requested]), Allowed)

    unknown = safety.check(ActionCategory.MEMORY_READ, [AddressRange(0x5000, 0x5004)])
    prohibited = safety.check(ActionCategory.MEMORY_READ, [AddressRange(0x4800, 0x4804)])
    assert isinstance(unknown, Refusal)
    assert unknown.classification is RegionKind.UNKNOWN
    assert isinstance(prohibited, Refusal)
    assert prohibited.classification is RegionKind.PROHIBITED


def test_breakpoint_requires_full_executable_segment_containment() -> None:
    safety = SafetyMap(
        [
            region("Application", RegionKind.APPLICATION_FLASH, 0x1000, 0x2000),
            region(
                "Application text",
                RegionKind.APPLICATION_FLASH,
                0x1100,
                0x1800,
                executable=True,
            ),
        ]
    )

    assert isinstance(
        safety.check(ActionCategory.BREAKPOINT, [AddressRange(0x1100, 0x1102)]), Allowed
    )
    refusal = safety.check(ActionCategory.BREAKPOINT, [AddressRange(0x17FF, 0x1801)])
    assert isinstance(refusal, Refusal)
    assert refusal.code == "safety/not-executable"


def test_partition_prohibited_overlap_matrix_is_reported() -> None:
    application = region("App", RegionKind.APPLICATION_FLASH, 0x1000, 0x2000)
    ram = region("RAM", RegionKind.RAM, 0x3000, 0x4000)
    option = region("Options", RegionKind.PROHIBITED, 0x1F00, 0x2100)
    safety = SafetyMap([application, ram, option])

    assert safety.partition_prohibited_conflicts() == ((application, option),)


def test_property_every_single_byte_is_mapped_or_unknown_and_end_is_exclusive() -> None:
    safety = SafetyMap([region("RAM", RegionKind.RAM, 10, 20)])

    for address in range(0, 30):
        expected = RegionKind.RAM if 10 <= address < 20 else RegionKind.UNKNOWN
        assert safety.classify(AddressRange(address, address + 1)) is expected


def test_multi_range_check_and_region_output_are_input_order_deterministic() -> None:
    first = region("RAM A", RegionKind.RAM, 0x1000, 0x1100)
    second = region("RAM B", RegionKind.RAM, 0x1100, 0x1200)
    forward = SafetyMap([first, second])
    reverse = SafetyMap([second, first])
    ranges = [AddressRange(0x1180, 0x1190), AddressRange(0x1010, 0x1020)]

    assert forward.regions == reverse.regions
    assert [item.to_document() for item in forward.regions] == [
        item.to_document() for item in reverse.regions
    ]
    assert forward.check(ActionCategory.MEMORY_WRITE, ranges) == reverse.check(
        ActionCategory.MEMORY_WRITE, list(reversed(ranges))
    )
    output = first.to_document()
    assert output["provenance"] == [
        {
            "authority": "reconciled",
            "source_id": "pack+datasheet",
            "detail": "two-source agreement",
        }
    ]

    build = Provenance(SourceAuthority.BUILD, "firmware.elf", "sha256:abc")
    document = Provenance(SourceAuthority.OFFICIAL_DOCUMENT, "RM-1", "revision 2")
    reversed_provenance = SafetyRegion(
        "ordered provenance",
        RegionKind.RAM,
        AddressRange(0x2000, 0x2100),
        (document, build),
    )
    assert reversed_provenance.provenance == (build, document)
