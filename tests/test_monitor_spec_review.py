"""Check internal consistency of the monitor review documentation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_evidence_privacy import (
    OPERATOR_PATH_PATTERNS,
    canonical_evidence_text,
    _contains_forbidden_identifier,
    _contains_forbidden_operator,
    privacy_violations,
    read_evidence_text,
    structured_identifier_violations,
    verify_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "sentry-evidence" / "plans" / "current-spec-implementation-review.md"
REMAINING = ROOT / "mcp-issue-monitor-remaining-work.md"


class CurrentSpecReviewTests(unittest.TestCase):
    def test_review_covers_every_spec_area_and_names_evidence(self) -> None:
        text = REVIEW.read_text(encoding="utf-8")
        self.assertIn("Review status: COMPLETE", text)
        for section in range(1, 10):
            self.assertIn(f"| §{section}", text)
        for evidence in (
            "tests/test_monitor_client_skill.py",
            "tests/test_monitor_ledger.py",
            "tests/test_monitor_wiring.py",
            "tests/test_monitor_delivery.py",
            "tests/test_can_server_contract.py",
        ):
            self.assertIn(evidence, text)
        self.assertIn("F-62/F-108/F-124 and AC-75 remote cutover pending", text)
        self.assertIn("F-27 (nonblocking", text)
        self.assertIn("F-60 (filler honesty", text)
        self.assertNotIn("F-27/F-60 remote cutover pending", text)

    def test_remaining_work_checklist_lists_only_real_remote_delivery(self) -> None:
        """This is a prose consistency check, not implementation evidence."""

        text = REMAINING.read_text(encoding="utf-8")
        open_items = [line for line in text.splitlines() if line.startswith("- [ ]")]
        self.assertEqual(len(open_items), 1)
        self.assertIn("OAuth/OpenID", open_items[0])
        self.assertIn("local", text.lower())
        self.assertIn("not off-box", text.lower())

    def test_checked_in_evidence_redacts_operator_paths_and_physical_probe_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            generated = Path(temporary) / "generated.log"
            generated.write_text("interpreter=/home/operator/private/python\n", encoding="utf-8")
            violations = privacy_violations((generated,))
        expected = f"{generated}: operator home/workspace path"
        self.assertEqual(violations, [expected])

    def test_evidence_sanitization_manifest_is_current(self) -> None:
        self.assertEqual(verify_manifest(), [])

    def test_evidence_reader_detects_unicode_encodings_and_escaped_paths(self) -> None:
        for label, encoding in (
            ("UTF-16 little endian without BOM", "utf-16-le"),
            ("UTF-16 big endian without BOM", "utf-16-be"),
            ("UTF-32 with BOM", "utf-32"),
            ("UTF-32 little endian without BOM", "utf-32-le"),
        ):
            with self.subTest(label):
                text = "C:" + r"\Users\operator\private\trace.log"
                temporary = ROOT / ".bomless-evidence-test"
                try:
                    temporary.write_bytes(text.encode(encoding))
                    decoded = read_evidence_text(temporary)
                finally:
                    temporary.unlink(missing_ok=True)
                self.assertEqual(decoded, text)

        with self.subTest("UTF-32 forbidden identifier remains detectable"):
            import hashlib
            from unittest.mock import patch

            candidate = "SYNTHETICIDENTIFIER123"
            digest = hashlib.sha256(candidate.casefold().encode()).hexdigest()
            temporary = ROOT / ".utf32-evidence-test"
            try:
                temporary.write_bytes(candidate.encode("utf-32-le"))
                decoded = read_evidence_text(temporary)
            finally:
                temporary.unlink(missing_ok=True)
            assert decoded is not None
            with patch(
                "scripts.verify_evidence_privacy.FORBIDDEN_IDENTIFIER_SHA256",
                frozenset({digest}),
            ):
                self.assertTrue(_contains_forbidden_identifier(decoded))

        with self.subTest("legacy UTF-16 assertion"):
            text = "C:" + r"\Users\operator\private\trace.log"
            self.assertTrue(any(pattern.search(text) for pattern in OPERATOR_PATH_PATTERNS))

        escaped = "%2Fhome%2F" + "operator%2Fprivate%2Ftrace.log"
        canonical = canonical_evidence_text(escaped)
        self.assertTrue(any(pattern.search(canonical) for pattern in OPERATOR_PATH_PATTERNS))

        deeply_escaped = "%252525252Fhome%252525252Foperator%252525252Ftrace.log"
        canonical = canonical_evidence_text(deeply_escaped)
        self.assertTrue(any(pattern.search(canonical) for pattern in OPERATOR_PATH_PATTERNS))

        root_path = "/" + "root/private/trace.log"
        self.assertTrue(any(pattern.search(root_path) for pattern in OPERATOR_PATH_PATTERNS))

    def test_fragmented_known_identifiers_are_detected(self) -> None:
        # The test never embeds the forbidden identifier itself. Find one only
        # from the repository's historical split-literal shape would recreate
        # the leak, so exercise the bounded concatenation generically by
        # patching the hash set at the module boundary.
        import hashlib
        from unittest.mock import patch

        candidate = "SYNTHETIC" + "IDENTIFIER123"
        digest = hashlib.sha256(candidate.casefold().encode()).hexdigest()
        with patch(
            "scripts.verify_evidence_privacy.FORBIDDEN_IDENTIFIER_SHA256",
            frozenset({digest}),
        ):
            self.assertTrue(_contains_forbidden_identifier('"SYNTHETIC" + "IDENTIFIER123"'))
            split = " + ".join(repr(character) for character in candidate)
            self.assertTrue(_contains_forbidden_identifier(split))
            commented = "\n".join(
                f"{character!r}  # synthetic separator" for character in candidate
            )
            self.assertTrue(_contains_forbidden_identifier(commented))
            self.assertTrue(_contains_forbidden_identifier(f"/* {candidate} */"))
            self.assertTrue(_contains_forbidden_identifier(f"// {candidate}"))
            self.assertTrue(_contains_forbidden_identifier(f"# {candidate}"))

    def test_known_operator_component_is_detected_without_a_home_prefix(self) -> None:
        import hashlib
        from unittest.mock import patch

        candidate = "syntheticoperator"
        digest = hashlib.sha256(candidate.encode()).hexdigest()
        with patch(
            "scripts.verify_evidence_privacy.FORBIDDEN_OPERATOR_COMPONENT_SHA256",
            frozenset({digest}),
        ):
            self.assertTrue(_contains_forbidden_operator(f"/tmp/{candidate}-python/bin/python"))
            split = " + ".join(repr(character) for character in candidate)
            self.assertTrue(_contains_forbidden_operator(split))
            commented = "".join(
                f"{character!r} /* synthetic separator */" for character in candidate
            )
            self.assertTrue(_contains_forbidden_operator(commented))
            self.assertTrue(_contains_forbidden_operator(f"/* {candidate} */"))
            self.assertTrue(_contains_forbidden_operator(f"// {candidate}"))
            self.assertTrue(_contains_forbidden_operator(f"# {candidate}"))

    def test_undecodable_text_like_evidence_fails_closed(self) -> None:
        from scripts.verify_evidence_privacy import _privacy_text

        temporary = ROOT / ".undecodable-evidence-test.log"
        try:
            temporary.write_bytes(b"operator path \xff")
            violations: list[str] = []
            self.assertIsNone(_privacy_text(temporary, violations))
        finally:
            temporary.unlink(missing_ok=True)
        self.assertTrue(violations)

    def test_structured_identifier_check_is_generic(self) -> None:
        unsafe = json.dumps({"probe_" + "uid": "physical-" + "identifier"})
        misleading = json.dumps({"probe_" + "id": "not-redacted-real-id"})
        unsafe_connection = json.dumps({"connection_" + "id": "customer-secret-hardware-1234"})
        unsafe_prefixes = (
            "synthetic-customer-secret-hardware-1234",
            "redacted-customer-secret-hardware-1234",
            "probe:synthetic-customer-secret-hardware-1234",
        )
        safe_connection = json.dumps({"connection_" + "id": "probe:redacted-stlink-probe"})
        safe = json.dumps({"usb_" + "serial": "synthetic-probe-a"})
        path = Path("evidence.json")
        self.assertEqual(
            structured_identifier_violations(path, unsafe),
            ["non-redacted probe_uid"],
        )
        self.assertEqual(
            structured_identifier_violations(path, misleading),
            ["non-redacted probe_id"],
        )
        self.assertEqual(
            structured_identifier_violations(path, unsafe_connection),
            ["non-redacted connection_id"],
        )
        prefixed = "2026-10-10 INFO " + unsafe
        self.assertEqual(
            structured_identifier_violations(Path("evidence.log"), prefixed),
            ["non-redacted probe_uid"],
        )
        for value in unsafe_prefixes:
            with self.subTest(value=value):
                encoded = json.dumps({"connection_id": value})
                self.assertEqual(
                    structured_identifier_violations(Path("evidence.log"), encoded),
                    ["non-redacted connection_id"],
                )
        self.assertEqual(structured_identifier_violations(path, safe_connection), [])
        self.assertEqual(structured_identifier_violations(path, safe), [])
        self.assertEqual(
            structured_identifier_violations(
                path, json.dumps({"probe-uid": "customer-secret-hardware-1234"})
            ),
            ["non-redacted probe-uid"],
        )
        self.assertEqual(
            structured_identifier_violations(
                path, json.dumps({"probeUid": "customer-secret-hardware-1234"})
            ),
            ["non-redacted probeUid"],
        )
        self.assertEqual(
            structured_identifier_violations(
                path,
                json.dumps({"message": json.dumps({"probe_uid": "customer-secret-hardware-1234"})}),
            ),
            ["non-redacted probe_uid"],
        )
        for key in ("board_id", "boardUid", "mcu_uid", "stlink_serial", "probeSerialNumber"):
            with self.subTest(key=key):
                self.assertEqual(
                    structured_identifier_violations(
                        path, json.dumps({key: "customer-secret-hardware-1234"})
                    ),
                    [f"non-redacted {key}"],
                )
        self.assertEqual(
            structured_identifier_violations(
                path,
                json.dumps(
                    {
                        "message": "INFO "
                        + json.dumps({"probe_uid": "customer-secret-hardware-1234"})
                    }
                ),
            ),
            ["non-redacted probe_uid"],
        )
        self.assertEqual(
            structured_identifier_violations(path, "probe_uid=customer-secret-hardware-1234"),
            ["non-redacted probe_uid"],
        )
        for suffix in (".dat", ""):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    structured_identifier_violations(
                        Path("capture" + suffix), "probe_uid=customer-secret-hardware-1234"
                    ),
                    ["non-redacted probe_uid"],
                )
        for key in ("probeID", "ProbeID"):
            with self.subTest(key=key):
                self.assertEqual(
                    structured_identifier_violations(
                        Path("capture"), f"{key}=customer-secret-hardware-1234"
                    ),
                    [f"non-redacted {key}"],
                )
        for prefix in ("- ", "  - "):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    structured_identifier_violations(
                        Path("capture"),
                        f"{prefix}probe_uid: customer-secret-hardware-1234",
                    ),
                    ["non-redacted probe_uid"],
                )
        self.assertEqual(
            structured_identifier_violations(
                Path("capture"), "- board_id: Stable logical board identifier."
            ),
            [],
        )
        for unsafe_schema in (
            "- board_id: Stable logical board identifier CUSTOMER-SECRET-1234",
            "- board_id: Stable logical board identifier. CUSTOMER-SECRET-1234",
            "x - board_id: Stable logical board identifier.",
        ):
            with self.subTest(unsafe_schema=unsafe_schema):
                self.assertEqual(
                    structured_identifier_violations(Path("capture"), unsafe_schema),
                    ["non-redacted board_id"],
                )
        for unsafe_value in (
            'probe_uid="redacted-stlink-probe CUSTOMER-SECRET-1234"',
            "probe_uid='redacted-stlink-probe CUSTOMER-SECRET-1234'",
            "probe_uid=redacted-stlink-probe CUSTOMER-SECRET-1234",
            "probe_uid=redacted-stlink-probe 123456789012",
            "probe_uid=redacted-stlink-probe CUSTOMERSECRET",
            "probe_uid=redacted-stlink-probe ABCD-EFGH",
            "probe_uid=redacted-stlink-probe ABCDEF123456",
        ):
            with self.subTest(unsafe_value=unsafe_value):
                self.assertEqual(
                    structured_identifier_violations(Path("capture"), unsafe_value),
                    ["non-redacted probe_uid"],
                )


if __name__ == "__main__":
    unittest.main()
