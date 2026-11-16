"""Content bars: what may leave, and what may never.

Two layers with different audiences. The mechanical layer is universal and must
carry no codebase content or reconstruction signal. The narrative layer exists
only in a personal build and is deliberately allowed to describe the codebase --
that is its purpose -- while still refusing verbatim payloads.
"""

from __future__ import annotations

import unittest


from pyocd_debug_mcp.monitor.redaction import (
    NarrativeContentError,
    check_narrative,
    check_no_self_rating,
    digest_id,
    scrub_mechanical,
)
from tests.monitor_support import MonitorTestCase


class PortablePathBasenames(MonitorTestCase):
    _PATH_CASES = (
        (r"C:\Users\operator\project-private\app.elf", "app.elf"),
        ("C:/Users/operator/project-private/app.elf", "app.elf"),
        (r"c:\Work Area\MixedCase.ELF", "MixedCase.ELF"),
        ("D:\\build\\固件 café.elf", "固件 café.elf"),
        (r"C:project-private\app.elf", "app.elf"),
        ("C:app.elf", "app.elf"),
        (r"project-private\build\app.elf", "app.elf"),
        (r".\build\app.elf", "app.elf"),
        (r"..\project-private\app.elf", "app.elf"),
        (r"\\server-private\share-private\project-private\app.elf", "app.elf"),
        ("//server-private/share-private/project-private/app.elf", "app.elf"),
        (r"\\?\C:\project-private\app.elf", "app.elf"),
        (r"\\?\UNC\server-private\share-private\project-private\app.elf", "app.elf"),
        ("C:\\project-private\\build\\", "build"),
        ("/home/operator/project-private/app.elf", "app.elf"),
        ("/tmp/project-private/build/", "build"),
        ("/opt/Work Area/MixedCase.ELF", "MixedCase.ELF"),
        ("/home/operator/固件 café.elf", "固件 café.elf"),
        ("project-private/build/app.elf", "app.elf"),
        ("./build/app.elf", "app.elf"),
        ("../project-private/app.elf", "app.elf"),
        ("app.elf", "app.elf"),
        ("固件 café.ELF", "固件 café.ELF"),
        ("README", "README"),
    )
    _FALLBACK_INPUTS = (
        "",
        ".",
        "./",
        ".\\",
        "/",
        "\\",
        "//",
        "///",
        "C:",
        "C:\\",
        "C:/",
        r"\\server-private\share-private",
        "\\\\server-private\\share-private\\",
        "//server-private/share-private/",
        "\\\\?\\C:\\",
        "\\\\?\\UNC\\server-private\\share-private\\",
    )

    def test_nested_payload_identifier_and_scalar_rules_remain_intact(self) -> None:
        artifact = r"C:\project-private\app.elf"
        probe = r"USB\probe-private"
        self.assertEqual(
            scrub_mechanical(
                {
                    "outer": {
                        "artifact_path": artifact,
                        "data": "secret contents",
                        "value": 42,
                        "blob": b"secret bytes",
                        "probe_uid": probe,
                        "note": "app.elf",
                        "board_id": "b1",
                        "word_size": 32,
                        "enabled": True,
                        "missing": None,
                        "items": ["private", "payload"],
                        "long_note": "x" * 513,
                        "hex_dump": "AA BB CC DD " * 40,
                    }
                }
            ),
            {
                "outer": {
                    "artifact_path": f"app.elf#{digest_id(artifact)}",
                    "data": "<omitted>",
                    "value": "<omitted>",
                    "blob": "<omitted>",
                    "probe_uid": digest_id(probe),
                    "note": "app.elf",
                    "board_id": "b1",
                    "word_size": 32,
                    "enabled": True,
                    "missing": None,
                    "items": "<2 items>",
                    "long_note": "<omitted>",
                    "hex_dump": "<omitted>",
                }
            },
        )


class NarrativeBar(MonitorTestCase):
    """Personal builds may describe the codebase; they may not dump payloads."""

    def test_memory_dump_is_rejected(self) -> None:
        with self.assertRaises(NarrativeContentError):
            check_narrative("Read back: " + "DE AD BE EF " * 40)

    def test_base64_blob_is_rejected(self) -> None:
        with self.assertRaises(NarrativeContentError):
            check_narrative("payload " + "QUJDREVG" * 40)

    def test_full_command_line_is_rejected(self) -> None:
        with self.assertRaises(NarrativeContentError):
            check_narrative(r"ran C:\tools\arm-none-eabi-gcc.exe -O2 -mcpu=cortex-m4 -o app.elf")

    def test_observable_outcomes_are_accepted(self) -> None:
        check_no_self_rating(
            "Flashed twice; the second attempt verified. Got stuck for three "
            "attempts on the linker map path and could not find a route."
        )


if __name__ == "__main__":
    unittest.main()
