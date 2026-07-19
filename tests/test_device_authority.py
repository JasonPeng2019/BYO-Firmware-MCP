from __future__ import annotations

import pytest

from pyocd_debug_mcp.setup_flow.device_authority import DeviceSupportAuthority
from pyocd_debug_mcp.setup_flow.device_support import DeviceSupportCandidate


def _candidate() -> DeviceSupportCandidate:
    return DeviceSupportCandidate(
        candidate_id="a" * 64,
        part_number="STM32L476RGT6",
        pdsc_device="STM32L476RGTx",
        pyocd_target="stm32l476rgtx",
        pack_id="Keil.STM32L4xx_DFP",
        pack_filename="Keil.STM32L4xx_DFP.3.1.0.pack",
        pack_sha256="b" * 64,
    )


def test_resolved_pack_authority_has_no_reference_board_policy() -> None:
    authority = DeviceSupportAuthority.from_resolved_pack(_candidate())

    assert authority.kind == "resolved_pack"
    assert authority.normalized_part_number == "stm32l476rgt6"
    assert authority.to_document()["source"] == _candidate().to_authority_document()
    assert len(authority.canonical_digest) == 64
    assert "application" not in authority.to_document()
    assert "probe_family" not in authority.to_document()


def test_device_authority_rejects_noncanonical_source_identity() -> None:
    with pytest.raises(ValueError, match="support_id"):
        DeviceSupportAuthority("resolved_pack", "bad", "part", "target", {"id": "x"})
    with pytest.raises(ValueError, match="source"):
        DeviceSupportAuthority("resolved_pack", "a" * 64, "part", "target", {})
