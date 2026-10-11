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
    report_fingerprint,
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
        unsalted = blake2b(canonical_json(value).encode("utf-8"), digest_size=8).hexdigest()
        self.assertNotEqual(fingerprint(value), unsalted)

    def test_salt_differs_across_deployments(self) -> None:
        from pyocd_debug_mcp.monitor import paths

        first = fingerprint({"address": 4096})
        paths._reset_cache(self.store_dir / "other")
        second = fingerprint({"address": 4096})
        self.assertNotEqual(first, second)

    def test_persisted_fingerprint_differs_across_report_contexts(self) -> None:
        stable = fingerprint({"address": 4096})
        self.assertNotEqual(
            report_fingerprint(stable, "rpt-one"),
            report_fingerprint(stable, "rpt-two"),
        )

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
        scrubbed = scrub_mechanical({"data": "DEADBEEF" * 100, "value": 42, "contents": "secret"})
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
        scrubbed = scrub_mechanical(
            {"probe_uid": "000682931234", "board_id": "customer-secret-board"}
        )
        self.assertNotIn("000682931234", scrubbed["probe_uid"])
        self.assertNotIn("customer-secret-board", scrubbed["board_id"])

    def test_long_hex_runs_are_dropped(self) -> None:
        scrubbed = scrub_mechanical({"note": "AA BB CC DD " * 40})
        self.assertEqual(scrubbed["note"], "<omitted>")

    def test_short_scalars_survive(self) -> None:
        scrubbed = scrub_mechanical({"word_size": 32, "phase": "open"})
        self.assertEqual(scrubbed["word_size"], 32)
        self.assertEqual(scrubbed["phase"], "open")

    def test_nested_mappings_are_scrubbed(self) -> None:
        scrubbed = scrub_mechanical({"outer": {"data": "x" * 900}})
        self.assertEqual(scrubbed["outer"]["data"], "<omitted>")

    def test_safe_path_and_digest_are_stable(self) -> None:
        self.assertEqual(safe_path("/tmp/a.elf"), safe_path("/tmp/a.elf"))
        self.assertEqual(digest_id("serial-1"), digest_id("serial-1"))

    def test_safe_path_uses_cross_host_basename_rules(self) -> None:
        self.assertTrue(safe_path(r"C:\Users\jason\project\app.elf").startswith("app.elf#"))
        self.assertTrue(safe_path("/home/jason/project/app.elf").startswith("app.elf#"))


class NarrativeBar(MonitorTestCase):
    """Personal builds may describe the codebase; they may not dump payloads."""

    def test_real_code_names_are_allowed(self) -> None:
        check_narrative(
            "I was editing handle_uart_rx() in src/uart.c to fix the ring buffer "
            "wraparound, then flashed the app to verify."
        )

    def test_file_paths_in_prose_are_allowed(self) -> None:
        check_narrative("Updated drivers/spi/spi_master.c and rebuilt.")

    def test_realistic_code_narratives_are_allowed(self) -> None:
        for value in (
            "Updated `handle_uart_rx()` in src/uart.c.",
            "Refactored the refresh_token() helper in auth/session.py.",
            "Moved the loader into secrets.py.",
            "Reworked the facade module.",
            "Ran unit tests for src/parser.c.",
            'Parser reported "invalid node" while reading the AST.',
            "The function returned -1 for invalid input.",
            "The UART output was truncated before the acknowledgement.",
            "The device returned an error instead of a successful acknowledgement.",
            "The board response was missing after reset.",
            "The token is expired and authentication needs a retry.",
            "The password is hashed before storage.",
            "The firmware output stage clamps invalid samples.",
            "The @dataclass decorator stores the board settings.",
            "Ran CMake to rebuild the project.",
            "Used the returned fields without adding a wrapper.",
            "make",
            "python",
            "pyocd",
        ):
            with self.subTest(value=value):
                check_narrative(value)

    def test_absolute_host_paths_are_rejected(self) -> None:
        for value in (
            "/home/alice/private/fw/main.c failed",
            r"C:\Users\alice\private\fw\main.c failed",
            r"\\build-host\private\fw\main.c failed",
            "file:///home/alice/private/fw/main.c failed",
            "file:%2F%2F%2Fhome%2Falice%2Fprivate%2Ffw.c failed",
            "%25252Fhome%25252Falice%25252Fprivate%25252Ffw%25252Fmain.c",
            "file:%25252F%25252F%25252Fhome%25252Falice%25252Fprivate%25252Ffw.c",
            "///home/alice/private/fw.c",
            "//home/alice/private/fw/main.c failed",
            "vscode://file/home/alice/private/fw/main.c failed",
            "/secret.txt",
            "Failed opening /firmware.elf",
            r"\Users\alice\private\fw\main.c failed",
            "Opened ⧸home⧸alice⧸private⧸firmware.elf.",
            "Opened C⦂⧹Users⧹alice⧹private⧹firmware.elf.",
            "Opened ∕home∕alice∕private∕fw.elf.",
            "Opened ~alice/customer/private/fw.elf",
            "%EF%BC%8F%EF%BD%88%EF%BD%8F%EF%BD%8D%EF%BD%85%EF%BC%8Fsecret",
        ):
            with self.subTest(value=value), self.assertRaises(NarrativeContentError):
                check_narrative(value)

    def test_credential_material_is_rejected(self) -> None:
        for value in (
            "password=hunter2",
            "The password is hunter2",
            '{"password": "hunter2"}',
            "The API credential is hunter2",
            "The passcode is hunter2",
            "The private key is abc123",
            "The authentication code is hunter2",
            "The value was " + "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef123456",
            "refresh_token=rt_live_XyZ987654321",
            "github_pat_" + "11ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef123456",
            "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef123456",
            "aws_secret_access_key=AbCdEf1234567890",
            "passphrase=hunter2",
            "refreshToken=AbCdEfGhIjKlMnOpQrStUvWx",
            "awsSecretAccessKey=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "xoxb-" + "123456789012-123456789012-abcdefghijklmnopqrstuvwx",
            "glpat-abcdefghijklmnopqrst",
            "Authorization: Bearer-secret",
            "Bearer abc.def.ghi",
            "https://alice:hunter2@example.invalid/repository",
            "password for the board: hunter2",
            'api_key "AbCdEfGhIjKlMnOpQrStUvWx"',
            "pa\ufe0fssword=hunter2",
            r"pa\u0073sword=hunter2",
            "pa&#115;sword=hunter2",
            "The token consisted of hunter2.",
            "The credential encoded as aHVudGVyMg==.",
            "The password used during board login was hunter2.",
            "The pаssword is hunter2",
            "The pаѕѕword is hunter2",
            "password → hunter2",
            "The password is hashed: hunter2",
            "password — hunter2",
            "password ‒ hunter2",
            "password hash hunter2",
            "password / hunter2",
            "p<!-- hidden -->assword=hunter2",
            "<!-- password=hunter2 -->",
            "password • hunter2",
            "password | hunter2",
            "credential set to hunter2",
            r"password \ hunter2",
            "password's value is hunter2",
            "secret code: 1234",
            "PIN: 1234",
            "Password SHA-256 was deadbeef123456.",
            "password ~ hunter2",
            "password (hunter2)",
            "password [hunter2]",
            "password contains hunter2",
            "The password has value hunter2.",
            "credential checksum deadbeef123456",
            "**password**: hunter2",
            "`password`: hunter2",
            "[password]: hunter2",
            "[password](https://example.invalid): hunter2",
            "(password): hunter2",
        ):
            with self.subTest(value=value), self.assertRaises(NarrativeContentError):
                check_narrative(value)

    def test_short_raw_payload_is_rejected(self) -> None:
        for value in (
            "Read back memory bytes: DE AD BE EF",
            "The UART bytes were DEADBEEF",
            "The UART output DEADBEEF",
            "The device replied DEADBEEF",
            "The UART gave 'OK READY'",
            "UART returned AQIDBA==",
            "The board said 'OK READY'",
            "raw DE AD BE",
            "raw DE AD",
            "UART bytes were DE.AD.BE",
            "UART output DEADBE",
            r"UART bytes were \x4f\x4b",
            "UART returned T0s=",
            "UART returned QUJDREVG",
            "UART returned T0tfUkVBRFk-__",
            "The device said “OK READY”",
            "The device said `OK READY`",
            "The UART printed OK READY",
            "UART returned hello",
            "raw byte 4F",
            "UART bytes were 0x4f 0x4b",
            r"UART bytes were \X4F \X4B",
            "UART bytes were 79 75",
            "UART bytes were 0117 0113",
            "artifact data: QQ==",
            "uart output: ok ready",
            "UART payload: 'OK READY'",
            "artifact contents=01234567",
            "The UART RX: OK READY",
            "The device RX: ACME-1234",
            "The artifact text: CUSTOMER_ID=ACME-1234",
            'The firmware printed "OK READY"',
            "Observed bytes DE AD",
            "The peripheral emitted `OK READY`.",
            "Captured QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo= from the device.",
            "Received from UART: OK READY CUSTOMER=ACME.",
            "Probe serial SYNTHETIC-PROBE-1234567890 was selected.",
            "Connection ID stlink:SYNTHETIC-CONNECTION-SECRET-1234 failed.",
            "probeid:stlink:SYNTHETIC-PROBE-SECRET-1234",
            "UART stream: OK READY CUSTOMER=ACME-1234",
            "CAN frame: 123#DEADBEEFCAFEBABE",
            "Memory at 0x20000000 contained DEADBEEF",
            "serial stream: \x1b[31mSECRET\x1b[0m",
            "device stream: OK\x00SECRET",
            "ST-LINK S/N SYNTHETIC-PROBE-1234567890 selected",
            "console transcript: ADMIN TOKEN ABC123",
            "encoded firmware blob " + ("Ab_cd-12" * 40),
            "MCU UID SYNTHETIC-MCU-0123456789 selected",
            "UART produced OK READY CUSTOMER=ACME-1234",
            "UART returned an error; OK READY CUSTOMER=ACME-1234",
            "CAN packet 123#DEADBEEFCAFEBABE",
            "RAM at 0x20000000 held DEADBEEF",
            "Board ID CUSTOMER-SECRET-1234 was selected.",
            "Probe S/N CUSTOMER-SECRET-1234 was selected.",
            "UART yielded OK READY CUSTOMER=ACME-1234",
            "A CAN message was 123#DEADBEEFCAFEBABE",
            "RAM location 0x20000000 held DEADBEEF",
            "connection_id=stlink:CUSTOMER-SECRET-1234",
            "UART contained OK READY CUSTOMER=ACME-9911",
            "RAM bytes: DE AD BE EF",
            "The frame on CAN carried 123#DEADBEEF",
            "Board ID; CUSTOMER-SECRET-1234",
            "Probe serial, CUSTOMER-SECRET-1234",
            "UART delivered OK READY CUSTOMER=ACME-9911",
            "UART capture: OK READY CUSTOMER=ACME-9911",
            "CAN traffic: 123#DEADBEEF",
            "RAM readout: DE AD BE EF",
            "Probe SN: CUSTOMER-SECRET-1234",
            "Board identifier: CUSTOMER-SECRET-1234",
            "<!-- UART returned OK READY CUSTOMER=ACME -->",
            "Safe summary <!-- Probe serial CUSTOMER-SECRET-1234 -->",
            "Probe serial. CUSTOMER-SECRET-1234",
            "Board ID unavailable. CUSTOMER-SECRET-1234 was observed.",
            "UART reported OK READY CUSTOMER=ACME",
            "UART log: OK READY CUSTOMER=ACME",
            "From UART came OK READY CUSTOMER=ACME",
            "CAN log: 123#DEADBEEF",
            "RAM snapshot: DE AD BE EF",
            "UART observed: OK READY CUSTOMER=ACME-9911",
            "On UART: OK READY CUSTOMER=ACME-9911",
            "The response from UART was OK READY CUSTOMER=ACME-9911",
            "RAM held DEADBEEF at startup.",
            "The RAM value was DEADBEEF.",
            "CAN observed 123#DEADBEEF.",
            "CAN read as 123#DEADBEEF.",
            "The CAN value was 123#DEADBEEF.",
            "The identifier of the probe was CUSTOMER-SECRET-1234",
            "Hardware identifier: CUSTOMER-SECRET-1234",
            "Probe ID helper leaked CUSTOMER-SECRET-1234; parser accepts formats.",
            "Board identifier parser exposed CUSTOMER-SECRET-1234 but now accepts formats.",
            "Credential digest parser exposed hunter2 and handles retries.",
            "UART response parser leaked OK READY CUSTOMER=ACME-9911.",
            "CAN frame parser leaked 123#DEADBEEF.",
            "Probe ID unavailable. 123456789012",
            "Board serial redacted. 000123456789",
            "Probe ID unavailable. SECRETIDENTIFIER",
            "UART response parser accepts OK READY SECRET PAYLOAD",
            "CAN frame parser validates DEADBEEF",
            "Password hash parser accepts DEADBEEF",
            "Credential digest helper masks HUNTERTWO",
            "**UART** returned OK READY CUSTOMER=ACME-9911",
            "`UART` returned OK READY CUSTOMER=ACME-9911",
            "**RAM** held DEADBEEF",
            "**CAN** observed 123#DEADBEEF",
            "[UART] returned OK READY CUSTOMER=ACME-9911",
            "(UART) returned OK READY CUSTOMER=ACME-9911",
        ):
            with self.subTest(value=value), self.assertRaises(NarrativeContentError):
                check_narrative(value)

    def test_memory_dump_is_rejected(self) -> None:
        with self.assertRaises(NarrativeContentError):
            check_narrative("Read back: " + "DE AD BE EF " * 40)

    def test_base64_blob_is_rejected(self) -> None:
        with self.assertRaises(NarrativeContentError):
            check_narrative("payload " + "QUJDREVG" * 40)

    def test_full_command_line_is_rejected(self) -> None:
        for value in (
            r"ran C:\tools\arm-none-eabi-gcc.exe -O2 -mcpu=cortex-m4 -o app.elf",
            "ran arm-none-eabi-gcc src/main.c -o app.elf",
            "Executed command: make clean && make all",
            "Executed command: arm-none-eabi-gcc @release.rsp",
            "command: make clean all",
            "make clean all",
            "ran make",
            "make clean | tee build.log",
            "make clean; make all",
            "make clean > build.log",
            "Ran `make clean all`",
            "pytest tests/test_flash.py",
            "Ran git diff HEAD",
            "Ran bash -lc 'make clean all'",
            "Executed pwsh -Command 'pytest -q'",
            "docker build --no-cache .",
            "Used `make clean all` to rebuild the firmware.",
            "mаke clean && mаke all",
            "Ran rm firmware.elf",
            "rm firmware.elf",
            "ar rcs libsecret.a customer.o",
            "custom-builder target release",
            "sudo make clean all",
            "CC=gcc make all",
            "builder target release",
            "Safe summary <!-- sudo make clean all -->",
            "command custombuilder target",
            "time make clean all",
            "env MODE=prod custombuilder target",
            "custombuilder target release",
            "mytool build firmware",
            "nohup make clean all",
            "$ make clean all",
            "> make clean all",
            "./custombuilder target",
            "make clean all because the handler handles failures",
            "git status; parser handles output",
            "ninja clean all while integration supports builds",
            "Next, make clean all",
            "Please run: make clean all",
            "make targets describes clean all",
            "foo-bar parser handles build target",
            "`custombuilder target release`",
            "**custombuilder target release**",
            "[custombuilder target release]",
            "(make clean all)",
        ):
            with self.subTest(value=value), self.assertRaises(NarrativeContentError):
                check_narrative(value)

    def test_self_rating_is_rejected(self) -> None:
        for value in (
            "I did well overall and my performance was strong.",
            "Ｉ did well overall.",
            "%49 did well overall.",
            "I\u200b did well overall.",
        ):
            with self.subTest(value=value), self.assertRaises(NarrativeContentError):
                check_no_self_rating(value)

    def test_observable_outcomes_are_accepted(self) -> None:
        import time

        for value in (
            "Flashed twice; the second attempt verified. Got stuck for three "
            "attempts on the linker map path and could not find a route.",
            "The comparison `left < right` selects the next node.",
            "The parser supports JSON | CBOR inputs.",
            "Used CMake to rebuild the project.",
            "Ran integration tests for drivers/uart.c.",
            "state-machine logic handles retries.",
            "src/parser.py handles invalid input.",
            "refresh_token helper masks stored credentials.",
            "error-handling code avoids retries.",
            "The board identifier parser accepts common formats.",
            "Probe ID support handles multiple vendors.",
            "The probe serial parser validates identifiers.",
            "The password hash parser rejects malformed input.",
            "The credential digest function masks its output.",
            "Git integration supports offline workflows.",
            "Cargo support remains optional.",
            "Make targets describe supported builds.",
            "The UART reported that the handler failed.",
        ):
            with self.subTest(value=value):
                check_narrative(value)
                check_no_self_rating(value)

        started = time.monotonic()
        check_narrative("(" * 64 + "safe" + ")" * 64)
        self.assertLess(time.monotonic() - started, 1.0)


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
