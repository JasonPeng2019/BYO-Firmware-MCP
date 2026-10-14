"""Test-only stdio server with a fixture-seeded full policy and simulated SWD.

Never installed as a product entry point. Public connect and board_validate run
normally; only the physical probe inventory and target adapter are simulated.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace


def main() -> None:
    from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
    from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
    from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
    from tests.tiered_acceptance_support import DeterministicTargetBackend, known_good_full_policy

    if os.environ.get("BYO_MCP_TEST_SEAMS") != "1":
        raise RuntimeError("CAN simulation server requires explicit test seams")
    root = Path(os.environ["BYO_MCP_ARTIFACT_ROOT"])
    from pyocd_debug_mcp.firmstore.store import FirmStore
    from pyocd_debug_mcp.setup_flow.device_support import resolve_builtin_target_support
    from pyocd_debug_mcp.setup_flow.datasheet_evidence import capture_datasheet_evidence
    from pyocd_debug_mcp import server
    import json

    policy = CapabilityPolicyRepository(root)
    profile, _ = known_good_full_policy()
    candidate = resolve_builtin_target_support("nRF52840-QIAA", "nrf52840").with_identity_proof(
        0x410FC241
    )
    proof = candidate.identity_proof
    assert proof is not None
    # Explicit fixture evidence: no physical board or official datasheet claim.
    datasheet = root / "simulated-datasheet.pdf"
    datasheet.write_bytes(
        b"%PDF-1.4\n% software acceptance fixture, not hardware qualification\n%%EOF\n"
    )
    captured = capture_datasheet_evidence(FirmStore(root), datasheet)
    profile.update(
        probe_family="cmsisdap",
        mcu_part_number="nRF52840-QIAA",
        device_support=candidate.to_authority_document(),
        datasheet_sha256=captured.sha256,
        datasheet_ref=captured.reference,
        silicon_id_address=proof.address,
        silicon_id_expected=proof.expected,
        silicon_id_mask=proof.mask,
        silicon_id_width_bits=proof.width_bits,
        silicon_id_label=proof.label,
    )
    hydrated = server._profile_repository.from_snapshot("legacy_full", profile)
    memory_map = server._derive_generic_safety_map("legacy_full", profile=hydrated)
    policy.commit(
        "legacy_full",
        Tier.SETUP_FULL,
        profile_snapshot=hydrated.to_document(),
        map_snapshot=memory_map.to_document(),
    )
    profile_path = root / ".firm/boards/legacy_full.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(json.dumps(hydrated.to_document()), encoding="utf-8")
    backend = DeterministicTargetBackend(read_value=0x410FC241)
    server.configure_tiered_test_seams(backend=backend, policy_repository=policy)
    inventory = ValidationInventory(
        probes=(
            ValidationProbe(
                "SIMULATED-PROBE",
                "Simulated CAN acceptance CMSIS-DAP",
                "cmsisdap",
                "SIMULATED-PROBE",
            ),
        )
    )
    server._hardware_inventory = SimpleNamespace(
        snapshot=lambda: snapshot_from_validation_inventory(inventory),
        validation_inventory=lambda: inventory,
    )
    server.main()
    # main returns only after the real ordered teardown has completed. The
    # client checks this receipt after EOF rather than inferring cleanup merely
    # from the stdio transport context closing.
    import multiprocessing

    receipt = {
        "main_returned": True,
        "can_phase": server._can_manager.phase,
        "active_can_workers": [
            child.pid
            for child in multiprocessing.active_children()
            if child.name == "byo-can-worker" and child.is_alive()
        ],
        "assigned_boards": list(server.connection_manager.assigned_board_ids()),
    }
    (root / "shutdown-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")


if __name__ == "__main__":
    main()
