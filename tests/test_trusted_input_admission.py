from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from firmware_mcp.firmstore.store import FirmStore
from firmware_mcp.pack_provision import (
    DeviceBinding,
    PackProvisionError,
    PackSpec,
    pack_spec_document,
    read_pack_bytes,
    sha256_file,
)
from firmware_mcp.setup_flow.device_support import (
    _select_pdsc_member,
    derive_candidate_binding,
    resolve_device_support_geometry,
    resolve_project_pack_support,
)


U5_FILENAME = "Keil.STM32U5xx_DFP.3.2.0.pack"
U5_SHA256 = "e320687fe534f2fe6902e9bcdee981315abea26c4ca547142af9b9439e958be6"
U5_URL = "https://keilpack.azureedge.net/pack/Keil.STM32U5xx_DFP.3.2.0.pack"
U5_PART = "STM32U575ZIT6Q"


class TrustedPackAdmissionTests(unittest.TestCase):
    def test_pack_reader_accepts_bytes_beyond_the_former_archive_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.pack"
            with path.open("wb") as stream:
                stream.seek(128 * 1024 * 1024)
                stream.write(b"x")

            payload = read_pack_bytes(path)

            self.assertEqual(len(payload), 128 * 1024 * 1024 + 1)
            self.assertEqual(payload[-1:], b"x")

    def test_archive_inventory_has_no_member_size_ratio_or_path_policy(self) -> None:
        pdsc = zipfile.ZipInfo("../trusted/vendor/device.pdsc")
        pdsc.file_size = 174_140_971
        pdsc.compress_size = 1
        members = [zipfile.ZipInfo(f"resources/{index}.txt") for index in range(4_096)]
        members.append(pdsc)

        selected = _select_pdsc_member(members)

        self.assertIs(selected, pdsc)

    def test_archive_inventory_still_requires_exactly_one_pdsc(self) -> None:
        with self.assertRaisesRegex(PackProvisionError, "exactly one PDSC"):
            _select_pdsc_member([zipfile.ZipInfo("readme.txt")])
        with self.assertRaisesRegex(PackProvisionError, "exactly one PDSC"):
            _select_pdsc_member([zipfile.ZipInfo("one.pdsc"), zipfile.ZipInfo("nested/two.PDSC")])

    def test_pack_reader_still_rejects_empty_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.pack"
            path.touch()
            with self.assertRaisesRegex(PackProvisionError, "non-empty regular file"):
                read_pack_bytes(path)


class OfficialSTM32U5PackIntegrationTests(unittest.TestCase):
    def test_official_u5_3_2_0_reaches_exact_flash_authority(self) -> None:
        configured = os.environ.get("BYO_MCP_OFFICIAL_U5_PACK", "").strip()
        if not configured:
            self.skipTest("set BYO_MCP_OFFICIAL_U5_PACK to the pinned official pack")
        source = Path(configured).expanduser().resolve(strict=True)
        self.assertEqual(source.name, U5_FILENAME)
        self.assertEqual(source.stat().st_size, 8_821_594)
        self.assertEqual(sha256_file(source), U5_SHA256)

        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            self.assertEqual(len(members), 401)
            self.assertEqual(sum(member.file_size for member in members), 174_140_971)

        binding = derive_candidate_binding(source, U5_PART)
        self.assertEqual(binding.pdsc_device, "STM32U575ZITxQ")
        self.assertEqual(binding.pyocd_target, "stm32u575zitxq")

        with tempfile.TemporaryDirectory() as directory:
            store = FirmStore(Path(directory))
            store.ensure_layout()
            store.layout.pack_files.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, store.layout.pack_files / U5_FILENAME)
            spec = PackSpec(
                id="Keil.STM32U5xx_DFP",
                version="3.2.0",
                filename=U5_FILENAME,
                url=U5_URL,
                sha256=U5_SHA256,
                provides_targets=(binding.pyocd_target,),
                device_bindings=(
                    DeviceBinding(
                        U5_PART,
                        binding.pdsc_device,
                        binding.pyocd_target,
                        binding.identity_proof,
                    ),
                ),
            )
            store.atomic_write_pack_manifest({"packs": [pack_spec_document(spec)]})

            candidate = resolve_project_pack_support(store, U5_PART)
            geometry = resolve_device_support_geometry(candidate, store)

        self.assertEqual(candidate.pack_sha256, U5_SHA256)
        self.assertEqual(candidate.pdsc_device, "STM32U575ZITxQ")
        self.assertEqual(candidate.pyocd_target, "stm32u575zitxq")
        self.assertEqual(geometry.flash_start, 0x08000000)
        self.assertEqual(geometry.flash_end, 0x08200000)
        self.assertEqual(geometry.ram_start, 0x20000000)
        self.assertEqual(geometry.ram_end, 0x20030000)
        self.assertEqual(len(geometry.erase_sectors), 256)
        self.assertEqual(geometry.erase_sectors[0], (0x08000000, 0x08002000))
        self.assertEqual(geometry.erase_sectors[-1], (0x081FE000, 0x08200000))
        self.assertTrue(all(end - start == 0x2000 for start, end in geometry.erase_sectors))
        self.assertEqual(
            geometry.driver_proof_digest,
            "e267001658e070bbd45ee3cc69b00d9b2ef7fbc3fc5e47f13744cebcd19035e3",
        )
        self.assertEqual(geometry.erased_byte_value, 0xFF)


if __name__ == "__main__":
    unittest.main()
