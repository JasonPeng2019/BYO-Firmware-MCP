"""Strict Windows mapping, sole release inventory and process-local receipts.

The launcher owns release trust and whole-runtime verification. This module binds
its supplied immutable root to the selected analyzer's bytes and SBOM references.
It never repairs a runtime or maintains a second digest inventory.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.code_analysis_config import AnalysisConfigError, strict_json
from pyocd_debug_mcp.code_analysis_runtime import Cancellation, remaining

_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_HASH = re.compile(r"[0-9a-f]{64}")
_lock = threading.Lock()


class RuntimeIntegrityError(ValueError):
    def __init__(
        self,
        path: str,
        cause: str,
        *,
        expected: Any = None,
        observed: Any = None,
        changed: bool = False,
    ) -> None:
        super().__init__(f"{path}: {cause}")
        self.path = path
        self.expected = {path: expected} if expected is not None else {}
        self.observed = {path: observed} if observed is not None else {}
        self.changed = changed
        self.receipt: PayloadReceipt | None = None
        self.metadata: _Metadata | None = None


def _require(ok: bool, path: str, cause: str) -> None:
    if not ok:
        raise RuntimeIntegrityError(path, cause)


def _object(value: Any, keys: set[str], path: str) -> dict[str, Any]:
    _require(isinstance(value, dict), path, "must be an object")
    _require(set(value) == keys, path, f"requires exactly {sorted(keys)}; got {sorted(value)}")
    return value


def _relative(value: Any) -> str:
    _require(isinstance(value, str) and bool(value), str(value), "must be a relative path string")
    for part in value.split("/"):
        _require(
            bool(part)
            and part not in {".", ".."}
            and not part.endswith((".", " "))
            and not any(ord(c) < 32 or c in '\\<>:"|?*' for c in part),
            value,
            "unsafe Windows path spelling",
        )
        base = part.split(".")[0].upper()
        _require(
            base not in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
            and re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", base) is None,
            value,
            "Windows reserved name",
        )
    return value


def _identity(path: Path, deadline: float, cancellation: Cancellation | None) -> tuple[Any, ...]:
    remaining(deadline, cancellation)
    info = path.lstat()
    _require(
        not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
        str(path),
        "symlink/reparse traversal is forbidden",
    )
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        getattr(info, "st_file_attributes", 0),
    )


def _path(
    root: Path,
    relative: str,
    deadline: float,
    cancellation: Cancellation | None,
    *,
    exact: bool = False,
) -> Path:
    path = root
    _identity(path, deadline, cancellation)
    for part in relative.split("/"):
        if exact:
            found = False
            with os.scandir(path) as entries:
                for entry in entries:
                    remaining(deadline, cancellation)
                    found = found or entry.name == part
            _require(found, relative, "filesystem spelling differs from canonical reference")
        path = path / part
        _identity(path, deadline, cancellation)
    _require(path.resolve().is_relative_to(root), relative, "path escapes runtime root")
    return path


def hash_file(
    path: Path,
    deadline: float,
    cancellation: Cancellation | None,
    *,
    collect: bool = False,
) -> tuple[str, bytes, tuple[Any, ...]]:
    before = _identity(path, deadline, cancellation)
    _require(stat.S_ISREG(before[2]), str(path), "must be a readable regular file")
    digest = hashlib.sha256()
    chunks = []
    with path.open("rb") as stream:
        while True:
            remaining(deadline, cancellation)
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if collect:
                chunks.append(chunk)
    after = _identity(path, deadline, cancellation)
    if after != before:
        raise RuntimeIntegrityError(
            str(path), "changed while reading", expected=before, observed=after, changed=True
        )
    return digest.hexdigest(), b"".join(chunks), after


def _windows_executable(path: Path, deadline: float, cancellation: Cancellation | None) -> None:
    # Check the selected executable's target before running it. Import closure
    # and upstream distribution completeness remain release-validator work.
    remaining(deadline, cancellation)
    with path.open("rb") as stream:
        dos = stream.read(64)
        _require(len(dos) == 64 and dos[:2] == b"MZ", str(path), "requires Windows PE executable")
        offset = int.from_bytes(dos[60:64], "little")
        _require(offset >= 64, str(path), "invalid PE header offset")
        remaining(deadline, cancellation)
        stream.seek(offset)
        header = stream.read(26)
        _require(
            len(header) == 26
            and header[:4] == b"PE\0\0"
            and int.from_bytes(header[4:6], "little") == 0x8664
            and int.from_bytes(header[20:22], "little") >= 2
            and int.from_bytes(header[24:26], "little") == 0x20B,
            str(path),
            "requires AMD64 architecture and PE32+ executable",
        )
    remaining(deadline, cancellation)


def _decode(data: bytes, path: str, deadline: float, cancellation: Cancellation | None) -> Any:
    remaining(deadline, cancellation)
    try:
        value = strict_json(data)
    except (ValueError, UnicodeError, AnalysisConfigError) as exc:
        raise RuntimeIntegrityError(path, f"invalid strict JSON: {exc}") from exc
    remaining(deadline, cancellation)
    return value


def _mapping(value: Any, deadline: float, cancellation: Cancellation | None) -> dict[str, Any]:
    value = _object(
        value, {"schema_version", "platform", "architecture", "programs"}, "analysis/runtime.json"
    )
    _require(
        type(value["schema_version"]) is int and value["schema_version"] == 1,
        "schema_version",
        "must be JSON integer 1",
    )
    _require(
        value["platform"] == "windows" and value["architecture"] == "x86_64",
        "platform/architecture",
        "requires windows/x86_64",
    )
    _require(
        os.name == "nt" and platform.machine().lower() in {"amd64", "x86_64"},
        "host.architecture",
        "requires Windows x86_64 host",
    )
    programs = _object(value["programs"], {"clangd", "cppcheck"}, "programs")
    for name, program in programs.items():
        remaining(deadline, cancellation)
        dirs = {"resource_dir"} if name == "clangd" else {"cfg_dir", "platforms_dir"}
        program = _object(
            program,
            {"version", "executable", "licenses", "dependencies", "sbom_ref"} | dirs,
            f"programs.{name}",
        )
        version = program["version"]
        _require(
            isinstance(version, str) and _VERSION.fullmatch(version) is not None,
            f"programs.{name}.version",
            "requires three-part numeric version",
        )
        expected = {
            "executable": f"analysis/{name}/"
            + ("bin/clangd.exe" if name == "clangd" else "cppcheck.exe")
        }
        if name == "clangd":
            major = version.split(".")[0]
            _require(int(major) > 0, "resource_dir", "resource major must be positive")
            expected["resource_dir"] = f"analysis/clangd/lib/clang/{major}"
            licenses = {"analysis/clangd/LICENSE.TXT"}
        else:
            expected.update(
                cfg_dir="analysis/cppcheck/cfg", platforms_dir="analysis/cppcheck/platforms"
            )
            licenses = {
                "analysis/cppcheck/licenses/" + p
                for p in ("COPYING", "simplecpp-LICENSE", "tinyxml2-LICENSE", "picojson-LICENSE")
            }
        for key, spelling in expected.items():
            _require(
                program[key] == spelling, f"programs.{name}.{key}", f"must be exactly {spelling}"
            )
        for key in ("licenses", "dependencies"):
            values = program[key]
            _require(
                isinstance(values, list), f"programs.{name}.{key}", "must be an explicit array"
            )
            seen = set()
            for item in values:
                remaining(deadline, cancellation)
                field = f"programs.{name}.{key}"
                try:
                    path = _relative(item)
                except RuntimeIntegrityError as exc:
                    raise RuntimeIntegrityError(field, str(exc)) from exc
                _require(
                    path.casefold() not in seen, field, f"duplicate/colliding reference: {path}"
                )
                seen.add(path.casefold())
                if key == "licenses":
                    _require(
                        path in licenses or path.startswith(f"analysis/{name}/licenses/"),
                        field,
                        "notice outside license namespace",
                    )
                else:
                    parent = f"analysis/{name}" + ("/bin" if name == "clangd" else "")
                    _require(
                        path.rsplit("/", 1)[0] == parent and path.endswith(".dll"),
                        field,
                        "dependency must be an adjacent DLL",
                    )
            if key == "licenses":
                _require(
                    licenses <= set(values),
                    f"programs.{name}.licenses",
                    "mandatory notices missing",
                )
        _require(
            program["sbom_ref"] == f"byo-analysis:{name}",
            f"programs.{name}.sbom_ref",
            "wrong application reference",
        )
    return programs


@dataclass
class _Metadata:
    root: Path
    programs: dict[str, Any]
    inventory: dict[str, dict[str, Any]]
    sbom: dict[str, Any]
    hashes: dict[str, str]
    identities: dict[Path, tuple[Any, ...]]


@dataclass
class PayloadReceipt:
    metadata: _Metadata
    name: str
    executable: Path
    data_paths: tuple[Path, ...]
    identities: dict[Path, tuple[Any, ...]]
    directories: dict[Path, tuple[str, ...]]
    identity: tuple[Any, ...] = field(init=False)

    def __post_init__(self) -> None:
        self.identity = (
            str(self.metadata.root),
            self.name,
            tuple(self.metadata.hashes.items()),
            tuple((str(p), i) for p, i in self.identities.items()),
        )


_metadata_cache: dict[Path, _Metadata] = {}
_receipt_cache: dict[tuple[Path, str], PayloadReceipt] = {}


def _inventory(
    manifest: Any, deadline: float, cancellation: Cancellation | None
) -> dict[str, dict[str, Any]]:
    manifest = _object(
        manifest,
        {
            "schema",
            "product",
            "version",
            "channel",
            "platform",
            "architecture",
            "launcher_protocol",
            "sidecar_protocol",
            "worker_protocol",
            "workflow_protocol",
            "capsule_schema",
            "project_state_schema",
            "development_unsigned",
            "source",
            "toolchain",
            "files",
        },
        "release-manifest.json",
    )
    _require(
        type(manifest.get("schema")) is int and manifest["schema"] == 1,
        "release-manifest.json.schema",
        "requires schema 1",
    )
    _require(
        manifest.get("platform") == "windows" and manifest.get("architecture") == "x86_64",
        "release-manifest.json.platform/architecture",
        "mapping and release target must agree",
    )
    _require(
        isinstance(manifest.get("version"), str) and bool(manifest["version"]),
        "release-manifest.json.version",
        "requires release version",
    )
    _require(
        manifest["product"] == "byo"
        and isinstance(manifest["channel"], str)
        and type(manifest["development_unsigned"]) is bool,
        "release-manifest.json",
        "invalid product/channel/development_unsigned",
    )
    for key in (
        "launcher_protocol",
        "sidecar_protocol",
        "worker_protocol",
        "workflow_protocol",
        "capsule_schema",
        "project_state_schema",
    ):
        _require(
            type(manifest[key]) is int and manifest[key] == 1,
            key,
            "requires integer protocol/schema 1",
        )
    source = _object(manifest["source"], {"installer", "agent_workspace", "firmware_mcp"}, "source")
    for key, value in source.items():
        revision = _object(value, {"repository", "commit"}, f"source.{key}")
        _require(
            isinstance(revision["repository"], str)
            and bool(revision["repository"])
            and isinstance(revision["commit"], str)
            and re.fullmatch(r"[0-9a-f]{40}", revision["commit"]) is not None,
            f"source.{key}",
            "invalid source revision",
        )
    toolchain = _object(manifest["toolchain"], {"rustc", "cargo", "python", "nuitka"}, "toolchain")
    _require(
        all(isinstance(v, str) and bool(v) for v in toolchain.values()),
        "toolchain",
        "requires toolchain version strings",
    )
    files = manifest.get("files")
    _require(isinstance(files, list), "release-manifest.json.files", "requires file inventory")
    inventory = {}
    spellings = {}
    for entry in files:
        remaining(deadline, cancellation)
        entry = _object(entry, {"path", "sha256", "size", "kind", "executable"}, "files entry")
        path = _relative(entry["path"])
        _require(path not in inventory, path, "duplicate inventory leaf")
        _require(
            isinstance(entry["sha256"], str) and _HASH.fullmatch(entry["sha256"]) is not None,
            path,
            "requires inventory SHA-256",
        )
        _require(
            type(entry["size"]) is int and 0 <= entry["size"] < 2**64,
            path,
            "requires unsigned integer byte size",
        )
        _require(
            isinstance(entry["kind"], str) and type(entry["executable"]) is bool,
            path,
            "invalid inventory kind/executable types",
        )
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            previous = spellings.setdefault(prefix.casefold(), prefix)
            _require(previous == prefix, path, f"case collision with {previous}")
            if index < len(parts):
                _require(prefix not in inventory, path, "inventory file/directory collision")
        inventory[path] = entry
    for path in inventory:
        remaining(deadline, cancellation)
        _require(
            not any(
                p in inventory
                for p in ("/".join(path.split("/")[:i]) for i in range(1, len(path.split("/"))))
            ),
            path,
            "inventory file/directory collision",
        )
    return inventory


def _check_digest(path: str, digest: str, identity: tuple[Any, ...], entry: dict[str, Any]) -> None:
    if entry["sha256"] != digest or entry["size"] != identity[3]:
        raise RuntimeIntegrityError(
            path,
            "inventory size/SHA-256 mismatch",
            expected={"sha256": entry["sha256"], "size": entry["size"]},
            observed={"sha256": digest, "size": identity[3]},
        )


def _load(root: Path, deadline: float, cancellation: Cancellation | None) -> _Metadata:
    hashes = {}
    identities = {}
    manifest_path = _path(root, "release-manifest.json", deadline, cancellation, exact=True)
    digest, data, identity = hash_file(manifest_path, deadline, cancellation, collect=True)
    hashes["release-manifest.json"] = digest
    identities[manifest_path] = identity
    inventory = _inventory(
        _decode(data, "release-manifest.json", deadline, cancellation), deadline, cancellation
    )
    decoded = {}
    for relative in ("analysis/runtime.json", "sbom.cdx.json"):
        _require(relative in inventory, relative, "metadata missing from release inventory")
        entry = inventory[relative]
        _require(
            not entry["executable"]
            and (relative != "analysis/runtime.json" or entry["kind"] == "metadata"),
            relative,
            "invalid metadata inventory classification",
        )
        path = _path(root, relative, deadline, cancellation, exact=True)
        digest, data, identity = hash_file(path, deadline, cancellation, collect=True)
        _check_digest(relative, digest, identity, entry)
        hashes[relative] = digest
        identities[path] = identity
        decoded[relative] = _decode(data, relative, deadline, cancellation)
    programs = _mapping(decoded["analysis/runtime.json"], deadline, cancellation)
    sbom = decoded["sbom.cdx.json"]
    _require(
        isinstance(sbom, dict) and sbom.get("bomFormat") == "CycloneDX",
        "sbom.cdx.json",
        "requires CycloneDX object",
    )
    return _Metadata(root, programs, inventory, sbom, hashes, identities)


def _scan(
    root: Path, relative: str, deadline: float, cancellation: Cancellation | None
) -> tuple[dict[Path, tuple[Any, ...]], dict[Path, tuple[str, ...]]]:
    identities = {}
    directories = {}

    def visit(path: Path) -> None:
        identity = _identity(path, deadline, cancellation)
        identities[path] = identity
        if stat.S_ISDIR(identity[2]):
            names = []
            with os.scandir(path) as entries:
                for entry in entries:
                    remaining(deadline, cancellation)
                    _relative(entry.name)
                    names.append(entry.name)
                    visit(path / entry.name)
            _require(
                len({n.casefold() for n in names}) == len(names),
                str(path),
                "filesystem case collision",
            )
            directories[path] = tuple(sorted(names))
        else:
            _require(stat.S_ISREG(identity[2]), str(path), "nonregular payload")

    visit(_path(root, relative, deadline, cancellation, exact=True))
    return identities, directories


def _sbom(
    metadata: _Metadata,
    name: str,
    leaves: set[str],
    deadline: float,
    cancellation: Cancellation | None,
) -> None:
    sbom = metadata.sbom
    components = sbom.get("components")
    _require(isinstance(components, list), "sbom.components", "requires component list")
    assert isinstance(components, list)
    refs = {}
    for item in components:
        remaining(deadline, cancellation)
        _require(
            isinstance(item, dict)
            and isinstance(item.get("bom-ref"), str)
            and bool(item["bom-ref"]),
            "sbom.components",
            "missing component reference",
        )
        ref = item["bom-ref"]
        _require(ref not in refs, ref, "duplicate SBOM reference")
        refs[ref] = item
    program = metadata.programs[name]
    reference = program["sbom_ref"]
    application = refs.get(reference, {})
    _require(
        application.get("type") == "application"
        and application.get("name") == name
        and application.get("version") == program["version"],
        reference,
        "application name/version mismatch",
    )
    licenses = application.get("licenses")
    _require(
        isinstance(licenses, list) and bool(licenses),
        reference,
        "application license references missing",
    )
    for item in licenses:
        remaining(deadline, cancellation)
        _require(isinstance(item, dict), reference, "invalid license reference")
        license = item.get("license", {})
        _require(
            (isinstance(item.get("expression"), str) and bool(item["expression"]))
            or (
                isinstance(license, dict)
                and any(
                    isinstance(license.get(k), str) and bool(license[k]) for k in ("id", "name")
                )
            ),
            reference,
            "invalid license reference",
        )
    properties = application.get("properties")
    _require(isinstance(properties, list), reference, "application provenance properties missing")
    provenance = {}
    for item in properties:
        remaining(deadline, cancellation)
        _require(
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("value"), str),
            reference,
            "application properties require string names and values",
        )
        if item["name"] in {"byo:analysis:upstream", "byo:analysis:recipe"}:
            _require(item["name"] not in provenance, reference, "duplicate provenance property")
            provenance[item["name"]] = item["value"]
    _require(
        all(
            provenance.get(key, "").strip()
            for key in ("byo:analysis:upstream", "byo:analysis:recipe")
        ),
        reference,
        "application upstream/recipe provenance properties missing",
    )
    relationships = sbom.get("dependencies")
    _require(isinstance(relationships, list), "sbom.dependencies", "relationships missing")
    assert isinstance(relationships, list)
    graph = {}
    for item in relationships:
        remaining(deadline, cancellation)
        _require(
            isinstance(item, dict)
            and isinstance(item.get("ref"), str)
            and bool(item["ref"])
            and isinstance(item.get("dependsOn"), list),
            "sbom.dependencies",
            "invalid relationship",
        )
        _require(item["ref"] not in graph, item["ref"], "duplicate relationship")
        _require(
            all(isinstance(r, str) and bool(r) for r in item["dependsOn"]),
            item["ref"],
            "invalid dependency reference",
        )
        graph[item["ref"]] = item["dependsOn"]
    pending = [reference]
    visited = set()
    delivered = set()
    while pending:
        remaining(deadline, cancellation)
        ref = pending.pop()
        if ref in visited:
            continue
        visited.add(ref)
        _require(ref in refs, ref, "dangling SBOM relationship")
        component = refs[ref]
        if component.get("type") == "file":
            path = component.get("name")
            _require(
                isinstance(path, str) and path in metadata.inventory,
                ref,
                "file component references missing inventory leaf",
            )
            if isinstance(path, str) and path.startswith(f"analysis/{name}/"):
                _require(path not in delivered, path, "duplicate file component")
                delivered.add(path)
                hashes = component.get("hashes", [])
                _require(isinstance(hashes, list), path, "SBOM hashes must be an array")
                for digest in hashes:
                    remaining(deadline, cancellation)
                    _require(isinstance(digest, dict), path, "invalid SBOM hash")
                    if digest.get("alg") == "SHA-256":
                        _require(
                            digest.get("content") == metadata.inventory[path]["sha256"],
                            path,
                            "SBOM hash disagrees with sole release inventory",
                        )
        pending.extend(graph.get(ref, []))
    _require(
        delivered == leaves,
        reference,
        f"relationships must cover delivered leaves; missing={sorted(leaves - delivered)}, extra={sorted(delivered - leaves)}",
    )


def _build(
    metadata: _Metadata, name: str, deadline: float, cancellation: Cancellation | None
) -> PayloadReceipt:
    root = metadata.root
    program = metadata.programs[name]
    prefix = f"analysis/{name}/"
    selected = {p: entry for p, entry in metadata.inventory.items() if p.startswith(prefix)}
    identities, directories = _scan(root, prefix[:-1], deadline, cancellation)
    leaves = {p.relative_to(root).as_posix() for p, i in identities.items() if stat.S_ISREG(i[2])}
    _require(
        leaves == set(selected),
        prefix,
        f"namespace differs from inventory; missing={sorted(set(selected) - leaves)}, extra={sorted(leaves - set(selected))}",
    )
    expected_dirs = {
        root / "/".join(p.split("/")[:i]) for p in selected for i in range(2, len(p.split("/")))
    }
    _require(
        set(directories) == expected_dirs, prefix, "extra/empty unreferenced payload directory"
    )
    data_keys = ("resource_dir",) if name == "clangd" else ("cfg_dir", "platforms_dir")
    data_paths = tuple(root / program[k] for k in data_keys)
    required = (
        program["resource_dir"] + "/include/stddef.h"
        if name == "clangd"
        else program["cfg_dir"] + "/std.cfg"
    )
    _require(required in selected, required, "required data leaf missing")
    for path in data_paths:
        _require(
            path in directories
            and any(p.is_relative_to(path) for p in identities if stat.S_ISREG(identities[p][2])),
            str(path),
            "requires nonempty data directory",
        )
    for relative, entry in selected.items():
        remaining(deadline, cancellation)
        if relative == program["executable"]:
            kind = "analysis-executable"
        elif relative in program["licenses"]:
            kind = "license"
        elif relative in program["dependencies"]:
            kind = "analysis-dependency"
        elif any(relative.startswith(program[k] + "/") for k in data_keys):
            kind = "analysis-resource" if name == "clangd" else "analysis-data"
        else:
            raise RuntimeIntegrityError(relative, "inventory leaf not referenced by mapping")
        _require(
            entry["kind"] == kind and entry["executable"] == (kind == "analysis-executable"),
            relative,
            "wrong inventory classification",
        )
        digest, _, identity = hash_file(
            _path(root, relative, deadline, cancellation), deadline, cancellation
        )
        _check_digest(relative, digest, identity, entry)
        _require(
            identity == identities[root / relative], relative, "payload changed during validation"
        )
        if kind == "analysis-executable":
            _windows_executable(root / relative, deadline, cancellation)
    for relative in [program["executable"], *program["licenses"], *program["dependencies"]]:
        _require(relative in selected, relative, "mapped reference missing inventory leaf")
    try:
        _sbom(metadata, name, set(selected), deadline, cancellation)
    except RuntimeIntegrityError as exc:
        raise RuntimeIntegrityError(f"sbom.cdx.json:{exc.path}", str(exc)) from exc
    # Retain ancestors so replacing the namespace or analysis directory with a
    # junction cannot bypass later leaf metadata comparisons.
    for path in (root, root / "analysis"):
        identities[path] = _identity(path, deadline, cancellation)
    receipt = PayloadReceipt(
        metadata, name, root / program["executable"], data_paths, identities, directories
    )
    _validate(receipt, deadline, cancellation)
    return receipt


def _validate(receipt: PayloadReceipt, deadline: float, cancellation: Cancellation | None) -> None:
    for path, expected in {**receipt.metadata.identities, **receipt.identities}.items():
        remaining(deadline, cancellation)
        try:
            observed = _identity(path, deadline, cancellation)
        except TimeoutError:
            raise
        except (OSError, RuntimeIntegrityError) as exc:
            raise RuntimeIntegrityError(
                str(path),
                f"receipt invalidated: {exc}",
                expected=expected,
                observed="missing/unusable",
                changed=True,
            ) from exc
        if observed != expected:
            entries = None
            if path in receipt.directories and stat.S_ISDIR(observed[2]):
                # Re-enumerate a changed directory; never silently revalidate.
                _, listing = _scan(
                    receipt.metadata.root,
                    path.relative_to(receipt.metadata.root).as_posix(),
                    deadline,
                    cancellation,
                )
                entries = listing[path]
            raise RuntimeIntegrityError(
                str(path),
                "receipt invalidated by in-place change",
                expected=expected,
                observed={"identity": observed, "entries": entries},
                changed=True,
            )


def _invalidate(root: Path) -> None:
    _metadata_cache.pop(root, None)
    for key in list(_receipt_cache):
        if key[0] == root:
            del _receipt_cache[key]


def payload_receipt(
    root: Path, name: str, deadline: float, cancellation: Cancellation | None
) -> PayloadReceipt:
    while not _lock.acquire(timeout=min(0.02, remaining(deadline, cancellation))):
        pass
    receipt = None
    metadata = None
    try:
        receipt = _receipt_cache.get((root, name))
        if receipt is not None:
            _validate(receipt, deadline, cancellation)
            return receipt
        metadata = _metadata_cache.get(root)
        if metadata is not None:
            for path, expected in metadata.identities.items():
                observed = _identity(path, deadline, cancellation)
                if observed != expected:
                    raise RuntimeIntegrityError(
                        str(path),
                        "metadata receipt changed",
                        expected=expected,
                        observed=observed,
                        changed=True,
                    )
        else:
            metadata = _load(root, deadline, cancellation)
        receipt = _build(metadata, name, deadline, cancellation)
        _metadata_cache[root] = metadata
        _receipt_cache[root, name] = receipt
        return receipt
    except TimeoutError:
        raise
    except (OSError, RuntimeIntegrityError) as exc:
        # Preserve already bound expected hashes separately from the failed
        # observation. Neither an invalid receipt nor its probe remains cached.
        failure = (
            exc
            if isinstance(exc, RuntimeIntegrityError)
            else RuntimeIntegrityError(str(exc.filename or root), str(exc))
        )
        failure.receipt = receipt
        failure.metadata = metadata
        _invalidate(root)
        if failure is exc:
            raise
        raise failure from exc
    finally:
        _lock.release()


def validate_receipt(
    receipt: PayloadReceipt, deadline: float, cancellation: Cancellation | None
) -> None:
    while not _lock.acquire(timeout=min(0.02, remaining(deadline, cancellation))):
        pass
    try:
        _validate(receipt, deadline, cancellation)
    except TimeoutError:
        raise
    except (OSError, RuntimeIntegrityError):
        _invalidate(receipt.metadata.root)
        raise
    finally:
        _lock.release()
