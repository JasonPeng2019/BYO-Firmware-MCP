"""Pinned, deterministic CMSIS-Pack provisioning.

The shipped server must not depend on the live cmsis-pack-manager index
(`pyocd pack update` / `pyocd pack install`). That flow bulk-fetches ~1500 vendor
descriptors and silently skips any that fail or time out, producing a partial
index that drops whole families (e.g. STM32L4) on restrictive networks.

Instead, packs are pinned in a project-local verified manifest by URL + sha256, fetched on
demand, verified, and loaded by pyOCD via its ``pack`` option in the shared
backend. ``ensure_all`` does the provisioning (network);
``verified_pack_for_target`` is the network-free runtime selector that binds one
target to one manifest-pinned pack. ``discover_local_packs`` remains inventory
only and never grants target-resolution authority.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.firmstore.store import FirmStore

_artifact_root = os.environ.get("BYO_MCP_ARTIFACT_ROOT", "").strip()
_DEFAULT_PROJECT_ROOT = (
    Path(_artifact_root).expanduser().resolve() if _artifact_root else Path.cwd().resolve()
)
_DEFAULT_STORE = FirmStore(_DEFAULT_PROJECT_ROOT)
PACKS_DIR = _DEFAULT_STORE.layout.pack_files
MANIFEST_PATH = _DEFAULT_STORE.layout.pack_manifest
_CHUNK = 1 << 16


class PackProvisionError(RuntimeError):
    """Raised when a pinned pack cannot be provisioned or verified."""


@dataclass(frozen=True)
class PackSpec:
    id: str
    version: str
    filename: str
    url: str
    sha256: str
    provides_targets: tuple[str, ...] = ()
    needed_by_boards: tuple[str, ...] = ()
    device_bindings: tuple["DeviceBinding", ...] = ()

    @property
    def is_pinned(self) -> bool:
        return bool(self.url and self.sha256)


@dataclass(frozen=True)
class DeviceBinding:
    """One provisioned exact part-to-PDSC-device-to-pyOCD-target relation.

    This is server-owned manifest data established during pack provisioning. Runtime agents
    may select a derived opaque candidate but cannot supply any of these fields.
    """

    part_number: str
    pdsc_device: str
    pyocd_target: str
    identity_proof: "LiveIdentityProof | None" = None

    def __post_init__(self) -> None:
        for name in ("part_number", "pdsc_device", "pyocd_target"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PackProvisionError(f"Device binding {name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())


@dataclass(frozen=True)
class LiveIdentityProof:
    """A provisioned, bounded device identity read (never a board policy)."""

    capability: str
    address: int
    expected: int
    mask: int
    width_bits: int
    label: str

    def __post_init__(self) -> None:
        if self.capability not in {"exact", "compatible"}:
            raise PackProvisionError("identity proof capability must be exact or compatible")
        if self.width_bits not in {8, 16, 32}:
            raise PackProvisionError("identity proof width_bits must be 8, 16, or 32")
        limit = (1 << self.width_bits) - 1
        if (
            not isinstance(self.address, int)
            or self.address < 0
            or not isinstance(self.expected, int)
            or not isinstance(self.mask, int)
            or not 0 <= self.expected <= limit
            or not 0 < self.mask <= limit
            or not isinstance(self.label, str)
            or not self.label.strip()
        ):
            raise PackProvisionError("identity proof fields are invalid")
        object.__setattr__(self, "label", self.label.strip())

    def to_document(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "address": self.address,
            "expected": self.expected,
            "mask": self.mask,
            "width_bits": self.width_bits,
            "label": self.label,
        }


@dataclass(frozen=True)
class VerifiedPack:
    """One manifest-selected pack whose bytes have been verified."""

    path: Path
    spec: PackSpec
    payload: bytes

    def verify_unchanged(self) -> None:
        """Fail closed if the selected pack is absent or no longer pinned bytes."""

        if not _verify(self.path, self.spec.sha256):
            raise PackProvisionError(
                f"Pinned pack changed or disappeared: {self.path} "
                f"(expected sha256 {self.spec.sha256})."
            )


def _text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if not isinstance(value, (list, tuple)):
        raise PackProvisionError(f"Pack manifest field '{field_name}' must be a string or list")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _device_bindings(value: object) -> tuple[DeviceBinding, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PackProvisionError("Pack manifest field 'device_bindings' must be a list")
    bindings: list[DeviceBinding] = []
    seen_parts: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise PackProvisionError(f"device_bindings[{index}] must be an object")
        fields = {"part_number", "pdsc_device", "pyocd_target"}
        optional = {"identity_proof"}
        if not fields.issubset(item) or set(item) - fields - optional:
            raise PackProvisionError(
                f"device_bindings[{index}] must contain {sorted(fields)} and optional identity_proof"
            )
        raw_proof = item.get("identity_proof")
        proof: LiveIdentityProof | None = None
        if raw_proof is not None:
            proof_fields = {"capability", "address", "expected", "mask", "width_bits", "label"}
            if not isinstance(raw_proof, dict) or set(raw_proof) != proof_fields:
                raise PackProvisionError("device binding identity_proof has an invalid field set")
            proof = LiveIdentityProof(
                str(raw_proof["capability"]),
                raw_proof["address"],  # type: ignore[arg-type]
                raw_proof["expected"],  # type: ignore[arg-type]
                raw_proof["mask"],  # type: ignore[arg-type]
                raw_proof["width_bits"],  # type: ignore[arg-type]
                str(raw_proof["label"]),
            )
        binding = DeviceBinding(
            str(item["part_number"]), str(item["pdsc_device"]), str(item["pyocd_target"]), proof
        )
        key = binding.part_number.casefold()
        if key in seen_parts:
            raise PackProvisionError(
                f"device_bindings must not duplicate exact part {binding.part_number!r}"
            )
        seen_parts.add(key)
        bindings.append(binding)
    return tuple(bindings)


def load_manifest_document(manifest_path: Path = MANIFEST_PATH) -> dict[str, Any]:
    """Load and structurally validate the authoritative pack manifest.

    Setup candidate promotion and ordinary pinned-pack provisioning deliberately
    share this parser so there is only one manifest interpretation path.
    """

    if not manifest_path.exists():
        return {"packs": []}
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - environment guard
        raise PackProvisionError(
            f"PyYAML is required to read {manifest_path.name}. Run 'uv sync'."
        ) from exc
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise PackProvisionError(f"Pack manifest root must be a mapping in {manifest_path}")
    entries = data.get("packs", [])
    if not isinstance(entries, list):
        raise PackProvisionError(f"Pack manifest field 'packs' must be a list in {manifest_path}")
    return dict(data)


def load_manifest(manifest_path: Path = MANIFEST_PATH) -> list[PackSpec]:
    entries = load_manifest_document(manifest_path).get("packs", [])
    specs: list[PackSpec] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        missing = [k for k in ("id", "filename", "url", "sha256") if not raw.get(k)]
        if missing:
            raise PackProvisionError(
                f"Pack manifest entry is missing required field(s) {missing} in {manifest_path}"
            )
        spec = PackSpec(
            id=str(raw["id"]).strip(),
            version=str(raw.get("version", "")).strip(),
            filename=str(raw["filename"]).strip(),
            url=str(raw["url"]).strip(),
            sha256=str(raw["sha256"]).strip().lower(),
            provides_targets=_text_tuple(raw.get("provides_targets"), "provides_targets"),
            needed_by_boards=_text_tuple(raw.get("needed_by_boards"), "needed_by_boards"),
            device_bindings=_device_bindings(raw.get("device_bindings")),
        )
        if (
            Path(spec.filename).name != spec.filename
            or Path(spec.filename).suffix.casefold() != ".pack"
        ):
            raise PackProvisionError(
                f"Pack manifest filename must be one plain .pack name in {manifest_path}"
            )
        if len(spec.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in spec.sha256
        ):
            raise PackProvisionError(
                f"Pack manifest sha256 must be 64 hexadecimal digits in {manifest_path}"
            )
        targets = {target.casefold() for target in spec.provides_targets}
        for binding in spec.device_bindings:
            if binding.pyocd_target.casefold() not in targets:
                raise PackProvisionError(
                    f"Device binding target {binding.pyocd_target!r} is not declared by {spec.id}"
                )
        specs.append(spec)
    return specs


def pack_spec_document(spec: PackSpec) -> dict[str, Any]:
    """Return the canonical manifest representation of a validated pack spec."""

    document: dict[str, Any] = {
        "id": spec.id,
        "version": spec.version,
        "filename": spec.filename,
        "url": spec.url,
        "sha256": spec.sha256.lower(),
    }
    if spec.provides_targets:
        document["provides_targets"] = list(spec.provides_targets)
    if spec.needed_by_boards:
        document["needed_by_boards"] = list(spec.needed_by_boards)
    if spec.device_bindings:
        document["device_bindings"] = [
            {
                "part_number": binding.part_number,
                "pdsc_device": binding.pdsc_device,
                "pyocd_target": binding.pyocd_target,
                **(
                    {"identity_proof": binding.identity_proof.to_document()}
                    if binding.identity_proof is not None
                    else {}
                ),
            }
            for binding in spec.device_bindings
        ]
    return document


def sha256_bytes(payload: bytes) -> str:
    """Return the lowercase SHA-256 digest used by every pack path."""

    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """Hash a pack file without loading the whole artifact into memory."""

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def read_pack_bytes(path: Path) -> bytes:
    """Read one non-empty pack exactly and reject a file changed during the read."""

    try:
        size = path.stat().st_size
        if size <= 0 or not path.is_file():
            raise PackProvisionError("CMSIS-Pack archive must be a non-empty regular file")
        payload = path.read_bytes()
    except PackProvisionError:
        raise
    except OSError as exc:
        raise PackProvisionError(f"CMSIS-Pack archive cannot be read: {exc}") from exc
    if len(payload) != size:
        raise PackProvisionError("CMSIS-Pack archive changed while it was being read")
    return payload


def verified_pack_for_spec(
    spec: PackSpec, *, packs_dir: Path = PACKS_DIR
) -> VerifiedPack:
    """Load one exact manifest-selected package without target-wide provider lookup."""

    roots = [packs_dir]
    candidates: list[Path] = []
    for root in roots:
        candidate = root / spec.filename
        if candidate.is_file():
            resolved = candidate.resolve()
            if resolved not in candidates:
                candidates.append(resolved)
    if not candidates:
        raise PackProvisionError(
            f"Pinned pack is absent: expected exact package {spec.id} / {spec.filename}."
        )
    selected: VerifiedPack | None = None
    for candidate in candidates:
        payload = read_pack_bytes(candidate)
        if sha256_bytes(payload) != spec.sha256:
            raise PackProvisionError(
                f"Pinned pack checksum mismatch for {candidate}: expected {spec.sha256}."
            )
        if selected is None:
            selected = VerifiedPack(candidate, spec, payload)
    assert selected is not None
    return selected


def _verify(path: Path, expected_sha256: str) -> bool:
    return path.is_file() and sha256_file(path) == expected_sha256.lower()


def _download(url: str, dest: Path) -> None:
    tmp = dest.parent / (dest.name + ".part")
    try:
        import httpx

        with httpx.stream("GET", url, follow_redirects=True, timeout=300.0) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(_CHUNK):
                    fh.write(chunk)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise PackProvisionError(f"Failed to download {url}: {exc}") from exc
    tmp.replace(dest)


def ensure_pack(spec: PackSpec, packs_dir: Path = PACKS_DIR) -> Path:
    """Return a local path to the verified pack, downloading it if needed."""
    packs_dir.mkdir(parents=True, exist_ok=True)
    dest = packs_dir / spec.filename
    if _verify(dest, spec.sha256):
        return dest
    if not spec.is_pinned:
        raise PackProvisionError(
            f"Pack {spec.id} is not pinned (needs url + sha256) and is absent at {dest}."
        )
    _download(spec.url, dest)
    actual = sha256_file(dest)
    if actual != spec.sha256:
        dest.unlink(missing_ok=True)
        raise PackProvisionError(
            f"Checksum mismatch for {spec.filename}: expected {spec.sha256}, got {actual}. "
            "Downloaded file removed."
        )
    return dest


def ensure_all(manifest_path: Path = MANIFEST_PATH, packs_dir: Path = PACKS_DIR) -> list[Path]:
    """Provision every pinned pack in the manifest; returns local paths."""
    return [ensure_pack(spec, packs_dir) for spec in load_manifest(manifest_path)]


def discover_local_packs(packs_dir: Path = PACKS_DIR) -> list[Path]:
    """Return local ``*.pack`` files present, for pyOCD's ``pack`` option.

    Network-free: only returns files already on disk. ``ensure_all`` is what
    fetches them; runtime just loads whatever is present so a connect never
    depends on the live pack index.
    """
    roots = [packs_dir]
    discovered = {
        pack.resolve()
        for root in roots
        if root.is_dir()
        for pack in root.iterdir()
        if pack.is_file() and pack.suffix.casefold() == ".pack"
    }
    return sorted(discovered)


def verified_pack_for_target(
    target: str,
    *,
    manifest_path: Path = MANIFEST_PATH,
    packs_dir: Path = PACKS_DIR,
) -> VerifiedPack | None:
    """Return the sole pinned local pack that authoritatively provides ``target``.

    Targets absent from the manifest are expected to be built into pyOCD and
    therefore return ``None``. A manifest target must have exactly one provider;
    runtime never gives pyOCD unrelated packs and never silently accepts stale
    or differently-versioned bytes.
    """

    normalized = target.strip().casefold()
    sources = [(manifest_path, packs_dir)]
    matching_sources = [
        (spec, source_packs)
        for source_manifest, source_packs in sources
        for spec in load_manifest(source_manifest)
        if normalized in {item.casefold() for item in spec.provides_targets}
    ]
    # Multiple entries for the same immutable package do not create two providers.
    unique: dict[
        tuple[str, str, str, str, str, tuple[str, ...]],
        tuple[PackSpec, list[Path]],
    ] = {}
    for spec, source_packs in matching_sources:
        provider_key = (
            spec.id,
            spec.version,
            spec.filename,
            spec.url,
            spec.sha256,
            spec.provides_targets,
        )
        row = unique.setdefault(provider_key, (spec, []))
        row[1].append(source_packs)
    matches = list(unique.values())
    if not matches:
        return None
    if len(matches) != 1:
        raise PackProvisionError(
            f"Target {target!r} has {len(matches)} providers in the pinned pack manifest; "
            "exactly one is required."
        )
    spec, provider_roots = matches[0]

    # Only exact bytes beneath the selected project store are eligible.
    roots = list(provider_roots)
    candidates: list[Path] = []
    for root in roots:
        candidate = root / spec.filename
        if candidate.is_file():
            resolved = candidate.resolve()
            if resolved not in candidates:
                candidates.append(resolved)
    if not candidates:
        raise PackProvisionError(
            f"Pinned pack for target {target!r} is absent: expected {spec.filename}. "
            "Complete project-local pack setup first."
        )
    payloads: list[tuple[Path, bytes]] = []
    for candidate in candidates:
        payload = read_pack_bytes(candidate)
        if sha256_bytes(payload) != spec.sha256:
            raise PackProvisionError(
                f"Pinned pack checksum mismatch for {candidate}: expected {spec.sha256}."
            )
        payloads.append((candidate, payload))
    selected_path, selected_payload = payloads[0]
    return VerifiedPack(path=selected_path, spec=spec, payload=selected_payload)
