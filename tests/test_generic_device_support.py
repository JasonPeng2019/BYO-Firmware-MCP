from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from pyocd_debug_mcp.pack_provision import DeviceBinding, PackProvisionError, PackSpec, VerifiedPack
from pyocd_debug_mcp.setup_flow.device_support import DeviceSupportResolver, normalize_part_number


def _selected(*, bindings: tuple[DeviceBinding, ...] = ()) -> VerifiedPack:
    return VerifiedPack(
        path=Path("verified.pack"),
        spec=PackSpec(
            id="Vendor.Device_DFP",
            version="1",
            filename="verified.pack",
            url="https://example.invalid/verified.pack",
            sha256="a" * 64,
            provides_targets=("device-target",),
            device_bindings=bindings,
        ),
        payload=b"verified",
    )


def _pack_payload(*, member_name: str = "Vendor.pdsc", pdsc: bytes = b"<package/>") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member_name, pdsc)
    return buffer.getvalue()


def test_normalize_part_number_allows_only_cosmetic_separators() -> None:
    assert normalize_part_number(" STM32-L476_RGT6 ") == "stm32l476rgt6"
    with pytest.raises(PackProvisionError):
        normalize_part_number("---")


def test_resolver_uses_exact_provisioned_binding_and_pdsc_leaf() -> None:
    binding = DeviceBinding("STM32L476RGT6", "STM32L476RGTx", "device-target")
    selected = _selected(bindings=(binding,))
    resolver = DeviceSupportResolver(
        pack_loader=lambda target: selected if target == "device-target" else None,
        device_names=lambda _pack: ("STM32L476RGTx",),
    )

    candidates = resolver.candidates("stm32-l476-rgt6", ("device-target",))

    assert len(candidates) == 1
    assert candidates[0].part_number == "STM32L476RGT6"
    assert candidates[0].pdsc_device == "STM32L476RGTx"
    assert candidates[0].pyocd_target == "device-target"


def test_resolver_rejects_a_provisioned_leaf_missing_from_verified_pack() -> None:
    binding = DeviceBinding("STM32L476RGT6", "STM32L476RGTx", "device-target")
    selected = _selected(bindings=(binding,))
    resolver = DeviceSupportResolver(
        pack_loader=lambda _target: selected,
        device_names=lambda _pack: ("different-device",),
    )

    with pytest.raises(PackProvisionError, match="absent"):
        resolver.candidates("STM32L476RGT6", ("device-target",))


def test_resolver_never_matches_a_family_prefix() -> None:
    binding = DeviceBinding("STM32L476RGT6", "STM32L476RGTx", "device-target")
    selected = _selected(bindings=(binding,))
    resolver = DeviceSupportResolver(
        pack_loader=lambda _target: selected,
        device_names=lambda _pack: ("STM32L476RGTx",),
    )

    assert resolver.candidates("STM32L476", ("device-target",)) == ()


@pytest.mark.parametrize(
    ("member_name", "pdsc", "message"),
    [
        ("../outside.pdsc", b"<package/>", "unsafe member path"),
        ("Vendor.pdsc", b"<!DOCTYPE package [<!ENTITY x 'x'>]><package/>", "XML entities"),
    ],
)
def test_default_pack_parser_rejects_unsafe_archive_before_pyocd(
    member_name: str, pdsc: bytes, message: str
) -> None:
    selected = _selected()
    unsafe = VerifiedPack(selected.path, selected.spec, _pack_payload(member_name=member_name, pdsc=pdsc))

    with pytest.raises(PackProvisionError, match=message):
        DeviceSupportResolver._cmsis_device_names(unsafe)
