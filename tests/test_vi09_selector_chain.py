"""ROOT's four VI09 findings, exercised above the server token/native boundary."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pyocd.core.session import Session
from pyocd.probe.aggregator import PROBE_CLASSES, DebugProbeAggregator
from pyocd.probe.stlink_probe import STLinkUSBInterface, StlinkProbe
from pyocd.probe.tcp_client_probe import TCPClientProbe

from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.adapters.swd_process import ProcessIsolatedSWDInterface
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.capabilities.policy import Tier
from pyocd_debug_mcp.hardware_inventory import (
    ActiveConnectionRow,
    HardwareInventoryService,
    ProbeSelectionStore,
    SelectionDisappeared,
)
from pyocd_debug_mcp.pack_provision import load_manifest
from pyocd_debug_mcp.probe_inventory import (
    NativeProbeListing,
    ProbeInfo,
    provider_qualified_probe_uid,
)
from pyocd_debug_mcp.services.connections import probe_connection_id
from pyocd_debug_mcp.setup_flow.device_support import (
    derive_candidate_binding,
    resolve_builtin_target_support,
)
from pyocd_debug_mcp.setup_flow.preflight import PreflightDecision, SetupUserInput
from pyocd_debug_mcp.setup_flow.packs import PackCandidate
from pyocd_debug_mcp.setup_flow.setup import SetupPhaseContext
from pyocd_debug_mcp.target_errors import ProbeNotFoundError, TargetConnectionError
from tests.tiered_acceptance_support import isolated_project, tiered_test_server
from tests.vi09_provider_fixture import (
    CMSISDAPProbe,
    EVENTS,
    JLinkProbe,
    MetadataAdapter,
    STLinkProbe,
)

OBSERVATIONS = {}
FIXTURE_FIRM = (
    Path(__file__).resolve().parents[1] / "fallback-support-evidence/results/hil-artifacts/.firm"
)


def listing(probes):
    return NativeProbeListing(tuple(probes), ("simulated-discovery",), 0, False, "", "")


def board(family="cmsisdap", target="cortex_m"):
    return BoardConfig(
        board_id="vi09_board",
        display_name="VI09 board",
        mcu_family="simulated",
        probe_family=family,
        pyocd_target=target,
        probe_type=family,
        probe_hint_terms=(),
        serial_hint_terms=(),
        test_addr=None,
    )


class SelectorChain(unittest.TestCase):
    def setUp(self):
        EVENTS.clear()
        for provider in (STLinkProbe, CMSISDAPProbe, JLinkProbe):
            provider.devices = []
        self.enterContext(
            patch.dict(
                PROBE_CLASSES,
                {
                    "stlink": STLinkProbe,
                    "cmsisdap": CMSISDAPProbe,
                    "jlink": JLinkProbe,
                },
                clear=True,
            )
        )
        self.details = {}
        self.addCleanup(self.retain)

    def retain(self):
        OBSERVATIONS[self.id()] = {"events": list(EVENTS), "details": self.details}

    def shared(self, uid="SHARED"):
        first = STLinkProbe(uid, "ST-Link simulated endpoint")
        chosen = CMSISDAPProbe(uid, "CMSIS-DAP simulated endpoint")
        STLinkProbe.devices = [first]
        CMSISDAPProbe.devices = [chosen]
        return first, chosen

    def assert_zero_open(self):
        self.assertFalse(any(e["kind"] in {"probe_open", "reset"} for e in EVENTS), EVENTS)

    @contextlib.contextmanager
    def actual_stlink(self, uids):
        devices = [
            SimpleNamespace(
                serial_number=uid,
                vendor_name="ST",
                product_name="ST-Link simulated USB",
            )
            for uid in uids
        ]
        with (
            patch.dict(PROBE_CLASSES, {"stlink": StlinkProbe}, clear=True),
            patch.object(
                STLinkUSBInterface,
                "get_all_connected_devices",
                return_value=devices,
            ),
        ):
            yield devices

    def test_actual_stlink_exact_id_precedes_containing_ids(self):
        with self.actual_stlink(["ABC", "ABC-SECOND"]) as devices:
            self.assertEqual(
                [p.unique_id for p in DebugProbeAggregator.get_all_connected_probes("stlink:ABC")],
                ["ABC"],
            )
            chosen = swd_pyocd._choose_unique_probe("stlink:ABC")
            self.assertIs(chosen._link._device, devices[0])

    def test_actual_stlink_duplicate_exact_refuses_before_session(self):
        with (
            self.actual_stlink(["ABC", "ABC"]),
            patch.object(swd_pyocd, "Session", wraps=Session) as constructor,
        ):
            with self.assertRaisesRegex(TargetConnectionError, "ambiguous"):
                swd_pyocd.PyOCDSWDInterface._choose_session(probe_uid="stlink:ABC", options={})
            constructor.assert_not_called()

    def test_actual_stlink_true_partial_collision_refuses(self):
        with self.actual_stlink(["FIRST-ABC", "SECOND-ABC"]):
            with self.assertRaisesRegex(TargetConnectionError, "ambiguous"):
                swd_pyocd._choose_unique_probe("stlink:ABC")

    def test_corrected_shared_exact_oracle_selects_before_partial(self):
        exact = STLinkProbe("SHARED")
        STLinkProbe.devices = [exact, STLinkProbe("SHARED-SECOND")]
        self.assertIs(swd_pyocd._choose_unique_probe("stlink:SHARED"), exact)
        self.assertFalse(any(e["kind"] == "fast_lookup" for e in EVENTS))

    def test_shared_true_partial_control_still_refuses_zero_open(self):
        STLinkProbe.devices = [STLinkProbe("SHARED"), STLinkProbe("SHARED-SECOND")]
        with patch.object(swd_pyocd, "Session", wraps=Session) as constructor:
            with self.assertRaisesRegex(TargetConnectionError, "ambiguous"):
                swd_pyocd.PyOCDSWDInterface._choose_session(probe_uid="stlink:SHAR", options={})
            constructor.assert_not_called()
        self.assert_zero_open()

    def test_selected_and_unselected_is_exact_success(self):
        selected = STLinkProbe("SELECTED", "ST-Link selected")
        STLinkProbe.devices = [STLinkProbe("UNSELECTED", "ST-Link other"), selected]
        with self.chain() as (_, server):
            token = self.publish(server, "stlink", "SELECTED")
            server.assignment_store.replace({token: "vi09_board"})
            server.connect("vi09_board")
            self.assertIs(
                server.connection_manager.handle_for("vi09_board").session.probe, selected
            )
            self.assertEqual([p.opens for p in STLinkProbe.devices], [0, 1])

    def test_plural_filter_may_be_ignored_and_unique_partial_remains_case_insensitive(self):
        selected = STLinkProbe("FIRST-AbC")
        STLinkProbe.devices = [STLinkProbe("OTHER"), selected]
        self.assertIs(swd_pyocd._choose_unique_probe("STLINK:abc"), selected)
        self.assertIsNone(swd_pyocd._choose_unique_probe("stlink:absent"))

    def test_enumerates_later_provider_before_exact_acceptance(self):
        self.shared()
        with self.assertRaisesRegex(TargetConnectionError, "ambiguous"):
            swd_pyocd._choose_unique_probe("SHARED")
        self.assertEqual(
            [e["provider"] for e in EVENTS if e["kind"] == "enumerate"],
            ["stlink", "cmsisdap", "jlink"],
        )
        self.assert_zero_open()

    def test_repeated_same_object_coalesces_but_distinct_equal_objects_refuse(self):
        probe = STLinkProbe("ABC")
        STLinkProbe.devices = [probe, probe]
        self.assertIs(swd_pyocd._choose_unique_probe("ABC"), probe)
        STLinkProbe.devices = [probe, STLinkProbe("ABC")]
        with self.assertRaisesRegex(TargetConnectionError, "ambiguous"):
            swd_pyocd._choose_unique_probe("ABC")
        self.assert_zero_open()

    def test_tcp_provider_normal_default_and_padded_ports(self):
        with (
            patch.dict(PROBE_CLASSES, {"remote": TCPClientProbe}, clear=True),
            patch("pyocd.probe.tcp_client_probe.ClientSocket") as socket,
        ):
            for selector, expected in (
                ("remote:review.invalid:5555", "remote:review.invalid:5555"),
                ("remote:review.invalid", "remote:review.invalid:5555"),
                ("remote:review.invalid:05555", "remote:review.invalid:5555"),
                ("remote:review.invalid:01234", "remote:review.invalid:1234"),
            ):
                with self.subTest(selector=selector):
                    probe = swd_pyocd._choose_unique_probe(selector)
                    self.assertIsInstance(probe, TCPClientProbe)
                    self.assertEqual(probe.unique_id, expected)
            socket.return_value.connect.assert_not_called()

    def test_provider_selector_preserves_opaque_uid_and_existing_qualification(self):
        for provider, uid, expected in (
            ("remote", "remote:host.invalid:05555", "remote:host.invalid:05555"),
            ("hook_provider", "HOOK_PROVIDER:3-1.4:1.0%/opaque", "HOOK_PROVIDER:3-1.4:1.0%/opaque"),
            ("stlink", "cmsisdap:3-1.4:1.0%/opaque", "stlink:cmsisdap:3-1.4:1.0%/opaque"),
        ):
            self.assertEqual(provider_qualified_probe_uid(provider, uid), expected)

    @contextlib.contextmanager
    def chain(self, *, process=False, config=None):
        with isolated_project() as project, tiered_test_server(project) as server:
            events = project / "child-events.jsonl"
            env = (
                {"VI09_CHILD_CONFIG": json.dumps(config or {}), "VI09_CHILD_EVENTS": str(events)}
                if process
                else {}
            )
            backend = (
                ProcessIsolatedSWDInterface(
                    worker_argv=[
                        sys.executable,
                        "-B",
                        str(Path(__file__).with_name("vi09_provider_worker.py")),
                    ]
                )
                if process
                else MetadataAdapter()
            )
            server.configure_tiered_test_seams(backend=backend)

            def cli(_command):
                probes = STLinkProbe.devices + CMSISDAPProbe.devices + JLinkProbe.devices
                return (
                    0,
                    "".join(
                        f"{i}  {p.description}  {p.uid}\n" for i, p in enumerate(probes) if p.uid
                    ),
                    "",
                )

            with (
                patch.dict(os.environ, env),
                patch.object(server, "_run_cmd", side_effect=cli),
                patch.object(
                    server._hardware_inventory,
                    "native_uarts",
                    return_value=[],
                ),
                patch.object(server._hardware_inventory, "vendor_uarts", return_value=()),
                patch.object(
                    server._hardware_inventory,
                    "remote_probes",
                    return_value=(),
                ),
            ):
                try:
                    yield project, server
                finally:
                    for name in server.connection_manager.assigned_board_ids():
                        server.disconnect(name)
                    if events.exists():
                        child = [json.loads(line) for line in events.read_text().splitlines()]
                        self.details["child_events"] = child
                        for item in child:
                            if item["kind"] == "probe_open":
                                self.assertFalse(Path(item["project_dir"]).exists())

    def publish(self, server, provider="cmsisdap", uid="SHARED"):
        overview = server._setup_overview(None)
        token = probe_connection_id(provider, uid)
        self.details["connections"] = overview["connections"]
        self.assertIn(token, [r["connection_id"] for r in overview["connections"]])
        self.assertEqual(server._probe_selection_store.recorded(token).provider, provider)
        return token

    def test_server_issued_choice_preserves_provider_through_both_exits_and_public_connect(self):
        first, chosen = self.shared()
        with self.chain() as (_, server):
            token = self.publish(server)
            server.assignment_store.replace({token: "vi09_board"})
            self.assertEqual(
                server._assigned_probe_uid_for_connect("vi09_board"), "cmsisdap:SHARED"
            )
            self.assertEqual(server._resolved_probe_uid_for_connection(token), "cmsisdap:SHARED")
            server.connect("vi09_board")
            handle = server.connection_manager.handle_for("vi09_board")
            self.assertIsInstance(handle.session, Session)
            self.assertIs(handle.session.probe, chosen)
            self.assertEqual(handle.metadata.probe_family, "cmsisdap")
            self.assertEqual((first.opens, chosen.opens), (0, 1))
            self.assertEqual(
                [e["provider"] for e in EVENTS if e["kind"] == "enumerate"], ["cmsisdap"]
            )

    def test_server_issued_choice_survives_actual_serialized_child(self):
        self.shared()
        with self.chain(process=True, config={"stlink": ["SHARED"], "cmsisdap": ["SHARED"]}) as (
            _,
            server,
        ):
            token = self.publish(server)
            server.assignment_store.replace({token: "vi09_board"})
            server.connect("vi09_board")
            handle = server.connection_manager.handle_for("vi09_board")
            self.assertEqual(handle.metadata.probe_family, "cmsisdap")
            self.assertEqual(handle.metadata.probe_uid, "SHARED")
            self.assertIsNotNone(handle.worker)
        child = self.details["child_events"]
        self.assertEqual([e["provider"] for e in child if e["kind"] == "enumerate"], ["cmsisdap"])
        self.assertEqual(
            [(e["provider"], e["uid"]) for e in child if e["kind"] == "probe_open"],
            [("cmsisdap", "SHARED")],
        )
        self.assertFalse(any(e["kind"] == "fast_lookup" for e in child))
        self.assertTrue(
            all(
                e["auto_unlock"] is False and e["no_config"] is True and e["user_script"] is None
                for e in child
                if e["kind"] == "probe_open"
            )
        )
        self.assertEqual(len([e for e in child if e["kind"] == "probe_close"]), 1)

    def test_actual_child_duplicate_exact_refuses_without_open(self):
        with (
            isolated_project() as project,
            patch.dict(
                os.environ,
                {
                    "VI09_CHILD_CONFIG": json.dumps({"stlink": ["ABC", "ABC"]}),
                    "VI09_CHILD_EVENTS": str(project / "child.jsonl"),
                },
            ),
        ):
            backend = ProcessIsolatedSWDInterface(
                worker_argv=[
                    sys.executable,
                    "-B",
                    str(Path(__file__).with_name("vi09_provider_worker.py")),
                ]
            )
            with self.assertRaisesRegex(TargetConnectionError, "ambiguous"):
                backend.open(
                    board=None,
                    unique_id="stlink:ABC",
                    target="cortex_m",
                    operation_timeout_seconds=45,
                )
            child = [
                json.loads(line) for line in (project / "child.jsonl").read_text().splitlines()
            ]
            self.details["child_events"] = child
            self.assertFalse(any(e["kind"] in {"probe_open", "reset"} for e in child))

    def test_new_duplicate_after_recording_refuses_before_native_enumeration(self):
        self.shared()
        with self.chain() as (_, server):
            token = self.publish(server)
            server.assignment_store.replace({token: "vi09_board"})
            CMSISDAPProbe.devices.append(CMSISDAPProbe("SHARED", "CMSIS-DAP duplicate"))
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                server.connect("vi09_board")
            self.assertFalse(any(e["kind"] == "enumerate" for e in EVENTS))
            self.assert_zero_open()

    def test_server_token_roundtrip_retains_opaque_uid_colons_and_percent(self):
        uid = "3-1.4:1.0%/Opaque"
        self.shared(uid)
        with self.chain() as (_, server):
            token = self.publish(server, "stlink", uid)
            server.assignment_store.replace({token: "vi09_board"})
            self.assertEqual(server._resolved_probe_uid_for_connection(token), "stlink:" + uid)
            server.connect("vi09_board")
            self.assertEqual(STLinkProbe.devices[0].opens, 1)
            self.assertEqual(CMSISDAPProbe.devices[0].opens, 0)

    def validation_probe(self, server):
        token = self.publish(server)
        probe = next(
            p for p in server._validation_inventory().probes if p.probe_family == "cmsisdap"
        )
        self.assertEqual(server._resolved_probe_uid_for_connection(token), "cmsisdap:SHARED")
        return probe

    def test_validation_native_open_preserves_inventory_provider_and_policy(self):
        first, chosen = self.shared()
        with self.chain() as (_, server):
            probe = self.validation_probe(server)
            profile = SimpleNamespace(
                board_id="vi09_board",
                board=replace(board(), debug_connect_mode="attach", debug_clock_hz=123456),
                device_support=None,
            )
            connection = server._validation_connect(profile, probe, 3.25)
            self.assertIs(connection.handle.session.probe, chosen)
            self.assertEqual((first.opens, chosen.opens), (0, 1))
            self.assertEqual(connection.handle.session.options.get("frequency"), 123456)
            reused = server._validation_connect(profile, probe, 3.25)
            self.assertIs(reused.handle, connection.handle)
            self.assertEqual(chosen.opens, 1)

    def setup_context(self, project, server, *, tier, target, part):
        token = self.publish(server)
        server.assignment_store.assign(token, "vi09_board")
        server._setup_tiers["vi09_board"] = tier
        datasheet = project / "official.pdf"
        shutil.copyfile(next((FIXTURE_FIRM / "evidence/datasheets").glob("*.pdf")), datasheet)
        user = SetupUserInput(
            "vi09_board",
            token,
            "VI09 board",
            part,
            None,
            datasheet_path=str(datasheet),
            requires_uart=False,
        )
        inventory = server._setup_inventory(user)
        self.assertEqual(len(inventory.probes), 1)
        self.assertEqual(inventory.probes[0].probe_family, "cmsisdap")
        return SetupPhaseContext(
            "vi09-continuation",
            "vi09-attempt",
            "setup",
            user,
            PreflightDecision(
                "preflight_ready",
                "setup/preflight-ready",
                "ready",
                selected_probe=inventory.probes[0],
                selected_target=target,
            ),
            {},
        )

    def test_setup_lite_real_callback_keeps_selected_provider(self):
        first, chosen = self.shared()
        with self.chain() as (project, server):
            context = self.setup_context(
                project, server, tier=Tier.SETUP_LITE, target="cortex_m", part="operator-part"
            )
            outcome = server._setup_connection_phase(context)
            self.assertTrue(outcome.verified, outcome)
            self.assertEqual((first.opens, chosen.opens, chosen.closes), (0, 1, 1))

    def test_full_generic_builtin_real_callback_and_validation_keep_provider(self):
        first, chosen = self.shared()
        with self.chain() as (project, server):
            server._setup_builtin_candidates["vi09_board"] = resolve_builtin_target_support(
                "nRF52840-QIAA", "nrf52840"
            )
            context = self.setup_context(
                project, server, tier=Tier.SETUP_FULL, target="nrf52840", part="nRF52840-QIAA"
            )
            outcome = server._setup_connection_phase(context)
            self.assertTrue(outcome.verified, outcome)
            profile = server._profile_repository.load("vi09_board")
            probe = self.validation_probe(server)
            connection = server._validation_connect(profile, probe, 4.0)
            self.assertIs(connection.handle.session.probe, chosen)
            self.assertEqual(first.opens, 0)
            self.assertEqual(chosen.opens, 2)

    def test_full_generic_pack_real_callback_and_validation_keep_provider(self):
        first, chosen = self.shared()
        with self.chain() as (project, server):
            shutil.copytree(FIXTURE_FIRM / "packs", project / ".firm/packs")
            context = self.setup_context(
                project, server, tier=Tier.SETUP_FULL, target="stm32l476rgtx", part="STM32L476RGT6"
            )
            outcome = server._setup_connection_phase(context)
            self.assertTrue(outcome.verified, outcome)
            profile = server._profile_repository.load("vi09_board")
            probe = self.validation_probe(server)
            connection = server._validation_connect(profile, probe, 4.0)
            self.assertIs(connection.handle.session.probe, chosen)
            self.assertIsNotNone(type(connection.handle.session.target)._pack_device)
            self.assertEqual((first.opens, chosen.opens), (0, 2))

    def test_builtin_research_open_uses_resolved_server_token(self):
        first, chosen = self.shared()
        with self.chain() as (_, server):
            token = self.publish(server)
            candidate = resolve_builtin_target_support("nRF52840-QIAA", "nrf52840")
            proven, policy = server._live_test_builtin_setup_target(
                probe_uid=server._resolved_probe_uid_for_connection(token),
                candidate=candidate,
                requested_policy=("swd", "attach", 1_000_000),
            )
            self.assertIsNotNone(proven.identity_proof)
            self.assertEqual(policy, ("swd", "attach", 1_000_000))
            self.assertEqual((first.opens, chosen.opens, chosen.closes), (0, 1, 1))

    def test_pack_research_pipeline_uses_resolved_server_token_and_verified_bytes(self):
        first, chosen = self.shared()
        with self.chain() as (_, server):
            token = self.publish(server)
            spec = next(
                p
                for p in load_manifest(FIXTURE_FIRM / "packs/manifest.yaml")
                if "stm32l476rgtx" in p.provides_targets
            )
            source = FIXTURE_FIRM / "packs/files" / spec.filename
            candidate = PackCandidate(
                spec.id, spec.version, spec.filename, spec.url, source, spec.sha256
            )
            binding = derive_candidate_binding(source, "STM32L476RGT6")
            pipeline, policies = server._setup_pack_pipeline(
                "vi09_board",
                "vi09-pack-research",
                server._resolved_probe_uid_for_connection(token),
                ("swd", "attach", 1_000_000),
            )
            validated = pipeline.validate_device(
                candidate, required_target="stm32l476rgtx", device_binding=binding
            )
            self.assertIsNotNone(validated)
            self.assertEqual(policies, [("swd", "attach", 1_000_000)])
            self.assertEqual((first.opens, chosen.opens, chosen.closes), (0, 1, 1))

    def test_active_aliases_require_same_provider_and_keep_native_multiplicity(self):
        for native, active in (("ABC", "ABC"), ("AbCd", "aBcD"), ("000123", "123")):
            for active_family, expected_count in (("stlink", 2), ("jlink", 3)):
                with self.subTest(native=native, active=active, provider=active_family):
                    info = ProbeInfo(native, "native", native, family="stlink")
                    service = HardwareInventoryService(
                        lambda: listing([info, info]),
                        lambda: [],
                        active_connections=lambda: [
                            ActiveConnectionRow(active, active, "owned", active_family)
                        ],
                    )
                    snapshot = service.snapshot()
                    self.assertEqual(len(snapshot.probes), expected_count)
                    self.assertEqual(sum(p.provider == "stlink" for p in snapshot.probes), 2)

    def test_uidless_active_selection_keeps_exact_live_session_token(self):
        token = "session:vi09-live-exact"
        service = HardwareInventoryService(
            lambda: listing([]),
            lambda: [],
            active_connections=lambda: [ActiveConnectionRow(token, None, "owned", "cmsisdap")],
        )
        selection = ProbeSelectionStore().resolve(token, service.snapshot())
        self.assertEqual(selection.connection_id, token)
        self.assertIsNone(selection.unique_id)
        self.assertFalse(selection.durable)
        with self.chain() as (_, server), patch.object(server, "_hardware_inventory", service):
            self.assertEqual(server._resolved_probe_uid_for_connection(token), token)
        with self.assertRaises(SelectionDisappeared):
            ProbeSelectionStore().resolve("session:another", service.snapshot())

    def test_actual_uidless_session_token_reuses_only_its_exact_active_handle(self):
        CMSISDAPProbe.devices = [CMSISDAPProbe("", "CMSIS-DAP UID-less live endpoint")]
        with self.chain() as (_, server):
            server.connect("vi09_board")
            managed = server.connection_manager.connection_for("vi09_board")
            token = managed.connection_id
            self.assertTrue(token.startswith("session:"))
            overview = server._setup_overview(None)
            self.assertIn(token, [row["connection_id"] for row in overview["connections"]])
            server.assignment_store.replace({token: "vi09_board"})
            self.assertEqual(server._assigned_probe_uid_for_connect("vi09_board"), token)
            self.assertEqual(server._resolved_probe_uid_for_connection(token), token)
            probe = server.ValidationProbe(token, "UID-less live endpoint", "cmsisdap", None)
            reused = server._validation_connect(SimpleNamespace(board_id="vi09_board"), probe, 1.0)
            self.assertIs(reused.handle, managed.handle)
            with self.assertRaisesRegex(TargetConnectionError, "does not match"):
                server._validation_connect(
                    SimpleNamespace(board_id="vi09_board"),
                    replace(probe, probe_id="session:another"),
                    1.0,
                )
            self.assertEqual(CMSISDAPProbe.devices[0].opens, 1)

    def test_qualified_jlink_uidless_retry_verifies_uid_and_provider(self):
        probe = JLinkProbe("123")
        JLinkProbe.devices = [probe]
        probe.fail_opens = 1
        adapter = swd_pyocd.PyOCDSWDInterface()
        with patch.object(
            swd_pyocd, "_single_matching_probe_visible_for_board_family", return_value=True
        ):
            handle = adapter.open(board=board("jlink"), unique_id="jlink:123", target="cortex_m")
            try:
                self.assertIs(handle.session.probe, probe)
                self.assertEqual(probe.opens, 2)
            finally:
                adapter.close(handle)
        self.assertFalse(any(e["kind"] == "fast_lookup" for e in EVENTS))

    def test_qualified_jlink_retry_rejects_other_provider_even_with_same_uid(self):
        probe = JLinkProbe("123")
        JLinkProbe.devices = [probe]
        probe.fail_opens = 1
        other = STLinkProbe("123")
        original_open = probe.open

        def fail_then_switch():
            try:
                original_open()
            finally:
                JLinkProbe.devices = []
                STLinkProbe.devices = [other]

        with (
            patch.object(probe, "open", side_effect=fail_then_switch),
            patch.object(
                swd_pyocd,
                "_single_matching_probe_visible_for_board_family",
                return_value=True,
            ),
        ):
            with self.assertRaisesRegex(ProbeNotFoundError, "refusing to open a different"):
                swd_pyocd.PyOCDSWDInterface().open(
                    board=board("jlink"), unique_id="jlink:123", target="cortex_m"
                )
        self.assertEqual((probe.opens, other.opens), (1, 0))


if __name__ == "__main__":
    unittest.main()
