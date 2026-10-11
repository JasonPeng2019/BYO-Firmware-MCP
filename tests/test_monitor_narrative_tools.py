"""The three agent-facing actions and the two model-authored forms.

Model output is untrusted, so the forms are fixed schemas rather than free prose.
The three actions must never be conflated: a routine check-in is not an issue
report, and the health check is not a submission.
"""

from __future__ import annotations

import json
import time
import unittest

from pyocd_debug_mcp.monitor.monitor import ArtifactPersistence
from pyocd_debug_mcp.monitor.narrative import (
    validate_checkin_form,
    validate_issue_form,
)
from pyocd_debug_mcp.monitor.tools import (
    CHECKIN_TOOL_NAME,
    HEALTH_TOOL_NAME,
    MONITOR_TOOL_NAMES,
    REPORT_TOOL_NAME,
    build_monitor_tools,
)
from tests.monitor_support import MonitorTestCase, make_context


def issue_form(**overrides):
    form = {
        "signal_type": "S-4",
        "codebase_objective": "A motor controller firmware; adding closed-loop speed control.",
        "hypothesis": "The plan envelope rejected my nested action_parameters object.",
        "goal": "Flash the new application image to the bench board.",
        "plan": "Collect artifacts, submit the flash plan, then flash.",
        "failure_point": {
            "action_taken": "Submitted flash_application-plan with the populated plan.",
            "observed_result": "Rejected for an unexpected field.",
            "named_step": "flash_application-plan",
        },
        "recent_actions": [
            {
                "action": f"step {index}",
                "result": "ok",
                "code_context": "Building the speed-control loop in src/control.c.",
            }
            for index in range(5)
        ],
        "earlier_phases": ["Set up the board", "Built the firmware"],
        "session_start": "Started by asking which board to use.",
    }
    form.update(overrides)
    return form


def checkin_form(**overrides):
    form = {
        "codebase_summary": "Motor controller firmware; currently adding speed control.",
        "work_summary": "Built the firmware, flashed it twice, and read back status.",
        "tools_used": [{"tool": "flash", "purpose": "wrote firmware twice"}],
        "effectiveness_observed": (
            "Flashed successfully on the second attempt; spent three tries finding "
            "the linker map path."
        ),
    }
    form.update(overrides)
    return form


class IssueForm(unittest.TestCase):
    def test_valid_form_is_accepted(self) -> None:
        validated, signal = validate_issue_form(issue_form())
        self.assertEqual(signal.value, "S-4")
        self.assertEqual(len(validated["recent_actions"]), 5)

    def test_more_than_five_recent_actions_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_issue_form(
                issue_form(
                    recent_actions=[
                        {"action": "a", "result": "b", "code_context": "c"} for _ in range(6)
                    ]
                )
            )

    def test_missing_required_field_is_rejected(self) -> None:
        form = issue_form()
        del form["hypothesis"]
        with self.assertRaises(ValueError):
            validate_issue_form(form)

    def test_extra_field_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_issue_form(issue_form(sneaky="value"))

    def test_freeform_submission_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_issue_form("something went wrong")

    def test_server_detected_signal_cannot_be_filed_by_the_model(self) -> None:
        with self.assertRaises(ValueError) as caught:
            validate_issue_form(issue_form(signal_type="S-1"))
        self.assertIn("server-detected", str(caught.exception))

    def test_subcase_required_for_guidance_signal(self) -> None:
        with self.assertRaises(ValueError):
            validate_issue_form(issue_form(signal_type="S-6"))
        validated, _ = validate_issue_form(
            issue_form(signal_type="S-6", signal_subcase="guidance_was_unusable")
        )
        self.assertEqual(validated["signal_subcase"], "guidance_was_unusable")

    def test_subcase_required_for_remedy_dead_end(self) -> None:
        with self.assertRaises(ValueError):
            validate_issue_form(issue_form(signal_type="S-7", signal_subcase="wrong"))
        validate_issue_form(issue_form(signal_type="S-7", signal_subcase="no_remedy"))

    def test_subcase_is_rejected_when_the_signal_has_no_subcase_enum(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not accept signal_subcase"):
            validate_issue_form(
                issue_form(
                    signal_type="S-4",
                    signal_subcase="refresh_token=rt_live_XyZ987654321",
                )
            )

    def test_every_issue_narrative_field_applies_the_content_bar(self) -> None:
        for needle in ("refreshToken=AbCdEfGhIjKlMnOpQrStUvWx", "/secret.txt"):
            forms = []
            for field in ("codebase_objective", "hypothesis", "goal", "plan", "session_start"):
                forms.append(issue_form(**{field: needle}))
            for field in ("action_taken", "observed_result", "named_step"):
                form = issue_form()
                form["failure_point"][field] = needle
                forms.append(form)
            for field in ("action", "result", "code_context"):
                form = issue_form()
                form["recent_actions"][0][field] = needle
                forms.append(form)
            forms.append(issue_form(earlier_phases=[needle]))

            for form in forms:
                with self.subTest(needle=needle, form=form), self.assertRaises(ValueError):
                    validate_issue_form(form)

    def test_payload_in_narrative_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_issue_form(issue_form(hypothesis="dump: " + "DE AD BE EF " * 40))

    def test_real_code_names_are_allowed_in_narrative(self) -> None:
        validate_issue_form(
            issue_form(hypothesis="pwm_set_duty() in src/pwm.c clamps the value early.")
        )


class CheckInForm(unittest.TestCase):
    def test_valid_form_is_accepted(self) -> None:
        validated = validate_checkin_form(checkin_form())
        self.assertIn("work_summary", validated)

    def test_self_rating_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_checkin_form(
                checkin_form(effectiveness_observed="I did well and my performance was good.")
            )

    def test_missing_codebase_summary_is_rejected(self) -> None:
        form = checkin_form()
        del form["codebase_summary"]
        with self.assertRaises(ValueError):
            validate_checkin_form(form)

    def test_extra_field_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_checkin_form(checkin_form(severity="high"))

    def test_check_in_carries_no_issue_fields(self) -> None:
        """A health record must never be shaped like an issue."""

        validated = validate_checkin_form(checkin_form())
        for banned in ("severity", "signal_type", "grouping_key"):
            self.assertNotIn(banned, validated)

    def test_every_checkin_narrative_field_applies_the_content_bar(self) -> None:
        for needle in ("refreshToken=AbCdEfGhIjKlMnOpQrStUvWx", "/secret.txt"):
            forms = [
                checkin_form(codebase_summary=needle),
                checkin_form(work_summary=needle),
                checkin_form(effectiveness_observed=needle),
                checkin_form(tools_used=[{"tool": needle, "purpose": "build"}]),
                checkin_form(tools_used=[{"tool": "build", "purpose": needle}]),
            ]
            for form in forms:
                with self.subTest(needle=needle, form=form), self.assertRaises(ValueError):
                    validate_checkin_form(form)


class MonitorToolSurface(MonitorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.monitor = self.make_monitor()
        self.tools = build_monitor_tools(self.monitor)

    def test_exactly_three_actions_in_a_personal_build(self) -> None:
        self.assertEqual(set(self.tools), {REPORT_TOOL_NAME, HEALTH_TOOL_NAME, CHECKIN_TOOL_NAME})

    def test_none_of_them_take_a_board_id(self) -> None:
        """No board_id is what keeps them off per-board serialization."""

        import inspect

        for name, handler in self.tools.items():
            with self.subTest(tool=name):
                self.assertNotIn("board_id", inspect.signature(handler).parameters)

    def test_all_carry_usable_descriptions(self) -> None:
        for name, handler in self.tools.items():
            with self.subTest(tool=name):
                doc = handler.__doc__ or ""
                self.assertIn("When to reach for it", doc)
                self.assertGreater(len(doc), 200)

    def test_report_tool_states_the_classification_rule(self) -> None:
        doc = self.tools[REPORT_TOOL_NAME].__doc__ or ""
        self.assertIn("NOT reportable", doc)

    def test_health_check_is_side_effect_free(self) -> None:
        first = json.loads(self.tools[HEALTH_TOOL_NAME]())
        second = json.loads(self.tools[HEALTH_TOOL_NAME]())
        first.pop("uptime_seconds", None)
        second.pop("uptime_seconds", None)
        self.assertEqual(first, second)

    def test_health_check_emits_no_record_and_no_report(self) -> None:
        before = len(self.ledger_files()) + len(self.report_files())
        self.tools[HEALTH_TOOL_NAME]()
        self.tools[HEALTH_TOOL_NAME]()
        after = len(self.ledger_files()) + len(self.report_files())
        self.assertEqual(before, after)

    def test_health_check_declares_the_build_profile(self) -> None:
        payload = json.loads(self.tools[HEALTH_TOOL_NAME]())
        self.assertIn(payload["narrative_logging"], ("enabled", "not_built"))

    def test_report_submission_records_an_issue(self) -> None:
        self.monitor.bind_workspace(None)
        payload = json.loads(self.tools[REPORT_TOOL_NAME](**issue_form()))
        self.assertEqual(payload["status"], "report_recorded")
        self.assertTrue(self.report_files())

    def test_malformed_report_is_rejected_not_recorded(self) -> None:
        payload = json.loads(
            self.tools[REPORT_TOOL_NAME](
                signal_type="S-4",
                codebase_objective="x",
                hypothesis="y",
                goal="z",
                plan="p",
                failure_point={"action_taken": "a"},
                recent_actions=[],
                session_start="s",
            )
        )
        self.assertEqual(payload["status"], "report_rejected")
        self.assertFalse(self.report_files())

    def test_forbidden_narrative_never_reaches_any_delivery_artifact(self) -> None:
        self.monitor.bind_workspace(None)
        forbidden_values = (
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
            "password for the board: hunter2",
            'api_key "AbCdEfGhIjKlMnOpQrStUvWx"',
            "pa\ufe0fssword=hunter2",
            r"pa\u0073sword=hunter2",
            "pa&#115;sword=hunter2",
            "The token consisted of hunter2.",
            "The credential encoded as aHVudGVyMg==.",
            "The password used during board login was hunter2.",
            "/home/alice/private/fw/main.c failed",
            "/secret.txt",
            "Failed opening /firmware.elf",
            r"\Users\alice\private\fw\main.c failed",
            "Opened ⧸home⧸alice⧸private⧸firmware.elf.",
            "Opened C⦂⧹Users⧹alice⧹private⧹firmware.elf.",
            "Opened ∕home∕alice∕private∕fw.elf.",
            "%EF%BC%8F%EF%BD%88%EF%BD%8F%EF%BD%8D%EF%BD%85%EF%BC%8Fsecret",
            "file:///home/alice/private/fw/main.c failed",
            "file:%2F%2F%2Fhome%2Falice%2Fprivate%2Ffw.c failed",
            "%25252Fhome%25252Falice%25252Fprivate%25252Ffw%25252Fmain.c",
            "file:%25252F%25252F%25252Fhome%25252Falice%25252Fprivate%25252Ffw.c",
            "///home/alice/private/fw.c",
            "//home/alice/private/fw/main.c failed",
            "vscode://file/home/alice/private/fw/main.c failed",
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
            "The pаssword is hunter2",
            "UART stream: OK READY CUSTOMER=ACME-1234",
            "CAN frame: 123#DEADBEEFCAFEBABE",
            "Memory at 0x20000000 contained DEADBEEF",
            "mаke clean && mаke all",
            "serial stream: \x1b[31mSECRET\x1b[0m",
            "device stream: OK\x00SECRET",
            "ST-LINK S/N SYNTHETIC-PROBE-1234567890 selected",
            "UART produced OK READY CUSTOMER=ACME-1234",
            "UART returned an error; OK READY CUSTOMER=ACME-1234",
            "CAN packet 123#DEADBEEFCAFEBABE",
            "RAM at 0x20000000 held DEADBEEF",
            "Ran rm firmware.elf",
            "Board ID CUSTOMER-SECRET-1234 was selected.",
            "Probe S/N CUSTOMER-SECRET-1234 was selected.",
            "The password is hashed: hunter2",
            "password — hunter2",
            "Opened ~alice/customer/private/fw.elf",
            "UART yielded OK READY CUSTOMER=ACME-1234",
            "A CAN message was 123#DEADBEEFCAFEBABE",
            "RAM location 0x20000000 held DEADBEEF",
            "connection_id=stlink:CUSTOMER-SECRET-1234",
            "password hash hunter2",
            "password ‒ hunter2",
            "rm firmware.elf",
            "ar rcs libsecret.a customer.o",
            "UART contained OK READY CUSTOMER=ACME-9911",
            "RAM bytes: DE AD BE EF",
            "The frame on CAN carried 123#DEADBEEF",
            "password / hunter2",
            "p<!-- hidden -->assword=hunter2",
            "Board ID; CUSTOMER-SECRET-1234",
            "Probe serial, CUSTOMER-SECRET-1234",
            "custom-builder target release",
            "UART delivered OK READY CUSTOMER=ACME-9911",
            "UART capture: OK READY CUSTOMER=ACME-9911",
            "CAN traffic: 123#DEADBEEF",
            "RAM readout: DE AD BE EF",
            "password • hunter2",
            "password | hunter2",
            "credential set to hunter2",
            "Probe SN: CUSTOMER-SECRET-1234",
            "Board identifier: CUSTOMER-SECRET-1234",
            "sudo make clean all",
            "CC=gcc make all",
            "builder target release",
            "<!-- password=hunter2 -->",
            "<!-- UART returned OK READY CUSTOMER=ACME -->",
            "Safe summary <!-- Probe serial CUSTOMER-SECRET-1234 -->",
            "Safe summary <!-- sudo make clean all -->",
            "Probe serial. CUSTOMER-SECRET-1234",
            "Board ID unavailable. CUSTOMER-SECRET-1234 was observed.",
            r"password \ hunter2",
            "password's value is hunter2",
            "secret code: 1234",
            "PIN: 1234",
            "command custombuilder target",
            "time make clean all",
            "env MODE=prod custombuilder target",
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
            "Password SHA-256 was deadbeef123456.",
            "password ~ hunter2",
            "password (hunter2)",
            "password [hunter2]",
            "password contains hunter2",
            "The password has value hunter2.",
            "credential checksum deadbeef123456",
            "The identifier of the probe was CUSTOMER-SECRET-1234",
            "Hardware identifier: CUSTOMER-SECRET-1234",
            "custombuilder target release",
            "mytool build firmware",
            "nohup make clean all",
            "$ make clean all",
            "> make clean all",
            "./custombuilder target",
            "Probe ID helper leaked CUSTOMER-SECRET-1234; parser accepts formats.",
            "Board identifier parser exposed CUSTOMER-SECRET-1234 but now accepts formats.",
            "Credential digest parser exposed hunter2 and handles retries.",
            "UART response parser leaked OK READY CUSTOMER=ACME-9911.",
            "CAN frame parser leaked 123#DEADBEEF.",
            "make clean all because the handler handles failures",
            "git status; parser handles output",
            "ninja clean all while integration supports builds",
            "Probe ID unavailable. 123456789012",
            "Board serial redacted. 000123456789",
            "Probe ID unavailable. SECRETIDENTIFIER",
            "UART response parser accepts OK READY SECRET PAYLOAD",
            "CAN frame parser validates DEADBEEF",
            "Password hash parser accepts DEADBEEF",
            "Credential digest helper masks HUNTERTWO",
            "Next, make clean all",
            "Please run: make clean all",
            "make targets describes clean all",
            "foo-bar parser handles build target",
            "**password**: hunter2",
            "`password`: hunter2",
            "[password]: hunter2",
            "[password](https://example.invalid): hunter2",
            "(password): hunter2",
            "**UART** returned OK READY CUSTOMER=ACME-9911",
            "`UART` returned OK READY CUSTOMER=ACME-9911",
            "**RAM** held DEADBEEF",
            "**CAN** observed 123#DEADBEEF",
            "[UART] returned OK READY CUSTOMER=ACME-9911",
            "(UART) returned OK READY CUSTOMER=ACME-9911",
            "`custombuilder target release`",
            "**custombuilder target release**",
            "[custombuilder target release]",
            "(make clean all)",
        )
        for value in forbidden_values:
            with self.subTest(value=value):
                payload = json.loads(self.tools[REPORT_TOOL_NAME](**issue_form(hypothesis=value)))
                self.assertEqual(payload["status"], "report_rejected")
                self.assertNotIn(value, self.all_store_text())
        self.assertFalse(self.report_files())

    def test_forbidden_checkin_narrative_never_reaches_any_delivery_artifact(self) -> None:
        self.monitor.bind_workspace(None)
        for value in (
            'api_key "AbCdEfGhIjKlMnOpQrStUvWx"',
            "/secret.txt",
            "The UART RX: OK READY",
            "Ran `make clean all`",
        ):
            with self.subTest(value=value):
                payload = json.loads(
                    self.tools[CHECKIN_TOOL_NAME](**checkin_form(work_summary=value))
                )
                self.assertEqual(payload["status"], "checkin_rejected")
                self.assertNotIn(value, self.all_store_text())

    def test_model_named_step_never_becomes_a_mechanical_tool_identity(self) -> None:
        self.monitor.bind_workspace(None)
        model_step = "AcmeSafetyCalibration::loadCustomerTuning"
        form = issue_form()
        form["failure_point"]["named_step"] = model_step

        payload = json.loads(self.tools[REPORT_TOOL_NAME](**form))

        self.assertEqual(payload["status"], "report_recorded")
        report = self.read_report(self.report_files()[0])
        self.assertIsNone(report["tool_name"])
        report_records = [record for record in self.ledger_records() if record["kind"] == "report"]
        self.assertEqual(len(report_records), 1)
        self.assertIsNone(report_records[0]["detail"]["tool"])
        self.assertEqual(report["narrative"]["failure_point"]["named_step"], model_step)

    def test_model_prose_stays_only_in_the_narrative_section(self) -> None:
        self.monitor.bind_workspace(None)
        form = issue_form(
            goal="AcmeSafetyCalibration customer objective",
            hypothesis="AcmePrivateParser rejected a semantic node.",
        )

        payload = json.loads(self.tools[REPORT_TOOL_NAME](**form))

        self.assertEqual(payload["status"], "report_recorded")
        report = self.read_report(self.report_files()[0])
        self.assertNotIn("AcmeSafetyCalibration", report["title"])
        self.assertNotIn("AcmePrivateParser", report["description"])
        self.assertEqual(report["narrative"]["goal"], form["goal"])
        self.assertEqual(report["narrative"]["hypothesis"], form["hypothesis"])

    def test_invalid_subcase_never_reaches_any_delivery_artifact(self) -> None:
        self.monitor.bind_workspace(None)
        needle = "refresh_token=rt_live_XyZ987654321"
        payload = json.loads(
            self.tools[REPORT_TOOL_NAME](**issue_form(signal_type="S-4", signal_subcase=needle))
        )
        self.assertEqual(payload["status"], "report_rejected")
        self.assertNotIn(needle, self.all_store_text())
        self.assertFalse(self.report_files())

    def test_checkin_records_a_summary_not_an_issue(self) -> None:
        self.monitor.bind_workspace(None)
        payload = json.loads(self.tools[CHECKIN_TOOL_NAME](**checkin_form()))
        self.assertEqual(payload["status"], "checkin_recorded")
        self.assertIn("summary_id", payload)
        self.assertFalse(self.report_files())

    def test_submission_never_claims_recorded_when_body_persistence_fails(self) -> None:
        from unittest.mock import patch

        for tool_name, form, expected_status in (
            (REPORT_TOOL_NAME, issue_form(), "report_not_recorded"),
            (CHECKIN_TOOL_NAME, checkin_form(), "checkin_not_recorded"),
        ):
            with self.subTest(tool=tool_name):
                monitor = self.make_monitor(context=make_context(run_id=f"run-failed-{tool_name}"))
                monitor.bind_workspace(None)
                tools = build_monitor_tools(monitor)
                with (
                    patch.object(
                        monitor,
                        "_write_artifact_file",
                        return_value=ArtifactPersistence.FAILED,
                    ),
                    patch.object(monitor._delivery, "enqueue_report") as enqueue,
                ):
                    payload = json.loads(tools[tool_name](**form))
                self.assertEqual(payload["status"], expected_status)
                enqueue.assert_not_called()
                record_kind = "report" if tool_name == REPORT_TOOL_NAME else "checkin"
                records = [
                    record for record in self.ledger_records() if record["kind"] == record_kind
                ]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["detail"]["body_persistence"], "failed")
                self.assertFalse(records[0]["detail"]["body_persisted"])
                self.assertFalse(records[0]["detail"]["delivery_enqueued"])
                monitor.closeout("test")

    def test_submission_discloses_post_publish_persistence_uncertainty(self) -> None:
        from unittest.mock import patch

        for tool_name, form, expected_status in (
            (REPORT_TOOL_NAME, issue_form(), "report_recording_uncertain"),
            (CHECKIN_TOOL_NAME, checkin_form(), "checkin_recording_uncertain"),
        ):
            with self.subTest(tool=tool_name):
                monitor = self.make_monitor(
                    context=make_context(run_id=f"run-uncertain-{tool_name}")
                )
                monitor.bind_workspace(None)
                tools = build_monitor_tools(monitor)
                with (
                    patch.object(
                        monitor,
                        "_write_artifact_file",
                        return_value=ArtifactPersistence.UNCERTAIN,
                    ),
                    patch.object(monitor._delivery, "enqueue_report") as enqueue,
                ):
                    payload = json.loads(tools[tool_name](**form))
                self.assertEqual(payload["status"], expected_status)
                self.assertIn("may be recovered for delivery after restart", payload["message"])
                enqueue.assert_not_called()
                record_kind = "report" if tool_name == REPORT_TOOL_NAME else "checkin"
                records = [
                    record for record in self.ledger_records() if record["kind"] == record_kind
                ]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["detail"]["body_persistence"], "uncertain")
                self.assertFalse(records[0]["detail"]["body_persisted"])
                self.assertFalse(records[0]["detail"]["delivery_enqueued"])
                monitor.closeout("test")

    def test_post_publish_sync_failure_is_disclosed_before_restart_recovery(self) -> None:
        from unittest.mock import patch

        monitor = self.make_monitor(context=make_context(run_id="run-post-publish-uncertain"))
        monitor.bind_workspace(None)
        report_directory = self.server_data / monitor._workspace / "reports"
        report_directory.mkdir(parents=True, exist_ok=True)
        tools = build_monitor_tools(monitor)
        with (
            patch(
                "pyocd_debug_mcp.monitor.monitor._fsync_directory",
                side_effect=OSError("simulated post-publish sync failure"),
            ),
            patch.object(monitor._delivery, "enqueue_report") as enqueue,
        ):
            payload = json.loads(tools[REPORT_TOOL_NAME](**issue_form()))

        self.assertEqual(payload["status"], "report_recording_uncertain")
        self.assertIn("may be recovered for delivery after restart", payload["message"])
        enqueue.assert_not_called()
        records = [record for record in self.ledger_records() if record["kind"] == "report"]
        self.assertEqual(records[-1]["detail"]["body_persistence"], "uncertain")
        self.assertFalse(records[-1]["detail"]["delivery_enqueued"])

        with (
            patch(
                "pyocd_debug_mcp.monitor.monitor._fsync_directory",
                side_effect=OSError("simulated post-publish sync failure"),
            ),
            patch.object(monitor._delivery, "enqueue_report") as enqueue,
        ):
            checkin = json.loads(tools[CHECKIN_TOOL_NAME](**checkin_form()))
        self.assertEqual(checkin["status"], "checkin_recording_uncertain")
        self.assertIn("may be recovered for delivery after restart", checkin["message"])
        enqueue.assert_not_called()
        checkin_records = [
            record for record in self.ledger_records() if record["kind"] == "checkin"
        ]
        self.assertEqual(checkin_records[-1]["detail"]["body_persistence"], "uncertain")
        self.assertFalse(checkin_records[-1]["detail"]["delivery_enqueued"])

        monitor.closeout("simulated-restart")
        restarted = self.make_monitor(context=make_context(run_id="run-test-0002"))
        restarted.bind_workspace(None)
        recovered = self.simulated_remote / restarted._workspace / "reports"
        deadline = time.monotonic() + 2.0
        while (
            not list(recovered.glob("rpt-*.json")) or not list(recovered.glob("sum-*.json"))
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(list(recovered.glob("rpt-*.json")))
        self.assertTrue(list(recovered.glob("sum-*.json")))

    def test_monitor_tool_names_cover_the_surface(self) -> None:
        self.assertEqual(
            MONITOR_TOOL_NAMES,
            frozenset({REPORT_TOOL_NAME, HEALTH_TOOL_NAME, CHECKIN_TOOL_NAME}),
        )


class ProfessionalBuild(MonitorTestCase):
    """A build cut without narrative logging strips the model-authored layer."""

    def setUp(self) -> None:
        super().setUp()
        import pyocd_debug_mcp.monitor.tools as tools_module

        self._original = tools_module.NARRATIVE_LOGGING
        tools_module.NARRATIVE_LOGGING = False
        self.addCleanup(setattr, tools_module, "NARRATIVE_LOGGING", self._original)
        self.monitor = self.make_monitor()
        self.tools = build_monitor_tools(self.monitor)

    def test_check_in_is_absent_not_disabled(self) -> None:
        # Absent, because it is server-prompted: with no prompt the agent never
        # expects it, so a missing tool cannot be misfiled as a discovery failure.
        self.assertNotIn(CHECKIN_TOOL_NAME, self.tools)

    def test_report_tool_is_present_and_explains(self) -> None:
        self.assertIn(REPORT_TOOL_NAME, self.tools)
        payload = json.loads(self.tools[REPORT_TOOL_NAME](**issue_form()))
        self.assertEqual(payload["status"], "reporting_disabled")
        self.assertIn("professional license", payload["message"])

    def test_refused_report_stores_no_narrative(self) -> None:
        self.monitor.bind_workspace(None)
        self.tools[REPORT_TOOL_NAME](**issue_form())
        self.assertFalse(self.report_files())
        self.assertNotIn("closed-loop speed control", self.all_store_text())

    def test_health_check_still_available(self) -> None:
        self.assertIn(HEALTH_TOOL_NAME, self.tools)


if __name__ == "__main__":
    unittest.main()
