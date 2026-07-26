"""Adversarial specifications for H04 generic datasheet applicability."""

from __future__ import annotations

import hashlib
import io
import sys
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyocd_debug_mcp.pack_provision import PackSpec, VerifiedPack
from pyocd_debug_mcp.setup_flow.datasheet_evidence import (
    DatasheetApplicabilityError,
    DatasheetIdentityTerm,
    normalize_identity_text,
    prove_datasheet_applicability,
)
from pyocd_debug_mcp.setup_flow.device_support import (
    BuiltInTargetSupportCandidate,
    DeviceSupportCandidate,
    _pdsc_ancestry_terms,
    derive_candidate_binding,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ARTIFACT_ROOT = _REPO_ROOT.parent
_STM32_PDF = _ARTIFACT_ROOT / "stm32L476rgt.pdf"
_NRF_PDF = _ARTIFACT_ROOT / "Nano_BLE_MCU-nRF52840_PS_v1.1.pdf"
_STM32_PACK = _REPO_ROOT / "testing_folder" / "Keil.STM32L4xx_DFP.3.1.0.pack"
_STM32_PDF_SHA256 = "a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65"
_STM32_PACK_SHA256 = "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"


def _pdf_with_text(text: str) -> bytes:
    """Make a tiny valid text PDF without making the test depend on a host tool."""

    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode())
        result.extend(body)
        result.extend(b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    result.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)


def _synthetic_pdsc_pack(xml: str) -> VerifiedPack:
    """Return a verified in-memory pack for generic PDSC ancestry tests."""

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("ACME.pdsc", xml)
    payload = stream.getvalue()
    return VerifiedPack(
        Path("synthetic-acme.pack"),
        PackSpec("ACME.DFP", "1.0", "synthetic-acme.pack", "", hashlib.sha256(payload).hexdigest()),
        payload,
    )


class DatasheetApplicabilitySpecTests(unittest.TestCase):
    """CL-001: parser evidence must bind actual bytes to server-issued identity terms."""

    def test_exact_part_match_is_normalized_and_bound_to_the_parsed_bytes(self) -> None:
        payload = _pdf_with_text("Official device reference: Acme-X 42")

        proof = prove_datasheet_applicability(
            payload, requested_part="ACME X-42", authority_terms=("ACME X-42",)
        )

        self.assertEqual(proof.requested_identity, "acmex42")
        self.assertEqual(proof.matched_term, "acmex42")
        self.assertEqual(proof.evidence_locus, "page:1")
        self.assertEqual(proof.pdf_sha256, hashlib.sha256(payload).hexdigest())
        self.assertTrue(proof.parser_version.startswith("pypdf-"))

    def test_verified_family_term_can_cover_a_package_variant_but_unissued_terms_cannot(self) -> None:
        family_pdf = _pdf_with_text("ACME Z9 family reference manual")
        proof = prove_datasheet_applicability(
            family_pdf,
            requested_part="ACME-Z9-144",
            authority_terms=("ACME-Z9-144", "ACME Z9"),
        )
        self.assertEqual(proof.matched_term, "acmez9")

        with self.assertRaisesRegex(DatasheetApplicabilityError, "not established"):
            prove_datasheet_applicability(
                family_pdf,
                requested_part="ACME-Z9-144",
                authority_terms=("ACME-Z9-144",),
            )

    def test_wrong_or_textless_pdf_is_an_actionable_typed_refusal_not_a_filename_guess(self) -> None:
        for payload in (_pdf_with_text("ACME Q7 family reference"), _pdf_with_text("")):
            with self.subTest(payload=payload), self.assertRaises(DatasheetApplicabilityError) as raised:
                prove_datasheet_applicability(
                    payload,
                    requested_part="ACME-Z9-144",
                    authority_terms=("ACME-Z9-144", "ACME Z9"),
                )
            self.assertIn("ACME-Z9-144", str(raised.exception))
            self.assertIn("verifiable official datasheet evidence", str(raised.exception))

    def test_caller_part_must_be_present_in_server_derived_authority_terms(self) -> None:
        with self.assertRaisesRegex(DatasheetApplicabilityError, "not server-derived"):
            prove_datasheet_applicability(
                _pdf_with_text("ACME-Z9-144"),
                requested_part="ACME-Z9-144",
                authority_terms=("ACME Z9",),
            )

    def test_longer_unrelated_sku_cannot_satisfy_an_exact_part_by_prefix(self) -> None:
        """An exact server-derived part term is not a prefix match for another SKU."""

        with self.assertRaisesRegex(DatasheetApplicabilityError, "not established"):
            prove_datasheet_applicability(
                _pdf_with_text("Official ACME-Z9-1440 datasheet"),
                requested_part="ACME-Z9-144",
                authority_terms=("ACME-Z9-144",),
            )

    def test_normalization_does_not_turn_non_ascii_lookalikes_into_an_identity_match(self) -> None:
        self.assertEqual(normalize_identity_text("ACME-Z9-144"), "acmez9144")
        self.assertNotEqual(normalize_identity_text("АCME-Z9-144"), "acmez9144")

    def test_exact_only_identity_cannot_use_the_family_placeholder_convention(self) -> None:
        with self.assertRaisesRegex(DatasheetApplicabilityError, "not established"):
            prove_datasheet_applicability(
                _pdf_with_text("ACME9xx reference"),
                requested_part="ACME9",
                authority_terms=(DatasheetIdentityTerm("ACME9"),),
            )

    def test_only_verified_family_identity_can_use_the_placeholder_convention(self) -> None:
        proof = prove_datasheet_applicability(
            _pdf_with_text("ACME9xx reference"),
            requested_part="ACME9-1",
            authority_terms=(
                DatasheetIdentityTerm("ACME9-1"),
                DatasheetIdentityTerm("ACME9", permits_family_placeholder=True),
            ),
        )
        self.assertEqual(proof.matched_term, "acme9")
        with self.assertRaisesRegex(DatasheetApplicabilityError, "not established"):
            prove_datasheet_applicability(
                _pdf_with_text("ACME90 reference"),
                requested_part="ACME9-1",
                authority_terms=(
                    DatasheetIdentityTerm("ACME9-1"),
                    DatasheetIdentityTerm("ACME9", permits_family_placeholder=True),
                ),
            )

    def test_letter_ending_verified_family_can_use_placeholder_but_exact_only_cannot(self) -> None:
        family_terms = (
            DatasheetIdentityTerm("LPC55S-1"),
            DatasheetIdentityTerm("LPC55S", permits_family_placeholder=True),
        )
        proof = prove_datasheet_applicability(
            _pdf_with_text("LPC55Sxx family reference"),
            requested_part="LPC55S-1",
            authority_terms=family_terms,
        )
        self.assertEqual(proof.matched_term, "lpc55s")

        for terms, document in (
            (
                (DatasheetIdentityTerm("LPC55S-1"), DatasheetIdentityTerm("LPC55S")),
                "LPC55Sxx reference",
            ),
            (family_terms, "LPC55S0 concrete reference"),
        ):
            with self.subTest(terms=terms, document=document), self.assertRaisesRegex(
                DatasheetApplicabilityError, "not established"
            ):
                prove_datasheet_applicability(
                    _pdf_with_text(document),
                    requested_part="LPC55S-1",
                    authority_terms=terms,
                )


class PdscAncestrySpecTests(unittest.TestCase):
    """CL-001/CL-002: ancestry follows the actual verified PDSC XML tree."""

    def test_direct_family_device_derives_family_without_requiring_a_subfamily(self) -> None:
        pack = _synthetic_pdsc_pack(
            '<package><family Dfamily="ACME Q Series"><device Dname="ACMEQ123x" />'
            "</family></package>"
        )

        self.assertEqual(_pdsc_ancestry_terms(pack, "ACMEQ123x"), ("ACME Q Series",))

    def test_variant_leaf_in_nested_family_and_subfamily_derives_both_ancestors(self) -> None:
        pack = _synthetic_pdsc_pack(
            '<package><family Dfamily="ACME Q Series"><subFamily DsubFamily="ACMEQ">'
            '<device Dname="ACMEQ123x"><variant Dvariant="ACMEQ123A" /></device>'
            "</subFamily></family></package>"
        )

        self.assertEqual(
            _pdsc_ancestry_terms(pack, "ACMEQ123A"), ("ACME Q Series", "ACMEQ")
        )


class OfficialH04ArtifactSpecTests(unittest.TestCase):
    """CL-001/CL-002: real H04 documents must use ancestry from verified pack bytes."""

    @staticmethod
    def _stm32_candidate() -> DeviceSupportCandidate:
        for artifact in (_STM32_PDF, _NRF_PDF, _STM32_PACK):
            if not artifact.is_file():
                raise AssertionError(f"required H04 test artifact is missing: {artifact}")
        payload = _STM32_PACK.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == _STM32_PACK_SHA256
        selected = VerifiedPack(
            _STM32_PACK,
            PackSpec("Keil.STM32L4xx_DFP", "3.1.0", _STM32_PACK.name, "", _STM32_PACK_SHA256),
            payload,
        )
        binding = derive_candidate_binding(_STM32_PACK, "STM32L476RGT6")
        assert binding.pdsc_device == "STM32L476RGTx"
        return DeviceSupportCandidate.from_verified_pack(selected, binding)

    def test_official_stm32_family_pdf_is_accepted_from_exact_pack_ancestry(self) -> None:
        candidate = self._stm32_candidate()
        pdf = _STM32_PDF.read_bytes()
        self.assertEqual(hashlib.sha256(pdf).hexdigest(), _STM32_PDF_SHA256)
        self.assertIn("STM32L476", candidate.pdsc_ancestry_terms)

        proof = prove_datasheet_applicability(
            pdf,
            requested_part="STM32L476RGT6",
            authority_terms=candidate.datasheet_identity_terms(),
        )

        self.assertEqual(proof.requested_identity, "stm32l476rgt6")
        self.assertEqual(proof.matched_term, "stm32l476")
        self.assertEqual(proof.pdf_sha256, _STM32_PDF_SHA256)

    def test_official_nrf_pdf_is_refused_for_the_same_verified_stm32_authority(self) -> None:
        candidate = self._stm32_candidate()

        with self.assertRaisesRegex(DatasheetApplicabilityError, "STM32L476RGT6"):
            prove_datasheet_applicability(
                _NRF_PDF.read_bytes(),
                requested_part="STM32L476RGT6",
                authority_terms=candidate.datasheet_identity_terms(),
            )


class GenericSetupByteBindingSpecTests(unittest.TestCase):
    """CL-002: promotion must reject a changed byte snapshot after the digest was obtained."""

    def test_generic_proof_refuses_when_current_bytes_do_not_match_the_resolved_digest(self) -> None:
        from pyocd_debug_mcp import server

        reviewed = _pdf_with_text("ACME-Z9-144")
        changed = _pdf_with_text("ACME-Z9-144 revision two")

        candidate = BuiltInTargetSupportCandidate("ACME-Z9-144", "generic-target", "0" * 64)
        with (
            patch.object(server, "read_datasheet_pdf", return_value=changed),
            self.assertRaisesRegex(DatasheetApplicabilityError, "bytes changed"),
        ):
            server._prove_generic_datasheet(
                Path("does-not-matter.pdf"),
                hashlib.sha256(reviewed).hexdigest(),
                "ACME-Z9-144",
                candidate,
            )


class GenericProfileReplaySpecTests(unittest.TestCase):
    """CL-003: replay is offline, authority-bound, and legacy-compatible only when provable."""

    def _profile(self, proof: object) -> object:
        digest = hashlib.sha256(_pdf_with_text("ACME-Z9-144")).hexdigest()
        return SimpleNamespace(
            device_support={"kind": "resolved_pack"},
            mcu_part_number="ACME-Z9-144",
            to_document=lambda: {
                "datasheet_sha256": digest,
                "datasheet_ref": f".firm/evidence/datasheets/{digest}.pdf",
                "datasheet_applicability": proof,
            },
        )

    def test_replay_rejects_tampered_durable_proof_without_any_network_resolution(self) -> None:
        from pyocd_debug_mcp import server

        payload = _pdf_with_text("ACME-Z9-144")
        expected = prove_datasheet_applicability(
            payload, requested_part="ACME-Z9-144", authority_terms=("ACME-Z9-144",)
        ).to_document()
        tampered = {**expected, "matched_term": "acmeq7"}
        candidate = SimpleNamespace(datasheet_identity_terms=lambda: ("ACME-Z9-144",))
        repository = SimpleNamespace(
            store=SimpleNamespace(
                layout=SimpleNamespace(datasheet_evidence=lambda _digest: Path("captured.pdf"))
            )
        )
        with (
            patch.object(server, "_profile_repository", repository),
            patch.object(server, "resolve_persisted_pack_support", return_value=candidate) as resolve,
            patch.object(server, "replay_datasheet_evidence"),
            patch.object(server, "read_datasheet_pdf", return_value=payload),
            self.assertRaisesRegex(server.PackProvisionError, "no longer replays"),
        ):
            server._replay_profile_device_support(self._profile(tampered))
        resolve.assert_called_once_with(repository.store, "ACME-Z9-144", {"kind": "resolved_pack"})

    def test_legacy_profile_without_saved_proof_is_accepted_only_after_current_offline_reproof(self) -> None:
        from pyocd_debug_mcp import server

        payload = _pdf_with_text("ACME-Z9-144")
        candidate = SimpleNamespace(datasheet_identity_terms=lambda: ("ACME-Z9-144",))
        repository = SimpleNamespace(
            store=SimpleNamespace(
                layout=SimpleNamespace(datasheet_evidence=lambda _digest: Path("captured.pdf"))
            )
        )
        with (
            patch.object(server, "_profile_repository", repository),
            patch.object(server, "resolve_persisted_pack_support", return_value=candidate),
            patch.object(server, "replay_datasheet_evidence"),
            patch.object(server, "read_datasheet_pdf", return_value=payload),
        ):
            self.assertIs(server._replay_profile_device_support(self._profile(None)), candidate)
