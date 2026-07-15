"""Repair a partial live CMSIS-Pack descriptor index.

`pyocd pack update` delegates to cmsis-pack-manager, which bulk-fetches every
descriptor in the master index. On some hosts that fan-out silently drops a
subset of PDSCs, leaving `index.json` missing entire device families even though
the command appears to succeed.

This module provides a deterministic repair path:

1. Download the master `index.pidx`.
2. Select exact descriptors to repair (all missing, or a vendor/name subset).
3. Fetch those PDSCs one-by-one with retries.
4. Rebuild the local `index.json` / `aliases.json` from every cached PDSC.

It exists for operators and diagnostics. The shipped runtime still prefers the
repo's pinned `.pack` manifest for target support.
"""

from __future__ import annotations

import argparse
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, cast
from urllib.parse import urljoin

import httpx
from cmsis_pack_manager import Cache, ffi, lib  # type: ignore[import-untyped]

DEFAULT_MASTER_INDEX_URL = "https://www.keil.com/pack/index.pidx"
_CHUNK = 1 << 16


class PackIndexRepairError(RuntimeError):
    """Raised when the live CMSIS-Pack index cannot be repaired."""


@dataclass(frozen=True)
class PdscRef:
    vendor: str
    name: str
    version: str
    base_url: str

    @property
    def cache_filename(self) -> str:
        return f"{self.vendor}.{self.name}.{self.version}.pdsc"

    @property
    def remote_pdsc_url(self) -> str:
        return urljoin(self.base_url, f"{self.vendor}.{self.name}.pdsc")


@dataclass(frozen=True)
class RepairResult:
    master_count: int
    selected_count: int
    download_count: int
    cached_pdsc_count: int
    device_count: int
    data_path: Path


def parse_master_index(xml_text: str) -> list[PdscRef]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise PackIndexRepairError(f"Failed to parse CMSIS master index: {exc}") from exc

    refs: list[PdscRef] = []
    for node in root.findall(".//pdsc"):
        vendor = (node.attrib.get("vendor") or "").strip()
        name = (node.attrib.get("name") or "").strip()
        version = (node.attrib.get("version") or "").strip()
        base_url = (node.attrib.get("url") or "").strip()
        if not vendor or not name or not version or not base_url:
            continue
        refs.append(PdscRef(vendor=vendor, name=name, version=version, base_url=base_url))
    return refs


def fetch_master_index(
    index_url: str = DEFAULT_MASTER_INDEX_URL, timeout: float = 60.0
) -> list[PdscRef]:
    try:
        response = httpx.get(index_url, follow_redirects=True, timeout=timeout)
        response.raise_for_status()
    except Exception as exc:
        raise PackIndexRepairError(
            f"Failed to fetch CMSIS master index {index_url}: {exc}"
        ) from exc
    return parse_master_index(response.text)


def select_refs(
    refs: Iterable[PdscRef],
    *,
    vendors: Iterable[str] = (),
    pack_names: Iterable[str] = (),
    name_contains: Iterable[str] = (),
) -> list[PdscRef]:
    vendor_set = {item.strip().lower() for item in vendors if item.strip()}
    pack_name_set = {item.strip().lower() for item in pack_names if item.strip()}
    contains = [item.strip().lower() for item in name_contains if item.strip()]

    selected: list[PdscRef] = []
    for ref in refs:
        if vendor_set and ref.vendor.lower() not in vendor_set:
            continue
        if pack_name_set and ref.name.lower() not in pack_name_set:
            continue
        if contains and not all(token in ref.name.lower() for token in contains):
            continue
        selected.append(ref)
    return selected


def descriptor_path(data_path: Path, ref: PdscRef) -> Path:
    return data_path / ref.cache_filename


def plan_downloads(
    refs: Iterable[PdscRef], data_path: Path, *, missing_only: bool = True
) -> list[PdscRef]:
    planned: list[PdscRef] = []
    for ref in refs:
        if missing_only and descriptor_path(data_path, ref).is_file():
            continue
        planned.append(ref)
    return planned


def _download_descriptor(ref: PdscRef, dest: Path, *, timeout: float, retries: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f"{dest.name}.part"
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            with httpx.stream(
                "GET",
                ref.remote_pdsc_url,
                follow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": "pyocd-debug-mcp/pack-index-repair"},
            ) as response:
                response.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in response.iter_bytes(_CHUNK):
                        if chunk:
                            fh.write(chunk)

            prefix = tmp.read_bytes()[:256].lstrip()
            if not prefix.startswith(b"<?xml") and b"<package" not in prefix:
                raise PackIndexRepairError(
                    f"Downloaded {ref.remote_pdsc_url} but content did not look like a PDSC XML file"
                )
            tmp.replace(dest)
            return
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            last_error = exc
            if attempt < retries:
                time.sleep(min(float(attempt), 3.0))

    assert last_error is not None
    raise PackIndexRepairError(
        f"Failed to fetch {ref.remote_pdsc_url}: {last_error}"
    ) from last_error


def rebuild_cached_index(cache: Cache) -> tuple[int, int]:
    data_path = Path(cache.data_path)
    pdsc_files = sorted(path for path in data_path.glob("*.pdsc") if path.is_file())
    if not pdsc_files:
        raise PackIndexRepairError(f"No cached PDSC files found under {data_path}")
    Path(cache.index_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cache.aliases_path).parent.mkdir(parents=True, exist_ok=True)

    update_pdsc_index_new = cast(Callable[[], Any], getattr(lib, "update_pdsc_index_new"))
    update_pdsc_index_free = cast(Callable[[Any], None], getattr(lib, "update_pdsc_index_free"))
    update_pdsc_index_push = cast(
        Callable[[Any, Any], None], getattr(lib, "update_pdsc_index_push")
    )

    pdsc_index = ffi.gc(update_pdsc_index_new(), update_pdsc_index_free)
    for path in pdsc_files:
        cpath = ffi.new("char[]", str(path).encode("utf-8"))
        update_pdsc_index_push(pdsc_index, cpath)

    parsed = cache._call_rust_parse(pdsc_index)
    cache._call_rust_dump(parsed)
    cache._index = {}
    cache._aliases = {}
    return len(pdsc_files), len(cache.index)


def repair_live_pack_index(
    *,
    vendors: Iterable[str] = (),
    pack_names: Iterable[str] = (),
    name_contains: Iterable[str] = (),
    missing_only: bool = True,
    timeout: float = 60.0,
    retries: int = 3,
    index_url: str = DEFAULT_MASTER_INDEX_URL,
    json_path: str | None = None,
    data_path: str | None = None,
) -> RepairResult:
    cache = Cache(True, False, json_path=json_path, data_path=data_path)
    refs = fetch_master_index(index_url=index_url, timeout=timeout)
    selected = select_refs(
        refs, vendors=vendors, pack_names=pack_names, name_contains=name_contains
    )
    if not selected:
        raise PackIndexRepairError("No descriptors matched the requested filters")

    cache_dir = Path(cache.data_path)
    downloads = plan_downloads(selected, cache_dir, missing_only=missing_only)
    for ref in downloads:
        _download_descriptor(ref, descriptor_path(cache_dir, ref), timeout=timeout, retries=retries)

    cached_pdsc_count, device_count = rebuild_cached_index(cache)
    return RepairResult(
        master_count=len(refs),
        selected_count=len(selected),
        download_count=len(downloads),
        cached_pdsc_count=cached_pdsc_count,
        device_count=device_count,
        data_path=cache_dir,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Repair a partial cmsis-pack-manager descriptor cache with controlled fetches."
    )
    parser.add_argument(
        "--vendor",
        action="append",
        default=[],
        help="Exact vendor filter. Repeat to allow multiple vendors (for example: Keil).",
    )
    parser.add_argument(
        "--pack-name",
        action="append",
        default=[],
        help="Exact pack-name filter. Repeat to repair multiple packs (for example: STM32L4xx_DFP).",
    )
    parser.add_argument(
        "--name-contains",
        action="append",
        default=[],
        help="Substring filter applied to the pack name. Repeat to require multiple substrings.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Redownload matching descriptors even if they already exist locally.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Per-request timeout in seconds (default: 60).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Retry attempts per descriptor (default: 3).",
    )
    parser.add_argument(
        "--index-url",
        default=DEFAULT_MASTER_INDEX_URL,
        help="Override the master CMSIS pack index URL.",
    )
    parser.add_argument("--json-path", help="Override the cmsis-pack-manager json cache path.")
    parser.add_argument(
        "--data-path", help="Override the cmsis-pack-manager descriptor cache path."
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = repair_live_pack_index(
            vendors=args.vendor,
            pack_names=args.pack_name,
            name_contains=args.name_contains,
            missing_only=not args.refresh,
            timeout=args.timeout,
            retries=args.retries,
            index_url=args.index_url,
            json_path=args.json_path,
            data_path=args.data_path,
        )
    except PackIndexRepairError as exc:
        print(f"[FAIL] {exc}")
        return 1

    print(f"[PASS] master index descriptors: {result.master_count}")
    print(f"[PASS] selected descriptors: {result.selected_count}")
    print(f"[PASS] downloaded descriptors: {result.download_count}")
    print(f"[PASS] cached descriptors after rebuild: {result.cached_pdsc_count}")
    print(f"[PASS] devices in rebuilt index: {result.device_count}")
    print(f"[PASS] cache path: {result.data_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
