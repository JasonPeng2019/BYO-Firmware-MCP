"""Internal SWD adapter contract for shared target-control services."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from firmware_mcp.board_config import BoardConfig


@dataclass(frozen=True, slots=True)
class TargetSessionMetadata:
    """Serializable facts for one live target session.

    ``runtime_token`` identifies only this live session. It is deliberately
    separate from optional hardware identity and is not stable across reconnects.
    """

    board_name: str
    probe_description: str
    probe_family: str
    probe_uid: str | None
    live_part_number: str | None
    route_used: str
    target_override: str | None
    runtime_token: str
    live_identity: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PhysicalMemoryRegion:
    """One provider-observed physical region for the current live session.

    Bounds are half-open.  ``kind`` and ``name`` are diagnostics only; access
    decisions use the explicit provider-reported access flags.
    """

    start: int
    end: int
    readable: bool
    writable: bool
    executable: bool
    kind: str
    name: str
    provenance: str
    session_token: str

    def to_record(self) -> dict[str, object]:
        return {
            "start": self.start,
            "end": self.end,
            "readable": self.readable,
            "writable": self.writable,
            "executable": self.executable,
            "kind": self.kind,
            "name": self.name,
            "provenance": self.provenance,
            "session_token": self.session_token,
        }

    @classmethod
    def from_record(cls, value: object) -> "PhysicalMemoryRegion":
        required = {
            "start",
            "end",
            "readable",
            "writable",
            "executable",
            "kind",
            "name",
            "provenance",
            "session_token",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("worker physical-memory region schema was invalid")
        start, end = value["start"], value["end"]
        flags = (value["readable"], value["writable"], value["executable"])
        text = (value["kind"], value["name"], value["provenance"], value["session_token"])
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not 0 <= start < end <= 1 << 64
            or any(not isinstance(flag, bool) for flag in flags)
            or any(not isinstance(item, str) or not item.strip() for item in text)
        ):
            raise ValueError("worker physical-memory region fields were invalid")
        return cls(start, end, *flags, *(item.strip() for item in text))


@dataclass(frozen=True, slots=True)
class RecoveryCapability:
    """One live provider-described destructive recovery operation.

    The coverage union is intentionally provider-neutral.  It is validated at
    the worker boundary so the parent never turns an opaque provider string
    into an invented erase claim.
    """

    mechanism: str
    effect: str
    coverage: dict[str, object]
    effect_verification: str
    session_postcondition: str

    def to_record(self) -> dict[str, object]:
        return {
            "mechanism": self.mechanism,
            "effect": self.effect,
            "coverage": self.coverage,
            "effect_verification": self.effect_verification,
            "session_postcondition": self.session_postcondition,
        }

    @classmethod
    def from_record(cls, value: object) -> "RecoveryCapability":
        fields = {"mechanism", "effect", "coverage", "effect_verification", "session_postcondition"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("worker recovery capability schema was invalid")
        text = ("mechanism", "effect", "effect_verification", "session_postcondition")
        if any(not isinstance(value[key], str) or not value[key].strip() for key in text):
            raise ValueError("worker recovery capability fields were invalid")
        if value["session_postcondition"] not in {"preserved", "invalidated", "unknown"}:
            raise ValueError("worker recovery session postcondition was invalid")
        coverage = value["coverage"]
        if not isinstance(coverage, dict) or not isinstance(coverage.get("kind"), str):
            raise ValueError("worker recovery coverage was invalid")
        if coverage["kind"] == "all_matching":
            kinds = coverage.get("physical_kinds")
            if (
                set(coverage) != {"kind", "physical_kinds"}
                or not isinstance(kinds, list)
                or not kinds
                or kinds != sorted(set(kinds))
                or any(not isinstance(item, str) or not item for item in kinds)
            ):
                raise ValueError("worker all-matching recovery coverage was invalid")
        elif coverage["kind"] == "exact_ranges":
            ranges = coverage.get("ranges")
            if set(coverage) != {"kind", "ranges"} or not isinstance(ranges, list) or not ranges:
                raise ValueError("worker exact-range recovery coverage was invalid")
            previous = -1
            for item in ranges:
                if not isinstance(item, dict) or set(item) != {"start", "end"}:
                    raise ValueError("worker recovery range schema was invalid")
                start, end = item["start"], item["end"]
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or not isinstance(end, int)
                    or isinstance(end, bool)
                    or not 0 <= start < end <= 1 << 64
                    or start < previous
                ):
                    raise ValueError("worker recovery ranges were invalid")
                previous = end
        else:
            raise ValueError("worker recovery coverage kind was invalid")
        return cls(
            *(
                value[key].strip() if isinstance(value[key], str) else value[key]
                for key in (
                    "mechanism",
                    "effect",
                    "coverage",
                    "effect_verification",
                    "session_postcondition",
                )
            )
        )


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    mechanism: str
    accepted: bool
    verification: str
    observed_session_postcondition: str

    def to_record(self) -> dict[str, object]:
        return {
            "mechanism": self.mechanism,
            "accepted": self.accepted,
            "verification": self.verification,
            "observed_session_postcondition": self.observed_session_postcondition,
        }

    @classmethod
    def from_record(cls, value: object) -> "RecoveryResult":
        fields = {"mechanism", "accepted", "verification", "observed_session_postcondition"}
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or not isinstance(value["mechanism"], str)
            or not value["mechanism"].strip()
            or not isinstance(value["accepted"], bool)
            or not isinstance(value["verification"], str)
            or not value["verification"].strip()
            or value["observed_session_postcondition"]
            not in {"preserved", "invalidated", "unknown"}
        ):
            raise ValueError("worker recovery result was invalid")
        return cls(
            value["mechanism"].strip(),
            value["accepted"],
            value["verification"].strip(),
            value["observed_session_postcondition"],
        )


@dataclass(frozen=True, slots=True)
class FlashVerification:
    """Evidence from a completed program and byte-for-byte target readback."""

    firmware_path: str
    byte_count: int
    verified_ranges: tuple[tuple[int, int], ...]
    expected_sha256: str
    observed_sha256: str
    final_reset_postcondition: str
    final_reset_error_type: str | None = None
    final_reset_error_message: str | None = None
    session_token: str | None = None
    support_identity: str | None = None

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "firmware_path": self.firmware_path,
            "byte_count": self.byte_count,
            "verified_ranges": [list(item) for item in self.verified_ranges],
            "expected_sha256": self.expected_sha256,
            "observed_sha256": self.observed_sha256,
            "final_reset_postcondition": self.final_reset_postcondition,
        }
        if self.final_reset_postcondition.casefold() in {"unknown", "failed"}:
            record["final_reset_error_type"] = self.final_reset_error_type
            record["final_reset_error_message"] = self.final_reset_error_message
        if self.session_token is not None or self.support_identity is not None:
            record["session_token"] = self.session_token
            record["support_identity"] = self.support_identity
        return record

    @classmethod
    def from_record(
        cls, value: object, *, allow_uncertain_final_reset: bool = False
    ) -> "FlashVerification":
        success_fields = {
            "firmware_path",
            "byte_count",
            "verified_ranges",
            "expected_sha256",
            "observed_sha256",
            "final_reset_postcondition",
        }
        uncertain_fields = success_fields | {"final_reset_error_type", "final_reset_error_message"}
        binding_fields = {"session_token", "support_identity"}
        allowed = {
            frozenset(success_fields),
            frozenset(uncertain_fields),
            frozenset(success_fields | binding_fields),
            frozenset(uncertain_fields | binding_fields),
        }
        if not isinstance(value, dict) or frozenset(value) not in allowed:
            raise ValueError("worker flash verification result was invalid")
        path = value["firmware_path"]
        byte_count = value["byte_count"]
        digest_fields = (value["expected_sha256"], value["observed_sha256"])
        final_state = value["final_reset_postcondition"]
        error_type = value.get("final_reset_error_type")
        error_message = value.get("final_reset_error_message")
        session_token = value.get("session_token")
        support_identity = value.get("support_identity")
        raw_ranges = value["verified_ranges"]
        if (
            not isinstance(path, str)
            or not path.strip()
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 1
            or any(
                not isinstance(item, str) or re.fullmatch(r"[0-9a-fA-F]{64}", item) is None
                for item in digest_fields
            )
            or digest_fields[0].casefold() != digest_fields[1].casefold()
            or not isinstance(final_state, str)
            or not final_state.strip()
            or (final_state.casefold() in {"unknown", "failed"} and not allow_uncertain_final_reset)
            or not isinstance(raw_ranges, list)
        ):
            raise ValueError("worker flash verification fields were invalid")
        uncertain = final_state.casefold() in {"unknown", "failed"}
        unbound_fields = uncertain_fields if uncertain else success_fields
        if set(value) not in (unbound_fields, unbound_fields | binding_fields):
            raise ValueError("worker flash verification mixes success and uncertain reset fields")
        if (session_token is None) != (support_identity is None) or (
            session_token is not None
            and (
                not isinstance(session_token, str)
                or not session_token.strip()
                or not isinstance(support_identity, str)
                or not support_identity.strip()
            )
        ):
            raise ValueError("worker flash verification session binding was invalid")
        if uncertain and (
            not isinstance(error_type, str)
            or not error_type.strip()
            or not isinstance(error_message, str)
            or not error_message.strip()
        ):
            raise ValueError("worker uncertain flash verification lacks reset error evidence")
        ranges: list[tuple[int, int]] = []
        for item in raw_ranges:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or any(not isinstance(part, int) or isinstance(part, bool) for part in item)
                or item[0] < 0
                or item[1] <= item[0]
            ):
                raise ValueError("worker flash verification range was invalid")
            if ranges and item[0] <= ranges[-1][1]:
                raise ValueError(
                    "worker flash verification ranges must be sorted, nonoverlapping, and coalesced"
                )
            ranges.append((item[0], item[1]))
        if not ranges or byte_count != sum(end - start for start, end in ranges):
            raise ValueError("worker flash verification byte_count does not match its ranges")
        return cls(
            path.strip(),
            byte_count,
            tuple(ranges),
            digest_fields[0].casefold(),
            digest_fields[1].casefold(),
            final_state.strip(),
            error_type.strip() if isinstance(error_type, str) else None,
            error_message.strip() if isinstance(error_message, str) else None,
            session_token.strip() if isinstance(session_token, str) else None,
            support_identity.strip() if isinstance(support_identity, str) else None,
        )


@dataclass(frozen=True, slots=True)
class TargetSessionHandle:
    """Open target session plus the board facts and routing used to create it."""

    session: Any
    board: BoardConfig | None
    probe_uid: str | None
    route_used: str
    target_override: str | None
    worker: Any | None = None
    metadata: TargetSessionMetadata | None = None

    def __post_init__(self) -> None:
        if self.metadata is not None:
            return
        board_name = self.board.display_name if self.board is not None else ""
        probe_family = self.board.probe_family if self.board is not None else "unknown"
        object.__setattr__(
            self,
            "metadata",
            TargetSessionMetadata(
                board_name=board_name,
                probe_description="",
                probe_family=probe_family,
                probe_uid=self.probe_uid,
                live_part_number=None,
                route_used=self.route_used,
                target_override=self.target_override,
                runtime_token=uuid4().hex,
            ),
        )


def session_metadata(handle: TargetSessionHandle) -> TargetSessionMetadata:
    """Return the immutable metadata record established for every live handle."""

    if handle.metadata is None:
        raise RuntimeError("Target session has no immutable metadata record.")
    return handle.metadata


class DebugInterface(ABC):
    """Minimal target-control surface shared by server and Stage 0."""

    @abstractmethod
    def open(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        protocol: str | None = None,
        connect_mode: str | None = None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
        frequency_hz: int | None = None,
    ) -> TargetSessionHandle:
        """Open a live debug session, optionally with one quarantined pack candidate."""

    @abstractmethod
    def close(self, handle: TargetSessionHandle) -> dict[str, object] | None:
        """Close a previously opened session."""

    @abstractmethod
    def connect_under_reset(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
    ) -> TargetSessionHandle:
        """Assert physical reset, attach and halt, then release reset."""

    @abstractmethod
    def get_state(self, handle: TargetSessionHandle) -> str:
        """Return the target's current run state."""

    @abstractmethod
    def read_memory(
        self,
        handle: TargetSessionHandle,
        address: int,
        width_bits: int,
    ) -> int:
        """Read one memory value."""

    @abstractmethod
    def read_memory_block(
        self, handle: TargetSessionHandle, address: int, length: int
    ) -> list[int]:
        """Read a block of bytes from target memory."""

    @abstractmethod
    def physical_memory_regions(
        self, handle: TargetSessionHandle
    ) -> tuple[PhysicalMemoryRegion, ...]:
        """Return current live provider regions for this exact session only."""

    @abstractmethod
    def write_memory(
        self,
        handle: TargetSessionHandle,
        address: int,
        value: int,
        width_bits: int,
    ) -> None:
        """Write one memory value."""

    @abstractmethod
    def read_core_register(self, handle: TargetSessionHandle, name: str) -> int:
        """Read one core register."""

    @abstractmethod
    def write_core_register(self, handle: TargetSessionHandle, name: str, value: int) -> None:
        """Write one core register."""

    @abstractmethod
    def supported_core_registers(self, handle: TargetSessionHandle) -> tuple[str, ...]:
        """Return register names discovered from the connected core at runtime."""

    @abstractmethod
    def halt(self, handle: TargetSessionHandle) -> None:
        """Halt the target core."""

    @abstractmethod
    def resume(self, handle: TargetSessionHandle) -> None:
        """Resume the target core."""

    @abstractmethod
    def step(self, handle: TargetSessionHandle) -> None:
        """Single-step one instruction."""

    @abstractmethod
    def reset(self, handle: TargetSessionHandle) -> None:
        """Reset and run the target."""

    @abstractmethod
    def reset_and_halt(self, handle: TargetSessionHandle) -> None:
        """Reset and halt the target."""

    @abstractmethod
    def release_reset(self, handle: TargetSessionHandle) -> None:
        """Deassert the connected probe's wired reset line."""

    @abstractmethod
    def flash(
        self,
        handle: TargetSessionHandle,
        firmware: Path,
        *,
        halt_after_reset: bool,
    ) -> FlashVerification:
        """Flash a target artifact using the backend's native path."""

    @abstractmethod
    def recovery_capabilities(self, handle: TargetSessionHandle) -> tuple[RecoveryCapability, ...]:
        """Return all current typed recovery capabilities for this live session."""

        return ()

    @abstractmethod
    def recover(self, handle: TargetSessionHandle, mechanism: str) -> RecoveryResult:
        """Execute exactly one selected live recovery capability."""

    @abstractmethod
    def set_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        """Set a breakpoint."""

    @abstractmethod
    def remove_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        """Remove a breakpoint."""
