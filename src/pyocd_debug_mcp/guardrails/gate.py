"""Run-scoped, default-closed live-identity and safety-map gate state."""

from __future__ import annotations

import threading
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone


class GateRefusal(RuntimeError):
    """A validated session or current safety-map association is unavailable."""

    def __init__(self, code: str, message: str, *, remedy: tuple[str, ...]) -> None:
        self.code = code
        self.remedy = remedy
        rendered = " then ".join(remedy)
        super().__init__(f"{message} Required remedy: {rendered}.")


@dataclass(frozen=True, slots=True)
class LiveIdentityStamp:
    """One validation run's proof of the silicon attached to a connection."""

    board_id: str
    connection_id: str
    probe_identity: str
    observed_mcu: str
    identity_capability: str
    validation_run: str
    validated_at: str


@dataclass(frozen=True, slots=True)
class SafetyMapStamp:
    """The canonical digest currently associated with a live identity proof."""

    board_id: str
    map_digest: str


@dataclass(frozen=True, slots=True)
class ValidationStamp:
    """Cohesive view of the two independently managed gate concepts."""

    live_identity: LiveIdentityStamp
    safety_map: SafetyMapStamp

    @property
    def board_id(self) -> str:
        return self.live_identity.board_id

    @property
    def connection_id(self) -> str:
        return self.live_identity.connection_id

    @property
    def probe_identity(self) -> str:
        return self.live_identity.probe_identity

    @property
    def observed_mcu(self) -> str:
        return self.live_identity.observed_mcu

    @property
    def identity_capability(self) -> str:
        """Whether the live proof is exact or a documented compatibility proof."""

        return self.live_identity.identity_capability

    @property
    def validation_run(self) -> str:
        return self.live_identity.validation_run

    @property
    def validated_at(self) -> str:
        return self.live_identity.validated_at

    @property
    def map_digest(self) -> str:
        return self.safety_map.map_digest


@dataclass(frozen=True, slots=True)
class MismatchAllowance:
    """Exact, run-scoped evidence permitting only the new-profile adoption route."""

    board_id: str
    connection_id: str
    probe_identity: str
    expected_mcu: str
    observed_mcu: str
    validation_run: str
    recorded_at: str


class GateManager:
    """Own run-scoped identity, map, and mismatch state; none is serializable here."""

    _IDENTITY = "live_identity"
    _MAP = "safety_map"
    _MISMATCH = "mismatch"

    def __init__(
        self,
        state: MutableMapping[object, object] | None = None,
    ) -> None:
        self._state = state if state is not None else {}
        self._guard = threading.RLock()
        self._closure_reasons: dict[str, str] = {}

    @staticmethod
    def _required(value: str, label: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{label} must be non-empty")
        return normalized

    @classmethod
    def _identity_key(cls, board_id: str) -> tuple[str, str]:
        return (cls._IDENTITY, board_id)

    @classmethod
    def _map_key(cls, board_id: str) -> tuple[str, str]:
        return (cls._MAP, board_id)

    @classmethod
    def _mismatch_key(
        cls,
        board_id: str,
        connection_id: str,
        probe_identity: str,
        expected_mcu: str,
        observed_mcu: str,
    ) -> tuple[str, str, str, str, str, str]:
        return (
            cls._MISMATCH,
            board_id,
            connection_id,
            probe_identity,
            expected_mcu,
            observed_mcu,
        )

    def stamp_validation(
        self,
        *,
        board_id: str,
        connection_id: str,
        probe_identity: str,
        observed_mcu: str,
        validation_run: str,
        map_digest: str,
        identity_capability: str = "exact",
    ) -> ValidationStamp:
        """Atomically establish live identity and bind the current parsed map."""

        board = self._required(board_id, "board_id")
        connection = self._required(connection_id, "connection_id")
        probe = self._required(probe_identity, "probe_identity")
        observed = self._required(observed_mcu, "observed_mcu")
        capability = self._required(identity_capability, "identity_capability")
        if capability not in {"exact", "compatible"}:
            raise ValueError("identity_capability must be exact or compatible")
        validation = self._required(validation_run, "validation_run")
        digest = self._required(map_digest, "map_digest")
        identity = LiveIdentityStamp(
            board,
            connection,
            probe,
            observed,
            capability,
            validation,
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        safety_map = SafetyMapStamp(board, digest)
        with self._guard:
            self._state[self._identity_key(board)] = identity
            self._state[self._map_key(board)] = safety_map
            self._clear_mismatches_locked(board_id=board)
            self._closure_reasons.pop(board, None)
        return ValidationStamp(identity, safety_map)

    def live_identity(self, board_id: str) -> LiveIdentityStamp | None:
        board = self._required(board_id, "board_id")
        with self._guard:
            value = self._state.get(self._identity_key(board))
            return value if isinstance(value, LiveIdentityStamp) else None

    def map_stamp(self, board_id: str) -> SafetyMapStamp | None:
        board = self._required(board_id, "board_id")
        with self._guard:
            value = self._state.get(self._map_key(board))
            return value if isinstance(value, SafetyMapStamp) else None

    def snapshot(self, board_id: str) -> ValidationStamp | None:
        board = self._required(board_id, "board_id")
        with self._guard:
            identity = self._state.get(self._identity_key(board))
            safety_map = self._state.get(self._map_key(board))
            if not isinstance(identity, LiveIdentityStamp) or not isinstance(
                safety_map, SafetyMapStamp
            ):
                return None
            return ValidationStamp(identity, safety_map)

    def clear(self, board_id: str, reason: str) -> ValidationStamp | None:
        """Clear all live proof and mismatch routing for one logical board."""

        board = self._required(board_id, "board_id")
        with self._guard:
            previous = self.snapshot(board)
            self._state.pop(self._identity_key(board), None)
            self._state.pop(self._map_key(board), None)
            self._clear_mismatches_locked(board_id=board)
            self._closure_reasons[board] = reason.strip() or "gate closed"
            return previous

    def clear_connection(self, connection_id: str, reason: str) -> tuple[str, ...]:
        """Clear live proof and mismatch allowances tied to one connection."""

        connection = self._required(connection_id, "connection_id")
        with self._guard:
            boards = {
                value.board_id
                for value in self._state.values()
                if (
                    isinstance(value, (LiveIdentityStamp, MismatchAllowance))
                    and value.connection_id == connection
                )
            }
            for board in boards:
                self._state.pop(self._identity_key(board), None)
                self._state.pop(self._map_key(board), None)
                self._closure_reasons[board] = reason.strip() or "connection closed"
            self._clear_mismatches_locked(connection_id=connection)
            return tuple(sorted(boards))

    def require_validated(self, board_id: str, connection_id: str) -> ValidationStamp:
        board = self._required(board_id, "board_id")
        connection = self._required(connection_id, "connection_id")
        identity = self.live_identity(board)
        if identity is None:
            reason = self._closure_reasons.get(board, "this Server Run has no live identity proof")
            raise GateRefusal(
                "gate/validation-required",
                f"Board '{board}' is not validated for this connection ({reason}).",
                remedy=("board_validate",),
            )
        if identity.connection_id != connection:
            self.clear(board, "connection identity changed")
            raise GateRefusal(
                "gate/connection-changed",
                f"Board '{board}' was validated on a different connection.",
                remedy=("board_validate",),
            )
        safety_map = self.map_stamp(board)
        if safety_map is None:
            raise GateRefusal(
                "gate/safety-map-unbound",
                f"Board '{board}' has live identity proof but no current safety-map binding.",
                remedy=("board_safety_refresh",),
            )
        return ValidationStamp(identity, safety_map)

    def require_write(
        self,
        board_id: str,
        connection_id: str,
        current_map_digest: str,
    ) -> ValidationStamp:
        stamp = self.require_validated(board_id, connection_id)
        current = self._required(current_map_digest, "current_map_digest")
        if stamp.map_digest != current:
            # A stable-map change does not invalidate the independently proven live identity.
            with self._guard:
                self._state.pop(self._map_key(stamp.board_id), None)
            raise GateRefusal(
                "gate/safety-map-stale",
                f"Board '{board_id}' safety map changed after it was associated with validation.",
                remedy=("board_safety_refresh",),
            )
        return stamp

    def refresh_map_stamp(
        self,
        board_id: str,
        connection_id: str,
        map_digest: str,
    ) -> ValidationStamp | None:
        """Update only the map association; never create live identity authority."""

        board = self._required(board_id, "board_id")
        connection = self._required(connection_id, "connection_id")
        digest = self._required(map_digest, "map_digest")
        with self._guard:
            identity = self._state.get(self._identity_key(board))
            if not isinstance(identity, LiveIdentityStamp):
                return None
            if identity.connection_id != connection:
                return None
            safety_map = SafetyMapStamp(board, digest)
            self._state[self._map_key(board)] = safety_map
            return ValidationStamp(identity, safety_map)

    def record_mismatch(
        self,
        *,
        board_id: str,
        connection_id: str,
        probe_identity: str,
        expected_mcu: str,
        observed_mcu: str,
        validation_run: str,
    ) -> MismatchAllowance:
        """Record exact mismatch evidence without permitting in-place profile mutation."""

        board = self._required(board_id, "board_id")
        connection = self._required(connection_id, "connection_id")
        probe = self._required(probe_identity, "probe_identity")
        expected = self._required(expected_mcu, "expected_mcu")
        observed = self._required(observed_mcu, "observed_mcu")
        validation = self._required(validation_run, "validation_run")
        allowance = MismatchAllowance(
            board,
            connection,
            probe,
            expected,
            observed,
            validation,
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        with self._guard:
            self._state.pop(self._identity_key(board), None)
            self._state.pop(self._map_key(board), None)
            self._clear_mismatches_locked(board_id=board)
            self._state[
                self._mismatch_key(board, connection, probe, expected, observed)
            ] = allowance
            self._closure_reasons[board] = "live MCU identity mismatched the established profile"
        return allowance

    def mismatch_allowance(
        self,
        *,
        board_id: str,
        connection_id: str,
        probe_identity: str,
        expected_mcu: str,
        observed_mcu: str,
    ) -> MismatchAllowance | None:
        """Return only an exact allowance; partial or fuzzy matching is prohibited."""

        values = (
            self._required(board_id, "board_id"),
            self._required(connection_id, "connection_id"),
            self._required(probe_identity, "probe_identity"),
            self._required(expected_mcu, "expected_mcu"),
            self._required(observed_mcu, "observed_mcu"),
        )
        with self._guard:
            value = self._state.get(self._mismatch_key(*values))
            return value if isinstance(value, MismatchAllowance) else None

    def current_mismatch(
        self,
        board_id: str,
        connection_id: str,
        probe_identity: str,
    ) -> MismatchAllowance | None:
        """Find the one server-recorded exact mismatch without caller-supplied MCU facts."""

        board = self._required(board_id, "board_id")
        connection = self._required(connection_id, "connection_id")
        probe = self._required(probe_identity, "probe_identity")
        with self._guard:
            matches = [
                value
                for value in self._state.values()
                if isinstance(value, MismatchAllowance)
                and value.board_id == board
                and value.connection_id == connection
                and value.probe_identity == probe
            ]
            return matches[0] if len(matches) == 1 else None

    def clear_mismatch(self, board_id: str) -> None:
        board = self._required(board_id, "board_id")
        with self._guard:
            self._clear_mismatches_locked(board_id=board)

    def rollback_validation(self, board_id: str, validation_run: str) -> bool:
        """Remove only authority created by one failed validation completion."""

        board = self._required(board_id, "board_id")
        validation = self._required(validation_run, "validation_run")
        removed = False
        with self._guard:
            identity = self._state.get(self._identity_key(board))
            if isinstance(identity, LiveIdentityStamp) and identity.validation_run == validation:
                self._state.pop(self._identity_key(board), None)
                self._state.pop(self._map_key(board), None)
                removed = True
            mismatch_keys = [
                key
                for key, value in self._state.items()
                if isinstance(value, MismatchAllowance)
                and value.board_id == board
                and value.validation_run == validation
            ]
            for key in mismatch_keys:
                self._state.pop(key, None)
                removed = True
            if removed:
                self._closure_reasons[board] = "validation completion failed"
        return removed

    def _clear_mismatches_locked(
        self,
        *,
        board_id: str | None = None,
        connection_id: str | None = None,
    ) -> None:
        matching = [
            key
            for key, value in self._state.items()
            if isinstance(value, MismatchAllowance)
            and (board_id is None or value.board_id == board_id)
            and (connection_id is None or value.connection_id == connection_id)
        ]
        for key in matching:
            self._state.pop(key, None)
