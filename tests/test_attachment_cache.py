from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyocd_debug_mcp.firmstore.cache import (
    AttachmentCache,
    AttachmentCacheError,
    ProbeIdentity,
    SerialEndpoint,
)
from pyocd_debug_mcp.firmstore.store import FirmStore, PERSISTED_AUTHORITY_KEYS


PROBE = ProbeIdentity("stlink", "PROBE-001")
UART = SerialEndpoint("COM3", "UART-001", 0x0483, 0x5740)


def cache(tmp_path: Path) -> AttachmentCache:
    return AttachmentCache(FirmStore(tmp_path))


def test_exact_match_reuses_current_port_path_in_a_later_run(tmp_path: Path) -> None:
    first_run = cache(tmp_path)
    first_run.confirm(
        "bench_board",
        PROBE,
        UART,
        confirmed_at="2026-07-17T08:00:00Z",
    )

    persisted = json.loads(first_run.path.read_text(encoding="utf-8"))
    assert "port_path" not in json.dumps(persisted)
    second_run = cache(tmp_path)
    resolution = second_run.resolve(
        "bench_board",
        PROBE,
        [SerialEndpoint("COM19", "UART-001", 0x0483, 0x5740)],
    )

    assert resolution.reused is True
    assert resolution.reason == "exact_match"
    assert resolution.port_path == "COM19"


def test_no_record_does_not_silently_resolve(tmp_path: Path) -> None:
    resolution = cache(tmp_path).resolve("bench_board", PROBE, [UART])

    assert resolution.reused is False
    assert resolution.reason == "no_record"


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("missing_probe_identity", "missing_stable_identity"),
        ("missing_uart_identity", "missing_stable_identity"),
        ("hardware_changed", "hardware_changed"),
        ("different_probe", "probe_changed"),
        ("multiple_records", "multiple_matches"),
        ("revoked", "revoked"),
    ],
)
def test_every_cache_ignore_condition_requires_reconfirmation(
    tmp_path: Path,
    case: str,
    expected_reason: str,
) -> None:
    attachments = cache(tmp_path)
    attachments.confirm(
        "bench_board",
        PROBE,
        UART,
        confirmed_at="2026-07-17T08:00:00Z",
    )
    probe = PROBE
    endpoints = [UART]

    if case == "missing_probe_identity":
        probe = ProbeIdentity("stlink", None)
    elif case == "missing_uart_identity":
        endpoints = [SerialEndpoint("COM3", None, 0x0483, 0x5740)]
    elif case == "hardware_changed":
        endpoints = [SerialEndpoint("COM3", "UART-CHANGED", 0x0483, 0x5740)]
    elif case == "different_probe":
        probe = ProbeIdentity("stlink", "PROBE-CHANGED")
    elif case == "multiple_records":
        second_uart = SerialEndpoint("COM4", "UART-002", 0x10C4, 0xEA60)
        attachments.confirm(
            "bench_board",
            PROBE,
            second_uart,
            confirmed_at="2026-07-17T08:01:00Z",
        )
        endpoints = [UART, second_uart]
    elif case == "revoked":
        assert attachments.revoke(
            "bench_board",
            PROBE,
            UART,
            revoked_at="2026-07-17T08:02:00Z",
        )

    resolution = attachments.resolve("bench_board", probe, endpoints)

    assert resolution.reused is False
    assert resolution.reason == expected_reason
    assert resolution.port_path is None


def test_confirmation_requires_complete_stable_identities(tmp_path: Path) -> None:
    attachments = cache(tmp_path)

    with pytest.raises(AttachmentCacheError, match="Stable UART"):
        attachments.confirm(
            "bench_board",
            PROBE,
            SerialEndpoint("COM3", None, 0x0483, 0x5740),
        )

    assert not attachments.path.exists()


def test_cache_schema_contains_no_authority_bearing_fields(tmp_path: Path) -> None:
    attachments = cache(tmp_path)
    attachments.confirm("bench_board", PROBE, UART)
    document = json.loads(attachments.path.read_text(encoding="utf-8"))

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {key for item in value.values() for key in keys(item)}
        if isinstance(value, list):
            return {key for item in value for key in keys(item)}
        return set()

    assert keys(document).isdisjoint(PERSISTED_AUTHORITY_KEYS)
    assert document["records"][0]["confirmed"] is True


def test_revocation_survives_cache_reconstruction(tmp_path: Path) -> None:
    attachments = cache(tmp_path)
    attachments.confirm("bench_board", PROBE, UART)
    assert attachments.revoke("bench_board", PROBE, UART)

    records = cache(tmp_path).load_records()
    assert len(records) == 1
    assert records[0].confirmed is False
    assert records[0].revoked_at is not None
