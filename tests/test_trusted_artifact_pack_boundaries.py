"""Trusted artifact pack boundaries contracts."""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pyocd_debug_mcp.pack_provision import (
    DeviceBinding,
    PackProvisionError,
    PackSpec,
    VerifiedPack,
    sha256_bytes,
)
from pyocd_debug_mcp.setup_flow.device_support import DeviceSupportResolver
from pyocd_debug_mcp.setup_flow.packs import (
    PackCandidate,
    PackCandidateError,
    PackCandidatePipeline,
)
from pyocd_debug_mcp.safety.enforce import LoadedSafetyMap, SafetyPolicy, SafetyPolicyError
from pyocd_debug_mcp.safety.map_build import GenericMapGeometry, GenericSafetyMapDocument
from pyocd_debug_mcp.safety.regions import AddressRange


def _pack_bytes(pdsc: bytes) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("Vendor.Device.pdsc", pdsc)
    return stream.getvalue()


class RoundFourRegressionTests(unittest.TestCase):
    def test_pack_stability_parser_and_exact_binding_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = _pack_bytes(b"<package/>")
            changed = root / "changed.pack"
            changed.write_bytes(original)
            spec = PackSpec("id", "", changed.name, "url", sha256_bytes(original))
            selected = VerifiedPack(changed, spec, original)
            changed.write_bytes(_pack_bytes(b"<package><name>changed</name></package>"))
            with self.assertRaisesRegex(PackProvisionError, "changed or disappeared"):
                selected.verify_unchanged()

            malformed = VerifiedPack(Path("bad.pack"), spec, b"not-a-zip")
            with self.assertRaises(PackProvisionError):
                DeviceSupportResolver._validate_archive(malformed)
            unparseable = _pack_bytes(b"<package>")
            bad_pdsc = VerifiedPack(
                Path("bad-pdsc.pack"),
                PackSpec("id", "", "bad-pdsc.pack", "", sha256_bytes(unparseable)),
                unparseable,
            )
            with self.assertRaises(PackProvisionError):
                DeviceSupportResolver._cmsis_device_names(bad_pdsc)

            candidate = PackCandidate(
                "id",
                "1",
                "candidate.pack",
                "https://vendor.invalid",
                changed,
                sha256_bytes(changed.read_bytes()),
            )
            from pyocd_debug_mcp.firmstore.store import FirmStore

            pipeline = PackCandidatePipeline(
                FirmStore(root), enumerate_targets=lambda *_: (), live_connect=lambda *_: None
            )
            with self.assertRaises(PackCandidateError) as mismatch:
                pipeline.validate_device(
                    candidate,
                    required_target="target-b",
                    device_binding=DeviceBinding("PART", "PART", "target-a"),
                )
            self.assertEqual(mismatch.exception.code, "package/device-binding-target-mismatch")

            changed_before_promotion = PackCandidate(
                "changed",
                "1",
                "changed-before-promotion.pack",
                "https://vendor.invalid/changed",
                changed,
                sha256_bytes(original),
            )
            with self.assertRaises(PackCandidateError) as unstable:
                PackCandidatePipeline(
                    FirmStore(root), enumerate_targets=lambda *_: (), live_connect=lambda *_: None
                ).validate(changed_before_promotion, required_target="target")
            self.assertEqual(unstable.exception.code, "package/checksum-mismatch")

        resolver = DeviceSupportResolver(pack_loader=lambda _target: None)
        with patch.object(resolver, "candidates", return_value=(Mock(), Mock())):
            with self.assertRaisesRegex(PackProvisionError, "More than one"):
                resolver.resolve("PART", ("target",))

        incomplete = object.__new__(GenericSafetyMapDocument)
        object.__setattr__(
            incomplete,
            "geometry",
            GenericMapGeometry(
                (AddressRange(0x08000000, 0x08001000),),
                (AddressRange(0x20000000, 0x20001000),),
                erase_available=False,
            ),
        )
        object.__setattr__(incomplete, "identity", SimpleNamespace(pyocd_target="target"))
        policy = SafetyPolicy(Mock())
        with patch.object(policy, "load", return_value=LoadedSafetyMap(incomplete, Mock())):
            with self.assertRaises(SafetyPolicyError) as missing_flash_authority:
                policy.check_generic_application_candidate(
                    "board", Path("firmware.elf"), current_target="target"
                )
        self.assertEqual(missing_flash_authority.exception.code, "safety/geometry-incomplete")


if __name__ == "__main__":
    unittest.main()
