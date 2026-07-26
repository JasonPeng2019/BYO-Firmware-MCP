from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

from pyocd_debug_mcp.pack_provision import PackSpec, VerifiedPack
from pyocd_debug_mcp.setup_flow.datasheet_evidence import (
    DatasheetApplicabilityError,
    DatasheetIdentityTerm,
    _contains_identity_term,
    prove_datasheet_applicability,
)
from pyocd_debug_mcp.setup_flow.device_support import (
    DeviceSupportCandidate,
    _derive_verified_binding,
    _pdsc_ancestry_terms,
)


REPO = Path(__file__).resolve().parents[4]
TRIAL = REPO.parent
PACK = REPO / "testing_folder" / "Keil.STM32L4xx_DFP.3.1.0.pack"
STM32_PDF = TRIAL / "stm32L476rgt.pdf"
NRF_PDF = TRIAL / "Nano_BLE_MCU-nRF52840_PS_v1.1.pdf"

PACK_SHA256 = "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"
STM32_SHA256 = "a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65"
NRF_SHA256 = "c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422"


def verified_pdsc(xml: str) -> VerifiedPack:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("Synthetic.DFP.pdsc", xml)
    return VerifiedPack(
        Path("synthetic.pack"),
        PackSpec("synthetic", "", "synthetic.pack", "", "0" * 64),
        payload.getvalue(),
    )


def main() -> None:
    pack_bytes = PACK.read_bytes()
    stm32_bytes = STM32_PDF.read_bytes()
    nrf_bytes = NRF_PDF.read_bytes()
    assert hashlib.sha256(pack_bytes).hexdigest() == PACK_SHA256
    assert hashlib.sha256(stm32_bytes).hexdigest() == STM32_SHA256
    assert hashlib.sha256(nrf_bytes).hexdigest() == NRF_SHA256

    selected = VerifiedPack(
        PACK,
        PackSpec("Keil.STM32L4xx_DFP", "3.1.0", PACK.name, "", PACK_SHA256),
        pack_bytes,
    )
    binding = _derive_verified_binding(selected, "STM32L476RGT6")
    candidate = DeviceSupportCandidate.from_verified_pack(selected, binding)
    proof = prove_datasheet_applicability(
        stm32_bytes,
        requested_part="STM32L476RGT6",
        authority_terms=candidate.datasheet_identity_terms(),
    )
    assert proof.matched_term == "stm32l476"
    assert proof.pdf_sha256 == STM32_SHA256
    try:
        prove_datasheet_applicability(
            nrf_bytes,
            requested_part="STM32L476RGT6",
            authority_terms=candidate.datasheet_identity_terms(),
        )
    except DatasheetApplicabilityError as exc:
        assert "not established" in str(exc)
    else:
        raise AssertionError("wrong-family nRF PDF was accepted")

    direct = verified_pdsc(
        '<package><family Dfamily="ACME Q Series">'
        '<device Dname="ACMEQ123x"/></family></package>'
    )
    variant = verified_pdsc(
        '<package><family Dfamily="ACME Q Series">'
        '<subFamily DsubFamily="ACMEQ">'
        '<device Dname="ACMEQ123"><variant Dvariant="ACMEQ124x"/></device>'
        "</subFamily></family></package>"
    )
    assert _pdsc_ancestry_terms(direct, "ACMEQ123x") == ("ACME Q Series",)
    assert _pdsc_ancestry_terms(variant, "ACMEQ124x") == ("ACME Q Series", "ACMEQ")

    assert not _contains_identity_term("LPC55Sxx", "LPC55S")
    assert _contains_identity_term(
        "LPC55Sxx", DatasheetIdentityTerm("LPC55S", True),
        permits_family_placeholder=True,
    )
    assert not _contains_identity_term(
        "LPC55S0", DatasheetIdentityTerm("LPC55S", True),
        permits_family_placeholder=True,
    )
    assert not _contains_identity_term(
        "ACME-Z9-1440", DatasheetIdentityTerm("ACME-Z9-144", True),
        permits_family_placeholder=True,
    )

    authority = candidate.to_authority_document()
    legacy = dict(authority, support_id=candidate.legacy_support_id)
    assert candidate.matches_authority_document(authority)
    assert candidate.matches_authority_document(legacy)
    assert not candidate.matches_authority_document(dict(authority, pack_sha256="0" * 64))

    print("MAIN_ORACLE_PASS")
    print(f"pack_sha256={PACK_SHA256}")
    print(f"stm32_pdf_sha256={STM32_SHA256}")
    print(f"nrf_pdf_sha256={NRF_SHA256}")
    print(f"pdsc_device={binding.pdsc_device}")
    print(f"pdsc_ancestry_terms={candidate.pdsc_ancestry_terms!r}")
    print(f"matched_term={proof.matched_term}")
    print(f"evidence_locus={proof.evidence_locus}")
    print("wrong_family=refused")
    print("direct_family=accepted")
    print("nested_variant=accepted")
    print("placeholder_provenance=verified")
    print("legacy_authority_replay=verified")


if __name__ == "__main__":
    main()
