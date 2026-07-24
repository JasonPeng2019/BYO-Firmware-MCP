"""Schema-v2 board profiles stored in the project-local FirmStore."""

from __future__ import annotations

import copy
import re
import threading
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal

from pyocd_debug_mcp.board_config import (
    BOARD_CONFIG_SUFFIXES,
    BoardConfig,
    ConfigError,
    load_board_config_document,
    make_board_config,
    preview_board_config_paths,
)
from pyocd_debug_mcp.firmstore.store import (
    FirmStore,
    ensure_no_persisted_authority,
)

PROFILE_SCHEMA_VERSION = 2

_REQUIRED_CORE_FIELDS = frozenset(
    {
        "board_id",
        "display_name",
        "mcu_part_number",
        "mcu_family",
        "probe_family",
        "pyocd_target",
    }
)
_CORE_INPUT_FIELDS = _REQUIRED_CORE_FIELDS | {
    "probe_type",
    "probe_hint_terms",
    "serial_hint_terms",
    "serial_baudrate",
    "uart_note",
    "requires_recover_validation",
    "recover_mode",
    "schema_version",
}
_OPTIONAL_FIELDS = frozenset(
    {
        "test_read_address",
        "silicon_id_address",
        "silicon_id_expected",
        "silicon_id_mask",
        "silicon_id_width_bits",
        "silicon_id_label",
        "datasheet_sha256",
        "datasheet_ref",
        "expected_uart_substring",
        "debug_protocol",
        "debug_connect_mode",
        "debug_clock_hz",
        "device_support",
    }
)
_PROFILE_METADATA_FIELDS = frozenset(
    {
        "schema_version",
        "mcu_part_number",
        "created_at",
        "updated_at",
        "safety_ref",
        "datasheet_sha256",
        "datasheet_ref",
        "device_support",
    }
)
_V2_FIELDS = (
    _CORE_INPUT_FIELDS
    | _OPTIONAL_FIELDS
    | {
        "created_at",
        "updated_at",
        "safety_ref",
    }
)
_PACKAGE_METADATA_FIELDS = frozenset(
    {
        "needed_by_boards",
        "pack_filename",
        "pack_id",
        "pack_name",
        "pack_sha256",
        "pack_url",
        "pack_version",
        "provides_targets",
    }
)

_PACK_DEVICE_SUPPORT_FIELDS = frozenset(
    {
        "kind",
        "support_id",
        "pack_id",
        "pack_filename",
        "pack_sha256",
        "pdsc_device",
        "pyocd_target",
    }
)
_BUILTIN_DEVICE_SUPPORT_FIELDS = frozenset(
    {
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
)

DeviceSupportVerifier = Callable[[str, BoardConfig, Mapping[str, str]], None]


class ProfileError(ConfigError):
    """A profile violates schema or repository integrity requirements."""


class StaleProfileStageError(ProfileError):
    """The profile changed after a staged update was prepared."""


def utc_timestamp() -> str:
    """Return an absolute, timezone-bearing UTC timestamp."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_absolute_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProfileError(f"{field_name} must be an absolute timezone-bearing timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProfileError(f"{field_name} must be an absolute timezone-bearing timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProfileError(f"{field_name} must include an explicit timezone")
    return value


def _require_board_id(value: object) -> str:
    board_id = str(value).strip()
    if not re.fullmatch(r"[a-z0-9_]{1,64}", board_id):
        raise ProfileError("board_id must be 1-64 lowercase letters, numbers, or underscores")
    return board_id


def _display_identity(display_name: str) -> str:
    return unicodedata.normalize("NFC", display_name).casefold()


def _make_profile_board(raw: dict[str, object], source_path: Path | None) -> BoardConfig:
    try:
        return make_board_config(raw, source_path)
    except ConfigError as exc:
        raise ProfileError(str(exc)) from exc


def _reject_package_metadata(document: Mapping[str, object], *, location: str) -> None:
    present = sorted(set(document) & _PACKAGE_METADATA_FIELDS)
    if present:
        raise ProfileError(
            f"{location} must not contain device-support package metadata fields "
            f"{present}; the project-local verified pack registry is authoritative"
        )


def _validate_device_support(value: object) -> dict[str, str] | None:
    """Validate the closed, server-generated generic support source record.

    This is source identity, not caller authority.  It is intentionally kept
    out of ``BoardConfig`` so board parsing cannot reinterpret it as a user
    supplied target/pack setting.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProfileError("device_support must be an object")
    raw = dict(value)
    kind = raw.get("kind")
    expected_fields = (
        _PACK_DEVICE_SUPPORT_FIELDS
        if kind == "resolved_pack"
        else _BUILTIN_DEVICE_SUPPORT_FIELDS
        if kind == "resolved_builtin_target"
        else None
    )
    if expected_fields is None:
        raise ProfileError(
            "device_support.kind must be 'resolved_pack' or 'resolved_builtin_target'"
        )
    if set(raw) != expected_fields:
        raise ProfileError(
            f"device_support must contain exactly {sorted(expected_fields)} for kind {kind!r}"
        )
    result: dict[str, str] = {}
    for field_name in expected_fields:
        field_value = raw[field_name]
        if not isinstance(field_value, str) or not field_value.strip():
            raise ProfileError(f"device_support.{field_name} must be a non-empty string")
        result[field_name] = field_value.strip()
    if re.fullmatch(r"[0-9a-f]{64}", result["support_id"]) is None:
        raise ProfileError("device_support.support_id must be a lowercase SHA-256 digest")
    digest_field = "pack_sha256" if kind == "resolved_pack" else "geometry_sha256"
    if re.fullmatch(r"[0-9a-f]{64}", result[digest_field]) is None:
        raise ProfileError(f"device_support.{digest_field} must be a lowercase SHA-256 digest")
    return result


def _verify_registered_device_support(
    mcu_part_number: str,
    board: BoardConfig,
    source: Mapping[str, str],
    *,
    store: FirmStore | None = None,
) -> None:
    """Require a persisted generic source to replay to immutable registry bytes."""

    try:
        from pyocd_debug_mcp.setup_flow.device_support import (
            replay_live_cpuid_compatibility_proof,
            resolve_persisted_builtin_target_support,
            resolve_persisted_pack_support,
            resolve_registered_pack_support,
        )

        if source.get("kind") == "resolved_builtin_target":
            candidate = resolve_persisted_builtin_target_support(mcu_part_number, source)
        else:
            candidate = (
                resolve_registered_pack_support(mcu_part_number)
                if store is None
                else resolve_persisted_pack_support(store, mcu_part_number, source)
            )
        expected = candidate.to_authority_document()
    except Exception as exc:  # noqa: BLE001 - profile authority must fail closed
        raise ProfileError(
            f"device_support cannot be replayed from verified support: {exc}"
        ) from exc
    if expected["pyocd_target"].casefold() != board.pyocd_target.casefold():
        raise ProfileError("device_support target does not match the profile target")
    if dict(source) != expected:
        raise ProfileError("device_support does not match the current verified binding")
    proof = candidate.identity_proof
    actual_identity = (
        board.silicon_id_addr,
        board.silicon_id_expected,
        board.silicon_id_mask,
    )
    if proof is None:
        if actual_identity != (None, None, None):
            try:
                replay_live_cpuid_compatibility_proof(
                    address=board.silicon_id_addr,
                    expected=board.silicon_id_expected,
                    mask=board.silicon_id_mask,
                    width_bits=board.silicon_id_width_bits,
                    label=board.silicon_id_label,
                )
            except Exception as exc:  # noqa: BLE001 - reject noncanonical persisted identity
                raise ProfileError(
                    "profile identity fields do not contain canonical live CPUID evidence"
                ) from exc
    elif (
        actual_identity != (proof.address, proof.expected, proof.mask)
        or board.silicon_id_width_bits != proof.width_bits
        or board.silicon_id_label != proof.label
    ):
        raise ProfileError("profile identity fields do not match verified device-support evidence")


def _validate_safety_ref(value: object, expected_prefix: PurePosixPath) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProfileError("safety_ref must be a non-empty project-relative path")
    reference = PurePosixPath(value)
    if (
        reference.is_absolute()
        or ".." in reference.parts
        or reference == expected_prefix
        or not reference.is_relative_to(expected_prefix)
    ):
        raise ProfileError(f"safety_ref must name an artifact below {expected_prefix.as_posix()}/")
    return reference.as_posix()


@dataclass(frozen=True)
class BoardProfile:
    """A validated schema-v2 board profile."""

    schema_version: int
    mcu_part_number: str | None
    board: BoardConfig
    created_at: str | None
    updated_at: str | None
    safety_ref: str | None
    device_support: Mapping[str, str] | None
    source_path: Path
    _document: dict[str, Any] = field(repr=False, compare=False)

    @property
    def board_id(self) -> str:
        return self.board.board_id

    @property
    def display_name(self) -> str:
        return self.board.display_name

    def to_document(self) -> dict[str, Any]:
        """Return a detached, package-metadata-free representation."""

        document = copy.deepcopy(self._document)
        for field_name in _PACKAGE_METADATA_FIELDS:
            document.pop(field_name, None)
        return document


@dataclass(frozen=True)
class StagedProfile:
    """Validated profile content awaiting one atomic repository commit."""

    operation: Literal["core", "optional", "safety_ref"]
    profile: BoardProfile
    expected_updated_at: str | None


class ProfileRepository:
    """Read profiles and commit prevalidated stages only through FirmStore."""

    def __init__(
        self,
        store: FirmStore,
        *,
        device_support_verifier: DeviceSupportVerifier | None = None,
    ) -> None:
        self.store = store
        self._device_support_verifier = device_support_verifier or (
            lambda part, board, source: _verify_registered_device_support(
                part, board, source, store=self.store
            )
        )
        self._commit_lock = threading.RLock()

    def _v2_paths(self) -> list[Path]:
        return preview_board_config_paths(self.store.layout.boards)

    def _from_v2_document(self, raw: Mapping[str, object], path: Path) -> BoardProfile:
        document = copy.deepcopy(dict(raw))
        ensure_no_persisted_authority(document, location=f"profile {path.name}")
        _reject_package_metadata(document, location=path.name)
        unknown = sorted(set(document) - _V2_FIELDS)
        if unknown:
            raise ProfileError(f"Unknown schema-v2 profile fields in {path.name}: {unknown}")
        if document.get("schema_version") != PROFILE_SCHEMA_VERSION:
            raise ProfileError(f"{path.name} must declare schema_version: {PROFILE_SCHEMA_VERSION}")
        missing = sorted(field for field in _REQUIRED_CORE_FIELDS if field not in document)
        if missing:
            raise ProfileError(f"Missing required schema-v2 profile fields: {', '.join(missing)}")

        board_id = _require_board_id(document["board_id"])
        if path.stem != board_id:
            raise ProfileError(
                f"Profile filename stem '{path.stem}' does not match board_id '{board_id}'"
            )
        part_number = document["mcu_part_number"]
        if not isinstance(part_number, str) or not part_number.strip():
            raise ProfileError("mcu_part_number must be the exact non-empty user-supplied string")
        datasheet_sha256 = document.get("datasheet_sha256")
        if datasheet_sha256 is not None and (
            not isinstance(datasheet_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", datasheet_sha256) is None
        ):
            raise ProfileError("datasheet_sha256 must be a lowercase SHA-256 digest")
        datasheet_ref = document.get("datasheet_ref")
        if datasheet_ref is not None:
            if not isinstance(datasheet_ref, str) or datasheet_sha256 is None:
                raise ProfileError("datasheet_ref requires text and datasheet_sha256")
            expected = self.store.layout.datasheet_reference(datasheet_sha256)
            if PurePosixPath(datasheet_ref) != expected:
                raise ProfileError("datasheet_ref must be the canonical captured evidence path")
            try:
                from pyocd_debug_mcp.setup_flow.datasheet_evidence import (
                    replay_datasheet_evidence,
                )

                replay_datasheet_evidence(self.store, datasheet_ref, datasheet_sha256)
            except Exception as exc:  # noqa: BLE001 - evidence replay must fail closed
                raise ProfileError(f"datasheet evidence replay failed: {exc}") from exc
        device_support = _validate_device_support(document.get("device_support"))
        if device_support is not None and (datasheet_sha256 is None or datasheet_ref is None):
            raise ProfileError(
                "generic device_support requires captured datasheet_sha256 and datasheet_ref"
            )
        created_at = _validate_absolute_timestamp(document.get("created_at"), "created_at")
        updated_at = _validate_absolute_timestamp(document.get("updated_at"), "updated_at")

        board_document = {
            key: value for key, value in document.items() if key not in _PROFILE_METADATA_FIELDS
        }
        board = _make_profile_board(board_document, path)
        if device_support is not None:
            self._device_support_verifier(part_number, board, device_support)
        safety_ref = _validate_safety_ref(
            document.get("safety_ref"),
            self.store.layout.safety_reference_prefix(board_id),
        )
        return BoardProfile(
            schema_version=PROFILE_SCHEMA_VERSION,
            mcu_part_number=part_number,
            board=board,
            created_at=created_at,
            updated_at=updated_at,
            safety_ref=safety_ref,
            device_support=device_support,
            source_path=path,
            _document=document,
        )

    def _load_v2_path(self, path: Path) -> BoardProfile:
        try:
            document = load_board_config_document(path)
        except ConfigError as exc:
            raise ProfileError(f"Invalid schema-v2 profile {path.name}: {exc}") from exc
        return self._from_v2_document(document, path)

    @staticmethod
    def _validate_unique(profiles: list[BoardProfile]) -> None:
        board_ids: dict[str, Path] = {}
        display_names: dict[str, BoardProfile] = {}
        for profile in profiles:
            previous_path = board_ids.get(profile.board_id)
            if previous_path is not None:
                raise ProfileError(
                    f"Duplicate board_id '{profile.board_id}' in {previous_path} "
                    f"and {profile.source_path}"
                )
            board_ids[profile.board_id] = profile.source_path
            display_key = _display_identity(profile.display_name)
            previous = display_names.get(display_key)
            if previous is not None:
                raise ProfileError(
                    f"Duplicate display_name '{profile.display_name}' for boards "
                    f"'{previous.board_id}' and '{profile.board_id}'"
                )
            display_names[display_key] = profile

    def load_all(self) -> list[BoardProfile]:
        v2_profiles = [self._load_v2_path(path) for path in self._v2_paths()]
        profiles_by_id: dict[str, BoardProfile] = {}
        for profile in v2_profiles:
            if profile.board_id in profiles_by_id:
                raise ProfileError(f"Duplicate board_id '{profile.board_id}' in profile store")
            profiles_by_id[profile.board_id] = profile

        profiles = list(profiles_by_id.values())
        self._validate_unique(profiles)
        return sorted(profiles, key=lambda profile: profile.board_id)

    def load(self, board_id: str) -> BoardProfile:
        identity = _require_board_id(board_id)
        matches = [profile for profile in self.load_all() if profile.board_id == identity]
        if not matches:
            raise ProfileError(f"Board profile not found: {identity}")
        return matches[0]

    def _assert_display_available(self, candidate: BoardProfile) -> None:
        candidate_key = _display_identity(candidate.display_name)
        for existing in self.load_all():
            if existing.board_id == candidate.board_id:
                continue
            if _display_identity(existing.display_name) == candidate_key:
                raise ProfileError(
                    f"Duplicate display_name '{candidate.display_name}' already belongs to "
                    f"board '{existing.board_id}'"
                )

    def _materialize_core_document(self, fields: Mapping[str, object]) -> dict[str, Any]:
        document = copy.deepcopy(dict(fields))
        ensure_no_persisted_authority(document, location="profile core")
        _reject_package_metadata(document, location="schema-v2 profile")
        unknown = sorted(set(document) - _CORE_INPUT_FIELDS)
        if unknown:
            raise ProfileError(f"Core profile stage contains unsupported fields: {unknown}")
        missing = sorted(field for field in _REQUIRED_CORE_FIELDS if field not in document)
        if missing:
            raise ProfileError(f"Missing required core profile fields: {', '.join(missing)}")
        if document.get("schema_version", PROFILE_SCHEMA_VERSION) != PROFILE_SCHEMA_VERSION:
            raise ProfileError(f"schema_version must be {PROFILE_SCHEMA_VERSION}")
        part_number = document["mcu_part_number"]
        if not isinstance(part_number, str) or not part_number.strip():
            raise ProfileError("mcu_part_number must be the exact non-empty user-supplied string")
        board_document = {
            key: value
            for key, value in document.items()
            if key not in {"schema_version", "mcu_part_number"}
        }
        board = _make_profile_board(board_document, None)
        now = utc_timestamp()
        document["schema_version"] = PROFILE_SCHEMA_VERSION
        document["created_at"] = now
        document["updated_at"] = now
        document.setdefault("probe_type", board.probe_type)
        document.setdefault("probe_hint_terms", list(board.probe_hint_terms))
        document.setdefault("serial_hint_terms", list(board.serial_hint_terms))
        document.setdefault("requires_recover_validation", board.requires_recover_validation)
        if board.recover_mode is not None:
            document.setdefault("recover_mode", board.recover_mode)
        return document

    def stage_core(self, fields: Mapping[str, object]) -> StagedProfile:
        document = self._materialize_core_document(fields)
        board_id = _require_board_id(document["board_id"])
        target = self.store.layout.board_profile(board_id)
        profile = self._from_v2_document(document, target)
        self._assert_display_available(profile)
        return StagedProfile("core", profile, None)

    def _paths_for_board(self, board_id: str) -> list[Path]:
        return [
            path
            for path in self._v2_paths()
            if path.stem == board_id and path.suffix.lower() in BOARD_CONFIG_SUFFIXES
        ]

    def _write(self, profile: BoardProfile, *, create_only: bool = False) -> BoardProfile:
        target = self.store.layout.board_profile(profile.board_id)
        candidate = self._from_v2_document(profile.to_document(), target)
        if create_only:
            self.store.atomic_create_yaml(target, candidate.to_document())
        else:
            self.store.atomic_write_yaml(target, candidate.to_document())
        return self._load_v2_path(target)

    def commit_core(self, staged: StagedProfile) -> BoardProfile:
        if staged.operation != "core" or staged.expected_updated_at is not None:
            raise ProfileError("commit_core requires a core StagedProfile")
        with self._commit_lock:
            if self._paths_for_board(staged.profile.board_id):
                raise ProfileError(
                    f"Schema-v2 profile already exists for '{staged.profile.board_id}'"
                )
            self._assert_display_available(staged.profile)
            return self._write(staged.profile, create_only=True)

    def stage_optional(
        self,
        board_id: str,
        fields: Mapping[str, object],
    ) -> StagedProfile:
        unknown = sorted(set(fields) - _OPTIONAL_FIELDS)
        if unknown:
            raise ProfileError(f"Optional profile stage contains unsupported fields: {unknown}")
        ensure_no_persisted_authority(fields, location="profile optional fields")
        current = self.load(board_id)
        document = current.to_document()
        document.update(copy.deepcopy(dict(fields)))
        document["updated_at"] = utc_timestamp()
        profile = self._from_v2_document(document, current.source_path)
        return StagedProfile("optional", profile, current.updated_at)

    def _commit_existing(self, staged: StagedProfile, operation: str) -> BoardProfile:
        if staged.operation != operation or staged.expected_updated_at is None:
            raise ProfileError(f"commit_{operation} requires a matching StagedProfile")
        with self._commit_lock:
            current = self.load(staged.profile.board_id)
            if current.updated_at != staged.expected_updated_at:
                raise StaleProfileStageError(
                    f"Profile '{current.board_id}' changed after the update was staged"
                )
            self._assert_display_available(staged.profile)
            return self._write(staged.profile)

    def commit_optional(self, staged: StagedProfile) -> BoardProfile:
        return self._commit_existing(staged, "optional")

    def stage_safety_ref(self, board_id: str, safety_ref: str) -> StagedProfile:
        current = self.load(board_id)
        reference = _validate_safety_ref(
            safety_ref,
            self.store.layout.safety_reference_prefix(current.board_id),
        )
        assert reference is not None
        document = current.to_document()
        document["safety_ref"] = reference
        document["updated_at"] = utc_timestamp()
        profile = self._from_v2_document(document, current.source_path)
        return StagedProfile("safety_ref", profile, current.updated_at)

    def commit_safety_ref(self, staged: StagedProfile) -> BoardProfile:
        return self._commit_existing(staged, "safety_ref")
