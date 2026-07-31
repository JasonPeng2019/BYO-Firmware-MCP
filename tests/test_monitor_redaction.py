"""Content bars: what may leave, and what may never.

Two layers with different audiences. The mechanical layer is universal and must
carry no codebase content or reconstruction signal. The narrative layer exists
only in a personal build and is deliberately allowed to describe the codebase --
that is its purpose -- while still refusing verbatim payloads.
"""

from __future__ import annotations

import unittest

from pathlib import Path

from mcp.types import TextContent

from pyocd_debug_mcp.monitor.paths import workspace_id
from pyocd_debug_mcp.monitor.redaction import (
    NarrativeContentError,
    check_narrative,
    check_no_self_rating,
    digest_id,
    fingerprint,
    normalize_signature,
    result_text,
    safe_path,
    scrub_mechanical,
)
from tests.monitor_support import MonitorTestCase


class Fingerprints(MonitorTestCase):
    def test_same_arguments_fingerprint_alike_within_a_deployment(self) -> None:
        self.assertEqual(fingerprint({"a": 1, "b": 2}), fingerprint({"b": 2, "a": 1}))

    def test_different_arguments_differ(self) -> None:
        self.assertNotEqual(fingerprint({"address": 1}), fingerprint({"address": 2}))

    def test_small_guessable_input_is_not_brute_forceable(self) -> None:
        """An unsalted hash of a register address would be trivially reversible.

        The salt is what stops a reader of the reports rebuilding the real value
        from a dictionary of plausible inputs.
        """

        from hashlib import blake2b

        from pyocd_debug_mcp.guardrails.plan_engine import canonical_json

        value = {"address": 536883712}
        unsalted = blake2b(
            canonical_json(value).encode("utf-8"), digest_size=8
        ).hexdigest()
        self.assertNotEqual(fingerprint(value), unsalted)

    def test_salt_differs_across_deployments(self) -> None:
        from pyocd_debug_mcp.monitor import paths

        first = fingerprint({"address": 4096})
        paths._reset_cache(self.store_dir / "other")
        second = fingerprint({"address": 4096})
        self.assertNotEqual(first, second)

    def test_fingerprint_never_raises_on_odd_input(self) -> None:
        class Weird:
            def __repr__(self) -> str:
                raise RuntimeError("no repr for you")

        self.assertTrue(fingerprint({"x": object()}))


class SaltPersistence(MonitorTestCase):
    """Regression: the salt must not depend on call order within a process.

    Reading a not-yet-resolved store handed back a throwaway process-local salt,
    so the same workspace hashed differently in two processes sharing one store.
    """

    def test_salt_is_stable_across_processes(self) -> None:
        import subprocess
        import sys

        script = (
            "import sys; sys.path.insert(0, 'src');"
            "from pyocd_debug_mcp.monitor import paths;"
            f"paths._reset_cache(__import__('pathlib').Path(r'{self.store_dir}'));"
            "print(paths.deployment_salt().hex())"
        )
        root = Path(__file__).resolve().parents[1]
        first = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(root)
        ).stdout.strip()
        second = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(root)
        ).stdout.strip()
        self.assertTrue(first)
        self.assertEqual(first, second)

    def test_workspace_id_is_stable_across_processes(self) -> None:
        import subprocess
        import sys

        project = self.store_dir / "proj"
        project.mkdir(exist_ok=True)
        script = (
            "import sys; sys.path.insert(0, 'src');"
            "from pathlib import Path;"
            "from pyocd_debug_mcp.monitor import paths;"
            f"paths._reset_cache(Path(r'{self.store_dir}'));"
            f"print(paths.workspace_id(Path(r'{project}')))"
        )
        root = Path(__file__).resolve().parents[1]
        first = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(root)
        ).stdout.strip()
        second = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(root)
        ).stdout.strip()
        self.assertTrue(first)
        self.assertEqual(first, second)


class MechanicalBar(MonitorTestCase):
    def test_payload_keys_are_dropped(self) -> None:
        scrubbed = scrub_mechanical(
            {"data": "DEADBEEF" * 100, "value": 42, "contents": "secret"}
        )
        self.assertEqual(scrubbed["data"], "<omitted>")
        self.assertEqual(scrubbed["contents"], "<omitted>")

    def test_raw_bytes_are_dropped(self) -> None:
        self.assertEqual(scrub_mechanical({"blob": b"\x00\x01\x02"})["blob"], "<omitted>")

    def test_full_host_paths_are_reduced(self) -> None:
        scrubbed = scrub_mechanical({"elf_path": r"C:\Users\jason\secret-project\app.elf"})
        self.assertNotIn("secret-project", scrubbed["elf_path"])
        self.assertNotIn("jason", scrubbed["elf_path"])
        self.assertTrue(scrubbed["elf_path"].startswith("app.elf#"))

    def test_hardware_identifiers_are_digested(self) -> None:
        scrubbed = scrub_mechanical({"probe_uid": "000682931234"})
        self.assertNotIn("000682931234", scrubbed["probe_uid"])

    def test_long_hex_runs_are_dropped(self) -> None:
        scrubbed = scrub_mechanical({"note": "AA BB CC DD " * 40})
        self.assertEqual(scrubbed["note"], "<omitted>")

    def test_short_scalars_survive(self) -> None:
        scrubbed = scrub_mechanical({"word_size": 32, "board_id": "b1"})
        self.assertEqual(scrubbed["word_size"], 32)
        self.assertEqual(scrubbed["board_id"], "b1")

    def test_nested_mappings_are_scrubbed(self) -> None:
        scrubbed = scrub_mechanical({"outer": {"data": "x" * 900}})
        self.assertEqual(scrubbed["outer"]["data"], "<omitted>")

    def test_safe_path_and_digest_are_stable(self) -> None:
        self.assertEqual(safe_path("/tmp/a.elf"), safe_path("/tmp/a.elf"))
        self.assertEqual(digest_id("serial-1"), digest_id("serial-1"))


class NarrativeBar(MonitorTestCase):
    """Personal builds may describe the codebase; they may not dump payloads."""

    def test_real_code_names_are_allowed(self) -> None:
        check_narrative(
            "I was editing handle_uart_rx() in src/uart.c to fix the ring buffer "
            "wraparound, then flashed the app to verify."
        )

    def test_file_paths_in_prose_are_allowed(self) -> None:
        check_narrative("Updated drivers/spi/spi_master.c and rebuilt.")

    def test_memory_dump_is_rejected(self) -> None:
        with self.assertRaises(NarrativeContentError):
            check_narrative("Read back: " + "DE AD BE EF " * 40)

    def test_base64_blob_is_rejected(self) -> None:
        with self.assertRaises(NarrativeContentError):
            check_narrative("payload " + "QUJDREVG" * 40)

    def test_full_command_line_is_rejected(self) -> None:
        with self.assertRaises(NarrativeContentError):
            check_narrative(
                r"ran C:\tools\arm-none-eabi-gcc.exe -O2 -mcpu=cortex-m4 -o app.elf"
            )

    def test_self_rating_is_rejected(self) -> None:
        with self.assertRaises(NarrativeContentError):
            check_no_self_rating("I did well overall and my performance was strong.")

    def test_observable_outcomes_are_accepted(self) -> None:
        check_no_self_rating(
            "Flashed twice; the second attempt verified. Got stuck for three "
            "attempts on the linker map path and could not find a route."
        )


class ResultTextExtraction(unittest.TestCase):
    """Tools return content blocks, not strings; classification depends on this."""

    def test_content_block_list(self) -> None:
        blocks = [TextContent(type="text", text="Refused [x/y]: nope")]
        self.assertEqual(result_text(blocks), "Refused [x/y]: nope")

    def test_multiple_blocks_join(self) -> None:
        blocks = [
            TextContent(type="text", text="a"),
            TextContent(type="text", text="b"),
        ]
        self.assertEqual(result_text(blocks), "a\nb")

    def test_structured_tuple_form(self) -> None:
        blocks = [TextContent(type="text", text="hello")]
        self.assertEqual(result_text((blocks, {"k": 1})), "hello")

    def test_plain_string(self) -> None:
        self.assertEqual(result_text("plain"), "plain")

    def test_unknown_shape_is_empty_not_a_guess(self) -> None:
        self.assertEqual(result_text(object()), "")


class SignatureNormalisation(unittest.TestCase):
    def test_addresses_and_counts_are_normalised(self) -> None:
        self.assertEqual(
            normalize_signature("fail at 0x2000 after 3"),
            normalize_signature("fail at 0x8000 after 91"),
        )

    def test_paths_are_normalised(self) -> None:
        self.assertEqual(
            normalize_signature(r"missing C:\Users\a\x.elf"),
            normalize_signature(r"missing C:\Users\b\y.elf"),
        )


class WorkspaceAnonymisation(MonitorTestCase):
    def test_workspace_id_hides_the_path(self) -> None:
        from pathlib import Path

        wid = workspace_id(Path(self.store_dir))
        self.assertNotIn("byo-monitor-test", wid)
        self.assertNotIn(str(self.store_dir), wid)

    def test_workspace_id_is_stable(self) -> None:
        from pathlib import Path

        self.assertEqual(workspace_id(Path(self.store_dir)), workspace_id(Path(self.store_dir)))

    def test_unbound_when_no_path(self) -> None:
        self.assertEqual(workspace_id(None), "unbound")


if __name__ == "__main__":
    unittest.main()
