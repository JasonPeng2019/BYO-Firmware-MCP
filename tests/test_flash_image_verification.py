from __future__ import annotations

import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

from firmware_mcp.safety.linker import (
    LinkerEvidenceError,
    canonical_image_digest,
    executable_elf_ranges,
    file_backed_elf_ranges,
    parse_flash_image,
)


def _record(address: int, record_type: int, data: bytes) -> str:
    body = bytes((len(data), (address >> 8) & 0xFF, address & 0xFF, record_type)) + data
    return ":" + (body + bytes(((-sum(body)) & 0xFF,))).hex().upper()


class FlashImageVerificationTests(unittest.TestCase):
    def _write_hex(self, *lines: str) -> Path:
        temporary = tempfile.NamedTemporaryFile(
            suffix=".hex", delete=False, mode="w", encoding="ascii"
        )
        self.addCleanup(lambda: Path(temporary.name).unlink(missing_ok=True))
        temporary.write("\n".join(lines) + "\n")
        temporary.close()
        return Path(temporary.name)

    def test_sparse_hex_preserves_holes_and_digest_orders_by_address(self) -> None:
        path = self._write_hex(
            _record(0x0010, 0, b"\xbb"),
            _record(0x0001, 0, b"\xaa"),
            _record(0, 1, b""),
        )

        image = parse_flash_image(path)

        self.assertEqual(image.bytes_by_address, {0x0001: 0xAA, 0x0010: 0xBB})
        self.assertEqual(image.ranges, ((0x0001, 0x0002), (0x0010, 0x0011)))
        expected_stream = (0x0001).to_bytes(8, "big") + b"\xaa"
        expected_stream += (0x0010).to_bytes(8, "big") + b"\xbb"
        self.assertEqual(image.sha256, hashlib.sha256(expected_stream).hexdigest())
        self.assertEqual(canonical_image_digest({0x0010: 0xBB, 0x0001: 0xAA}), image.sha256)

    def test_identical_hex_overlap_is_coalesced_but_conflicting_overlap_is_rejected(self) -> None:
        identical = self._write_hex(
            _record(0x0000, 0, b"\x01\x02"),
            _record(0x0001, 0, b"\x02\x03"),
            _record(0, 1, b""),
        )
        self.assertEqual(
            parse_flash_image(identical).bytes_by_address,
            {0x0000: 0x01, 0x0001: 0x02, 0x0002: 0x03},
        )

        conflicting = self._write_hex(
            _record(0x0000, 0, b"\x01"),
            _record(0x0000, 0, b"\x02"),
            _record(0, 1, b""),
        )
        with self.assertRaisesRegex(LinkerEvidenceError, "disagree"):
            parse_flash_image(conflicting)

    def test_malformed_hex_never_produces_a_flash_image(self) -> None:
        path = self._write_hex(":0100000001FF", _record(0, 1, b""))

        with self.assertRaisesRegex(LinkerEvidenceError, "checksum"):
            parse_flash_image(path)

    def _write_elf(self, suffix: str, segments: list[tuple[int, bytes, int]]) -> Path:
        """Create a minimal little-endian ELF32 with PT_LOAD physical addresses."""

        phoff = 52
        phentsize = 32
        data_offset = 0x100
        headers = bytearray()
        payload = bytearray(data_offset)
        cursor = data_offset
        for paddr, data, memsz in segments:
            headers.extend(
                struct.pack("<IIIIIIII", 1, cursor, paddr, paddr, len(data), memsz, 5, 4)
            )
            payload.extend(data)
            cursor += len(data)
        payload[:52] = struct.pack(
            "<16sHHIIIIIHHHHHH",
            b"\x7fELF\x01\x01\x01" + b"\0" * 9,
            2,
            40,
            1,
            0,
            phoff,
            0,
            0,
            52,
            phentsize,
            len(segments),
            0,
            0,
            0,
        )
        payload[phoff : phoff + len(headers)] = headers
        temporary = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        self.addCleanup(lambda: Path(temporary.name).unlink(missing_ok=True))
        temporary.write(payload)
        temporary.close()
        return Path(temporary.name)

    def _patch_elf32_program_offset(self, path: Path, segment_index: int, offset: int) -> None:
        """Replace one ELF32 ``p_offset`` field without changing file contents."""

        payload = bytearray(path.read_bytes())
        struct.pack_into("<I", payload, 52 + segment_index * 32 + 4, offset)
        path.write_bytes(payload)

    def _write_elf64(self, suffix: str, segments: list[tuple[int, bytes, int]]) -> Path:
        """Create a minimal ELF64 with physical PT_LOAD addresses."""

        phoff = 64
        phentsize = 56
        data_offset = 0x100
        headers = bytearray()
        payload = bytearray(data_offset)
        cursor = data_offset
        for paddr, data, memsz in segments:
            headers.extend(
                struct.pack("<IIQQQQQQ", 1, 5, cursor, paddr, paddr, len(data), memsz, 4)
            )
            payload.extend(data)
            cursor += len(data)
        payload[:64] = struct.pack(
            "<16sHHIQQQIHHHHHH",
            b"\x7fELF\x02\x01\x01" + b"\0" * 9,
            2,
            62,
            1,
            0,
            phoff,
            0,
            0,
            64,
            phentsize,
            len(segments),
            0,
            0,
            0,
        )
        payload[phoff : phoff + len(headers)] = headers
        temporary = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        self.addCleanup(lambda: Path(temporary.name).unlink(missing_ok=True))
        temporary.write(payload)
        temporary.close()
        return Path(temporary.name)

    def test_elf_and_axf_extract_sparse_physical_load_bytes_without_bss(self) -> None:
        segments = [(0x08000010, b"\xaa\xbb", 8), (0x08000100, b"\xcc", 1)]
        elf = parse_flash_image(self._write_elf(".elf", segments))
        axf = parse_flash_image(self._write_elf(".axf", list(reversed(segments))))

        expected = {0x08000010: 0xAA, 0x08000011: 0xBB, 0x08000100: 0xCC}
        self.assertEqual(elf.bytes_by_address, expected)
        self.assertEqual(elf.ranges, ((0x08000010, 0x08000012), (0x08000100, 0x08000101)))
        self.assertEqual(elf.sha256, axf.sha256)

    def test_bss_only_load_segment_is_structurally_valid_but_not_programmed(self) -> None:
        """A PT_LOAD's memory-only BSS portion is not an image byte range."""

        image = parse_flash_image(
            self._write_elf(
                ".elf",
                [
                    (0x08000010, b"\xaa\xbb", 2),
                    # This is a valid memory allocation with no file-backed bytes.
                    (0x20000000, b"", 0x80),
                ],
            )
        )

        self.assertEqual(image.bytes_by_address, {0x08000010: 0xAA, 0x08000011: 0xBB})
        self.assertEqual(image.ranges, ((0x08000010, 0x08000012),))
        self.assertEqual(
            image.sha256,
            canonical_image_digest({0x08000010: 0xAA, 0x08000011: 0xBB}),
        )

    def test_bss_only_segment_offset_beyond_eof_is_refused_before_skip(self) -> None:
        path = self._write_elf(
            ".elf",
            [(0x08000010, b"\xaa", 1), (0x20000000, b"", 0x80)],
        )
        self._patch_elf32_program_offset(path, 1, 0xFFFF)

        with self.assertRaisesRegex(LinkerEvidenceError, "extend beyond the artifact"):
            parse_flash_image(path)

    def test_bss_only_segment_physical_range_overflow_is_refused_before_skip(self) -> None:
        path = self._write_elf64(
            ".axf",
            [(0x08000010, b"\xaa", 1), ((1 << 64) - 1, b"", 2)],
        )

        with self.assertRaisesRegex(LinkerEvidenceError, "unsigned 64-bit"):
            parse_flash_image(path)

    def test_only_zero_file_byte_load_segments_are_an_empty_image(self) -> None:
        path = self._write_elf(
            ".elf",
            [(0x20000000, b"", 0), (0x20000010, b"", 0x40)],
        )

        with self.assertRaisesRegex(LinkerEvidenceError, "contains no bytes"):
            parse_flash_image(path)

    def test_elf_identical_overlap_coalesces_and_conflicting_overlap_fails(self) -> None:
        coalesced = parse_flash_image(
            self._write_elf(".elf", [(0x1000, b"\x01\x02", 2), (0x1001, b"\x02\x03", 2)])
        )
        self.assertEqual(coalesced.bytes_by_address, {0x1000: 1, 0x1001: 2, 0x1002: 3})
        with self.assertRaisesRegex(LinkerEvidenceError, "disagree"):
            parse_flash_image(self._write_elf(".elf", [(0x1000, b"\x01", 1), (0x1000, b"\x02", 1)]))

    def test_all_file_backed_ranges_include_non_executable_loads(self) -> None:
        path = self._write_elf(".elf", [(0x1000, b"\x01", 1), (0x2000, b"\x02", 1)])
        payload = bytearray(path.read_bytes())
        # Clear PF_X on the second program header while retaining its file bytes.
        struct.pack_into("<I", payload, 52 + 32 + 24, 4)
        path.write_bytes(payload)
        self.assertEqual(file_backed_elf_ranges(path), ((0x1000, 0x1001), (0x2000, 0x2001)))
        self.assertEqual(executable_elf_ranges(path), ((0x1000, 0x1001),))

    def test_file_backed_and_executable_consumers_reject_offset_beyond_snapshot(self) -> None:
        path = self._write_elf(".elf", [(0x1000, b"\x01", 1)])
        self._patch_elf32_program_offset(path, 0, 0xFFFF)
        with self.assertRaisesRegex(LinkerEvidenceError, "extend beyond the artifact"):
            file_backed_elf_ranges(path)
        with self.assertRaisesRegex(LinkerEvidenceError, "extend beyond the artifact"):
            executable_elf_ranges(path)


if __name__ == "__main__":
    unittest.main()
