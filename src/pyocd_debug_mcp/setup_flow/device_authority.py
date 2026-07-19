"""Common, non-board-owned device-support authority records.

The setup surface must distinguish *device* support from reference-board
deployment and transport policy.  This module deliberately contains no probe,
partition, or caller-provided address fields; those have different authority
owners.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Mapping

from pyocd_debug_mcp.setup_flow.device_support import DeviceSupportCandidate, normalize_part_number

AuthorityKind = Literal["catalog", "resolved_pack"]


def _canonical_digest(domain: str, document: Mapping[str, str]) -> str:
    payload = json.dumps(
        document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(domain.encode("utf-8") + b"\0" + payload).hexdigest()


@dataclass(frozen=True, slots=True)
class DeviceSupportAuthority:
    """The canonical server-selected device-support identity.

    A record is enough to revalidate exact pack/target provenance.  It is not
    a deployment policy and therefore carries no application partition,
    bootloader, board wiring, probe family, reset mode, or clock rate.
    """

    kind: AuthorityKind
    support_id: str
    mcu_part_number: str
    pyocd_target: str
    source: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.kind not in {"catalog", "resolved_pack"}:
            raise ValueError("device authority kind is invalid")
        if len(self.support_id) != 64 or any(
            character not in "0123456789abcdef" for character in self.support_id
        ):
            raise ValueError("device authority support_id must be a lowercase SHA-256")
        if not self.mcu_part_number.strip() or not self.pyocd_target.strip():
            raise ValueError("device authority requires MCU part and pyOCD target")
        if not self.source or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for key, value in self.source.items()
        ):
            raise ValueError("device authority source must be a non-empty string mapping")

    @classmethod
    def from_resolved_pack(cls, candidate: DeviceSupportCandidate) -> "DeviceSupportAuthority":
        source = candidate.to_authority_document()
        return cls(
            "resolved_pack",
            candidate.support_id,
            candidate.part_number,
            candidate.pyocd_target,
            source,
        )

    @property
    def normalized_part_number(self) -> str:
        return normalize_part_number(self.mcu_part_number)

    def to_document(self) -> dict[str, object]:
        """Return a detached persistence-ready record with closed top-level keys."""

        return {
            "kind": self.kind,
            "support_id": self.support_id,
            "mcu_part_number": self.mcu_part_number,
            "pyocd_target": self.pyocd_target,
            "source": dict(sorted(self.source.items())),
        }

    @property
    def canonical_digest(self) -> str:
        document = {
            "kind": self.kind,
            "support_id": self.support_id,
            "mcu_part_number": self.mcu_part_number,
            "pyocd_target": self.pyocd_target,
            **{f"source.{key}": value for key, value in sorted(self.source.items())},
        }
        return _canonical_digest("device-support-authority-v1", document)

