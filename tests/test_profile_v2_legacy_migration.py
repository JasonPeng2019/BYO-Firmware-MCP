from __future__ import annotations

import copy
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Mapping, cast
from unittest.mock import patch

from firmware_mcp import server
from firmware_mcp.board_config import load_board_config_document
from firmware_mcp.firmstore.profiles import ProfileError, ProfileRepository
from firmware_mcp.firmstore.store import FirmStore
from firmware_mcp.pack_provision import PackSpec, pack_spec_document, sha256_bytes
from firmware_mcp.setup_flow.datasheet_evidence import capture_datasheet_evidence
from firmware_mcp.setup_flow.device_support import (
    derive_candidate_binding,
    live_cpuid_compatibility_proof,
    resolve_builtin_target_support,
    resolve_project_pack_support,
)
from firmware_mcp.setup_flow.validate import ValidationInventory, ValidationProbe


# This is deliberately opt-in HIL corpus coverage. Normal source, wheel, and CI
# tests use the generated exact-replay fixture below and never infer a user or
# sibling-project path. Set it to a Firmware-2 project root to validate released
# profile bytes read-only.
_EXTERNAL_CORPUS_ENV = "FIRMWARE_MCP_HIL_LEGACY_PROFILE_ROOT"
_PACK_FILENAME = "portable-legacy-test.pack"
_PART_NUMBER = "TEST123"
_PDSC = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<package schemaVersion=\"1.7.36\"><name>PortableLegacy</name><vendor>Firmware MCP tests</vendor>
<description>Portable historical schema-v2 replay fixture</description><releases><release version=\"1.0.0\">test</release></releases>
<devices><family Dvendor=\"Firmware MCP tests\" Dfamily=\"Portable\"><device Dname=\"TEST123\">
<processor Pname=\"Cortex-M4\"/><memory name=\"IROM1\" access=\"rx\" start=\"0x08000000\" size=\"0x1000\" default=\"1\" startup=\"1\"/>
<memory name=\"IRAM1\" access=\"rwx\" start=\"0x20000000\" size=\"0x1000\" default=\"1\"/>
</device></family></devices></package>"""


class LegacySchemaV2ProfileMigrationTests(unittest.TestCase):
    """Exercise released schema-v2 replay with portable verified artifacts."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.store = FirmStore(self.root)
        self.store.ensure_layout()
        self.candidate = self._install_verified_pack_fixture()
        self._install_legacy_profile()
        self.repository = ProfileRepository(self.store)
        self.base_document = dict(load_board_config_document(self.path))

    @property
    def path(self) -> Path:
        return self.store.layout.board_profile("board_a")

    def _install_verified_pack_fixture(self):
        pack_path = self.store.layout.pack_files / _PACK_FILENAME
        pack_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(pack_path, "w") as archive:
            archive.writestr("PortableLegacy.pdsc", _PDSC)
        payload = pack_path.read_bytes()
        binding = derive_candidate_binding(pack_path, _PART_NUMBER)
        spec = PackSpec(
            id="firmware-mcp::portable-legacy",
            version="1.0.0",
            filename=_PACK_FILENAME,
            url="https://example.invalid/portable-legacy-test.pack",
            sha256=sha256_bytes(payload),
            provides_targets=(binding.pyocd_target,),
            device_bindings=(binding,),
        )
        self.store.atomic_write_pack_manifest({"packs": [pack_spec_document(spec)]})
        return resolve_project_pack_support(self.store, _PART_NUMBER)

    def _install_legacy_profile(self) -> None:
        datasheet = self.root / "portable-datasheet.pdf"
        datasheet.write_bytes(b"%PDF-1.4\nportable legacy fixture\n")
        evidence = capture_datasheet_evidence(self.store, datasheet)
        proof = self.candidate.identity_proof
        assert proof is not None
        self.store.atomic_write_yaml(
            self.path,
            {
                "schema_version": 2,
                "board_id": "board_a",
                "display_name": "Portable Board A",
                "mcu_part_number": _PART_NUMBER,
                "mcu_family": "portable",
                "probe_family": "portable",
                "probe_type": "portable probe",
                "pyocd_target": self.candidate.pyocd_target,
                "serial_baudrate": 115200,
                "created_at": "2026-07-22T17:44:19.969400Z",
                "updated_at": "2026-07-22T17:44:32.235459Z",
                "requires_recover_validation": False,
                "test_read_address": 0,
                "datasheet_sha256": evidence.sha256,
                "datasheet_ref": evidence.reference,
                "device_support": {
                    **self.candidate.to_authority_document(),
                    "support_id": self.candidate.legacy_schema_v2_support_id,
                },
                "silicon_id_address": proof.address,
                "silicon_id_expected": proof.expected,
                "silicon_id_mask": proof.mask,
                "silicon_id_width_bits": proof.width_bits,
                "silicon_id_label": proof.label,
                "debug_connect_mode": "attach",
                "safety_ref": ".firm/safety/board_a/memory_map.yaml",
            },
        )

    def _document(self) -> dict[str, object]:
        return copy.deepcopy(self.base_document)

    def _write(self, document: Mapping[str, object]) -> None:
        self.store.atomic_write_yaml(self.path, document)

    def _load_legacy(self, **changes: object):
        document = self._document()
        document.update(changes)
        self._write(document)
        return self.repository.load("board_a")

    def test_external_released_corpus_loads_read_only_when_explicitly_configured(self) -> None:
        configured = os.environ.get(_EXTERNAL_CORPUS_ENV, "").strip()
        if not configured:
            self.skipTest(
                f"set {_EXTERNAL_CORPUS_ENV} to a released Firmware-2 project root "
                "for the external read-only corpus assertion"
            )
        root = Path(configured).expanduser().resolve(strict=True)
        store = FirmStore(root)
        repository = ProfileRepository(store)
        paths = sorted(store.layout.boards.glob("*.y*ml")) + sorted(
            store.layout.boards.glob("*.json")
        )
        self.assertTrue(paths, "external corpus has no board profiles")
        before = {path: path.read_bytes() for path in paths}

        profiles = repository.load_all()

        self.assertEqual({profile.source_path for profile in profiles}, set(paths))
        self.assertTrue(
            any("pyocd_target" in load_board_config_document(path) for path in paths),
            "external corpus must contain at least one released pyocd_target profile",
        )
        for profile in profiles:
            raw = load_board_config_document(profile.source_path)
            if "pyocd_target" in raw:
                self.assertEqual(profile.board.provider_id, "pyocd")
                self.assertEqual(profile.board.target, raw["pyocd_target"])
                canonical = profile.to_document()
                self.assertIn("target", canonical)
                self.assertIn("provider_id", canonical)
                self.assertNotIn("pyocd_target", canonical)
                self.assertNotIn("requires_recover_validation", canonical)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_legacy_pack_profile_loads_read_only_and_normalizes_support(self) -> None:
        before = self.path.read_bytes()

        profile = self.repository.load("board_a")

        self.assertEqual(profile.board.provider_id, "pyocd")
        self.assertEqual(profile.board.target, self.candidate.pyocd_target)
        self.assertEqual(profile.created_at, "2026-07-22T17:44:19.969400Z")
        self.assertEqual(profile.safety_ref, ".firm/safety/board_a/memory_map.yaml")
        self.assertEqual(profile.device_support, self.candidate.to_authority_document())
        canonical = profile.to_document()
        self.assertIn("target", canonical)
        self.assertIn("provider_id", canonical)
        self.assertNotIn("pyocd_target", canonical)
        self.assertNotIn("requires_recover_validation", canonical)
        self.assertEqual(before, self.path.read_bytes())

    def test_legacy_recovery_forms_and_alias_normalize_only_with_marker(self) -> None:
        for required, mode in (
            (False, None),
            (True, ""),
            (False, " backend_mass_erase "),
            (True, "MANUAL_ONLY"),
        ):
            with self.subTest(required=required, mode=mode):
                profile = self._load_legacy(requires_recover_validation=required, recover_mode=mode)
                canonical = profile.to_document()
                self.assertEqual(profile.board.provider_id, "pyocd")
                self.assertNotIn("requires_recover_validation", canonical)
                self.assertNotIn("recover_mode", canonical)

        document = self._document()
        document["target"] = document["pyocd_target"]
        self._write(document)
        self.assertEqual(self.repository.load("board_a").board.target, self.candidate.pyocd_target)
        document["target"] = "different-target"
        self._write(document)
        with self.assertRaisesRegex(ProfileError, "target and pyocd_target"):
            self.repository.load("board_a")

    def test_legacy_fields_are_strict_and_current_documents_are_not_migrated(self) -> None:
        for change in (
            {"requires_recover_validation": "false"},
            {"recover_mode": 1},
            {"recover_mode": "unsafe-erase"},
            {"unrelated_legacy_field": "value"},
        ):
            with self.subTest(change=change):
                with self.assertRaises(ProfileError):
                    self._load_legacy(**change)

        document = self._document()
        document["target"] = document.pop("pyocd_target")
        document.pop("provider_id", None)
        document.pop("requires_recover_validation", None)
        with self.subTest("target only has no implicit provider"):
            self._write(document)
            with self.assertRaisesRegex(ProfileError, "provider_id"):
                self.repository.load("board_a")

        document["provider_id"] = "pyocd"
        with self.subTest("historical support IDs are not canonical authority"):
            self._write(document)
            with self.assertRaises(ProfileError):
                self.repository.load("board_a")

        document["requires_recover_validation"] = False
        with self.subTest("current documents retain unknown legacy fields"):
            self._write(document)
            with self.assertRaisesRegex(ProfileError, "Unknown schema-v2"):
                self.repository.load("board_a")
        document.pop("requires_recover_validation")
        document["recover_mode"] = "manual_only"
        with self.subTest("current documents retain every legacy recovery field"):
            self._write(document)
            with self.assertRaisesRegex(ProfileError, "Unknown schema-v2"):
                self.repository.load("board_a")

    def test_real_pack_replay_rejects_any_historical_backing_mismatch(self) -> None:
        cases: dict[str, tuple[str, object]] = {
            "support id": ("support_id", "0" * 64),
            "pack id": ("pack_id", "other-pack"),
            "filename": ("pack_filename", "other.pack"),
            "digest": ("pack_sha256", "0" * 64),
            "PDSC leaf": ("pdsc_device", "other-device"),
            "target": ("pyocd_target", "other-target"),
        }
        for label, (field, value) in cases.items():
            with self.subTest(label=label):
                document = self._document()
                support = cast(dict[str, str], document["device_support"])
                support[field] = cast(str, value)
                self._write(document)
                with self.assertRaises(ProfileError):
                    self.repository.load("board_a")

        document = self._document()
        document["silicon_id_expected"] = 0
        self._write(document)
        with self.assertRaises(ProfileError):
            self.repository.load("board_a")

        document = self._document()
        document["mcu_part_number"] = "TEST124"
        self._write(document)
        with self.assertRaises(ProfileError):
            self.repository.load("board_a")

        pack = self.store.layout.pack_files / _PACK_FILENAME
        pack.write_bytes(pack.read_bytes() + b"changed")
        with self.assertRaises(ProfileError):
            self.repository.load("board_a")

    def test_real_builtin_historical_support_replays_and_rejects_mismatches(self) -> None:
        proof = live_cpuid_compatibility_proof(0x410FC241)
        candidate = resolve_builtin_target_support(
            "nRF52840-QIAA", "stm32f103rc", identity_proof=proof
        )
        document = self._document()
        document["mcu_part_number"] = candidate.part_number
        document["mcu_family"] = "nrf52840"
        document["device_support"] = {
            **candidate.to_authority_document(),
            "support_id": candidate.legacy_schema_v2_support_id,
        }
        document["pyocd_target"] = candidate.pyocd_target
        document["silicon_id_address"] = proof.address
        document["silicon_id_expected"] = proof.expected
        document["silicon_id_mask"] = proof.mask
        document["silicon_id_width_bits"] = proof.width_bits
        document["silicon_id_label"] = proof.label
        self._write(document)

        profile = self.repository.load("board_a")
        self.assertEqual(profile.device_support, candidate.to_authority_document())

        for field, value in (
            ("support_id", "0" * 64),
            ("part_number", "OTHER"),
            ("pyocd_target", "stm32f103rb"),
            ("geometry_sha256", "0" * 64),
            ("identity_expected", "0"),
        ):
            with self.subTest(field=field):
                mutated = self._document()
                mutated["mcu_part_number"] = candidate.part_number
                support = cast(dict[str, str], mutated["device_support"])
                support.update(candidate.to_authority_document())
                support["support_id"] = candidate.legacy_schema_v2_support_id
                support[field] = value
                mutated["pyocd_target"] = candidate.pyocd_target
                mutated["silicon_id_address"] = proof.address
                mutated["silicon_id_expected"] = proof.expected
                mutated["silicon_id_mask"] = proof.mask
                mutated["silicon_id_width_bits"] = proof.width_bits
                mutated["silicon_id_label"] = proof.label
                self._write(mutated)
                with self.assertRaises(ProfileError):
                    self.repository.load("board_a")

    def test_staged_write_is_canonical_and_setup_overview_remains_available(self) -> None:
        staged = self.repository.stage_optional(
            "board_a", {"expected_uart_substring": "canonical write"}
        )
        self.repository.commit_optional(staged)
        written = load_board_config_document(self.path)
        self.assertIn("target", written)
        self.assertIn("provider_id", written)
        self.assertNotIn("pyocd_target", written)
        self.assertNotIn("requires_recover_validation", written)

        inventory = ValidationInventory(
            probes=(ValidationProbe("probe-a", "Probe A", "cmsis-dap", "probe-a"),)
        )
        with (
            patch.object(server, "_profile_repository", self.repository),
            patch.object(server, "_validation_inventory", return_value=inventory),
            patch.object(server, "_replace_setup_assignments") as replace_assignments,
        ):
            overview = server._setup_overview(
                ["Portable Board A"], {"Portable Board A": "probe:probe-a"}
            )
        self.assertEqual(overview["status"], "setup_routes_ready")
        profiles = cast(list[dict[str, object]], overview["profiles"])
        self.assertEqual(profiles[0]["board_id"], "board_a")
        replace_assignments.assert_called_once_with(
            {"probe:probe-a": "board_a"}, "setup overview assignment replaced"
        )


if __name__ == "__main__":
    unittest.main()
