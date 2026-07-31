"""Single-owner path layout and atomic writes for the project artifact store."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

FIRMSTORE_DIRNAME = ".firm"


class FirmStoreError(RuntimeError):
    """The requested artifact operation violates the FirmStore contract."""


class PersistedAuthorityError(FirmStoreError):
    """Run-scoped execution authority must never be written to disk."""


class ImmutableArtifactError(FirmStoreError):
    """An immutable artifact already exists at the requested path."""


PERSISTED_AUTHORITY_KEYS = frozenset(
    {
        "active_gate",
        "active_permission",
        "active_plan",
        "gate",
        "gate_open",
        "gate_state",
        "gates",
        "permission",
        "permission_grant",
        "permission_state",
        "permissions",
        "plan",
        "plan_grant",
        "plans",
        "remaining_calls",
        "unlocked_tools",
    }
)


def ensure_no_persisted_authority(value: object, *, location: str = "artifact") -> None:
    """Reject state that could restore run-scoped gate, plan, or permission authority."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in PERSISTED_AUTHORITY_KEYS:
                raise PersistedAuthorityError(
                    f"{location} cannot persist run-scoped authority field '{key}'"
                )
            ensure_no_persisted_authority(item, location=f"{location}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            ensure_no_persisted_authority(item, location=f"{location}[{index}]")


def _safe_component(value: str, label: str) -> str:
    component = value.strip()
    if not component or component in {".", ".."}:
        raise FirmStoreError(f"{label} must be a non-empty path component")
    if Path(component).name != component or "/" in component or "\\" in component:
        raise FirmStoreError(f"{label} must not contain path separators")
    return component


@dataclass(frozen=True, slots=True)
class FirmLayout:
    """All project-local artifact paths owned by FirmStore."""

    project_root: Path
    root: Path
    boards: Path
    packs: Path
    setup: Path
    safety: Path
    validation: Path
    cache: Path
    discovery_hooks: Path
    remote_probes: Path

    @classmethod
    def for_project(cls, project_root: Path) -> FirmLayout:
        project = Path(project_root).expanduser().resolve()
        root = project / ".firm"
        return cls(
            project_root=project,
            root=root,
            boards=root / "boards",
            packs=root / "packs",
            setup=root / "setup",
            safety=root / "safety",
            validation=root / "validation",
            cache=root / "cache",
            discovery_hooks=root / "discovery_hooks",
            # Not under discovery_hooks/: this is a plain registry file, not a hook. It
            # is never executed and is not subject to hook source hashing or the
            # per-kind gate that discovery_hooks/ contents are.
            remote_probes=root / "remote_probes.json",
        )

    def board_profile(self, board_id: str, *, suffix: str = ".yaml") -> Path:
        identity = _safe_component(board_id, "board_id")
        if suffix not in {".json", ".yaml", ".yml"}:
            raise FirmStoreError("board profile suffix must be .json, .yaml, or .yml")
        return self.boards / f"{identity}{suffix}"

    def setup_attempt(self, setup_id: str) -> Path:
        return self.setup / _safe_component(setup_id, "setup_id")

    def safety_board(self, board_id: str) -> Path:
        return self.safety / _safe_component(board_id, "board_id")

    def safety_reference_prefix(self, board_id: str) -> PurePosixPath:
        """Return the portable project-relative prefix for a board's safety artifacts."""

        relative = self.safety_board(board_id).relative_to(self.project_root)
        return PurePosixPath(relative.as_posix())

    def validation_attempt(self, validation_id: str) -> Path:
        return self.validation / _safe_component(validation_id, "validation_id")

    def cache_artifact(self, name: str) -> Path:
        return self.cache / _safe_component(name, "cache artifact name")

    def datasheet_evidence(self, sha256: str) -> Path:
        digest = _safe_component(sha256, "datasheet digest")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise FirmStoreError("datasheet digest must be a lowercase SHA-256")
        return self.root / "evidence" / "datasheets" / f"{digest}.pdf"

    def datasheet_reference(self, sha256: str) -> PurePosixPath:
        """Return the canonical project-relative reference for captured PDF bytes."""

        return PurePosixPath(
            self.datasheet_evidence(sha256).relative_to(self.project_root).as_posix()
        )

    @property
    def pack_files(self) -> Path:
        return self.packs / "files"

    @property
    def pack_manifest(self) -> Path:
        """Return the sole project-owned device-support metadata manifest."""

        return self.packs / "manifest.yaml"


class FirmStore:
    """The only writer for new `.firm` artifacts."""

    def __init__(self, project_root: Path) -> None:
        self.layout = FirmLayout.for_project(project_root)
        self._write_lock = threading.RLock()

    def ensure_layout(self) -> FirmLayout:
        for directory in (
            self.layout.boards,
            self.layout.packs,
            self.layout.setup,
            self.layout.safety,
            self.layout.validation,
            self.layout.cache,
            # FirmStore only names and creates this directory. Hook manifests and
            # hook programs are agent-authored; FirmStore never writes them, so no
            # write_hook() helper exists here by design.
            self.layout.discovery_hooks,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self.layout

    def _owned_target(self, target: Path) -> Path:
        candidate = Path(target).expanduser().resolve()
        if candidate == self.layout.root or not candidate.is_relative_to(self.layout.root):
            raise FirmStoreError(
                f"FirmStore writes must stay below {self.layout.root}; got {candidate}"
            )
        return candidate

    def atomic_write_bytes(self, target: Path, payload: bytes) -> Path:
        """Durably stage bytes beside the target and atomically replace it."""

        destination = self._owned_target(target)
        return self._atomic_write_bytes(destination, payload)

    def atomic_write_bundle(self, artifacts: Mapping[Path, bytes]) -> tuple[Path, ...]:
        """Replace a small related artifact set with in-process rollback on interruption.

        Each member is still staged and replaced atomically. The writer lock prevents other
        FirmStore writes from observing the replacement sequence, and a failed member restores
        every earlier member before the error is propagated.
        """

        if not artifacts:
            raise FirmStoreError("an atomic artifact bundle must not be empty")
        resolved = sorted(
            ((self._owned_target(path), bytes(payload)) for path, payload in artifacts.items()),
            key=lambda item: str(item[0]),
        )
        destinations = [item[0] for item in resolved]
        if len(set(destinations)) != len(destinations):
            raise FirmStoreError("an atomic artifact bundle contains duplicate destinations")
        with self._write_lock:
            previous = {
                destination: destination.read_bytes() if destination.is_file() else None
                for destination in destinations
            }
            attempted: list[Path] = []
            try:
                for destination, payload in resolved:
                    attempted.append(destination)
                    self._atomic_write_bytes(destination, payload)
            except BaseException:
                for destination in reversed(attempted):
                    prior = previous[destination]
                    if prior is None:
                        destination.unlink(missing_ok=True)
                    else:
                        self._atomic_write_bytes(destination, prior)
                raise
        return tuple(destinations)

    def remove_artifact(self, target: Path) -> None:
        """Remove a FirmStore-owned staging artifact under the writer lock."""

        destination = self._owned_target(target)
        with self._write_lock:
            destination.unlink(missing_ok=True)

    def _atomic_write_bytes(self, destination: Path, payload: bytes) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def atomic_write_pack_manifest(self, document: Mapping[str, Any]) -> Path:
        """Atomically replace the project-local promoted-support index.

        Package bytes and their index both remain below ``.firm/packs``. The
        external or project pack registry is never a FirmStore write
        target.
        """

        ensure_no_persisted_authority(document, location="pack manifest")
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - environment guard
            raise FirmStoreError("PyYAML is required to write the pack manifest") from exc
        text = yaml.safe_dump(
            dict(document),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        with self._write_lock:
            return self._atomic_write_bytes(
                self.layout.pack_manifest.resolve(), text.encode("utf-8")
            )

    def update_pack_manifest(
        self, update: Callable[[Path], Mapping[str, Any]]
    ) -> Path:
        """Serialize one manifest read/merge/write transaction in this server."""

        with self._write_lock:
            document = update(self.layout.pack_manifest)
            return self.atomic_write_pack_manifest(document)

    def atomic_create_bytes(self, target: Path, payload: bytes) -> Path:
        """Atomically create an immutable artifact without replacing an existing file."""

        destination = self._owned_target(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise ImmutableArtifactError(
                    f"Immutable artifact already exists: {destination}"
                ) from exc
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def atomic_write_text(self, target: Path, text: str) -> Path:
        return self.atomic_write_bytes(target, text.encode("utf-8"))

    def atomic_write_json(self, target: Path, document: Mapping[str, Any]) -> Path:
        ensure_no_persisted_authority(document)
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        return self.atomic_write_text(target, text)

    def atomic_create_json(self, target: Path, document: Mapping[str, Any]) -> Path:
        ensure_no_persisted_authority(document)
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        return self.atomic_create_bytes(target, text.encode("utf-8"))

    def append_jsonl(self, target: Path, document: Mapping[str, Any]) -> Path:
        """Append one complete JSON record without offering a rewrite operation."""

        ensure_no_persisted_authority(document)
        destination = self._owned_target(target)
        payload = (
            json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self._write_lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                destination,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return destination

    def atomic_write_yaml(self, target: Path, document: Mapping[str, Any]) -> Path:
        ensure_no_persisted_authority(document)
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - environment guard
            raise FirmStoreError("PyYAML is required to write FirmStore YAML artifacts") from exc
        text = yaml.safe_dump(
            dict(document),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        return self.atomic_write_text(target, text)

    def atomic_create_yaml(self, target: Path, document: Mapping[str, Any]) -> Path:
        ensure_no_persisted_authority(document)
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - environment guard
            raise FirmStoreError("PyYAML is required to write FirmStore YAML artifacts") from exc
        text = yaml.safe_dump(
            dict(document),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        return self.atomic_create_bytes(target, text.encode("utf-8"))
