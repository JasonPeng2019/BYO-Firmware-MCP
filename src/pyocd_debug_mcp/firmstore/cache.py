"""Host-local attachment hints keyed only by stable USB identities."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal

from pyocd_debug_mcp.firmstore.store import FirmStore, ensure_no_persisted_authority

CACHE_SCHEMA_VERSION = 1


class AttachmentCacheError(ValueError):
    """Attachment cache input or persisted content is invalid."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _absolute_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AttachmentCacheError(f"{field_name} must be an absolute timezone-bearing timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttachmentCacheError(
            f"{field_name} must be an absolute timezone-bearing timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AttachmentCacheError(f"{field_name} must include an explicit timezone")
    return value


def _board_id(value: object) -> str:
    identity = str(value).strip()
    if not re.fullmatch(r"[a-z0-9_]{1,64}", identity):
        raise AttachmentCacheError(
            "board_id must be 1-64 lowercase letters, numbers, or underscores"
        )
    return identity


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttachmentCacheError(f"{field_name} must be a non-empty string")
    return value.strip()


def _usb_id(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFF:
        raise AttachmentCacheError(f"{field_name} must be an integer from 0 through 65535")
    return value


@dataclass(frozen=True, slots=True)
class ProbeIdentity:
    probe_family: str
    usb_serial: str | None

    @property
    def is_stable(self) -> bool:
        return bool(self.probe_family.strip() and self.usb_serial and self.usb_serial.strip())


@dataclass(frozen=True, slots=True)
class SerialEndpoint:
    port_path: str
    usb_serial: str | None
    vid: int | None
    pid: int | None

    @property
    def has_stable_identity(self) -> bool:
        return bool(
            self.usb_serial
            and self.usb_serial.strip()
            and isinstance(self.vid, int)
            and not isinstance(self.vid, bool)
            and 0 <= self.vid <= 0xFFFF
            and isinstance(self.pid, int)
            and not isinstance(self.pid, bool)
            and 0 <= self.pid <= 0xFFFF
        )

    @property
    def is_resolvable(self) -> bool:
        return self.has_stable_identity and bool(self.port_path.strip())

    def stable_key(self) -> tuple[str, int, int] | None:
        if not self.has_stable_identity:
            return None
        assert self.usb_serial is not None and self.vid is not None and self.pid is not None
        return (self.usb_serial.strip(), self.vid, self.pid)


@dataclass(frozen=True, slots=True)
class AttachmentRecord:
    board_id: str
    probe_family: str
    probe_usb_serial: str
    uart_usb_serial: str
    uart_vid: int
    uart_pid: int
    confirmed: bool
    confirmed_at: str
    revoked_at: str | None = None

    @property
    def probe_key(self) -> tuple[str, str]:
        return (self.probe_family, self.probe_usb_serial)

    @property
    def uart_key(self) -> tuple[str, int, int]:
        return (self.uart_usb_serial, self.uart_vid, self.uart_pid)

    def to_document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, raw: object) -> AttachmentRecord:
        if not isinstance(raw, dict):
            raise AttachmentCacheError("Each attachment cache record must be an object")
        allowed = {
            "board_id",
            "probe_family",
            "probe_usb_serial",
            "uart_usb_serial",
            "uart_vid",
            "uart_pid",
            "confirmed",
            "confirmed_at",
            "revoked_at",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise AttachmentCacheError(f"Unknown attachment cache fields: {unknown}")
        missing = sorted((allowed - {"revoked_at"}) - set(raw))
        if missing:
            raise AttachmentCacheError(f"Missing attachment cache fields: {missing}")
        if not isinstance(raw["confirmed"], bool):
            raise AttachmentCacheError("confirmed must be a boolean")
        revoked_at = raw.get("revoked_at")
        if revoked_at is not None:
            revoked_at = _absolute_timestamp(revoked_at, "revoked_at")
        record = cls(
            board_id=_board_id(raw["board_id"]),
            probe_family=_nonempty(raw["probe_family"], "probe_family"),
            probe_usb_serial=_nonempty(raw["probe_usb_serial"], "probe_usb_serial"),
            uart_usb_serial=_nonempty(raw["uart_usb_serial"], "uart_usb_serial"),
            uart_vid=_usb_id(raw["uart_vid"], "uart_vid"),
            uart_pid=_usb_id(raw["uart_pid"], "uart_pid"),
            confirmed=raw["confirmed"],
            confirmed_at=_absolute_timestamp(raw["confirmed_at"], "confirmed_at"),
            revoked_at=revoked_at,
        )
        if record.confirmed == (record.revoked_at is not None):
            raise AttachmentCacheError(
                "confirmed records must not have revoked_at; revoked records must be unconfirmed"
            )
        return record


ResolutionReason = Literal[
    "exact_match",
    "no_record",
    "missing_stable_identity",
    "hardware_changed",
    "multiple_matches",
    "probe_changed",
    "revoked",
]


@dataclass(frozen=True, slots=True)
class CacheResolution:
    reused: bool
    reason: ResolutionReason
    port_path: str | None = None


class AttachmentCache:
    """Persist and resolve non-authoritative attachment hints."""

    def __init__(self, store: FirmStore) -> None:
        self.store = store
        self.path = store.layout.cache_artifact("attachments.json")
        self._lock = threading.RLock()

    def load_records(self) -> list[AttachmentRecord]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AttachmentCacheError(f"Could not parse attachment cache: {exc}") from exc
        ensure_no_persisted_authority(raw, location="attachment cache")
        if not isinstance(raw, dict) or raw.get("schema_version") != CACHE_SCHEMA_VERSION:
            raise AttachmentCacheError(
                f"Attachment cache must declare schema_version {CACHE_SCHEMA_VERSION}"
            )
        if set(raw) != {"schema_version", "records"} or not isinstance(raw["records"], list):
            raise AttachmentCacheError("Attachment cache must contain only a records list")
        return [AttachmentRecord.from_document(record) for record in raw["records"]]

    def _write(self, records: list[AttachmentRecord]) -> None:
        document = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "records": [record.to_document() for record in records],
        }
        self.store.atomic_write_json(self.path, document)

    @staticmethod
    def _validated_identity(
        board_id: str,
        probe: ProbeIdentity,
        uart: SerialEndpoint,
    ) -> tuple[str, tuple[str, str], tuple[str, int, int]]:
        identity = _board_id(board_id)
        if not probe.is_stable:
            raise AttachmentCacheError("A stable probe family and USB serial are required")
        if not uart.has_stable_identity:
            raise AttachmentCacheError(
                "Stable UART USB serial, vendor ID, and product ID are required"
            )
        assert probe.usb_serial is not None
        uart_key = uart.stable_key()
        assert uart_key is not None
        return (
            identity,
            (probe.probe_family.strip(), probe.usb_serial.strip()),
            uart_key,
        )

    def confirm(
        self,
        board_id: str,
        probe: ProbeIdentity,
        uart: SerialEndpoint,
        *,
        confirmed_at: str | None = None,
    ) -> AttachmentRecord:
        identity, probe_key, uart_key = self._validated_identity(board_id, probe, uart)
        timestamp = _absolute_timestamp(confirmed_at or _timestamp(), "confirmed_at")
        candidate = AttachmentRecord(
            board_id=identity,
            probe_family=probe_key[0],
            probe_usb_serial=probe_key[1],
            uart_usb_serial=uart_key[0],
            uart_vid=uart_key[1],
            uart_pid=uart_key[2],
            confirmed=True,
            confirmed_at=timestamp,
        )
        with self._lock:
            records = self.load_records()
            matching = [
                record
                for record in records
                if record.board_id == identity
                and record.probe_key == probe_key
                and record.uart_key == uart_key
            ]
            if len(matching) == 1 and matching[0] == candidate:
                return matching[0]
            records = [
                record
                for record in records
                if not (
                    record.board_id == identity
                    and record.probe_key == probe_key
                    and record.uart_key == uart_key
                )
            ]
            records.append(candidate)
            self._write(records)
        return candidate

    def revoke(
        self,
        board_id: str,
        probe: ProbeIdentity,
        uart: SerialEndpoint,
        *,
        revoked_at: str | None = None,
    ) -> bool:
        identity, probe_key, uart_key = self._validated_identity(board_id, probe, uart)
        timestamp = _absolute_timestamp(revoked_at or _timestamp(), "revoked_at")
        changed = False
        with self._lock:
            records = self.load_records()
            updated: list[AttachmentRecord] = []
            for record in records:
                if (
                    record.board_id == identity
                    and record.probe_key == probe_key
                    and record.uart_key == uart_key
                    and record.confirmed
                ):
                    updated.append(replace(record, confirmed=False, revoked_at=timestamp))
                    changed = True
                else:
                    updated.append(record)
            if changed:
                self._write(updated)
        return changed

    def resolve(
        self,
        board_id: str,
        probe: ProbeIdentity,
        uart_endpoints: list[SerialEndpoint],
    ) -> CacheResolution:
        identity = _board_id(board_id)
        if not probe.is_stable:
            return CacheResolution(False, "missing_stable_identity")
        assert probe.usb_serial is not None
        probe_key = (probe.probe_family.strip(), probe.usb_serial.strip())
        records = [record for record in self.load_records() if record.board_id == identity]
        if not records:
            return CacheResolution(False, "no_record")
        probe_records = [record for record in records if record.probe_key == probe_key]
        if not probe_records:
            return CacheResolution(False, "probe_changed")

        candidates = [endpoint for endpoint in uart_endpoints if endpoint.is_resolvable]
        matches = [
            (record, endpoint)
            for record in probe_records
            if record.confirmed and record.revoked_at is None
            for endpoint in candidates
            if endpoint.stable_key() == record.uart_key
        ]
        if len(matches) == 1:
            return CacheResolution(True, "exact_match", matches[0][1].port_path)
        if len(matches) > 1:
            return CacheResolution(False, "multiple_matches")

        revoked_match = any(
            not record.confirmed
            and record.revoked_at is not None
            and any(endpoint.stable_key() == record.uart_key for endpoint in candidates)
            for record in probe_records
        )
        if revoked_match:
            return CacheResolution(False, "revoked")
        if any(not endpoint.has_stable_identity for endpoint in uart_endpoints):
            return CacheResolution(False, "missing_stable_identity")
        return CacheResolution(False, "hardware_changed")
