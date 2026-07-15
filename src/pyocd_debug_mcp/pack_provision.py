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
from dataclasses import dataclass
from pathlib import Path

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

    @property
    def is_pinned(self) -> bool:
        return bool(self.url and self.sha256)


def load_manifest(manifest_path: Path = MANIFEST_PATH) -> list[PackSpec]:
    if not manifest_path.exists():
        return []
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - environment guard
        raise PackProvisionError(
            f"PyYAML is required to read {manifest_path.name}. Run 'uv sync'."
        ) from exc
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    entries = data.get("packs", []) if isinstance(data, dict) else []
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
            )
        )
    return specs


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify(path: Path, expected_sha256: str) -> bool:
    return path.is_file() and _sha256(path) == expected_sha256.lower()


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
    actual = _sha256(dest)
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
    if not packs_dir.is_dir():
        return []
    return sorted(p.resolve() for p in packs_dir.glob("*.pack") if p.is_file())
