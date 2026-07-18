"""Pinned, deterministic CMSIS-Pack provisioning.

The shipped server must not depend on the live cmsis-pack-manager index
(`pyocd pack update` / `pyocd pack install`). That flow bulk-fetches ~1500 vendor
descriptors and silently skips any that fail or time out, producing a partial
index that drops whole families (e.g. STM32L4) on restrictive networks.

Instead, packs are pinned in ``packs/manifest.yaml`` by URL + sha256, fetched on
demand, verified, and loaded by pyOCD via its ``pack`` option in the shared
backend. ``ensure_all`` does the provisioning (network); ``discover_local_packs``
is the network-free runtime lookup used to populate the pyOCD ``pack`` option for
both the Python-API path and the Stage 0 subprocess path.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.firmstore.store import FirmStore

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKS_DIR = REPO_ROOT / "packs"
MANIFEST_PATH = PACKS_DIR / "manifest.yaml"
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

    @property
    def is_pinned(self) -> bool:
        return bool(self.url and self.sha256)


def _text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if not isinstance(value, (list, tuple)):
        raise PackProvisionError(f"Pack manifest field '{field_name}' must be a string or list")
    return tuple(str(item).strip() for item in value if str(item).strip())


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
        specs.append(
            PackSpec(
                id=str(raw["id"]).strip(),
                version=str(raw.get("version", "")).strip(),
                filename=str(raw["filename"]).strip(),
                url=str(raw["url"]).strip(),
                sha256=str(raw["sha256"]).strip().lower(),
                provides_targets=_text_tuple(raw.get("provides_targets"), "provides_targets"),
                needed_by_boards=_text_tuple(raw.get("needed_by_boards"), "needed_by_boards"),
            )
        )
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
    roots = [packs_dir, FirmStore(REPO_ROOT).layout.pack_files]
    artifact_root = os.environ.get("BYO_MCP_ARTIFACT_ROOT", "").strip()
    if artifact_root:
        roots.append(FirmStore(Path(artifact_root).expanduser().resolve()).layout.pack_files)
    discovered = {
        pack.resolve()
        for root in roots
        if root.is_dir()
        for pack in root.glob("*.pack")
        if pack.is_file()
    }
    return sorted(discovered)
