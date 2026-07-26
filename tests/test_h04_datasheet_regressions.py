"""Regression checks for exact generic datasheet identity evidence."""

from __future__ import annotations

import hashlib
import io
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyocd_debug_mcp.pack_provision import PackSpec, VerifiedPack
from pyocd_debug_mcp.setup_flow.datasheet_evidence import (
    DatasheetApplicabilityError,
    DatasheetIdentityTerm,
    prove_datasheet_applicability,
)
from pyocd_debug_mcp.setup_flow.device_support import (
    DeviceSupportCandidate,
    _derive_verified_binding,
    _pdsc_ancestry_terms,
)

_STM32_PART = "STM32L476RGT6"
_STM32_PACK_SHA256 = "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"
_STM32_PDF_SHA256 = "a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65"
_NRF_PDF_SHA256 = "c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422"


def _pdf_with_text(text: str) -> bytes:
    """Make a small valid PDF whose text pypdf can extract."""

    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    result = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode())
        result.extend(body)
        result.extend(b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    result.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets))
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)


def _verified_pdsc(xml: str) -> VerifiedPack:
    """Build local verified-pack bytes for PDSC ancestry-shape regression cases."""

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as pack:
        pack.writestr("ACME.pdsc", xml)
    payload = archive.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    return VerifiedPack(Path("ACME.pack"), PackSpec("acme", "1", "ACME.pack", "", digest), payload)


class DatasheetIdentityBoundaryRegressionTests(unittest.TestCase):
    def test_longer_unrelated_part_number_cannot_satisfy_exact_part_identity(self) -> None:
        """Avoid promoting a different SKU merely because its normalized text has a prefix."""

        with self.assertRaisesRegex(DatasheetApplicabilityError, "not established"):
            prove_datasheet_applicability(
                _pdf_with_text("Official ACME-Z9-1440 datasheet"),
                requested_part="ACME-Z9-144",
                authority_terms=("ACME-Z9-144",),
            )

    def test_longer_unrelated_family_token_cannot_satisfy_pdsc_family_authority(self) -> None:
        """PDSC family authority must retain its token boundary in PDF text."""

        with self.assertRaisesRegex(DatasheetApplicabilityError, "not established"):
            prove_datasheet_applicability(
                _pdf_with_text("Official ACME-Z90 family reference"),
                requested_part="ACME-Z9-144",
                authority_terms=("ACME-Z9-144", "ACME-Z9"),
            )


class PdscAncestryShapeRegressionTests(unittest.TestCase):
    def test_direct_family_device_and_nested_variant_preserve_actual_ancestry(self) -> None:
        direct = _verified_pdsc(
            '<package><family Dfamily="ACME Q Series"><device Dname="ACMEQ123x" />'
            "</family></package>"
        )
        variant = _verified_pdsc(
            '<package><family Dfamily="ACME Q Series"><subFamily DsubFamily="ACME Q">'
            '<device Dname="ACMEQ123"><variant Dvariant="ACMEQ124x" /></device>'
            "</subFamily></family></package>"
        )

        self.assertEqual(_pdsc_ancestry_terms(direct, "ACMEQ123x"), ("ACME Q Series",))
        self.assertEqual(
            _pdsc_ancestry_terms(variant, "ACMEQ124x"), ("ACME Q Series", "ACME Q")
        )


class FamilyPlaceholderProvenanceRegressionTests(unittest.TestCase):
    def test_only_verified_family_authority_can_match_the_pdf_xx_convention(self) -> None:
        payload = _pdf_with_text("ACME9xx family datasheet")
        exact_only = (DatasheetIdentityTerm("ACME9"),)
        verified_family = (
            DatasheetIdentityTerm("ACME9"),
            DatasheetIdentityTerm("ACME9", permits_family_placeholder=True),
        )

        with self.assertRaisesRegex(DatasheetApplicabilityError, "not established"):
            prove_datasheet_applicability(
                payload, requested_part="ACME9", authority_terms=exact_only
            )
        proof = prove_datasheet_applicability(
            payload, requested_part="ACME9", authority_terms=verified_family
        )
        self.assertEqual(proof.matched_term, "acme9")
        with self.assertRaisesRegex(DatasheetApplicabilityError, "not established"):
            prove_datasheet_applicability(
                _pdf_with_text("ACME90 family datasheet"),
                requested_part="ACME9",
                authority_terms=verified_family,
            )

    def test_verified_letter_ending_family_can_match_xx_without_matching_concrete_skus(self) -> None:
        exact_only = (DatasheetIdentityTerm("LPC55S"),)
        verified_family = (
            DatasheetIdentityTerm("LPC55S"),
            DatasheetIdentityTerm("LPC55S", permits_family_placeholder=True),
        )

        with self.assertRaisesRegex(DatasheetApplicabilityError, "not established"):
            prove_datasheet_applicability(
                _pdf_with_text("LPC55Sxx family datasheet"),
                requested_part="LPC55S",
                authority_terms=exact_only,
            )
        proof = prove_datasheet_applicability(
            _pdf_with_text("LPC55Sxx family datasheet"),
            requested_part="LPC55S",
            authority_terms=verified_family,
        )
        self.assertEqual(proof.matched_term, "lpc55s")
        with self.assertRaisesRegex(DatasheetApplicabilityError, "not established"):
            prove_datasheet_applicability(
                _pdf_with_text("LPC55S0 datasheet"),
                requested_part="LPC55S",
                authority_terms=verified_family,
            )


class OfficialDatasheetAuthorityRegressionTests(unittest.TestCase):
    """Exercise PDSC-derived authority against the supplied official controls."""

    @staticmethod
    def _stm32_candidate() -> DeviceSupportCandidate:
        repository = Path(__file__).resolve().parents[1]
        pack_path = repository / "testing_folder" / "Keil.STM32L4xx_DFP.3.1.0.pack"
        payload = pack_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != _STM32_PACK_SHA256:
            raise AssertionError("STM32 control pack is not the approved exact bytes")
        selected = VerifiedPack(
            pack_path,
            PackSpec("stm32l4-control", "3.1.0", pack_path.name, "", _STM32_PACK_SHA256),
            payload,
        )
        return DeviceSupportCandidate.from_verified_pack(
            selected, _derive_verified_binding(selected, _STM32_PART)
        )

    def test_verified_pdsc_ancestry_accepts_official_family_pdf_and_refuses_nrf_control(self) -> None:
        artifacts = Path(__file__).resolve().parents[2]
        stm32_pdf = (artifacts / "stm32L476rgt.pdf").read_bytes()
        nrf_pdf = (artifacts / "Nano_BLE_MCU-nRF52840_PS_v1.1.pdf").read_bytes()
        self.assertEqual(hashlib.sha256(stm32_pdf).hexdigest(), _STM32_PDF_SHA256)
        self.assertEqual(hashlib.sha256(nrf_pdf).hexdigest(), _NRF_PDF_SHA256)
        candidate = self._stm32_candidate()
        self.assertIn("STM32L476", candidate.datasheet_identity_terms())

        proof = prove_datasheet_applicability(
            stm32_pdf,
            requested_part=_STM32_PART,
            authority_terms=candidate.datasheet_identity_terms(),
        )

        self.assertEqual(proof.matched_term, "stm32l476")
        with self.assertRaisesRegex(DatasheetApplicabilityError, "not established"):
            prove_datasheet_applicability(
                nrf_pdf,
                requested_part=_STM32_PART,
                authority_terms=candidate.datasheet_identity_terms(),
            )
