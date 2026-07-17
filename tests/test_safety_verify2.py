from __future__ import annotations

import copy

import pytest

from pyocd_debug_mcp.safety.regions import RegionKind
from pyocd_debug_mcp.safety.verify2 import (
    EvidenceError,
    HardwareEvidence,
    reconcile_hardware_evidence,
)


def region(
    *,
    fact_id: str = "uicr",
    name: str = "UICR",
    name_aliases: list[str] | None = None,
    kind: str = "prohibited",
    start: int | str = "0x10001000",
    end: int | str = "0x10001400",
    range_convention: str = "half_open",
    address_aliases: list[dict[str, object]] | None = None,
    bank: str = "bank0",
    block: str = "UICR",
) -> dict[str, object]:
    return {
        "fact_id": fact_id,
        "name": name,
        "name_aliases": name_aliases or [],
        "kind": kind,
        "start": start,
        "end": end,
        "range_convention": range_convention,
        "address_aliases": address_aliases or [],
        "bank": bank,
        "block": block,
    }


def document(
    role: str,
    *,
    part: str = "nRF52833-QIAA",
    target: str | None = None,
    regions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    support = role == "device_support"
    return {
        "schema_version": 1,
        "role": role,
        "device": {
            "mcu_part_number": part,
            "target": "nrf52833" if support and target is None else target,
        },
        "sources": [
            {
                "kind": "svd" if support else "reference_manual",
                "identifier": "Nordic.nRF52833.svd" if support else "nRF52833 PS",
                "version": "1.0.0" if support else "1.7",
                "revision": "pack-2026" if support else "2024-10",
            }
        ],
        "regions": regions or [region()],
    }


def reconcile(support_document: dict[str, object], official_document: dict[str, object]):
    return reconcile_hardware_evidence(
        expected_mcu_part_number="nRF52833-QIAA",
        expected_target="nrf52833",
        device_support=HardwareEvidence.from_document(support_document),
        official_document=HardwareEvidence.from_document(official_document),
    )


def test_exact_agreement_produces_only_reconciled_regions() -> None:
    result = reconcile(document("device_support"), document("official_document"))

    assert result.accepted
    assert result.status == "agreement"
    assert result.conflicts == ()
    assert len(result.regions) == 1
    safety_region = result.regions[0].to_safety_region()
    assert safety_region.kind is RegionKind.PROHIBITED
    assert safety_region.provenance[0].source_id.startswith("svd:")


def test_inclusive_end_and_hex_decimal_are_deterministically_reconciled() -> None:
    official = document(
        "official_document",
        regions=[region(start=0x10001000, end="0x100013ff", range_convention="inclusive_end")],
    )

    result = reconcile(document("device_support"), official)

    assert result.accepted
    assert result.regions[0].address_range.start == 0x10001000
    assert result.regions[0].address_range.end == 0x10001400


def test_explicit_name_and_address_aliases_reconcile_without_fuzzy_matching() -> None:
    support = document(
        "device_support",
        regions=[
            region(
                name="UICR",
                name_aliases=["User Information Configuration Registers"],
                address_aliases=[
                    {
                        "start": "0x20001000",
                        "end": "0x20001400",
                        "range_convention": "half_open",
                    }
                ],
            )
        ],
    )
    official = document(
        "official_document",
        regions=[
            region(
                fact_id="user_config",
                name="User Information Configuration Registers",
                name_aliases=["UICR"],
                start="0x20001000",
                end="0x20001400",
                address_aliases=[
                    {
                        "start": "0x10001000",
                        "end": "0x10001400",
                        "range_convention": "half_open",
                    }
                ],
            )
        ],
    )

    result = reconcile(support, official)

    assert result.accepted
    assert set(result.regions[0].reconciliations) == {
        "explicit_name_alias",
        "explicit_address_alias",
    }


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"kind": "peripheral"}, "verify/kind"),
        ({"end": "0x10001500"}, "verify/address"),
        ({"bank": "bank1"}, "verify/bank"),
        ({"block": "FICR"}, "verify/block"),
        ({"name": "Unrelated", "name_aliases": []}, "verify/name-alias"),
    ],
)
def test_region_disagreement_matrix_fails_closed(
    mutation: dict[str, object], expected_code: str
) -> None:
    official_region = region()
    official_region.update(mutation)

    result = reconcile(
        document("device_support"),
        document("official_document", regions=[official_region]),
    )

    assert not result.accepted
    assert result.status == "conflict"
    assert result.regions == ()
    assert expected_code in {conflict.code for conflict in result.conflicts}


@pytest.mark.parametrize(
    ("support_part", "support_target", "official_part", "expected_code"),
    [
        ("nRF52840-QIAA", "nrf52833", "nRF52833-QIAA", "verify/device-variant"),
        ("nRF52833-QIAA", "nrf52840", "nRF52833-QIAA", "verify/target"),
        ("nRF52833-QIAA", "nrf52833", "nRF52840-QIAA", "verify/device-variant"),
    ],
)
def test_device_and_target_conflict_matrix(
    support_part: str,
    support_target: str,
    official_part: str,
    expected_code: str,
) -> None:
    result = reconcile(
        document("device_support", part=support_part, target=support_target),
        document("official_document", part=official_part),
    )

    assert not result.accepted
    assert expected_code in {conflict.code for conflict in result.conflicts}


def test_missing_and_ambiguous_facts_fail_closed() -> None:
    missing = reconcile(
        document("device_support"),
        document(
            "official_document",
            regions=[region(fact_id="ficr", name="FICR", block="FICR")],
        ),
    )
    ambiguous = reconcile(
        document("device_support"),
        document(
            "official_document",
            regions=[
                region(fact_id="one"),
                region(fact_id="two", start="0x10002000", end="0x10002400"),
            ],
        ),
    )

    assert not missing.accepted
    assert not ambiguous.accepted
    assert "verify/missing-or-ambiguous-fact" in {conflict.code for conflict in ambiguous.conflicts}


def test_strict_schema_rejects_allowed_ranges_unknown_fields_and_wrong_authority() -> None:
    caller_ranges = document("official_document")
    caller_ranges["allowed_ranges"] = [{"start": 0, "end": 1}]
    with pytest.raises(EvidenceError) as unknown:
        HardwareEvidence.from_document(caller_ranges)
    assert unknown.value.code == "evidence/field-set"

    missing_revision = document("official_document")
    del missing_revision["sources"][0]["revision"]  # type: ignore[index]
    with pytest.raises(EvidenceError) as missing:
        HardwareEvidence.from_document(missing_revision)
    assert missing.value.code == "evidence/field-set"

    wrong_authority = document("official_document")
    wrong_authority["sources"][0]["kind"] = "svd"  # type: ignore[index]
    with pytest.raises(EvidenceError) as authority:
        HardwareEvidence.from_document(wrong_authority)
    assert authority.value.code == "evidence/source-authority"


def test_unknown_region_kind_and_duplicate_aliases_are_rejected() -> None:
    unknown = document("device_support", regions=[region(kind="unknown")])
    with pytest.raises(EvidenceError) as kind:
        HardwareEvidence.from_document(unknown)
    assert kind.value.code == "evidence/region-kind"

    duplicate = document(
        "device_support", regions=[region(name_aliases=["User Config", "user-config"])]
    )
    with pytest.raises(EvidenceError) as aliases:
        HardwareEvidence.from_document(duplicate)
    assert aliases.value.code == "evidence/name-aliases"


def test_input_documents_are_not_mutated() -> None:
    support = document("device_support")
    official = document("official_document")
    before = copy.deepcopy((support, official))

    reconcile(support, official)

    assert (support, official) == before


def test_reconciliation_and_provenance_are_deterministic_across_input_ordering() -> None:
    support_regions = [
        region(),
        region(
            fact_id="gpio",
            name="GPIO",
            kind="peripheral",
            start="0x50000000",
            end="0x50001000",
            bank="peripheral-bank-1",
            block="GPIO",
        ),
    ]
    official_regions = copy.deepcopy(support_regions)
    support = document("device_support", regions=support_regions)
    official = document("official_document", regions=official_regions)
    support["sources"].append(  # type: ignore[union-attr]
        {
            "kind": "pack",
            "identifier": "Nordic.nRF_DeviceFamilyPack",
            "version": "8.58.0",
            "revision": "pack-2026",
        }
    )
    official["sources"].append(  # type: ignore[union-attr]
        {
            "kind": "datasheet",
            "identifier": "nRF52833 Product Specification",
            "version": "1.7",
            "revision": "2024-10",
        }
    )
    shuffled_support = copy.deepcopy(support)
    shuffled_official = copy.deepcopy(official)
    shuffled_support["sources"].reverse()  # type: ignore[union-attr]
    shuffled_support["regions"].reverse()  # type: ignore[union-attr]
    shuffled_official["sources"].reverse()  # type: ignore[union-attr]
    shuffled_official["regions"].reverse()  # type: ignore[union-attr]

    forward = reconcile(support, official)
    reversed_inputs = reconcile(shuffled_support, shuffled_official)

    assert forward == reversed_inputs
    assert [item.fact_id for item in forward.regions] == ["gpio", "uicr"]
    for item in forward.regions:
        provenance = item.to_safety_region().to_document()["provenance"]
        assert isinstance(provenance, list)
        assert provenance[0]["authority"] == "reconciled"
        assert all(marker in provenance[0]["source_id"] for marker in ("pack:", "svd:"))


def test_adjacent_bank_facts_agree_but_boundary_drift_fails_closed() -> None:
    bank0 = region(
        fact_id="flash-bank-0",
        name="Flash bank 0",
        kind="physical_flash",
        start=0,
        end="0x8000",
        bank="bank0",
        block="NVMC",
    )
    bank1 = region(
        fact_id="flash-bank-1",
        name="Flash bank 1",
        kind="physical_flash",
        start="0x8000",
        end="0x10000",
        bank="bank1",
        block="NVMC",
    )
    agreement = reconcile(
        document("device_support", regions=[bank1, bank0]),
        document("official_document", regions=[copy.deepcopy(bank0), copy.deepcopy(bank1)]),
    )
    assert agreement.accepted

    drifted = copy.deepcopy(bank1)
    drifted["start"] = "0x7fff"
    conflict = reconcile(
        document("device_support", regions=[bank0, bank1]),
        document("official_document", regions=[copy.deepcopy(bank0), drifted]),
    )
    assert not conflict.accepted
    assert conflict.regions == ()
    assert "verify/address" in {item.code for item in conflict.conflicts}
