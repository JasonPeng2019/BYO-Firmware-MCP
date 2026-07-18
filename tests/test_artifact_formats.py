from pathlib import Path

from pyocd_debug_mcp.artifact_formats import (
    FirmwareFormat,
    detect_firmware_format,
    matching_elf_companion,
)
from pyocd_debug_mcp.guardrails.flash_gate import resolve_flash_request
from pyocd_debug_mcp.services.session_runtime import ActionContext


def test_elf_content_is_accepted_independent_of_vendor_suffix(tmp_path: Path) -> None:
    axf = tmp_path / "firmware.axf"
    axf.write_bytes(b"\x7fELF\x01\x01\x01")
    assert detect_firmware_format(axf) is FirmwareFormat.ELF

    request = resolve_flash_request(
        object(),  # type: ignore[arg-type]
        explicit_path=axf,
        action_context=ActionContext("mcp", "flash_application", "session"),
    )
    assert request.artifact_path == axf


def test_intel_hex_finds_axf_companion_without_suffix_authority(tmp_path: Path) -> None:
    image = tmp_path / "application.ihex"
    image.write_text(":00000001FF\n", encoding="ascii")
    companion = tmp_path / "application.axf"
    companion.write_bytes(b"\x7fELF\x01")

    assert detect_firmware_format(image) is FirmwareFormat.INTEL_HEX
    assert matching_elf_companion(image) == companion


def test_unknown_bytes_are_not_guessed_to_be_raw_binary(tmp_path: Path) -> None:
    binary = tmp_path / "firmware.out"
    binary.write_bytes(b"raw bytes without trusted address")
    assert detect_firmware_format(binary) is FirmwareFormat.UNKNOWN


def test_motorola_s_record_is_not_misclassified_as_raw_binary(tmp_path: Path) -> None:
    image = tmp_path / "firmware.any"
    image.write_text("S1130000285F245F2212226A000424290008237C2A\n", encoding="ascii")
    assert detect_firmware_format(image) is FirmwareFormat.MOTOROLA_S_RECORD


def test_uf2_requires_all_three_little_endian_block_magics(tmp_path: Path) -> None:
    block = bytearray(512)
    block[:8] = bytes.fromhex("5546320a57515d9e")
    block[508:512] = bytes.fromhex("306fb10a")
    image = tmp_path / "firmware.native"
    image.write_bytes(block)

    assert detect_firmware_format(image) is FirmwareFormat.UF2

    for offset in (0, 4, 508):
        malformed = bytearray(block)
        malformed[offset] ^= 0xFF
        image.write_bytes(malformed)
        assert detect_firmware_format(image) is FirmwareFormat.UNKNOWN
