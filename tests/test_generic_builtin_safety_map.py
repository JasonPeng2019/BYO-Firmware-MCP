"""Regression for persisted built-in-target safety-map replay."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import unittest
import zipfile
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.pack_provision import PackSpec, pack_spec_document
from pyocd_debug_mcp.safety.map_build import (
    GenericSafetyMapDocument,
    GenericSourceDigests,
    require_reconciled_authority,
)
from pyocd_debug_mcp.setup_flow import device_support
from pyocd_debug_mcp.setup_flow.device_support import resolve_builtin_target_support
from pyocd_debug_mcp.setup_flow.preflight import PreflightDecision, ProbeCandidate, SetupUserInput
from pyocd_debug_mcp.setup_flow.setup import SetupPhaseContext
from pyocd_debug_mcp.setup_flow.validate import (
    ValidationBackend,
    ValidationInventory,
    ValidationProbe,
)
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    public_text,
    tiered_test_server,
)


NRF_FIXTURES = Path(__file__).with_name("fixtures") / "nordic"
NRF_PART = "nRF52840-QIAA"
NRF_PACK_SHA256 = "95136b57a8310ba367aa46cb66c8d149561fc7876c5f3736f323a9dbfb8f559e"
NRF_PDF_SHA256 = "c3c94efaa39d8e2c7df48ba25f2d8b8430275c1f7bff22e1a93b5ab1052d5e94"


def observe_nrf_control(
    testcase: unittest.TestCase, fixture: SimpleNamespace, label: str, **details: object
) -> None:
    """Retain actual public replies, map/source binding and all backend effects in Actions."""
    destination = os.environ.get("BYO_FINAL_CASE_EVIDENCE")
    if destination:
        root = Path(destination) / testcase.id()
        root.mkdir(parents=True, exist_ok=True)
        state = fixture.server._tier_router.state_for(fixture.board_id)
        (root / (label + ".json")).write_text(
            json.dumps(
                {
                    "test": testcase.id(),
                    "pyocd_version": version("pyocd"),
                    "provenance": fixture.provenance,
                    "support": fixture.candidate.to_authority_document(),
                    "geometry": fixture.geometry.to_document(),
                    "map_snapshot": state.map_snapshot,
                    "profile_snapshot": state.profile_snapshot,
                    "generation_id": state.generation_id,
                    "setup_phases": fixture.setup_phases,
                    "backend_calls": fixture.backend.calls,
                    **details,
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )


def nrf_registered_pack_fixture(testcase: unittest.TestCase) -> SimpleNamespace:
    """Real Nordic PDSC/SVD/PDF -> setup -> committed generic map; only device I/O is fake.

    This is registered-pack authority. The built-in target's static map is not
    augmented or mocked. All temporary environment/registry paths are owned here.
    """
    root = testcase.enterContext(isolated_project())
    home = root / "home"
    home.mkdir()
    testcase.enterContext(
        patch.dict(
            os.environ,
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "APPDATA": str(home / "roaming"),
                "LOCALAPPDATA": str(home / "local"),
                "XDG_CACHE_HOME": str(home / "cache"),
                "XDG_DATA_HOME": str(home / "data"),
                "XDG_CONFIG_HOME": str(home / "config"),
            },
        )
    )
    server = testcase.enterContext(tiered_test_server(root))
    store = server._profile_repository.store
    # Bind the real resolver's path configuration to this private registry too;
    # do not leave its compatibility lookup pointing at another imported root.
    testcase.enterContext(patch.object(device_support, "MANIFEST_PATH", store.layout.pack_manifest))
    testcase.enterContext(patch.object(device_support, "PACKS_DIR", store.layout.pack_files))
    provenance = json.loads((NRF_FIXTURES / "PROVENANCE.json").read_text(encoding="utf-8"))
    pack_info = provenance["pack"]
    source = NRF_FIXTURES / pack_info["filename"]
    testcase.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), NRF_PACK_SHA256)
    testcase.assertEqual(source.stat().st_size, pack_info["bytes"])
    with zipfile.ZipFile(source) as archive:
        for member in pack_info["members"]:
            payload = archive.read(member["path"])
            testcase.assertEqual(len(payload), member["bytes"])
            testcase.assertEqual(hashlib.sha256(payload).hexdigest(), member["sha256"])
    store.layout.pack_files.mkdir(parents=True, exist_ok=True)
    pack_path = store.layout.pack_files / source.name
    shutil.copyfile(source, pack_path)
    # Both the exact PDSC leaf and its canonical target/compatible identity are
    # derived by the shipped parser, rather than trusted from a fixture label.
    binding = device_support.derive_candidate_binding(pack_path, NRF_PART)
    testcase.assertEqual(binding.pdsc_device, "nRF52840_xxAA")
    testcase.assertEqual(binding.pyocd_target, "nrf52840_xxaa")
    testcase.assertIsNotNone(binding.identity_proof)
    spec = PackSpec(
        "NordicSemiconductor.nRF_DeviceFamilyPack",
        "8.44.1",
        source.name,
        pack_info["source_url"],
        NRF_PACK_SHA256,
        provides_targets=(binding.pyocd_target,),
        device_bindings=(binding,),
    )
    store.atomic_write_yaml(store.layout.pack_manifest, {"packs": [pack_spec_document(spec)]})
    candidate = device_support.resolve_project_pack_support(store, NRF_PART)
    geometry = device_support.resolve_registered_pack_geometry(candidate, store)
    pdf_info = provenance["datasheet"]
    pdf = root / pdf_info["filename"]
    shutil.copyfile(NRF_FIXTURES / pdf.name, pdf)
    testcase.assertEqual(hashlib.sha256(pdf.read_bytes()).hexdigest(), NRF_PDF_SHA256)
    testcase.assertEqual(pdf.stat().st_size, pdf_info["bytes"])
    # A real Cortex-M4 compatibility read is simulated, not exact silicon proof.
    backend = DeterministicTargetBackend(
        read_value=0x410FC241, supported_targets=frozenset({binding.pyocd_target})
    )
    server.configure_tiered_test_seams(backend=backend)
    board_id = "fr15_nrf52840"
    fixture = SimpleNamespace(
        root=root,
        server=server,
        board_id=board_id,
        backend=backend,
        provenance=provenance,
        candidate=candidate,
        geometry=geometry,
        setup_phases=[],
    )
    testcase.addCleanup(observe_nrf_control, testcase, fixture, "setup-and-final-state")
    probe = ProbeCandidate("nrf-probe", "CMSIS-DAP", "cmsisdap", "fr15-nrf-serial")
    connection_id = server.probe_connection_id(probe.probe_family, probe.usb_serial)
    server.assignment_store.assign(connection_id, board_id)
    server._setup_tiers[board_id] = Tier.SETUP_FULL
    user = SetupUserInput(
        board_id,
        connection_id,
        "FR15 Nordic nRF52840",
        NRF_PART,
        None,
        datasheet_path=str(pdf),
        requires_uart=False,
    )
    context = SetupPhaseContext(
        "fr15-continuation",
        "fr15-attempt",
        "setup",
        user,
        PreflightDecision(
            "preflight_ready",
            "setup/preflight-ready",
            "ready",
            selected_probe=probe,
            selected_target=binding.pyocd_target,
        ),
        {},
    )
    # Inventory and device calls are the only simulated setup inputs. All
    # support replay, PDF capture, map generation, validation and publication run.
    with patch.object(
        server._board_validator,
        "_backend",
        ValidationBackend(
            inventory=lambda: ValidationInventory(
                probes=(
                    ValidationProbe(
                        probe.probe_id, probe.description, probe.probe_family, probe.usb_serial
                    ),
                )
            ),
            target_supported=server._validation_target_supported,
            connect=server._validation_connect,
            read_memory=server._validation_read,
            capture_serial=server._validation_capture,
            close=server._validation_close,
        ),
    ):
        testcase.addCleanup(server.disconnect, board_id)
        for phase in (
            server._setup_connection_phase,
            server._setup_validation_phase,
            server._setup_safety_research_phase,
            server._setup_safety_map_phase,
            server._setup_commit_phase,
        ):
            outcome = phase(context)
            fixture.setup_phases.append({"phase": phase.__name__, **asdict(outcome)})
            testcase.assertTrue(outcome.verified, (outcome.agent_prompt, outcome.details))
    state = CapabilityPolicyRepository(root).resolve(board_id)
    testcase.assertIs(state.tier, Tier.SETUP_FULL)
    assert state.profile_snapshot is not None
    testcase.assertEqual(state.profile_snapshot["pyocd_target"], binding.pyocd_target)
    testcase.assertEqual(server._handle(board_id).target_override, binding.pyocd_target)
    document = server._active_full_document(board_id)
    server._require_map_authority(document)
    testcase.assertEqual(document.authority_source, candidate.to_authority_document())
    loaded = json.loads(
        public_text(
            server,
            "load_setup_tool",
            {"board_id": board_id, "tool_name": "board_safety_refresh"},
        )
    )
    testcase.assertEqual(loaded["status"], "setup_tool_loaded", loaded)
    refresh = json.loads(public_text(server, "board_safety_refresh", {"board_id": board_id}))
    testcase.assertEqual(refresh["status"], "safety_refresh_completed", refresh)
    fixture.document = server._active_full_document(board_id)
    testcase.assertEqual(fixture.document.to_document(), document.to_document())
    testcase.assertEqual(fixture.document.identity.mcu_part_number, NRF_PART)
    testcase.assertEqual(fixture.document.identity.pyocd_target, binding.pyocd_target)
    observe_nrf_control(testcase, fixture, "public-regeneration", refresh=refresh)
    backend.calls.clear()
    return fixture


class BuiltInTargetMapReplay(unittest.TestCase):
    def test_round_trips_resolved_builtin_target_authority(self) -> None:
        authority = (
            resolve_builtin_target_support("nRF52840-QIAA", "nrf52840")
            .with_identity_proof(0x410FC241)
            .to_authority_document()
        )
        digest = authority["support_id"]
        provenance = [
            {
                "authority": "device_support",
                "source_id": f"builtin-target:{digest}",
                "detail": "installed built-in target nrf52840",
            }
        ]
        document = {
            "schema_version": 3,
            "board_id": "nrf52840",
            "identity": {
                "mcu_part_number": "nRF52840-QIAA",
                "pyocd_target": "nrf52840",
                "authority_kind": "resolved_builtin_target",
                "support_id": digest,
            },
            "authority_source": authority,
            "source_digests": {
                "semantic_profile": "c" * 64,
                "device_support": "d" * 64,
                "datasheet_evidence": "e" * 64,
                "deployment_policy": "f" * 64,
                "map_generator_schema": GenericSourceDigests.build(
                    profile={},
                    device_support={},
                    datasheet_evidence={},
                    deployment_policy={},
                ).map_generator_schema,
            },
            "geometry": {
                "physical_flash": [{"start": 0, "end": 4096}],
                "physical_ram": [{"start": 0x20000000, "end": 0x20001000}],
                "erase": {"kind": "unavailable"},
            },
            "partitions": {"application": None, "bootloader": None},
            "deployment_policy": {"kind": "none"},
            "regions": [
                {
                    "name": "built-in flash",
                    "kind": "physical_flash",
                    "start": 0,
                    "end": 4096,
                    "executable": False,
                    "provenance": provenance,
                },
                {
                    "name": "built-in RAM",
                    "kind": "physical_ram",
                    "start": 0x20000000,
                    "end": 0x20001000,
                    "executable": False,
                    "provenance": provenance,
                },
            ],
        }

        replayed = GenericSafetyMapDocument.from_document(document)

        self.assertEqual(replayed.to_document(), document)
        require_reconciled_authority(replayed)


class NrfRegisteredPackGeometry(unittest.TestCase):
    def test_real_nrf_pdsc_svd_geometry_survives_public_regeneration(self) -> None:
        fixture = nrf_registered_pack_fixture(self)
        # Expected vendor addresses detect selecting an STM/generic fallback;
        # the requests themselves are obtained from the interpreted SVD rows.
        expected = {
            "NVMC.CONFIG": (0x4001E504, True),
            "NVMC.ERASEPAGE": (0x4001E508, True),
            "NVMC.ERASEALL": (0x4001E50C, True),
            "NVMC.ERASEUICR": (0x4001E514, True),
            "UICR.APPROTECT": (0x10001208, True),
            "UICR.NFCPINS": (0x1000120C, True),
            "P0.OUT": (0x50000504, False),
        }
        rows = {row.name: row for row in fixture.geometry.peripheral_regions}
        for name, (address, prohibited) in expected.items():
            with self.subTest(register=name):
                row = rows[name]
                self.assertEqual((row.start, row.end), (address, address + 4))
                self.assertEqual(row.prohibited, prohibited)
                self.assertTrue(row.writable)
        self.assertTrue(rows["P0.OUT"].readable)
        self.assertFalse(rows["NVMC.ERASEALL"].readable)
        self.assertTrue(
            any(row.start == 0 and row.end == 0x100000 for row in fixture.geometry.flash_regions)
        )
        self.assertTrue(
            any(
                row.start == 0x20000000 and row.end == 0x20040000
                for row in fixture.geometry.ram_regions
            )
        )
        self.assertEqual(fixture.document.authority_source["kind"], "resolved_pack")
        self.assertEqual(fixture.document.authority_source["pack_sha256"], NRF_PACK_SHA256)
        self.assertEqual(fixture.backend.calls, [])


if __name__ == "__main__":
    unittest.main()
