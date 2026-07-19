"""Generic server-owned resolution of exact MCU part support from verified packs."""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from pyocd.target.pack.cmsis_pack import CmsisPack  # type: ignore[import-untyped]

from pyocd_debug_mcp.pack_provision import (
    MANIFEST_PATH,
    DeviceBinding,
    LiveIdentityProof,
    PackProvisionError,
    VerifiedPack,
    load_manifest,
    verified_registry_pack_for_target,
)

_MAX_PACK_MEMBERS = 4_096
_MAX_PACK_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
_MAX_PACK_COMPRESSION_RATIO = 200


def normalize_part_number(value: str) -> str:
    """Normalize only cosmetic separators; never infer a family or wildcard suffix."""

    normalized = "".join(character for character in value.casefold() if character.isalnum())
    if not normalized:
        raise PackProvisionError("MCU part number must contain letters or numbers")
    return normalized


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
    def from_verified_pack(cls, selected: VerifiedPack, binding: DeviceBinding) -> "DeviceSupportCandidate":
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
class PackMemoryGeometry:
    """Conservative default memory geometry parsed from one verified PDSC leaf."""

    flash_start: int
    flash_end: int
    ram_start: int
    ram_end: int

    def to_document(self) -> dict[str, int]:
        return {
            "flash_start": self.flash_start,
            "flash_end": self.flash_end,
            "ram_start": self.ram_start,
            "ram_end": self.ram_end,
        }


class DeviceSupportResolver:
    """Resolve only provisioned exact bindings whose pack bytes expose the PDSC leaf."""

    def __init__(
        self,
        *,
        pack_loader: Callable[[str], VerifiedPack | None],
        device_names: Callable[[VerifiedPack], Iterable[str]] | None = None,
    ) -> None:
        self._pack_loader = pack_loader
        self._device_names = device_names or self._cmsis_device_names

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
                    raise PackProvisionError("CMSIS-Pack member count is outside the supported limit")
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

    def candidates(self, part_number: str, targets: Iterable[str]) -> tuple[DeviceSupportCandidate, ...]:
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


def registered_pack_targets() -> tuple[str, ...]:
    """Return only target names present in the server-owned pack registry."""

    return tuple(
        sorted(
            {
                target.casefold()
                for spec in load_manifest(MANIFEST_PATH)
                for target in spec.provides_targets
                if spec.device_bindings
            }
        )
    )


def has_registered_pack_binding(part_number: str) -> bool:
    """Return whether the server registry owns an exact binding for ``part_number``.

    This intentionally examines only the immutable server manifest, not the pack
    payload.  Callers use it to distinguish "there is no generic record" from
    "the generic record exists but its pinned bytes can no longer be verified";
    the latter must never fall back to a catalog record.
    """

    requested = normalize_part_number(part_number)
    return any(
        normalize_part_number(binding.part_number) == requested
        for spec in load_manifest(MANIFEST_PATH)
        for binding in spec.device_bindings
    )


def resolve_registered_pack_support(part_number: str) -> DeviceSupportCandidate:
    """Resolve one exact local CMSIS-Pack support record from server authority."""

    resolver = DeviceSupportResolver(pack_loader=verified_registry_pack_for_target)
    return resolver.resolve(part_number, registered_pack_targets())


def resolve_registered_pack_geometry(candidate: DeviceSupportCandidate) -> PackMemoryGeometry:
    """Read the default flash/RAM pair from the selected verified PDSC leaf.

    A device can expose several RAM banks.  The generic v3 map deliberately
    exposes only the PDSC default RAM bank until a richer multi-bank authority
    is available; withholding a valid bank is safe, whereas joining disjoint
    banks would fabricate writable addresses across a gap.
    """

    selected = verified_registry_pack_for_target(candidate.pyocd_target)
    if selected is None:
        raise PackProvisionError("registered pack disappeared while resolving device geometry")
    if (
        selected.spec.id != candidate.pack_id
        or selected.spec.filename != candidate.pack_filename
        or selected.spec.sha256 != candidate.pack_sha256
    ):
        raise PackProvisionError("registered pack identity no longer matches the selected candidate")
    try:
        DeviceSupportResolver._validate_archive(selected)
        pack = CmsisPack(io.BytesIO(selected.payload))
        device = next(
            item for item in pack.devices if item.part_number.casefold() == candidate.pdsc_device.casefold()
        )
        regions = tuple(device.memory_map.regions)
    except StopIteration as exc:
        raise PackProvisionError("selected PDSC leaf disappeared from the verified pack") from exc
    except PackProvisionError:
        raise
    except Exception as exc:
        raise PackProvisionError(f"verified pack geometry could not be parsed: {exc}") from exc
    flash = next(
        (
            region
            for region in regions
            if str(region.type).casefold().endswith("flash") and bool(region.is_default)
        ),
        None,
    )
    ram = next(
        (
            region
            for region in regions
            if str(region.type).casefold().endswith("ram")
            and bool(region.is_default)
            and bool(region.is_writable)
        ),
        None,
    )
    if flash is None or ram is None:
        raise PackProvisionError("verified PDSC leaf lacks one default flash and writable RAM region")
    return PackMemoryGeometry(flash.start, flash.start + flash.length, ram.start, ram.start + ram.length)
