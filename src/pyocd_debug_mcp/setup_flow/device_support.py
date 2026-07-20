"""Generic server-owned resolution of exact MCU part support from verified packs."""

from __future__ import annotations

import hashlib
import io
import json
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pyocd.target.pack.cmsis_pack import CmsisPack  # type: ignore[import-untyped]
from pyocd.target import TARGET, normalise_target_type_name  # type: ignore[import-untyped]
from pyocd.debug.svd.parser import SVDParser  # type: ignore[import-untyped]

from pyocd_debug_mcp.pack_provision import (
    MANIFEST_PATH,
    PACKS_DIR,
    DeviceBinding,
    LiveIdentityProof,
    PackSpec,
    PackProvisionError,
    VerifiedPack,
    load_manifest,
    read_bounded_pack_bytes,
    verified_pack_for_spec,
)
from pyocd_debug_mcp.firmstore.store import FirmStore

_MAX_PACK_MEMBERS = 4_096
_MAX_PACK_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
_MAX_PACK_COMPRESSION_RATIO = 200
_ARM_CPUID_PARTNO = {
    "cortexm0": 0xC20,
    "cortexm0plus": 0xC60,
    "cortexm1": 0xC21,
    "cortexm3": 0xC23,
    "cortexm4": 0xC24,
    "cortexm7": 0xC27,
    "cortexm23": 0xD20,
    "cortexm33": 0xD21,
    "cortexm35p": 0xD31,
    "cortexm55": 0xD22,
    "cortexm85": 0xD23,
}
_ARM_CPUID_COMPATIBILITY_MASK = 0xFF0FFFF0
_ARM_CPUID_IMPLEMENTER = 0x41
_ARM_CPUID_ARCHITECTURES = {0xC, 0xF}


class BuiltInTargetGeometryError(PackProvisionError):
    """An installed target is real but its static geometry needs pack augmentation."""


def live_cpuid_compatibility_proof(observed_cpuid: int) -> LiveIdentityProof:
    """Bind standard Arm Cortex-M CPUID fields without enumerating core models."""

    implementer = (observed_cpuid >> 24) & 0xFF
    architecture = (observed_cpuid >> 16) & 0xF
    part_number = (observed_cpuid >> 4) & 0xFFF
    if (
        implementer != _ARM_CPUID_IMPLEMENTER
        or architecture not in _ARM_CPUID_ARCHITECTURES
        or part_number == 0
    ):
        raise PackProvisionError(
            "live CPUID is not a recognized Arm Cortex-M compatibility identity"
        )
    return LiveIdentityProof(
        "compatible",
        0xE000ED00,
        observed_cpuid & _ARM_CPUID_COMPATIBILITY_MASK,
        _ARM_CPUID_COMPATIBILITY_MASK,
        32,
        "live Arm implementer, architecture, and Cortex-M part compatibility identity",
    )


def replay_live_cpuid_compatibility_proof(
    *,
    address: int | None,
    expected: int | None,
    mask: int | None,
    width_bits: int,
    label: str,
) -> LiveIdentityProof:
    """Replay server-captured CPUID fields without trusting profile text as authority."""

    if address is None or expected is None or mask is None:
        raise PackProvisionError("live CPUID compatibility identity is incomplete")
    canonical = live_cpuid_compatibility_proof(expected)
    recorded = LiveIdentityProof(
        "compatible",
        address,
        expected,
        mask,
        width_bits,
        label,
    )
    if recorded != canonical:
        raise PackProvisionError("live CPUID compatibility identity is not canonical")
    return canonical


def normalize_part_number(value: str) -> str:
    """Normalize only cosmetic separators; never infer a family or wildcard suffix."""

    if not isinstance(value, str):
        raise PackProvisionError("MCU part number must be text")
    allowed_separators = {"-", "_", ".", "/"}
    normalized_characters: list[str] = []
    for character in value:
        if character.isascii() and character.isalnum():
            normalized_characters.append(character.casefold())
        elif character.isspace() or character in allowed_separators:
            continue
        else:
            raise PackProvisionError(
                "MCU part number contains a non-cosmetic or non-ASCII character"
            )
    normalized = "".join(normalized_characters)
    if not normalized:
        raise PackProvisionError("MCU part number must contain letters or numbers")
    return normalized


def _pdsc_leaf_matches_part(leaf: str, requested: str) -> bool:
    """Match an exact leaf or an explicit lowercase PDSC ``x`` placeholder.

    Uppercase ``X`` is common in literal vendor prefixes such as XMC and must
    never be reinterpreted as a wildcard.
    """

    try:
        normalize_part_number(requested)
    except PackProvisionError:
        return False
    separators = {"-", "_", ".", "/"}

    def compact(value: str) -> str:
        if any(
            not (character.isascii() and character.isalnum())
            and not character.isspace()
            and character not in separators
            for character in value
        ):
            return ""
        return "".join(
            character for character in value if character.isascii() and character.isalnum()
        )

    left = compact(leaf)
    right = compact(requested)
    if not left or not right or len(left) != len(right):
        return False
    return all(
        expected == "x" or expected.casefold() == actual.casefold()
        for expected, actual in zip(left, right)
    )


def _compatible_core_identity(device: object) -> LiveIdentityProof | None:
    """Derive a standard Arm core-class proof; never label it exact silicon identity."""

    processors = getattr(device, "processors_map", None)
    if not isinstance(processors, dict) or not processors:
        return None
    names = tuple(str(getattr(processor, "name", "")) for processor in processors.values())
    normalized = tuple(
        "".join(character for character in name.casefold() if character.isalnum()) for name in names
    )
    known_parts = {_ARM_CPUID_PARTNO[name] for name in normalized if name in _ARM_CPUID_PARTNO}
    if len(known_parts) == 1 and all(name in _ARM_CPUID_PARTNO for name in normalized):
        partno = next(iter(known_parts))
        return LiveIdentityProof(
            "compatible",
            0xE000ED00,
            partno << 4,
            0x0000FFF0,
            32,
            f"{names[0]} CPUID compatibility identity",
        )
    return None


def _derive_verified_binding(selected: VerifiedPack, part_number: str) -> DeviceBinding:
    """Derive an exact part binding from already bounded, digest-verified bytes."""

    normalize_part_number(part_number)
    try:
        DeviceSupportResolver._validate_archive(selected)
        pack = CmsisPack(io.BytesIO(selected.payload))
        matches = tuple(
            device
            for device in pack.devices
            if _pdsc_leaf_matches_part(device.part_number, part_number)
        )
    except PackProvisionError:
        raise
    except Exception as exc:
        raise PackProvisionError(
            f"candidate CMSIS-Pack metadata could not be parsed: {exc}"
        ) from exc
    if len(matches) != 1:
        raise PackProvisionError(
            "candidate CMSIS-Pack must expose exactly one PDSC leaf matching the exact MCU part"
        )
    try:
        from pyocd.target import normalise_target_type_name  # type: ignore[import-untyped]

        device = matches[0]
        leaf = device.part_number
        target = normalise_target_type_name(leaf)
    except Exception as exc:
        raise PackProvisionError(
            f"candidate PDSC leaf has no canonical pyOCD target: {exc}"
        ) from exc
    return DeviceBinding(part_number, leaf, target, _compatible_core_identity(device))


def derive_candidate_binding(pack_path: Path, part_number: str) -> DeviceBinding:
    """Derive one exact/wildcard PDSC leaf and canonical target from quarantined bytes."""

    payload = read_bounded_pack_bytes(pack_path)
    return _derive_verified_binding(
        VerifiedPack(
            pack_path,
            PackSpec("candidate", "", pack_path.name, "", "0" * 64),
            payload,
        ),
        part_number,
    )


@dataclass(frozen=True, slots=True)
class DeviceSupportCandidate:
    """One verified exact part-to-pack-to-target candidate issued by the server."""

    candidate_id: str
    part_number: str
    pdsc_device: str
    pyocd_target: str
    pack_id: str
    pack_filename: str
    pack_sha256: str
    identity_proof: LiveIdentityProof | None = None

    @classmethod
    def from_verified_pack(
        cls, selected: VerifiedPack, binding: DeviceBinding
    ) -> "DeviceSupportCandidate":
        material = "\0".join(
            (
                selected.spec.sha256,
                binding.part_number.casefold(),
                binding.pdsc_device.casefold(),
                binding.pyocd_target.casefold(),
                str(binding.identity_proof.to_document() if binding.identity_proof else None),
            )
        ).encode("utf-8")
        return cls(
            hashlib.sha256(material).hexdigest(),
            binding.part_number,
            binding.pdsc_device,
            binding.pyocd_target,
            selected.spec.id,
            selected.spec.filename,
            selected.spec.sha256,
            binding.identity_proof,
        )

    @property
    def support_id(self) -> str:
        """Return the immutable identifier persisted by generic profiles.

        Candidate IDs are already domain-separated from their component tuple
        and are deterministic for the exact registered authority.  Naming the
        persisted value separately avoids callers mistaking a target string for
        authority.
        """

        return self.candidate_id

    def to_authority_document(self) -> dict[str, str]:
        """Return the closed server-generated source record for a profile/map."""

        return {
            "kind": "resolved_pack",
            "support_id": self.support_id,
            "pack_id": self.pack_id,
            "pack_filename": self.pack_filename,
            "pack_sha256": self.pack_sha256,
            "pdsc_device": self.pdsc_device,
            "pyocd_target": self.pyocd_target,
        }


@dataclass(frozen=True, slots=True)
class BuiltInTargetSupportCandidate:
    """Agent-resolved built-in pyOCD target, replayable from the installed registry.

    The target name is a research result, not authority by itself. Authority is
    established by resolving that exact installed target, hashing its static
    memory geometry, and recording a compatible live Arm CPUID proof.
    """

    part_number: str
    pyocd_target: str
    geometry_sha256: str
    identity_proof: LiveIdentityProof | None = None

    @property
    def support_id(self) -> str:
        proof = self.identity_proof.to_document() if self.identity_proof is not None else None
        material = {
            "kind": "resolved_builtin_target",
            "part_number": normalize_part_number(self.part_number),
            "pyocd_target": self.pyocd_target.casefold(),
            "geometry_sha256": self.geometry_sha256,
            "identity_proof": proof,
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def with_identity_proof(self, observed_cpuid: int) -> "BuiltInTargetSupportCandidate":
        proof = live_cpuid_compatibility_proof(observed_cpuid)
        return BuiltInTargetSupportCandidate(
            self.part_number,
            self.pyocd_target,
            self.geometry_sha256,
            proof,
        )

    def to_authority_document(self) -> dict[str, str]:
        if self.identity_proof is None:
            raise PackProvisionError(
                "built-in target support has not captured a live compatible identity proof"
            )
        proof = self.identity_proof
        return {
            "kind": "resolved_builtin_target",
            "support_id": self.support_id,
            "part_number": self.part_number,
            "pyocd_target": self.pyocd_target,
            "geometry_sha256": self.geometry_sha256,
            "identity_address": str(proof.address),
            "identity_expected": str(proof.expected),
            "identity_mask": str(proof.mask),
            "identity_width_bits": str(proof.width_bits),
            "identity_label": proof.label,
        }


@dataclass(frozen=True, slots=True)
class PackAddressRegion:
    """One independently described pack/SVD address range."""

    name: str
    start: int
    end: int
    access: str = "r"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise PackProvisionError("pack address region name must be non-empty")
        if not 0 <= self.start < self.end <= 1 << 64:
            raise PackProvisionError("pack address region must be a valid 64-bit half-open range")

    def to_document(self) -> dict[str, object]:
        return {
            "name": self.name,
            "start": self.start,
            "end": self.end,
            "access": self.access,
        }

    @property
    def readable(self) -> bool:
        return self.access in {
            "r",
            "rx",
            "rw",
            "rwx",
            "read-only",
            "read-write",
            "read-writeOnce",
        }

    @property
    def writable(self) -> bool:
        return self.access in {
            "w",
            "rw",
            "rwx",
            "write-only",
            "writeOnce",
            "read-write",
            "read-writeOnce",
        }


@dataclass(frozen=True, slots=True)
class PackMemoryGeometry:
    """Conservative default memory geometry parsed from one verified PDSC leaf."""

    flash_start: int
    flash_end: int
    ram_start: int
    ram_end: int
    erase_sectors: tuple[tuple[int, int], ...] = ()
    driver_proof_digest: str | None = None
    erased_byte_value: int | None = None
    flash_regions: tuple[PackAddressRegion, ...] = ()
    ram_regions: tuple[PackAddressRegion, ...] = ()
    rom_regions: tuple[PackAddressRegion, ...] = ()
    peripheral_regions: tuple[PackAddressRegion, ...] = ()
    cpu_system_regions: tuple[PackAddressRegion, ...] = ()

    def to_document(self) -> dict[str, object]:
        return {
            "flash_start": self.flash_start,
            "flash_end": self.flash_end,
            "ram_start": self.ram_start,
            "ram_end": self.ram_end,
            "erase_sectors": [{"start": start, "end": end} for start, end in self.erase_sectors],
            "driver_proof_digest": self.driver_proof_digest,
            "erased_byte_value": self.erased_byte_value,
            "flash_regions": [item.to_document() for item in self.flash_regions],
            "ram_regions": [item.to_document() for item in self.ram_regions],
            "rom_regions": [item.to_document() for item in self.rom_regions],
            "peripheral_regions": [item.to_document() for item in self.peripheral_regions],
            "cpu_system_regions": [item.to_document() for item in self.cpu_system_regions],
        }


DeviceSupportAuthority = DeviceSupportCandidate | BuiltInTargetSupportCandidate


def _geometry_digest(geometry: PackMemoryGeometry) -> str:
    return hashlib.sha256(
        json.dumps(geometry.to_document(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _builtin_target_class(target: str) -> type[Any]:
    normalized = normalise_target_type_name(target)
    target_class = TARGET.get(normalized)
    if target_class is None:
        raise PackProvisionError(f"{target!r} is not an installed built-in pyOCD target")
    return cast(type[Any], target_class)


def resolve_builtin_target_geometry(target: str) -> PackMemoryGeometry:
    """Derive deterministic physical facts from one installed built-in target.

    This adapter deliberately uses only the target class's static memory map.
    Multiple flash banks are preserved and remain usable for read/debug, while
    erase authority requires one unambiguous programming bank. A target with no
    static physical flash or writable RAM needs an exact pack before the current
    generic-map schema can persist it.
    """

    target_class = _builtin_target_class(target)
    memory_map = getattr(target_class, "MEMORY_MAP", None)
    regions: tuple[Any, ...] = tuple(getattr(memory_map, "regions", ()) or ())
    flash: tuple[Any, ...] = tuple(
        region for region in regions if bool(getattr(region, "is_flash", False))
    )
    ram: tuple[Any, ...] = tuple(
        region
        for region in regions
        if bool(getattr(region, "is_ram", False)) and bool(getattr(region, "is_writable", False))
    )
    peripherals: tuple[Any, ...] = tuple(
        region
        for region in regions
        if bool(getattr(region, "is_device", False))
        or str(getattr(region, "type", "")).casefold().endswith("device")
    )
    boot_flash = tuple(region for region in flash if bool(getattr(region, "is_boot_memory", False)))
    marked_default = tuple(region for region in flash if bool(getattr(region, "is_default", False)))
    programmable_flash = (
        boot_flash
        if len(boot_flash) == 1
        else marked_default
        if len(marked_default) == 1
        else flash
        if len(flash) == 1
        else ()
    )
    if not flash or not ram:
        raise BuiltInTargetGeometryError(
            "built-in target lacks physical flash or writable RAM required by the current map schema"
        )
    selected_flash = (
        programmable_flash[0]
        if len(programmable_flash) == 1
        else min(flash, key=lambda item: int(item.start))
    )
    default_ram = tuple(region for region in ram if bool(getattr(region, "is_default", False)))
    selected_ram = (
        default_ram[0] if len(default_ram) == 1 else min(ram, key=lambda item: int(item.start))
    )
    sectors: list[tuple[int, int]] = []
    driver_rows: list[dict[str, object]] = []
    erased_values: set[int] = set()
    for region in programmable_flash:
        # Non-testable config/OTP regions remain visible physical memory but do
        # not become ordinary application-allocation sectors.
        if not bool(getattr(region, "is_testable", True)):
            continue
        sector_size = int(getattr(region, "sector_size", 0) or 0)
        start = int(region.start)
        end = int(region.end) + 1
        flash_class = getattr(region, "flash_class", None)
        if sector_size <= 0 or (end - start) % sector_size or flash_class is None:
            continue
        sectors.extend(
            (address, address + sector_size) for address in range(start, end, sector_size)
        )
        erased = int(getattr(region, "erased_byte_value", 0xFF))
        if not 0 <= erased <= 0xFF:
            raise PackProvisionError("built-in target has an invalid erased-byte value")
        erased_values.add(erased)
        driver_rows.append(
            {
                "start": start,
                "end": end,
                "sector_size": sector_size,
                "flash_class": f"{flash_class.__module__}.{flash_class.__qualname__}",
                "erased_byte_value": erased,
            }
        )
    if len(erased_values) != 1:
        sectors.clear()
        driver_rows.clear()
    driver_digest = (
        hashlib.sha256(
            json.dumps(driver_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if driver_rows
        else None
    )
    as_region = lambda region: PackAddressRegion(  # noqa: E731 - compact pure adapter
        str(region.name), int(region.start), int(region.end) + 1, str(region.access)
    )
    return PackMemoryGeometry(
        int(selected_flash.start),
        int(selected_flash.end) + 1,
        int(selected_ram.start),
        int(selected_ram.end) + 1,
        tuple(sectors),
        driver_digest,
        next(iter(erased_values)) if driver_digest is not None else None,
        tuple(as_region(region) for region in flash),
        tuple(as_region(region) for region in ram),
        peripheral_regions=tuple(as_region(region) for region in peripherals),
        cpu_system_regions=(
            PackAddressRegion("Arm Cortex-M system control space", 0xE0000000, 0xE0100000),
        ),
    )


def resolve_builtin_target_support(
    part_number: str,
    pyocd_target: str,
    *,
    identity_proof: LiveIdentityProof | None = None,
) -> BuiltInTargetSupportCandidate:
    """Resolve an agent-researched exact built-in target without a pack requirement."""

    normalized = normalise_target_type_name(pyocd_target)
    geometry = resolve_builtin_target_geometry(normalized)
    return BuiltInTargetSupportCandidate(
        part_number.strip(), normalized, _geometry_digest(geometry), identity_proof
    )


def resolve_persisted_builtin_target_support(
    part_number: str, authority: Mapping[str, str]
) -> BuiltInTargetSupportCandidate:
    """Replay a persisted built-in target against the current installed registry."""

    required = {
        "kind",
        "support_id",
        "part_number",
        "pyocd_target",
        "geometry_sha256",
        "identity_address",
        "identity_expected",
        "identity_mask",
        "identity_width_bits",
        "identity_label",
    }
    source = dict(authority)
    if set(source) != required or source.get("kind") != "resolved_builtin_target":
        raise PackProvisionError("Persisted built-in target authority is incomplete")
    if normalize_part_number(source["part_number"]) != normalize_part_number(part_number):
        raise PackProvisionError("Persisted built-in target part number does not match the profile")
    try:
        proof = LiveIdentityProof(
            "compatible",
            int(source["identity_address"], 0),
            int(source["identity_expected"], 0),
            int(source["identity_mask"], 0),
            int(source["identity_width_bits"], 0),
            source["identity_label"],
        )
    except (TypeError, ValueError) as exc:
        raise PackProvisionError("Persisted built-in target identity proof is invalid") from exc
    pending = resolve_builtin_target_support(part_number, source["pyocd_target"])
    try:
        canonical_proof = pending.with_identity_proof(proof.expected).identity_proof
    except PackProvisionError as exc:
        raise PackProvisionError(
            "Persisted built-in target identity proof is not canonical"
        ) from exc
    if proof != canonical_proof:
        raise PackProvisionError("Persisted built-in target identity proof is not canonical")
    candidate = resolve_builtin_target_support(
        part_number, source["pyocd_target"], identity_proof=canonical_proof
    )
    if candidate.geometry_sha256 != source["geometry_sha256"]:
        raise PackProvisionError("Built-in target memory geometry changed since setup")
    if candidate.to_authority_document() != source:
        raise PackProvisionError("Persisted built-in target authority failed exact replay")
    return candidate


def resolve_device_support_geometry(
    candidate: DeviceSupportAuthority, store: FirmStore | None = None
) -> PackMemoryGeometry:
    if isinstance(candidate, BuiltInTargetSupportCandidate):
        geometry = resolve_builtin_target_geometry(candidate.pyocd_target)
        if _geometry_digest(geometry) != candidate.geometry_sha256:
            raise PackProvisionError("Built-in target memory geometry changed since resolution")
        return geometry
    return resolve_registered_pack_geometry(candidate, store)


class DeviceSupportResolver:
    """Resolve only provisioned exact bindings whose pack bytes expose the PDSC leaf."""

    def __init__(
        self,
        *,
        pack_loader: Callable[[str], VerifiedPack | None],
        device_names: Callable[[VerifiedPack], Iterable[str]] | None = None,
        binding_deriver: Callable[[VerifiedPack, str], DeviceBinding] | None = None,
    ) -> None:
        self._pack_loader = pack_loader
        self._device_names = device_names or self._cmsis_device_names
        self._binding_deriver = binding_deriver or _derive_verified_binding

    @staticmethod
    def _cmsis_device_names(selected: VerifiedPack) -> tuple[str, ...]:
        try:
            DeviceSupportResolver._validate_archive(selected)
            pack = CmsisPack(io.BytesIO(selected.payload))
            return tuple(device.part_number for device in pack.devices)
        except Exception as exc:
            raise PackProvisionError(
                f"Verified pack {selected.spec.filename} has unreadable CMSIS-Pack metadata: {exc}"
            ) from exc

    @staticmethod
    def _validate_archive(selected: VerifiedPack) -> None:
        """Reject unsafe archive structure before the pyOCD CMSIS parser opens it."""

        try:
            with zipfile.ZipFile(io.BytesIO(selected.payload)) as archive:
                members = archive.infolist()
                if not 1 <= len(members) <= _MAX_PACK_MEMBERS:
                    raise PackProvisionError(
                        "CMSIS-Pack member count is outside the supported limit"
                    )
                total_size = 0
                pdsc_rows: list[zipfile.ZipInfo] = []
                for member in members:
                    path = member.filename.replace("\\", "/")
                    parts = tuple(part for part in path.split("/") if part)
                    if path.startswith("/") or ".." in parts:
                        raise PackProvisionError("CMSIS-Pack contains an unsafe member path")
                    if member.is_dir():
                        continue
                    total_size += member.file_size
                    if total_size > _MAX_PACK_UNCOMPRESSED_BYTES:
                        raise PackProvisionError("CMSIS-Pack exceeds the supported unpacked size")
                    if member.compress_size and (
                        member.file_size > member.compress_size * _MAX_PACK_COMPRESSION_RATIO
                    ):
                        raise PackProvisionError("CMSIS-Pack has an unsafe compression ratio")
                    if path.casefold().endswith(".pdsc"):
                        pdsc_rows.append(member)
                if len(pdsc_rows) != 1:
                    raise PackProvisionError("CMSIS-Pack must contain exactly one PDSC document")
                pdsc = archive.read(pdsc_rows[0])
                lowered_pdsc = pdsc.lower()
                if b"<!doctype" in lowered_pdsc or b"<!entity" in lowered_pdsc:
                    raise PackProvisionError("CMSIS-Pack PDSC must not declare XML entities")
        except (OSError, zipfile.BadZipFile) as exc:
            raise PackProvisionError("Verified CMSIS-Pack is not a safe ZIP archive") from exc

    def candidates(
        self, part_number: str, targets: Iterable[str]
    ) -> tuple[DeviceSupportCandidate, ...]:
        """Return exact provisioned candidates from a server-issued target inventory."""

        requested = normalize_part_number(part_number)
        candidates: list[DeviceSupportCandidate] = []
        seen: set[str] = set()
        for target in sorted({item.strip().casefold() for item in targets if item.strip()}):
            selected = self._pack_loader(target)
            if selected is None:
                continue
            available_devices = {name.casefold() for name in self._device_names(selected)}
            for binding in selected.spec.device_bindings:
                if normalize_part_number(binding.part_number) != requested:
                    continue
                if binding.pyocd_target.casefold() != target:
                    continue
                if binding.pdsc_device.casefold() not in available_devices:
                    raise PackProvisionError(
                        f"Provisioned PDSC device {binding.pdsc_device!r} is absent from "
                        f"verified pack {selected.spec.filename}"
                    )
                replayed = self._binding_deriver(selected, binding.part_number)
                if (
                    normalize_part_number(replayed.part_number) != requested
                    or replayed.pdsc_device.casefold() != binding.pdsc_device.casefold()
                    or replayed.pyocd_target.casefold() != binding.pyocd_target.casefold()
                ):
                    raise PackProvisionError(
                        "Provisioned device binding does not match the exact verified "
                        "PDSC leaf and canonical target"
                    )
                candidate = DeviceSupportCandidate.from_verified_pack(selected, binding)
                if candidate.candidate_id not in seen:
                    seen.add(candidate.candidate_id)
                    candidates.append(candidate)
        return tuple(candidates)

    def resolve(self, part_number: str, targets: Iterable[str]) -> DeviceSupportCandidate:
        """Resolve exactly one registered candidate or fail without fallback.

        This is deliberately a server-side decision: callers cannot pass a
        pack path, digest, or target identifier to disambiguate support.
        """

        candidates = self.candidates(part_number, targets)
        if not candidates:
            raise PackProvisionError(
                "No server-registered local device-support record matches the exact MCU part"
            )
        if len(candidates) != 1:
            raise PackProvisionError(
                "More than one server-registered local device-support record matches the exact MCU part"
            )
        return candidates[0]


def has_available_pack_binding(store: FirmStore, part_number: str) -> bool:
    """Detect repository or project bindings without accepting their payload bytes."""

    requested = normalize_part_number(part_number)
    manifests = [MANIFEST_PATH]
    if store.layout.pack_manifest.resolve() != MANIFEST_PATH.resolve():
        manifests.append(store.layout.pack_manifest)
    return any(
        normalize_part_number(binding.part_number) == requested
        for manifest in manifests
        for spec in load_manifest(manifest)
        for binding in spec.device_bindings
    )


def resolve_registered_pack_support(part_number: str) -> DeviceSupportCandidate:
    """Resolve one exact local CMSIS-Pack support record from server authority."""

    return _resolve_manifest_pack_support(
        MANIFEST_PATH,
        PACKS_DIR,
        part_number,
        missing_label="server-registered",
        allow_reviewed_identity_proof=True,
    )


def _same_derived_binding(left: DeviceBinding, right: DeviceBinding) -> bool:
    return (
        normalize_part_number(left.part_number) == normalize_part_number(right.part_number)
        and left.pdsc_device.casefold() == right.pdsc_device.casefold()
        and left.pyocd_target.casefold() == right.pyocd_target.casefold()
    )


def _resolve_manifest_pack_support(
    manifest_path: Path,
    packs_dir: Path,
    part_number: str,
    *,
    missing_label: str,
    allow_reviewed_identity_proof: bool,
) -> DeviceSupportCandidate:
    """Replay exact-part bindings from their own manifest-selected pack bytes."""

    requested = normalize_part_number(part_number)
    candidates: list[DeviceSupportCandidate] = []
    for spec in load_manifest(manifest_path):
        matching_bindings = tuple(
            binding
            for binding in spec.device_bindings
            if normalize_part_number(binding.part_number) == requested
        )
        if not matching_bindings:
            continue
        selected = verified_pack_for_spec(spec, packs_dir=packs_dir)
        for binding in matching_bindings:
            replayed = _derive_verified_binding(selected, binding.part_number)
            if not _same_derived_binding(replayed, binding):
                raise PackProvisionError(
                    "Persisted device binding does not match the exact verified PDSC leaf "
                    "and canonical target"
                )
            if (
                not allow_reviewed_identity_proof
                and binding.identity_proof != replayed.identity_proof
            ):
                raise PackProvisionError(
                    "Project device binding identity proof does not match the exact "
                    "verified PDSC leaf"
                )
            authoritative_binding = binding if allow_reviewed_identity_proof else replayed
            candidate = DeviceSupportCandidate.from_verified_pack(selected, authoritative_binding)
            if candidate.support_id not in {item.support_id for item in candidates}:
                candidates.append(candidate)
    if not candidates:
        raise PackProvisionError(
            f"No {missing_label} local device-support record matches the exact MCU part"
        )
    if len(candidates) != 1:
        raise PackProvisionError(
            f"More than one {missing_label} local device-support record matches the exact MCU part"
        )
    return candidates[0]


def resolve_project_pack_support(store: FirmStore, part_number: str) -> DeviceSupportCandidate:
    """Replay one dynamically promoted binding from bytes, never manifest claims alone."""

    if store.layout.pack_manifest.resolve() == MANIFEST_PATH.resolve():
        raise PackProvisionError(
            "No server-registered project device-support record matches the exact MCU part"
        )
    return _resolve_manifest_pack_support(
        store.layout.pack_manifest,
        store.layout.pack_files,
        part_number,
        missing_label="server-registered project",
        allow_reviewed_identity_proof=False,
    )


def resolve_available_pack_support(store: FirmStore, part_number: str) -> DeviceSupportCandidate:
    """Resolve one exact repository or project candidate, refusing conflicts."""

    candidates: list[DeviceSupportCandidate] = []
    for resolve in (
        lambda: resolve_registered_pack_support(part_number),
        lambda: resolve_project_pack_support(store, part_number),
    ):
        try:
            candidate = resolve()
        except PackProvisionError as exc:
            if "No server-registered" not in str(exc):
                raise
        else:
            if candidate.support_id not in {item.support_id for item in candidates}:
                candidates.append(candidate)
    if len(candidates) != 1:
        reason = "No" if not candidates else "More than one"
        raise PackProvisionError(f"{reason} verified exact device-support candidate is available")
    return candidates[0]


def resolve_persisted_pack_support(
    store: FirmStore,
    part_number: str,
    authority: Mapping[str, str],
) -> DeviceSupportCandidate:
    """Replay one saved profile by its complete immutable pack authority tuple.

    Part-number resolution is appropriate only while onboarding.  Once setup
    has persisted an authority document, later providers for the same part must
    neither redirect nor make that configured board ambiguous.
    """

    expected_fields = {
        "kind",
        "support_id",
        "pack_id",
        "pack_filename",
        "pack_sha256",
        "pdsc_device",
        "pyocd_target",
    }
    source = dict(authority)
    if set(source) != expected_fields or source.get("kind") != "resolved_pack":
        raise PackProvisionError("Persisted device-support authority is incomplete")
    requested = normalize_part_number(part_number)
    sources = [(MANIFEST_PATH, PACKS_DIR)]
    if store.layout.pack_manifest.resolve() != MANIFEST_PATH.resolve():
        sources.insert(0, (store.layout.pack_manifest, store.layout.pack_files))
    candidates: dict[str, DeviceSupportCandidate] = {}
    for manifest_path, packs_dir in sources:
        for spec in load_manifest(manifest_path):
            if (
                spec.id != source["pack_id"]
                or spec.filename != source["pack_filename"]
                or spec.sha256 != source["pack_sha256"]
            ):
                continue
            selected = verified_pack_for_spec(spec, packs_dir=packs_dir)
            for binding in spec.device_bindings:
                if (
                    normalize_part_number(binding.part_number) != requested
                    or binding.pdsc_device.casefold() != source["pdsc_device"].casefold()
                    or binding.pyocd_target.casefold() != source["pyocd_target"].casefold()
                ):
                    continue
                replayed = _derive_verified_binding(selected, binding.part_number)
                if not _same_derived_binding(replayed, binding):
                    raise PackProvisionError(
                        "Persisted device binding no longer matches the exact verified PDSC leaf"
                    )
                candidate = DeviceSupportCandidate.from_verified_pack(selected, binding)
                if candidate.to_authority_document() != source:
                    raise PackProvisionError(
                        "Persisted device-support authority does not match verified pack bytes"
                    )
                candidates[candidate.support_id] = candidate
    if len(candidates) != 1:
        reason = "unavailable" if not candidates else "ambiguous"
        raise PackProvisionError(f"Persisted exact device-support authority is {reason}")
    return next(iter(candidates.values()))


def _overlaps_memory(
    region: PackAddressRegion, memory_regions: tuple[PackAddressRegion, ...]
) -> bool:
    return any(region.start < memory.end and memory.start < region.end for memory in memory_regions)


def _resolved_svd_access(*levels: object | None) -> str:
    """Resolve inherited CMSIS-SVD access, whose schema default is read-write."""

    for access in levels:
        if access is not None:
            value = str(access)
            return value or "unspecified"
    return "read-write"


def _intersect_access_regions(
    rows: Iterable[PackAddressRegion],
) -> tuple[PackAddressRegion, ...]:
    """Segment aliases/overlaps using only access every covering source permits."""

    unique = set(rows)
    if not unique:
        return ()
    boundaries = sorted({point for row in unique for point in (row.start, row.end)})
    segmented: list[PackAddressRegion] = []
    for start, end in zip(boundaries, boundaries[1:]):
        covering = tuple(row for row in unique if row.start <= start and end <= row.end)
        if not covering:
            continue
        readable = all(row.readable for row in covering)
        writable = all(row.writable for row in covering)
        if not readable and not writable:
            continue
        access = "read-write" if readable and writable else "read-only" if readable else "write-only"
        names = sorted({row.name for row in covering}, key=str.casefold)
        name = names[0] if len(names) == 1 else f"{names[0]} (+{len(names) - 1} aliases)"
        if (
            segmented
            and segmented[-1].end == start
            and segmented[-1].access == access
            and segmented[-1].name == name
        ):
            previous = segmented.pop()
            segmented.append(PackAddressRegion(name, previous.start, end, access))
        else:
            segmented.append(PackAddressRegion(name, start, end, access))
    return tuple(segmented)


def _subtract_address_regions(
    row: PackAddressRegion,
    exclusions: Iterable[PackAddressRegion],
) -> tuple[PackAddressRegion, ...]:
    """Remove physical-memory/system spans without discarding unrelated peripheral pieces."""

    clipped = tuple(
        exclusion
        for exclusion in exclusions
        if row.start < exclusion.end and exclusion.start < row.end
    )
    boundaries = sorted(
        {
            row.start,
            row.end,
            *(
                point
                for exclusion in clipped
                for point in (
                    max(row.start, exclusion.start),
                    min(row.end, exclusion.end),
                )
            ),
        }
    )
    return tuple(
        PackAddressRegion(row.name, start, end, row.access)
        for start, end in zip(boundaries, boundaries[1:])
        if not any(start < exclusion.end and exclusion.start < end for exclusion in clipped)
    )


def _svd_peripheral_regions(
    device: object,
    memory_regions: tuple[PackAddressRegion, ...] = (),
) -> tuple[PackAddressRegion, ...]:
    """Return non-overlapping, access-aware register spans from an optional SVD."""

    try:
        stream = getattr(device, "svd", None)
        if stream is None:
            return ()
        payload = stream.read()
        if not isinstance(payload, bytes) or not payload:
            return ()
        lowered = payload.lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            return ()
        root = ET.fromstring(payload)
        # pyOCD maps both a missing <access> and a present empty <access/> to
        # None. Preserve the latter as an invalid token so only true absence
        # can inherit or reach the CMSIS-SVD read-write schema default.
        for access_element in root.iter("access"):
            if access_element.text is None:
                access_element.text = "unspecified"
        parsed = SVDParser(ET.ElementTree(root)).get_device()
        rows: set[PackAddressRegion] = set()
        for peripheral in parsed.peripherals or ():
            base = int(peripheral.base_address)
            registers = tuple(cast(Iterable[Any], peripheral.registers or ()))
            if registers:
                for register in registers:
                    try:
                        offset = int(register.address_offset)
                        width_bits = int(register.size or parsed.width or 32)
                        if not 0 < width_bits <= 1024:
                            continue
                        width_bytes = max(1, (width_bits + 7) // 8)
                        start = base + offset
                        end = start + width_bytes
                        row = PackAddressRegion(
                            f"{peripheral.name}.{register.name}",
                            start,
                            end,
                            _resolved_svd_access(register.access, peripheral.access, parsed.access),
                        )
                    except (AttributeError, TypeError, ValueError, PackProvisionError):
                        continue
                    if start < 0xE0100000 and end > 0xE0000000:
                        continue
                    if not _overlaps_memory(row, memory_regions):
                        rows.add(row)
                continue
            block = peripheral.address_block
            if block is None:
                continue
            usage = str(getattr(block, "usage", ""))
            if usage != "registers":
                continue
            try:
                start = base + int(block.offset)
                end = start + int(block.size)
                row = PackAddressRegion(
                    str(peripheral.name),
                    start,
                    end,
                    _resolved_svd_access(peripheral.access, parsed.access),
                )
            except (AttributeError, TypeError, ValueError, PackProvisionError):
                continue
            if start < 0xE0100000 and end > 0xE0000000:
                continue
            if not _overlaps_memory(row, memory_regions):
                rows.add(row)

        return _intersect_access_regions(rows)
    except Exception:  # noqa: BLE001 - malformed optional SVD removes only register capability
        return ()


def verified_pack_for_candidate(
    candidate: DeviceSupportCandidate, store: FirmStore | None
) -> VerifiedPack:
    """Replay a candidate from its exact package identity, never a target-wide lookup."""

    sources: list[tuple[Path, Path]] = []
    if store is not None and store.layout.pack_manifest.resolve() != MANIFEST_PATH.resolve():
        sources.append((store.layout.pack_manifest, store.layout.pack_files))
    sources.append((MANIFEST_PATH, PACKS_DIR))
    expected_binding = DeviceBinding(
        candidate.part_number,
        candidate.pdsc_device,
        candidate.pyocd_target,
    )
    matches: list[tuple[PackSpec, Path]] = []
    for manifest_path, packs_dir in sources:
        for spec in load_manifest(manifest_path):
            if (
                spec.id != candidate.pack_id
                or spec.filename != candidate.pack_filename
                or spec.sha256 != candidate.pack_sha256
                or not any(
                    _same_derived_binding(binding, expected_binding)
                    for binding in spec.device_bindings
                )
            ):
                continue
            roots = [packs_dir]
            if packs_dir.resolve() == PACKS_DIR.resolve():
                roots.append(FirmStore(PACKS_DIR.parent).layout.pack_files)
            if any((root / spec.filename).is_file() for root in roots):
                matches.append((spec, packs_dir))
    if not matches:
        raise PackProvisionError(
            "Exact persisted pack identity or its verified bytes are unavailable"
        )
    selected = verified_pack_for_spec(matches[0][0], packs_dir=matches[0][1])
    for spec, packs_dir in matches[1:]:
        other = verified_pack_for_spec(spec, packs_dir=packs_dir)
        if other.payload != selected.payload:
            raise PackProvisionError("Exact persisted pack identity resolves to different bytes")
    return selected


def resolve_registered_pack_geometry(
    candidate: DeviceSupportCandidate, store: FirmStore | None = None
) -> PackMemoryGeometry:
    """Read physical memory facts from the selected verified PDSC leaf.

    The scalar fields retain one deterministic default flash/RAM pair for
    compatibility and programming-domain selection. The region collections
    preserve every independently described flash, writable RAM, and ROM range;
    disjoint banks are never joined across gaps.
    """

    selected = verified_pack_for_candidate(candidate, store)
    if (
        selected.spec.id != candidate.pack_id
        or selected.spec.filename != candidate.pack_filename
        or selected.spec.sha256 != candidate.pack_sha256
    ):
        raise PackProvisionError(
            "registered pack identity no longer matches the selected candidate"
        )
    try:
        DeviceSupportResolver._validate_archive(selected)
        pack = CmsisPack(io.BytesIO(selected.payload))
        device = next(
            item
            for item in pack.devices
            if item.part_number.casefold() == candidate.pdsc_device.casefold()
        )
        regions = tuple(device.memory_map.regions)
    except StopIteration as exc:
        raise PackProvisionError("selected PDSC leaf disappeared from the verified pack") from exc
    except PackProvisionError:
        raise
    except Exception as exc:
        raise PackProvisionError(f"verified pack geometry could not be parsed: {exc}") from exc
    flash_candidates = tuple(
        region for region in regions if str(region.type).casefold().endswith("flash")
    )
    ram_candidates = tuple(
        region
        for region in regions
        if str(region.type).casefold().endswith("ram") and bool(region.is_writable)
    )

    rom_candidates = tuple(
        region for region in regions if str(region.type).casefold().endswith("rom")
    )
    peripheral_candidates = tuple(
        region for region in regions if str(region.type).casefold().endswith("device")
    )

    def select_unambiguous(
        candidates: tuple[Any, ...], *, allow_lowest: bool = False
    ) -> Any | None:
        defaults = tuple(item for item in candidates if bool(getattr(item, "is_default", False)))
        if len(defaults) == 1:
            return defaults[0]
        boot = tuple(item for item in candidates if bool(getattr(item, "is_boot_memory", False)))
        if len(boot) == 1:
            return boot[0]
        if not defaults and len(candidates) == 1:
            return candidates[0]
        return (
            min(candidates, key=lambda item: int(item.start))
            if allow_lowest and candidates
            else None
        )

    flash = select_unambiguous(flash_candidates, allow_lowest=True)
    programmable_flash = select_unambiguous(flash_candidates)
    ram = select_unambiguous(ram_candidates, allow_lowest=True)
    if flash is None or ram is None:
        raise PackProvisionError(
            "verified PDSC leaf lacks one unambiguous default flash and writable RAM region"
        )
    sectors: list[tuple[int, int]] = []
    driver_digest: str | None = None
    erased_byte: int | None = None
    flm = getattr(programmable_flash, "flm", None) if programmable_flash is not None else None
    sector_sizes = tuple(cast(Iterable[tuple[int, int]], getattr(flm, "sector_sizes", ()) or ()))
    symbols = getattr(flm, "symbols", {}) if flm is not None else {}
    if (
        flm is not None
        and sector_sizes
        and isinstance(symbols, dict)
        and "EraseSector" in symbols
        and "ProgramPage" in symbols
    ):
        assert programmable_flash is not None
        flash_length = int(programmable_flash.length)
        ordered = sorted((int(offset), int(size)) for offset, size in sector_sizes)
        valid = ordered[0][0] == 0 and all(
            offset >= 0 and size > 0 and offset < flash_length for offset, size in ordered
        )
        if valid:
            for index, (offset, size) in enumerate(ordered):
                boundary = ordered[index + 1][0] if index + 1 < len(ordered) else flash_length
                cursor = offset
                while cursor < boundary:
                    end = min(cursor + size, boundary)
                    if end - cursor != size:
                        valid = False
                        break
                    sectors.append(
                        (
                            int(programmable_flash.start) + cursor,
                            int(programmable_flash.start) + end,
                        )
                    )
                    cursor = end
                if not valid:
                    break
        if (
            valid
            and sectors
            and sectors[-1][1] == int(programmable_flash.start) + flash_length
        ):
            candidate_erased = getattr(getattr(flm, "flash_info", None), "value_empty", None)
            if isinstance(candidate_erased, int) and 0 <= candidate_erased <= 0xFF:
                erased_byte = candidate_erased
            proof_material = repr(
                (
                    candidate.pack_sha256,
                    tuple(ordered),
                    int(getattr(flm, "page_size", 0)),
                    bytes(getattr(flm, "algo_data", b"")),
                    tuple(sorted(str(name) for name in symbols)),
                    erased_byte,
                    "FileProgrammer:chip_erase=sector",
                )
            ).encode("utf-8")
            if erased_byte is not None:
                driver_digest = hashlib.sha256(proof_material).hexdigest()
        else:
            sectors.clear()
    if driver_digest is None and programmable_flash is not None:
        # Newer CMSIS-Packs may describe flash through debug-sequence
        # ``flashinfo`` instead of an FLM. pyOCD exposes those descriptions as
        # ordinary FlashRegion sector geometry and a concrete flash class.
        candidate_sectors: list[tuple[int, int]] = []
        subregions = tuple(
            getattr(getattr(programmable_flash, "submap", None), "regions", ()) or ()
        )
        sources = subregions or (programmable_flash,)
        valid = True
        for source in sorted(sources, key=lambda item: int(item.start)):
            start = int(source.start)
            end = start + int(source.length)
            size = int(getattr(source, "sector_size", 0) or 0)
            if (
                size <= 0
                or start < int(programmable_flash.start)
                or end > int(programmable_flash.start + programmable_flash.length)
            ):
                valid = False
                break
            cursor = start
            while cursor < end:
                next_cursor = cursor + size
                if next_cursor > end:
                    valid = False
                    break
                candidate_sectors.append((cursor, next_cursor))
                cursor = next_cursor
            if not valid:
                break
        contiguous = (
            bool(candidate_sectors)
            and candidate_sectors[0][0] == int(programmable_flash.start)
            and all(
                left[1] == right[0] for left, right in zip(candidate_sectors, candidate_sectors[1:])
            )
            and candidate_sectors[-1][1]
            == int(programmable_flash.start + programmable_flash.length)
        )
        candidate_erased = getattr(programmable_flash, "erased_byte_value", None)
        flash_class = getattr(programmable_flash, "flash_class", None)
        if (
            valid
            and contiguous
            and isinstance(candidate_erased, int)
            and 0 <= candidate_erased <= 0xFF
            and flash_class is not None
            and bool(getattr(flash, "is_erasable", False))
        ):
            sectors = candidate_sectors
            erased_byte = candidate_erased
            driver_digest = hashlib.sha256(
                repr(
                    (
                        candidate.pack_sha256,
                        tuple(candidate_sectors),
                        int(getattr(programmable_flash, "page_size", 0) or 0),
                        f"{flash_class.__module__}.{flash_class.__qualname__}",
                        erased_byte,
                        "FileProgrammer:chip_erase=sector",
                    )
                ).encode("utf-8")
            ).hexdigest()

    def memory_region(item: Any) -> PackAddressRegion:
        return PackAddressRegion(
            str(getattr(item, "name", str(item.type))),
            int(item.start),
            int(item.start + item.length),
            str(getattr(item, "access", "r")),
        )

    def canonical_memory_regions(candidates: tuple[Any, ...]) -> tuple[PackAddressRegion, ...]:
        by_range: dict[tuple[int, int], PackAddressRegion] = {}
        for item in sorted(candidates, key=lambda value: (int(value.start), str(value.name))):
            region = memory_region(item)
            by_range.setdefault((region.start, region.end), region)
        return tuple(sorted(by_range.values(), key=lambda item: (item.start, item.end, item.name)))

    cpu_system_regions = (
        (PackAddressRegion("Arm Cortex-M system control space", 0xE0000000, 0xE0100000),)
        if _compatible_core_identity(device) is not None
        else ()
    )
    canonical_flash = canonical_memory_regions(flash_candidates)
    canonical_ram = canonical_memory_regions(ram_candidates)
    canonical_rom = canonical_memory_regions(rom_candidates)
    peripheral_exclusions = (
        *canonical_flash,
        *canonical_ram,
        *canonical_rom,
        PackAddressRegion("Arm Cortex-M system control space", 0xE0000000, 0xE0100000),
    )
    canonical_peripherals = _intersect_access_regions(
        fragment
        for item in peripheral_candidates
        for fragment in _subtract_address_regions(memory_region(item), peripheral_exclusions)
    )
    svd_regions = _svd_peripheral_regions(
        device,
        (*canonical_flash, *canonical_ram, *canonical_rom),
    )
    return PackMemoryGeometry(
        int(flash.start),
        int(flash.start + flash.length),
        int(ram.start),
        int(ram.start + ram.length),
        tuple(sectors),
        driver_digest,
        erased_byte,
        canonical_flash,
        canonical_ram,
        canonical_rom,
        svd_regions or canonical_peripherals,
        cpu_system_regions,
    )
