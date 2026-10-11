"""Frozen startup/profile/CAN-gate contracts for the BYO CAN increment."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp.can_config import load_can_fixture_config
from pyocd_debug_mcp.can_errors import CanConfigError, CanPolicyError
from pyocd_debug_mcp.can_startup import CanMode, load_can_startup
from pyocd_debug_mcp.can_secure_files import read_private_file
from pyocd_debug_mcp.guardrails.can_gate import CanGate

ROOT = Path(__file__).resolve().parents[1]
RX_PROFILE = ROOT / "fixtures" / "can" / "virtual_rx_only.yaml"
TX_PROFILE = ROOT / "fixtures" / "can" / "virtual_tx_allowed.yaml"
OFF_CANONICAL = (
    b'{"effective_read_seconds":null,"mode":"off","profile_sha256":"unstaged",'
    b'"schema_version":1,"source_sha256":"unstaged"}'
)
OFF_DIGEST = "84efea239efe7672a0a8605dfe49f661382d399d653dec839bcdb475383eb625"
RX_PROFILE_DIGEST = "a41ec9421626d7f85b4c977f510a906cc77f3b4f14f76e1125f363102ec6ce73"
TX_PROFILE_DIGEST = "ba1dfcea5ae02d60d298caadda10806c928b69694d825e5336f263263a900fd3"
DERIVED_FILES = (
    "src/pyocd_debug_mcp/adapters/can_interface.py",
    "src/pyocd_debug_mcp/adapters/can_pythoncan.py",
    "src/pyocd_debug_mcp/adapters/can_socketcan.py",
    "src/pyocd_debug_mcp/can_capabilities.py",
    "src/pyocd_debug_mcp/can_config.py",
    "src/pyocd_debug_mcp/can_errors.py",
    "src/pyocd_debug_mcp/can_secure_files.py",
    "src/pyocd_debug_mcp/can_startup.py",
    "src/pyocd_debug_mcp/guardrails/can_gate.py",
    "src/pyocd_debug_mcp/services/can_worker.py",
    "src/pyocd_debug_mcp/services/can_session.py",
)


class CanStartupTests(unittest.TestCase):
    def test_off_contract_has_frozen_canonical_bytes_and_digest(self) -> None:
        loaded = load_can_startup({})
        self.assertIs(loaded.runtime.mode, CanMode.OFF)
        self.assertIsNone(loaded.fixture)
        self.assertEqual(loaded.runtime.canonical_bytes, OFF_CANONICAL)
        self.assertEqual(loaded.runtime.canonical_sha256, OFF_DIGEST)
        self.assertEqual(hashlib.sha256(OFF_CANONICAL).hexdigest(), OFF_DIGEST)

    def test_available_profile_is_snapshotted_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            loaded = load_can_startup(
                {"PYOCD_CAN_MODE": "available", "PYOCD_CAN_CONFIG": str(RX_PROFILE)},
                snapshot_dir=Path(temporary),
            )
        self.assertIs(loaded.runtime.mode, CanMode.AVAILABLE)
        self.assertIsNotNone(loaded.fixture)
        assert loaded.fixture is not None
        self.assertEqual(loaded.runtime.source_sha256, loaded.fixture.source_sha256)
        self.assertEqual(loaded.runtime.profile_sha256, loaded.fixture.canonical_sha256)
        self.assertEqual(loaded.runtime.effective_read_seconds, 0.25)
        self.assertIsNotNone(loaded.fixture.snapshot_path)

    def test_available_mode_without_can_extra_fails_with_install_remedy(self) -> None:
        with (
            patch("pyocd_debug_mcp.can_startup.importlib.util.find_spec", return_value=None),
            self.assertRaisesRegex(CanConfigError, r"pyocd-debug-mcp\[can\]"),
        ):
            load_can_startup({"PYOCD_CAN_MODE": "available", "PYOCD_CAN_CONFIG": str(RX_PROFILE)})

    def test_non_linux_configured_can_is_rejected_before_profile_read(self) -> None:
        for platform in ("win32", "cygwin", "msys", "darwin"):
            with (
                self.subTest(platform=platform),
                patch("pyocd_debug_mcp.can_config.sys.platform", platform),
                patch(
                    "pyocd_debug_mcp.can_config._read_profile_once",
                    side_effect=AssertionError("profile path must not be opened off Linux"),
                ),
                self.assertRaisesRegex(CanConfigError, "qualified Linux hosts"),
            ):
                load_can_startup(
                    {"PYOCD_CAN_MODE": "available", "PYOCD_CAN_CONFIG": str(RX_PROFILE)}
                )

    @unittest.skipUnless(sys.platform.startswith("win"), "native Windows CAN startup guard")
    def test_native_windows_configured_can_is_disabled(self) -> None:
        with self.assertRaisesRegex(CanConfigError, "qualified Linux hosts"):
            load_can_startup({"PYOCD_CAN_MODE": "available", "PYOCD_CAN_CONFIG": str(RX_PROFILE)})

    def test_off_rejects_authority_bearing_values(self) -> None:
        for name, value in (
            ("PYOCD_CAN_CONFIG", str(RX_PROFILE)),
            ("PYOCD_CAN_CONFIG_SHA256", "0" * 64),
            ("PYOCD_CAN_READ_SECONDS", "1"),
            ("PYOCD_FIXTURE_EVIDENCE_CONTRACT", "legacy"),
            ("PYOCD_FIXTURE_EVIDENCE_CONTRACT_SHA256", "0" * 64),
            ("PYOCD_CAN_REQUIRED_OUTCOME", "correlated_success"),
        ):
            with self.subTest(name=name), self.assertRaises(CanConfigError):
                load_can_startup({"PYOCD_CAN_MODE": "off", name: value})

    def test_required_and_unknown_can_modes_are_rejected(self) -> None:
        for mode in ("required", "invalid", " off "):
            with self.subTest(mode=mode), self.assertRaises(CanConfigError):
                load_can_startup({"PYOCD_CAN_MODE": mode})

    def test_whitespace_only_unknown_and_legacy_environment_are_rejected(self) -> None:
        for name in (
            "PYOCD_CAN_UNUSED",
            "PYOCD_FIXTURE_EVIDENCE_CONTRACT",
        ):
            with self.subTest(name=name), self.assertRaises(CanConfigError):
                load_can_startup({name: " "})

    def test_runtime_digest_must_match(self) -> None:
        with self.assertRaises(CanConfigError):
            load_can_startup({"PYOCD_CAN_RUNTIME_SHA256": "0" * 64})
        loaded = load_can_startup({"PYOCD_CAN_RUNTIME_SHA256": OFF_DIGEST})
        self.assertEqual(loaded.runtime.canonical_sha256, OFF_DIGEST)

    def test_rejected_runtime_contract_does_not_leave_a_profile_snapshot(self) -> None:
        rejected_environments = (
            {"PYOCD_CAN_READ_SECONDS": "999"},
            {"PYOCD_CAN_RUNTIME_SHA256": "0" * 64},
        )
        for overrides in rejected_environments:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as temporary:
                snapshot_dir = Path(temporary) / "snapshot"
                environment = {
                    "PYOCD_CAN_MODE": "available",
                    "PYOCD_CAN_CONFIG": str(RX_PROFILE),
                    **overrides,
                }

                with self.assertRaises(CanConfigError):
                    load_can_startup(environment, snapshot_dir=snapshot_dir)

                self.assertFalse(snapshot_dir.exists())

    def test_source_hash_mismatch_and_symlink_are_rejected(self) -> None:
        with self.assertRaises(CanConfigError):
            load_can_startup(
                {
                    "PYOCD_CAN_MODE": "available",
                    "PYOCD_CAN_CONFIG": str(RX_PROFILE),
                    "PYOCD_CAN_CONFIG_SHA256": "0" * 64,
                }
            )
        with tempfile.TemporaryDirectory() as temporary:
            linked = Path(temporary) / "linked.yaml"
            try:
                os.symlink(RX_PROFILE, linked)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(CanConfigError):
                load_can_startup({"PYOCD_CAN_MODE": "available", "PYOCD_CAN_CONFIG": str(linked)})

    @unittest.skipIf(os.name == "nt", "POSIX directory-fd no-follow contract")
    def test_profile_below_symlinked_ancestor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            linked_parent = root / "profiles"
            linked_parent.symlink_to(RX_PROFILE.parent, target_is_directory=True)

            with self.assertRaises(CanConfigError):
                load_can_fixture_config(linked_parent / RX_PROFILE.name)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO coverage")
    def test_profile_and_private_reads_reject_fifos_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "profile.yaml"
            os.mkfifo(profile, 0o600)
            with self.assertRaises(CanConfigError):
                load_can_fixture_config(profile)

            private = root / "private.bin"
            os.mkfifo(private, 0o600)
            with self.assertRaisesRegex(OSError, "not a regular file"):
                read_private_file(private, maximum_bytes=1024)

    @unittest.skipIf(os.name == "nt", "POSIX directory-fd no-follow contract")
    def test_snapshot_below_symlinked_ancestor_cannot_escape(self) -> None:
        with (
            tempfile.TemporaryDirectory() as project_text,
            tempfile.TemporaryDirectory() as outside_text,
        ):
            project = Path(project_text)
            outside = Path(outside_text)
            (project / ".firm").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(CanConfigError):
                load_can_fixture_config(
                    RX_PROFILE,
                    snapshot_dir=project / ".firm" / "can-profiles",
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_physical_profile_fails_startup(self) -> None:
        raw = RX_PROFILE.read_text(encoding="utf-8").replace(
            "kind: virtual\n  channel: byo-rx-only",
            "kind: socketcan\n  channel: can0\n  identity:\n"
            "    driver: gs_usb\n    usb_vid: 0x1d50\n    usb_pid: 0x606f\n"
            "    serial: adapter-1",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "physical.yaml"
            path.write_text(raw, encoding="utf-8")
            snapshot_dir = Path(temporary) / "snapshot"
            with self.assertRaises(CanConfigError):
                load_can_startup(
                    {"PYOCD_CAN_MODE": "available", "PYOCD_CAN_CONFIG": str(path)},
                    snapshot_dir=snapshot_dir,
                )
            self.assertFalse(snapshot_dir.exists())


class CanGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rx = load_can_fixture_config(RX_PROFILE)
        cls.tx = load_can_fixture_config(TX_PROFILE)

    def test_rx_only_profile_refuses_transmit(self) -> None:
        with self.assertRaisesRegex(CanPolicyError, "can/tx-disabled"):
            CanGate(self.rx.config).validate_transmit_request(
                0x123, False, "00", timeout_seconds=None
            )

    def test_tx_profile_accepts_exact_classical_frame(self) -> None:
        request = CanGate(self.tx.config).validate_transmit_request(
            0x123, False, "CA FE", timeout_seconds=None
        )
        self.assertEqual(request.identifier.arbitration_id, 0x123)
        self.assertEqual(request.data, bytes.fromhex("CAFE"))

    def test_tx_profile_refuses_wrong_id_and_oversized_payload(self) -> None:
        gate = CanGate(self.tx.config)
        with self.assertRaisesRegex(CanPolicyError, "can/id-forbidden"):
            gate.validate_transmit_request(0x124, False, "", timeout_seconds=None)
        with self.assertRaisesRegex(CanPolicyError, "can/invalid-argument"):
            gate.validate_transmit_request(
                0x123, False, "00 01 02 03 04 05 06 07 08", timeout_seconds=None
            )

    def test_profile_canonical_hash_is_stable(self) -> None:
        again = load_can_fixture_config(TX_PROFILE)
        self.assertEqual(self.tx.canonical_sha256, again.canonical_sha256)
        self.assertEqual(self.rx.canonical_sha256, RX_PROFILE_DIGEST)
        self.assertEqual(self.tx.canonical_sha256, TX_PROFILE_DIGEST)
        self.assertIsNone(self.rx.config.health_check)
        self.assertIsNotNone(self.tx.config.health_check)
        document = json.loads(json.dumps(self.tx.summary.as_dict(), sort_keys=True))
        self.assertEqual(document["profile_id"], "virtual_tx_allowed")


class CanProvenanceTests(unittest.TestCase):
    def test_derived_sources_and_distribution_notices_are_present(self) -> None:
        self.assertTrue((ROOT / "LICENSES" / "Apache-2.0.txt").is_file())
        notice = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("Firmware-CLI", notice)
        self.assertIn("0e8a45d", notice)
        for relative in DERIVED_FILES:
            with self.subTest(relative=relative):
                first_lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()[:2]
                self.assertIn("SPDX-License-Identifier: Apache-2.0", first_lines[0])
                self.assertIn("Firmware-CLI", first_lines[1])
                self.assertIn("0e8a45d", first_lines[1])

        project_metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"python-can==4.6.1"', project_metadata)
        self.assertIn(
            "\"pyroute2==0.9.6; sys_platform == 'linux'\"",
            project_metadata,
        )


if __name__ == "__main__":
    unittest.main()
