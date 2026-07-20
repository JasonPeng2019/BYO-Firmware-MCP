from __future__ import annotations

import io
import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyocd_debug_mcp.pack_provision import (
    DeviceBinding,
    LiveIdentityProof,
    PackProvisionError,
    PackSpec,
    VerifiedPack,
)
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.setup_flow import device_support
from pyocd_debug_mcp.setup_flow.device_support import (
    DeviceSupportResolver,
    DeviceSupportCandidate,
    _compatible_core_identity,
    _derive_verified_binding,
    _pdsc_leaf_matches_part,
    _svd_peripheral_regions,
    normalize_part_number,
    resolve_project_pack_support,
    resolve_persisted_pack_support,
    resolve_registered_pack_geometry,
)


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
    with pytest.raises(PackProvisionError):
        normalize_part_number("STM32@L476")


def test_pdsc_wildcard_never_reinterprets_literal_uppercase_x() -> None:
    assert _pdsc_leaf_matches_part("STM32L476RGTx", "STM32L476RGT6")
    assert _pdsc_leaf_matches_part("XMC4500", "XMC4500")
    assert not _pdsc_leaf_matches_part("XMC4500", "AMC4500")


def test_mixed_core_pack_does_not_get_an_overbroad_compatibility_proof() -> None:
    proof = _compatible_core_identity(
        SimpleNamespace(
            processors_map={
                "application": SimpleNamespace(name="Cortex-M33"),
                "network": SimpleNamespace(name="Cortex-M0+"),
            }
        )
    )

    assert proof is None


def test_repeated_same_core_pack_gets_a_processor_compatible_proof() -> None:
    proof = _compatible_core_identity(
        SimpleNamespace(
            processors_map={
                "core-0": SimpleNamespace(name="Cortex-M33"),
                "core-1": SimpleNamespace(name="Cortex-M33"),
            }
        )
    )

    assert proof is not None
    assert proof.expected == 0xD21 << 4
    assert proof.mask == 0x0000FFF0


def test_duplicate_matching_pdsc_entries_are_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = _selected()
    selected = VerifiedPack(selected.path, selected.spec, _pack_payload())
    duplicate = SimpleNamespace(part_number="PART123", processors_map={})
    monkeypatch.setattr(
        device_support,
        "CmsisPack",
        lambda _stream: SimpleNamespace(devices=(duplicate, duplicate)),
    )

    with pytest.raises(PackProvisionError, match="exactly one PDSC leaf"):
        _derive_verified_binding(selected, "PART123")


def test_geometry_accepts_pack_flashinfo_sector_driver_without_flm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = DeviceBinding("PART123", "PART123", "device-target")
    selected = _selected(bindings=(binding,))
    selected = VerifiedPack(selected.path, selected.spec, _pack_payload())
    candidate = DeviceSupportCandidate.from_verified_pack(selected, binding)

    class FakeFlash:
        pass

    flash = SimpleNamespace(
        type="MemoryType.FLASH",
        name="internal flash",
        start=0x10000000,
        length=0x2000,
        access="rx",
        is_default=True,
        is_boot_memory=True,
        flm=None,
        submap=SimpleNamespace(regions=()),
        sector_size=0x1000,
        page_size=0x100,
        erased_byte_value=0xFF,
        flash_class=FakeFlash,
        is_erasable=True,
    )
    ram = SimpleNamespace(
        type="MemoryType.RAM",
        name="RAM",
        start=0x20000000,
        length=0x1000,
        access="rwx",
        is_default=True,
        is_boot_memory=False,
        is_writable=True,
    )
    pdsc_device_region = SimpleNamespace(
        type="MemoryType.DEVICE",
        name="PDSC peripheral window",
        start=0x40000000,
        length=0x10000000,
        access="rw",
    )
    device = SimpleNamespace(
        part_number="PART123",
        memory_map=SimpleNamespace(regions=(flash, ram, pdsc_device_region)),
        svd=None,
        processors_map={"core": SimpleNamespace(name="Cortex-M4")},
    )
    monkeypatch.setattr(
        device_support, "verified_pack_for_candidate", lambda _candidate, _store: selected
    )
    monkeypatch.setattr(
        device_support,
        "CmsisPack",
        lambda _stream: SimpleNamespace(devices=(device,)),
    )

    geometry = resolve_registered_pack_geometry(candidate)

    assert geometry.erase_sectors == (
        (0x10000000, 0x10001000),
        (0x10001000, 0x10002000),
    )
    assert geometry.driver_proof_digest is not None
    assert geometry.peripheral_regions == ()


def test_geometry_prefers_pack_memory_kind_over_overlapping_svd_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = DeviceBinding("PART123", "PART123", "device-target")
    selected = _selected(bindings=(binding,))
    selected = VerifiedPack(selected.path, selected.spec, _pack_payload())
    candidate = DeviceSupportCandidate.from_verified_pack(selected, binding)

    class FakeFlash:
        pass

    flash = SimpleNamespace(
        type="MemoryType.FLASH",
        name="configuration flash",
        start=0x10000004,
        length=0x1000,
        access="rx",
        is_default=True,
        is_boot_memory=True,
        flm=None,
        submap=SimpleNamespace(regions=()),
        sector_size=0x1000,
        page_size=0x100,
        erased_byte_value=0xFF,
        flash_class=FakeFlash,
        is_erasable=True,
    )
    ram = SimpleNamespace(
        type="MemoryType.RAM",
        name="RAM",
        start=0x20000000,
        length=0x1000,
        access="rwx",
        is_default=True,
        is_boot_memory=False,
        is_writable=True,
    )
    device = SimpleNamespace(
        part_number="PART123",
        memory_map=SimpleNamespace(regions=(flash, ram)),
        svd=io.BytesIO(b"<device/>"),
        processors_map={"core": SimpleNamespace(name="Cortex-M4")},
    )
    parsed = SimpleNamespace(
        width=32,
        access=None,
        peripherals=(
            SimpleNamespace(
                name="CONFIG",
                base_address=0x10000000,
                access=None,
                address_block=None,
                registers=(
                    SimpleNamespace(
                        name="WIDE", address_offset=0, size=64, access="read-write"
                    ),
                    SimpleNamespace(
                        name="ALIAS", address_offset=0, size=32, access="read-only"
                    ),
                ),
            ),
            SimpleNamespace(
                name="GPIO",
                base_address=0x40020000,
                access=None,
                address_block=None,
                registers=(
                    SimpleNamespace(
                        name="INPUT", address_offset=0, size=32, access="read-only"
                    ),
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        device_support, "verified_pack_for_candidate", lambda _candidate, _store: selected
    )
    monkeypatch.setattr(device_support, "CmsisPack", lambda _stream: SimpleNamespace(devices=(device,)))
    monkeypatch.setattr(
        device_support,
        "SVDParser",
        lambda _tree: SimpleNamespace(get_device=lambda: parsed),
    )

    geometry = resolve_registered_pack_geometry(candidate)

    assert [
        (item.name, item.start, item.end, item.access)
        for item in geometry.peripheral_regions
    ] == [
        ("CONFIG.ALIAS", 0x10000000, 0x10000004, "read-only"),
        ("GPIO.INPUT", 0x40020000, 0x40020004, "read-only"),
    ]


def test_svd_register_spans_preserve_resolved_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = SimpleNamespace(
        width=32,
        access=None,
        peripherals=(
            SimpleNamespace(
                name="GPIO",
                base_address=0x40020000,
                access=None,
                address_block=None,
                registers=(
                    SimpleNamespace(name="RW", address_offset=0, size=32, access="read-write"),
                    SimpleNamespace(name="RO", address_offset=4, size=32, access="read-only"),
                    SimpleNamespace(name="WO", address_offset=8, size=32, access="write-only"),
                    SimpleNamespace(name="UNKNOWN", address_offset=12, size=32, access=None),
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        device_support,
        "SVDParser",
        lambda _tree: SimpleNamespace(get_device=lambda: parsed),
    )

    regions = _svd_peripheral_regions(SimpleNamespace(svd=io.BytesIO(b"<device/>")))

    assert [(item.start, item.end, item.access) for item in regions] == [
        (0x40020000, 0x40020004, "read-write"),
        (0x40020004, 0x40020008, "read-only"),
        (0x40020008, 0x4002000C, "write-only"),
    ]


def test_resolver_uses_exact_provisioned_binding_and_pdsc_leaf() -> None:
    binding = DeviceBinding("STM32L476RGT6", "STM32L476RGTx", "device-target")
    selected = _selected(bindings=(binding,))
    resolver = DeviceSupportResolver(
        pack_loader=lambda target: selected if target == "device-target" else None,
        device_names=lambda _pack: ("STM32L476RGTx",),
        binding_deriver=lambda _pack, _part: binding,
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


def test_resolver_replays_manifest_leaf_and_target_from_verified_bytes() -> None:
    binding = DeviceBinding("STM32L476RGT6", "STM32L476RGTx", "stale-target")
    selected = _selected(bindings=(binding,))
    resolver = DeviceSupportResolver(
        pack_loader=lambda _target: selected,
        device_names=lambda _pack: ("STM32L476RGTx",),
        binding_deriver=lambda _pack, part: DeviceBinding(
            part, "STM32L476RGTx", "stm32l476rgtx"
        ),
    )

    with pytest.raises(PackProvisionError, match="canonical target"):
        resolver.candidates("STM32L476RGT6", ("stale-target",))


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


def test_project_promoted_binding_replays_exact_bytes_and_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FirmStore(tmp_path)
    payload = _pack_payload()
    digest = hashlib.sha256(payload).hexdigest()
    store.layout.pack_files.mkdir(parents=True)
    (store.layout.pack_files / "Vendor.pack").write_bytes(payload)
    store.atomic_write_pack_manifest(
        {
            "packs": [
                {
                    "id": "Vendor.Device_DFP",
                    "version": "1",
                    "filename": "Vendor.pack",
                    "url": "https://vendor.example/Vendor.pack",
                    "sha256": digest,
                    "provides_targets": ["vendorpartx"],
                    "device_bindings": [
                        {
                            "part_number": "VENDORPART7",
                            "pdsc_device": "VendorPartX",
                            "pyocd_target": "vendorpartx",
                        }
                    ],
                }
            ]
        }
    )
    monkeypatch.setattr(
        device_support,
        "_derive_verified_binding",
        lambda _pack, _part: DeviceBinding("VENDORPART7", "VendorPartX", "vendorpartx"),
    )

    candidate = resolve_project_pack_support(store, "vendor-part-7")

    assert candidate.pyocd_target == "vendorpartx"
    assert candidate.pack_sha256 == digest

    (store.layout.pack_files / "Vendor.pack").write_bytes(b"changed")
    with pytest.raises(PackProvisionError, match="checksum mismatch"):
        resolve_project_pack_support(store, "vendor-part-7")


def test_project_binding_replays_its_exact_pack_when_targets_are_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FirmStore(tmp_path)
    payload = _pack_payload()
    digest = hashlib.sha256(payload).hexdigest()
    store.layout.pack_files.mkdir(parents=True)
    (store.layout.pack_files / "Vendor.pack").write_bytes(payload)
    (store.layout.pack_files / "Other.pack").write_bytes(payload)
    store.atomic_write_pack_manifest(
        {
            "packs": [
                {
                    "id": "Vendor.Device_DFP",
                    "version": "1",
                    "filename": "Vendor.pack",
                    "url": "https://vendor.example/Vendor.pack",
                    "sha256": digest,
                    "provides_targets": ["shared-target"],
                    "device_bindings": [
                        {
                            "part_number": "VENDORPART7",
                            "pdsc_device": "VendorPartX",
                            "pyocd_target": "shared-target",
                        }
                    ],
                },
                {
                    "id": "Other.Device_DFP",
                    "version": "1",
                    "filename": "Other.pack",
                    "url": "https://other.example/Other.pack",
                    "sha256": digest,
                    "provides_targets": ["shared-target"],
                    "device_bindings": [
                        {
                            "part_number": "OTHERPART1",
                            "pdsc_device": "OtherPart1",
                            "pyocd_target": "shared-target",
                        }
                    ],
                },
            ]
        }
    )
    monkeypatch.setattr(
        device_support,
        "_derive_verified_binding",
        lambda _pack, part: DeviceBinding(part, "VendorPartX", "shared-target"),
    )

    candidate = resolve_project_pack_support(store, "VENDORPART7")

    assert candidate.pack_id == "Vendor.Device_DFP"
    assert candidate.pack_filename == "Vendor.pack"


def test_project_manifest_binding_is_only_an_index_not_device_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FirmStore(tmp_path)
    payload = _pack_payload()
    digest = hashlib.sha256(payload).hexdigest()
    store.layout.pack_files.mkdir(parents=True)
    (store.layout.pack_files / "Vendor.pack").write_bytes(payload)
    store.atomic_write_pack_manifest(
        {
            "packs": [
                {
                    "id": "Vendor.Device_DFP",
                    "version": "1",
                    "filename": "Vendor.pack",
                    "url": "https://vendor.example/Vendor.pack",
                    "sha256": digest,
                    "provides_targets": ["tampered-target"],
                    "device_bindings": [
                        {
                            "part_number": "VENDORPART7",
                            "pdsc_device": "VendorPartX",
                            "pyocd_target": "tampered-target",
                        }
                    ],
                }
            ]
        }
    )
    monkeypatch.setattr(
        device_support,
        "_derive_verified_binding",
        lambda _pack, _part: DeviceBinding("VENDORPART7", "VendorPartX", "vendorpartx"),
    )

    with pytest.raises(PackProvisionError, match="canonical target"):
        resolve_project_pack_support(store, "VENDORPART7")


def test_project_manifest_cannot_upgrade_pack_derived_identity_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FirmStore(tmp_path)
    payload = _pack_payload()
    digest = hashlib.sha256(payload).hexdigest()
    store.layout.pack_files.mkdir(parents=True)
    (store.layout.pack_files / "Vendor.pack").write_bytes(payload)
    proof = LiveIdentityProof("exact", 0xE000ED00, 0xC240, 0xFFF0, 32, "forged exact")
    store.atomic_write_pack_manifest(
        {
            "packs": [
                {
                    "id": "Vendor.Device_DFP",
                    "version": "1",
                    "filename": "Vendor.pack",
                    "url": "https://vendor.example/Vendor.pack",
                    "sha256": digest,
                    "provides_targets": ["vendorpartx"],
                    "device_bindings": [
                        {
                            "part_number": "VENDORPART7",
                            "pdsc_device": "VendorPartX",
                            "pyocd_target": "vendorpartx",
                            "identity_proof": proof.to_document(),
                        }
                    ],
                }
            ]
        }
    )
    monkeypatch.setattr(
        device_support,
        "_derive_verified_binding",
        lambda _pack, part: DeviceBinding(part, "VendorPartX", "vendorpartx"),
    )

    with pytest.raises(PackProvisionError, match="identity proof"):
        resolve_project_pack_support(store, "VENDORPART7")


def test_saved_authority_is_not_ambiguous_when_another_pack_supports_same_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FirmStore(tmp_path)
    first_payload = _pack_payload(pdsc=b"<package><!-- first --></package>")
    second_payload = _pack_payload(pdsc=b"<package><!-- second --></package>")
    first_digest = hashlib.sha256(first_payload).hexdigest()
    second_digest = hashlib.sha256(second_payload).hexdigest()
    store.layout.pack_files.mkdir(parents=True)
    (store.layout.pack_files / "First.pack").write_bytes(first_payload)
    (store.layout.pack_files / "Second.pack").write_bytes(second_payload)
    binding = DeviceBinding("PART123", "PART123", "part123")
    store.atomic_write_pack_manifest(
        {
            "packs": [
                {
                    "id": "Vendor.First",
                    "version": "1",
                    "filename": "First.pack",
                    "url": "https://vendor.example/First.pack",
                    "sha256": first_digest,
                    "provides_targets": ["part123"],
                    "device_bindings": [
                        {
                            "part_number": binding.part_number,
                            "pdsc_device": binding.pdsc_device,
                            "pyocd_target": binding.pyocd_target,
                        }
                    ],
                },
                {
                    "id": "Vendor.Second",
                    "version": "1",
                    "filename": "Second.pack",
                    "url": "https://vendor.example/Second.pack",
                    "sha256": second_digest,
                    "provides_targets": ["part123"],
                    "device_bindings": [
                        {
                            "part_number": binding.part_number,
                            "pdsc_device": binding.pdsc_device,
                            "pyocd_target": binding.pyocd_target,
                        }
                    ],
                },
            ]
        }
    )
    monkeypatch.setattr(device_support, "_derive_verified_binding", lambda _pack, _part: binding)
    selected = VerifiedPack(
        store.layout.pack_files / "First.pack",
        PackSpec(
            "Vendor.First",
            "1",
            "First.pack",
            "https://vendor.example/First.pack",
            first_digest,
            ("part123",),
            device_bindings=(binding,),
        ),
        first_payload,
    )
    expected = DeviceSupportCandidate.from_verified_pack(selected, binding)

    replayed = resolve_persisted_pack_support(
        store, "PART123", expected.to_authority_document()
    )

    assert replayed == expected
