"""Crash-safe, per-logical-board tier policy persistence.

The generation pointer in this module, rather than a profile or map's mere
existence, is the authority for tier routing.  Generations are immutable and
the checksummed pointer is replaced only after a complete generation is on
disk.  That makes an interrupted pre-commit write leave the old policy live.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from yaml.error import YAMLError


# Capability pointers predate the strict profile schema and also serve fresh/raw
# logical-board names. Hyphens are safe single path components and are used by
# established callers; whitespace and separators remain forbidden.
_BOARD_ID = re.compile(r"[a-z0-9_-]{1,64}\Z")
_SCHEMA_VERSION = 1
_FAULT_ENV = "BYO_MCP_TIER_FAULT"
_TEST_FAULTS_ENV = "BYO_MCP_TEST_FAULTS"


class Tier(str, Enum):
    """The explicit policy applied to one named logical board."""

    NO_SETUP = "no-setup"
    SETUP_LITE = "setup-lite"
    SETUP_FULL = "setup-full"


class CapabilityPolicyError(RuntimeError):
    """A capability policy is malformed, ambiguous, or unavailable."""


@dataclass(frozen=True, slots=True)
class CapabilityState:
    """One resolved policy snapshot suitable for routing an operation."""

    board_id: str
    tier: Tier | None
    status: str
    policy_digest: str | None
    generation_id: str | None
    setup_incomplete: bool = False
    profile_snapshot: Mapping[str, object] | None = None
    map_snapshot: Mapping[str, object] | None = None
    evidence: Mapping[str, object] | None = None
    retained_generations: tuple[str, ...] = ()

    @property
    def hardware_allowed(self) -> bool:
        return self.tier is not None and self.status in {
            "committed",
            "legacy-migrated",
            "setup-incomplete",
        }


FaultHook = Callable[[str], None]


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


class CapabilityPolicyRepository:
    """Persist and resolve explicit tier policy below one project root.

    ``resolve`` is intentionally the migration choke point.  It only recognizes
    raw mode when absence is positive; a damaged or ambiguous legacy policy is
    represented as ``corrupt`` and therefore cannot reach hardware.
    """

    def __init__(self, project_root: Path, *, fault_hook: FaultHook | None = None) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.root = self.project_root / ".firm" / "capabilities"
        self._fault_hook = fault_hook
        self._guard = threading.RLock()
        self._board_locks: dict[str, threading.RLock] = {}

    @staticmethod
    def _board_id(board_id: str) -> str:
        if not isinstance(board_id, str) or _BOARD_ID.fullmatch(board_id) is None:
            raise CapabilityPolicyError(
                "board_id must be 1-64 lowercase letters, numbers, underscores, or hyphens"
            )
        return board_id

    def lock_for(self, board_id: str) -> threading.RLock:
        identity = self._board_id(board_id)
        with self._guard:
            return self._board_locks.setdefault(identity, threading.RLock())

    def board_root(self, board_id: str) -> Path:
        return self.root / self._board_id(board_id)

    def pointer_path(self, board_id: str) -> Path:
        return self.board_root(board_id) / "current.json"

    def resolve(self, board_id: str) -> CapabilityState:
        """Return the active explicit state, migrating only recognized legacy facts."""

        identity = self._board_id(board_id)
        with self.lock_for(identity):
            pointer = self.pointer_path(identity)
            if pointer.exists():
                try:
                    return self._load_pointer(identity)
                except CapabilityPolicyError as exc:
                    return self._corrupt(identity, str(exc))
            return self._migrate_or_initialize(identity)

    def commit(
        self,
        board_id: str,
        tier: Tier | str,
        *,
        setup_incomplete: bool = False,
        profile_snapshot: Mapping[str, object] | None = None,
        map_snapshot: Mapping[str, object] | None = None,
        evidence: Mapping[str, object] | None = None,
        retained_generations: tuple[str, ...] | None = None,
    ) -> CapabilityState:
        """Publish a complete immutable policy generation at one pointer commit point."""

        identity = self._board_id(board_id)
        selected = Tier(tier)
        if selected is not Tier.NO_SETUP and map_snapshot is None:
            raise CapabilityPolicyError(
                "setup-lite and setup-full require a committed map snapshot"
            )
        if selected is Tier.SETUP_LITE:
            map_snapshot = self._validate_lite_snapshot(map_snapshot, identity)
        if selected is Tier.SETUP_FULL:
            self._validate_full_snapshot(profile_snapshot, map_snapshot, identity)
        if selected is Tier.NO_SETUP and map_snapshot is not None:
            # Maps/evidence may be retained by reference, but they must not become
            # an active no-setup map that callers could accidentally consult.
            raise CapabilityPolicyError(
                "no-setup may retain prior evidence only through retained_generations"
            )
        with self.lock_for(identity):
            if retained_generations is None:
                current = self.resolve(identity)
                retained_generations = current.retained_generations
            return self._commit_locked(
                identity,
                selected,
                setup_incomplete=setup_incomplete,
                profile_snapshot=profile_snapshot,
                map_snapshot=map_snapshot,
                evidence=evidence,
                retained_generations=retained_generations,
                legacy_migrated=False,
            )

    def downgrade_to_no_setup(self, board_id: str) -> CapabilityState:
        """Commit a raw policy while retaining the previous immutable evidence inactive."""

        identity = self._board_id(board_id)
        with self.lock_for(identity):
            current = self.resolve(identity)
            retained = tuple(
                dict.fromkeys(
                    (
                        *current.retained_generations,
                        *(() if current.generation_id is None else (current.generation_id,)),
                    )
                )
            )
            committed = self._commit_locked(
                identity,
                Tier.NO_SETUP,
                setup_incomplete=True,
                profile_snapshot=None,
                map_snapshot=None,
                evidence=None,
                retained_generations=retained,
            )
            return self._with_status(committed, "setup-incomplete")

    def mark_setup_incomplete(self, board_id: str) -> CapabilityState:
        """Durably mark a failed/incomplete setup attempt without activating its writes.

        The active generation's tier, snapshots, and retained evidence remain
        unchanged.  Publishing this small replacement is the crash-safe record
        that a compatibility profile/map may have been staged before the real
        policy pointer commit.
        """

        identity = self._board_id(board_id)
        with self.lock_for(identity):
            current = self.resolve(identity)
            if current.tier is None:
                raise CapabilityPolicyError("cannot mark a corrupt policy setup-incomplete")
            if current.setup_incomplete:
                return current
            return self._commit_locked(
                identity,
                current.tier,
                setup_incomplete=True,
                profile_snapshot=current.profile_snapshot,
                map_snapshot=current.map_snapshot,
                evidence=current.evidence,
                retained_generations=current.retained_generations,
            )

    def _commit_locked(
        self,
        board_id: str,
        tier: Tier,
        *,
        setup_incomplete: bool,
        profile_snapshot: Mapping[str, object] | None,
        map_snapshot: Mapping[str, object] | None,
        evidence: Mapping[str, object] | None,
        retained_generations: tuple[str, ...],
        legacy_migrated: bool = False,
    ) -> CapabilityState:
        generation_id = secrets.token_hex(16)
        document: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "board_id": board_id,
            "tier": tier.value,
            "setup_incomplete": bool(setup_incomplete),
            "profile_snapshot": copy.deepcopy(dict(profile_snapshot))
            if profile_snapshot is not None
            else None,
            "map_snapshot": copy.deepcopy(dict(map_snapshot)) if map_snapshot is not None else None,
            "evidence": copy.deepcopy(dict(evidence)) if evidence is not None else None,
            "retained_generations": list(retained_generations),
            "legacy_migrated": legacy_migrated,
        }
        policy_digest = _digest(document)
        document["policy_digest"] = policy_digest
        generation_path = self.board_root(board_id) / "generations" / f"{generation_id}.json"
        self._atomic_write(generation_path, _canonical_json(document) + b"\n")
        self._trip_fault("after_generation_write")
        pointer = {
            "schema_version": _SCHEMA_VERSION,
            "board_id": board_id,
            "generation_id": generation_id,
            "generation_digest": hashlib.sha256(generation_path.read_bytes()).hexdigest(),
        }
        self._trip_fault("before_pointer_replace")
        self._atomic_write(self.pointer_path(board_id), _canonical_json(pointer) + b"\n")
        self._trip_fault("after_pointer_replace")
        return CapabilityState(
            board_id=board_id,
            tier=tier,
            status=(
                "legacy-migrated"
                if legacy_migrated
                else "setup-incomplete"
                if document["setup_incomplete"]
                else "committed"
            ),
            policy_digest=policy_digest,
            generation_id=generation_id,
            setup_incomplete=setup_incomplete,
            profile_snapshot=copy.deepcopy(document["profile_snapshot"])
            if isinstance(document["profile_snapshot"], Mapping)
            else None,
            map_snapshot=copy.deepcopy(document["map_snapshot"])
            if isinstance(document["map_snapshot"], Mapping)
            else None,
            evidence=copy.deepcopy(document["evidence"])
            if isinstance(document["evidence"], Mapping)
            else None,
            retained_generations=retained_generations,
        )

    def _load_pointer(self, board_id: str) -> CapabilityState:
        pointer = self._read_json(self.pointer_path(board_id), "current policy pointer")
        self._require_exact_keys(
            pointer,
            {"schema_version", "board_id", "generation_id", "generation_digest"},
            "current policy pointer",
        )
        if pointer["schema_version"] != _SCHEMA_VERSION or pointer["board_id"] != board_id:
            raise CapabilityPolicyError(
                "current policy pointer does not match this board or schema"
            )
        generation_id = pointer["generation_id"]
        digest = pointer["generation_digest"]
        if (
            not isinstance(generation_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", generation_id) is None
        ):
            raise CapabilityPolicyError("current policy pointer has an invalid generation id")
        generation_path = self.board_root(board_id) / "generations" / f"{generation_id}.json"
        if not generation_path.is_file():
            raise CapabilityPolicyError("committed policy generation is missing")
        if (
            not isinstance(digest, str)
            or hashlib.sha256(generation_path.read_bytes()).hexdigest() != digest
        ):
            raise CapabilityPolicyError("committed policy generation digest is invalid")
        document = self._read_json(generation_path, "committed policy generation")
        required = {
            "schema_version",
            "board_id",
            "tier",
            "setup_incomplete",
            "profile_snapshot",
            "map_snapshot",
            "evidence",
            "retained_generations",
            "legacy_migrated",
            "policy_digest",
        }
        self._require_exact_keys(document, required, "committed policy generation")
        material = {key: value for key, value in document.items() if key != "policy_digest"}
        if document["schema_version"] != _SCHEMA_VERSION or document["board_id"] != board_id:
            raise CapabilityPolicyError(
                "committed policy generation does not match this board or schema"
            )
        if document["policy_digest"] != _digest(material):
            raise CapabilityPolicyError("committed policy digest is invalid")
        try:
            tier = Tier(document["tier"])
        except (TypeError, ValueError) as exc:
            raise CapabilityPolicyError("committed policy has an invalid tier") from exc
        if not isinstance(document["setup_incomplete"], bool):
            raise CapabilityPolicyError("committed policy setup_incomplete must be boolean")
        if not isinstance(document["legacy_migrated"], bool):
            raise CapabilityPolicyError("committed policy legacy_migrated must be boolean")
        if tier is not Tier.NO_SETUP and not isinstance(document["map_snapshot"], Mapping):
            raise CapabilityPolicyError("protected committed policy has no map snapshot")
        if tier is Tier.NO_SETUP and document["map_snapshot"] is not None:
            raise CapabilityPolicyError("no-setup committed policy must not have an active map")
        if tier is Tier.SETUP_LITE:
            self._validate_lite_snapshot(document["map_snapshot"], board_id)
        if tier is Tier.SETUP_FULL:
            if not isinstance(document["profile_snapshot"], Mapping):
                raise CapabilityPolicyError("protected committed policy has no profile snapshot")
            self._validate_full_snapshot(
                document["profile_snapshot"], document["map_snapshot"], board_id
            )
        if not isinstance(document["retained_generations"], list) or not all(
            isinstance(item, str) and re.fullmatch(r"[0-9a-f]{32}", item)
            for item in document["retained_generations"]
        ):
            raise CapabilityPolicyError("committed policy retained generations are malformed")
        return CapabilityState(
            board_id=board_id,
            tier=tier,
            status=(
                "legacy-migrated"
                if document["legacy_migrated"]
                else "setup-incomplete"
                if document["setup_incomplete"]
                else "committed"
            ),
            policy_digest=cast_string(document["policy_digest"]),
            generation_id=generation_id,
            setup_incomplete=document["setup_incomplete"],
            profile_snapshot=copy.deepcopy(document["profile_snapshot"])
            if isinstance(document["profile_snapshot"], Mapping)
            else None,
            map_snapshot=copy.deepcopy(document["map_snapshot"])
            if isinstance(document["map_snapshot"], Mapping)
            else None,
            evidence=copy.deepcopy(document["evidence"])
            if isinstance(document["evidence"], Mapping)
            else None,
            retained_generations=tuple(document["retained_generations"]),
        )

    def _migrate_or_initialize(self, board_id: str) -> CapabilityState:
        profile_paths = tuple(
            (self.project_root / ".firm" / "boards" / f"{board_id}{suffix}")
            for suffix in (".yaml", ".yml", ".json")
        )
        map_path = self.project_root / ".firm" / "safety" / board_id / "memory_map.yaml"
        profiles = tuple(path for path in profile_paths if path.exists())
        has_map = map_path.exists()
        if not profiles and not has_map:
            return self._commit_locked(
                board_id,
                Tier.NO_SETUP,
                setup_incomplete=False,
                profile_snapshot=None,
                map_snapshot=None,
                evidence=None,
                retained_generations=(),
            )
        if len(profiles) != 1:
            return self._corrupt(board_id, "ambiguous legacy profile artifacts")
        if not has_map:
            try:
                profile = self._read_legacy_mapping(profiles[0])
            except CapabilityPolicyError as exc:
                return self._corrupt(board_id, str(exc))
            # The historical profile's safety reference is positive evidence
            # that protection was once committed.  Losing that referenced map
            # is damage, not a safe no-policy state.  A bare board-id marker,
            # or a syntactically valid staged v2 profile without that ref,
            # is the only recognizable incomplete-setup remnant.  Parseable
            # arbitrary YAML must never manufacture a raw hardware escape.
            if not self._recognized_incomplete_profile(profile, board_id):
                return self._corrupt(board_id, "legacy committed map is missing")
            committed = self._commit_locked(
                board_id,
                Tier.NO_SETUP,
                setup_incomplete=True,
                profile_snapshot=None,
                map_snapshot=None,
                evidence=None,
                retained_generations=(),
            )
            return self._with_status(committed, "setup-incomplete")
        try:
            profile = self._read_legacy_mapping(profiles[0])
            memory_map = self._read_legacy_mapping(map_path)
        except CapabilityPolicyError as exc:
            return self._corrupt(board_id, str(exc))
        if profile.get("board_id") != board_id or memory_map.get("board_id") != board_id:
            return self._corrupt(board_id, "legacy profile/map board identity is contradictory")
        try:
            self._validate_legacy_full(profile, memory_map, board_id)
        except CapabilityPolicyError as exc:
            return self._corrupt(board_id, str(exc))
        # Existing successful setup was guarded and identity-validated.  Its
        # migration must therefore preserve the old, strongest tier.
        state = self._commit_locked(
            board_id,
            Tier.SETUP_FULL,
            setup_incomplete=False,
            profile_snapshot=profile,
            map_snapshot=memory_map,
            evidence={},
            retained_generations=(),
            legacy_migrated=True,
        )
        return CapabilityState(
            board_id=state.board_id,
            tier=state.tier,
            status="legacy-migrated",
            policy_digest=state.policy_digest,
            generation_id=state.generation_id,
            setup_incomplete=state.setup_incomplete,
            profile_snapshot=state.profile_snapshot,
            map_snapshot=state.map_snapshot,
            evidence=state.evidence,
            retained_generations=state.retained_generations,
        )

    def _recognized_incomplete_profile(self, profile: Mapping[str, object], board_id: str) -> bool:
        """Recognize only positive, pre-map legacy setup remnants.

        Old setup could create an empty board marker before a profile was
        staged. It could also durably stage a complete schema-v2 profile just
        before associating a map. Those two shapes are safely distinguishable
        from an arbitrary parseable YAML mapping. In particular, no unknown
        fields, contradicting board id, safety reference, or partial schema-v2
        record becomes an implicit no-setup policy.
        """

        if profile == {"board_id": board_id}:
            return True
        if profile.get("board_id") != board_id or profile.get("safety_ref") is not None:
            return False
        try:
            from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
            from pyocd_debug_mcp.firmstore.store import FirmStore

            # This is the baseline strict v2 document loader, not a loose key
            # allowlist. It rejects unknown fields, malformed timestamps,
            # contradictory target facts, and incomplete staged profiles.
            ProfileRepository(FirmStore(self.project_root)).from_snapshot(board_id, profile)
        except Exception:
            return False
        return profile.get("schema_version") == 2

    @staticmethod
    def _validate_legacy_full(
        profile: Mapping[str, object], memory_map: Mapping[str, object], board_id: str
    ) -> None:
        """Require the baseline v2 semantic links before granting migrated full authority."""

        try:
            from pyocd_debug_mcp.safety.map_build import SafetyMapDocument, semantic_profile_digest

            document = SafetyMapDocument.from_document(memory_map)
            expected_ref = f".firm/safety/{board_id}/memory_map.yaml"
            if profile.get("schema_version") != 2:
                raise CapabilityPolicyError("legacy profile is not schema-v2")
            if profile.get("safety_ref") != expected_ref:
                raise CapabilityPolicyError(
                    "legacy profile safety_ref does not name this board's current map"
                )
            if document.identity.mcu_part_number != profile.get("mcu_part_number"):
                raise CapabilityPolicyError("legacy profile/map MCU identity is contradictory")
            if document.identity.pyocd_target != profile.get("pyocd_target"):
                raise CapabilityPolicyError("legacy profile/map target identity is contradictory")
            if document.source_digests.semantic_profile != semantic_profile_digest(profile):
                raise CapabilityPolicyError(
                    "legacy map semantic profile digest does not match the profile"
                )
        except CapabilityPolicyError:
            raise
        except Exception as exc:  # schema/authority parser errors are never raw fallback evidence
            raise CapabilityPolicyError(f"legacy full authority is invalid: {exc}") from exc

    @staticmethod
    def _validate_full_snapshot(
        profile_snapshot: Mapping[str, object] | None,
        map_snapshot: Mapping[str, object] | object | None,
        board_id: str,
    ) -> None:
        """Reject shape-only full generations before their pointer can publish.

        A full label is not authority: both the profile/map semantic link and a
        baseline schema validator must agree.  This accepts schema-v2 reviewed
        maps and schema-v3 generic maps because both are baseline full policy
        artifacts; it never accepts a partial compatibility projection.
        """

        if not isinstance(profile_snapshot, Mapping) or not isinstance(map_snapshot, Mapping):
            raise CapabilityPolicyError("setup-full requires validated profile and map snapshots")
        try:
            from pyocd_debug_mcp.safety.map_build import (
                GenericSafetyMapDocument,
                SafetyMapDocument,
                semantic_profile_digest,
            )

            schema = map_snapshot.get("schema_version")
            if schema == 2:
                document = SafetyMapDocument.from_document(map_snapshot)
            elif schema == 3:
                document = GenericSafetyMapDocument.from_document(map_snapshot)
            else:
                raise CapabilityPolicyError(
                    "setup-full map snapshot has an unsupported authority schema"
                )
            if profile_snapshot.get("board_id") != board_id or document.board_id != board_id:
                raise CapabilityPolicyError(
                    "setup-full profile/map snapshot board identity is contradictory"
                )
            if profile_snapshot.get("schema_version") != 2:
                raise CapabilityPolicyError("setup-full profile snapshot is not schema-v2")
            expected_ref = f".firm/safety/{board_id}/memory_map.yaml"
            if profile_snapshot.get("safety_ref") != expected_ref:
                raise CapabilityPolicyError(
                    "setup-full profile snapshot does not reference this board map"
                )
            if document.identity.mcu_part_number != profile_snapshot.get("mcu_part_number"):
                raise CapabilityPolicyError("setup-full profile/map MCU identity is contradictory")
            if document.identity.pyocd_target != profile_snapshot.get("pyocd_target"):
                raise CapabilityPolicyError(
                    "setup-full profile/map target identity is contradictory"
                )
            if document.source_digests.semantic_profile != semantic_profile_digest(
                profile_snapshot
            ):
                raise CapabilityPolicyError(
                    "setup-full map semantic profile digest does not match its profile"
                )
        except CapabilityPolicyError:
            raise
        except Exception as exc:
            raise CapabilityPolicyError(f"setup-full authority snapshot is invalid: {exc}") from exc

    @staticmethod
    def _validate_lite_snapshot(
        map_snapshot: Mapping[str, object] | object | None, board_id: str
    ) -> Mapping[str, object]:
        """Validate the complete normalized lite shape, not just one region kind."""

        if not isinstance(map_snapshot, Mapping):
            raise CapabilityPolicyError("setup-lite requires a normalized confirmed map snapshot")
        try:
            from pyocd_debug_mcp.capabilities.lite import normalize_lite_confirmation
            from pyocd_debug_mcp.safety.map_build import (
                GenericSafetyMapDocument,
                SafetyMapDocument,
            )

            # A prior, fully validated immutable safety map is stronger than a
            # lite confirmation.  It is permitted only as a retained crash
            # transition snapshot (never a shape-only shortcut), and is kept
            # verbatim so a post-pointer restart observes the exact generation
            # that committed before the process died.
            if map_snapshot.get("schema_version") in {2, 3}:
                document = (
                    SafetyMapDocument.from_document(map_snapshot)
                    if map_snapshot.get("schema_version") == 2
                    else GenericSafetyMapDocument.from_document(map_snapshot)
                )
                if document.board_id != board_id:
                    raise CapabilityPolicyError(
                        "setup-lite map snapshot board identity is contradictory"
                    )
                return copy.deepcopy(dict(map_snapshot))

            outer = set(map_snapshot)
            if outer not in (
                {"board_id", "regions", "flash"},
                {"board_id", "regions", "flash", "recovery"},
            ):
                raise CapabilityPolicyError("setup-lite map snapshot fields are invalid")
            if map_snapshot.get("board_id") != board_id:
                raise CapabilityPolicyError(
                    "setup-lite map snapshot board identity is contradictory"
                )
            normalized = normalize_lite_confirmation(
                board_id,
                {
                    "decision": "confirm-or-correct",
                    "regions": map_snapshot.get("regions"),
                    "flash": map_snapshot.get("flash"),
                    "recovery": map_snapshot.get("recovery"),
                },
            )
            if not CapabilityPolicyRepository._has_lite_safe_capability(normalized.map_snapshot):
                raise CapabilityPolicyError(
                    "setup-lite requires at least one confirmed map-dependent safe capability"
                )
            # Retain operator source spellings and numeric text as submitted;
            # containment parses this validated immutable evidence on use.
            # This is also what makes a crash after pointer replacement replay
            # precisely the committed generation, not a caller-owned object.
            return copy.deepcopy(dict(map_snapshot))
        except CapabilityPolicyError:
            raise
        except Exception as exc:
            raise CapabilityPolicyError(
                f"setup-lite confirmed map snapshot is invalid: {exc}"
            ) from exc

    def _corrupt(self, board_id: str, reason: str) -> CapabilityState:
        # Reason is deliberately not persisted as authority.  The caller can
        # surface a repair route while no hardware route sees a usable tier.
        del reason
        return CapabilityState(board_id, None, "corrupt", None, None)

    @staticmethod
    def _with_status(state: CapabilityState, status: str) -> CapabilityState:
        return CapabilityState(
            board_id=state.board_id,
            tier=state.tier,
            status=status,
            policy_digest=state.policy_digest,
            generation_id=state.generation_id,
            setup_incomplete=state.setup_incomplete,
            profile_snapshot=state.profile_snapshot,
            map_snapshot=state.map_snapshot,
            evidence=state.evidence,
            retained_generations=state.retained_generations,
        )

    @staticmethod
    def _has_lite_safe_capability(map_snapshot: Mapping[str, object] | None) -> bool:
        if map_snapshot is None:
            return False
        regions = map_snapshot.get("regions")
        if not isinstance(regions, list):
            return False
        for region in regions:
            if not isinstance(region, Mapping):
                continue
            kind = region.get("kind")
            readable = region.get("readable") is True
            writable = region.get("writable") is True
            executable = region.get("executable") is True
            if kind in {"ram", "physical_ram"} and (readable or writable):
                return True
            if kind in {"application_flash", "bootloader", "bootloader_flash", "rom"} and (
                readable or executable
            ):
                return True
            if kind in {"peripheral", "peripheral_read_only"} and readable:
                return True
            if kind in {"peripheral", "peripheral_write_only"} and writable:
                return True
        return False

    def _trip_fault(self, name: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(name)
        if os.environ.get(_TEST_FAULTS_ENV) == "1" and os.environ.get(_FAULT_ENV) == name:
            # os._exit is deliberate: subprocess tests need a real unhandled
            # process death at this persistence boundary.
            os._exit(86)  # noqa: PLW1510

    @staticmethod
    def _require_exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
        if set(value) != expected:
            raise CapabilityPolicyError(f"{label} has unexpected or missing fields")

    @staticmethod
    def _read_json(path: Path, label: str) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CapabilityPolicyError(f"{label} is unreadable or malformed") from exc
        if not isinstance(value, dict):
            raise CapabilityPolicyError(f"{label} must be an object")
        return value

    @staticmethod
    def _read_legacy_mapping(path: Path) -> dict[str, object]:
        try:
            if path.suffix.casefold() == ".json":
                value = json.loads(path.read_text(encoding="utf-8"))
            else:
                import yaml

                value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, YAMLError) as exc:
            raise CapabilityPolicyError(f"legacy artifact {path.name} is malformed") from exc
        if not isinstance(value, dict):
            raise CapabilityPolicyError(f"legacy artifact {path.name} must be an object")
        return value

    @staticmethod
    def _atomic_write(destination: Path, payload: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
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


def cast_string(value: object) -> str:
    if not isinstance(value, str):
        raise CapabilityPolicyError("committed policy digest is malformed")
    return value
