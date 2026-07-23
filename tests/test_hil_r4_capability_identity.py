"""Capability-aware live identity regressions for the HIL-R4 correction."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast
import unittest
from unittest.mock import Mock, patch

from firmware_mcp import server
from firmware_mcp.adapters.debug_interface import (
    PhysicalMemoryRegion,
    TargetSessionHandle,
    TargetSessionMetadata,
)
from firmware_mcp.firmstore.profiles import ProfileError, ProfileRepository
from firmware_mcp.pack_provision import LiveIdentityProof, PackProvisionError
from firmware_mcp.services.live_identity import (
    LiveIdentityContradiction,
    LiveIdentityObservationError,
    observe_live_identity,
)
from firmware_mcp.setup_flow.device_support import (
    BuiltInTargetSupportCandidate,
    live_cpuid_compatibility_proof,
)
from firmware_mcp.setup_flow.preflight import PreflightDecision, ProbeCandidate, SetupUserInput
from firmware_mcp.setup_flow.setup import SetupPhaseContext
from firmware_mcp.services.safety_authority import (
    SafetyAuthority,
    SafetyAuthorityError,
    build_document,
    map_digest,
    validate_document,
)
from firmware_mcp.target_errors import TargetStateError


def _board(*, capability: str = "compatible", provider_id: str = "pyocd") -> object:
    return SimpleNamespace(
        provider_id=provider_id,
        target="target-a",
        provider_support_identity="support-a",
        silicon_id_capability=capability,
        silicon_id_addr=0x10,
        silicon_id_expected=0x1234,
        silicon_id_mask=0xFFFF,
        silicon_id_width_bits=32,
        silicon_id_label="CPUID" if capability == "compatible" else "DEVICEID",
        silicon_id_provenance="verified support evidence",
        silicon_id_bound_part_number="PART-A",
        silicon_id_support_identity="support-a",
    )


def _handle(
    board: object, *, live_identity: dict[str, object] | None = None
) -> TargetSessionHandle:
    return cast(
        TargetSessionHandle,
        SimpleNamespace(
            board=board,
            metadata=SimpleNamespace(runtime_token="session-a", live_identity=live_identity),
        ),
    )


class CapabilityAwareIdentityTests(unittest.TestCase):
    def test_setup_cpuid_proof_rejects_malformed_values_before_compatibility_masking(self) -> None:
        for observed in (True, None, "0x410FC241", -1, 1 << 32):
            with self.subTest(observed=repr(observed)):
                with self.assertRaisesRegex(PackProvisionError, "observation"):
                    live_cpuid_compatibility_proof(cast(int, observed))

        proof = live_cpuid_compatibility_proof(0x410FC241)
        self.assertEqual(proof.capability, "compatible")
        self.assertEqual(proof.expected, 0x410FC240)

    def test_builtin_probe_rejects_malformed_cpuid_before_support_evidence_exists(self) -> None:
        candidate = BuiltInTargetSupportCandidate("PART-A", "target-a", "a" * 64)
        for observed in (True, None, "0x410FC241", -1, 1 << 32):
            with self.subTest(observed=repr(observed)):
                handle = object()
                with (
                    patch.object(server.target_control, "open_session", return_value=handle),
                    patch.object(
                        server.target_control,
                        "read_memory",
                        return_value=cast(int, observed),
                    ),
                    patch.object(server.target_control, "close_session") as close_session,
                    self.assertRaisesRegex(PackProvisionError, "observation"),
                ):
                    server._live_test_builtin_setup_target(
                        probe_uid="probe-a",
                        candidate=candidate,
                        requested_policy=(None, "attach", None),
                    )
                self.assertIsNone(candidate.identity_proof)
                close_session.assert_called_once_with(handle)

    def test_generic_setup_rejects_malformed_cpuid_before_profile_or_support_publication(
        self,
    ) -> None:
        context = SetupPhaseContext(
            continuation_id="continuation-a",
            attempt_id="attempt-a",
            mode="setup",
            user_input=SetupUserInput(
                board_id="board_a",
                connection_id="probe-a",
                display_name="Board A",
                mcu_part_number="PART-A",
                serial_baudrate=None,
                datasheet_path="unused-datasheet.pdf",
                requires_uart=False,
            ),
            preflight=PreflightDecision(
                "preflight_ready",
                "setup/complete",
                "",
                selected_probe=ProbeCandidate("probe-a", "Probe A", "generic", "probe-a"),
                selected_target="target-a",
            ),
            phase_records={},
        )
        candidate = BuiltInTargetSupportCandidate("PART-A", "target-a", "a" * 64)
        repository = SimpleNamespace(
            store=SimpleNamespace(
                layout=SimpleNamespace(
                    datasheet_evidence=lambda _digest: Path("project/evidence/datasheet.pdf"),
                    project_root=Path("project"),
                )
            ),
            load=Mock(side_effect=ProfileError("profile is absent")),
            stage_core=Mock(
                return_value=SimpleNamespace(
                    profile=SimpleNamespace(board=SimpleNamespace(target="target-a"))
                )
            ),
            commit_core=Mock(),
            stage_optional=Mock(),
            commit_optional=Mock(),
        )
        handle = TargetSessionHandle(
            object(),
            None,
            "probe-a",
            "route-a",
            "target-a",
            TargetSessionMetadata(
                "Board A",
                "Probe A",
                "generic",
                "probe-a",
                None,
                "route-a",
                "target-a",
                "runtime-a",
            ),
        )
        for observed in (True, None, "0x410FC241", -1, 1 << 32):
            with self.subTest(observed=repr(observed)):
                support = server._ResolvedGenericSetupSupport(
                    candidate, Path("unused-datasheet.pdf"), "a" * 64
                )
                with (
                    patch.object(server, "_profile_repository", repository),
                    patch.object(server, "_resolve_setup_support", return_value=support),
                    patch.object(
                        server,
                        "resolve_device_support_geometry",
                        return_value=SimpleNamespace(flash_start=0),
                    ),
                    patch.object(server.target_control, "open_session", return_value=handle),
                    patch.object(
                        server.target_control,
                        "read_memory",
                        return_value=cast(int, observed),
                    ),
                    patch.object(server.target_control, "close_session") as close_session,
                    patch.dict(server._setup_attachment_overrides, clear=True),
                ):
                    outcome = server._setup_connection_phase(context)
                self.assertEqual(outcome.code, "setup/live-connect-failed")
                self.assertIn("identity observation is malformed", outcome.agent_prompt.casefold())
                self.assertIsNone(candidate.identity_proof)
                repository.commit_core.assert_not_called()
                repository.commit_optional.assert_not_called()
                close_session.assert_called_once_with(handle)

    def test_generic_setup_replay_rejects_malformed_silicon_before_profile_publication(
        self,
    ) -> None:
        proof = LiveIdentityProof(
            "compatible",
            0xE000ED00,
            0x410FC240,
            0xFF0FFFF0,
            32,
            "replayed CPUID compatibility",
        )
        candidate = BuiltInTargetSupportCandidate("PART-A", "target-a", "a" * 64, proof)
        repository = SimpleNamespace(
            store=SimpleNamespace(
                layout=SimpleNamespace(
                    datasheet_evidence=lambda _digest: Path("project/evidence/datasheet.pdf"),
                    project_root=Path("project"),
                )
            ),
            load=Mock(side_effect=ProfileError("profile is absent")),
            stage_core=Mock(
                return_value=SimpleNamespace(
                    profile=SimpleNamespace(board=SimpleNamespace(target="target-a"))
                )
            ),
            commit_core=Mock(),
            stage_optional=Mock(),
            commit_optional=Mock(),
        )
        context = SetupPhaseContext(
            continuation_id="continuation-a",
            attempt_id="attempt-a",
            mode="setup",
            user_input=SetupUserInput(
                board_id="board_a",
                connection_id="probe-a",
                display_name="Board A",
                mcu_part_number="PART-A",
                serial_baudrate=None,
                datasheet_path="unused-datasheet.pdf",
                requires_uart=False,
            ),
            preflight=PreflightDecision(
                "preflight_ready",
                "setup/complete",
                "",
                selected_probe=ProbeCandidate("probe-a", "Probe A", "generic", "probe-a"),
                selected_target="target-a",
            ),
            phase_records={},
        )
        handle = TargetSessionHandle(
            object(),
            None,
            "probe-a",
            "route-a",
            "target-a",
            TargetSessionMetadata(
                "Board A",
                "Probe A",
                "generic",
                "probe-a",
                None,
                "route-a",
                "target-a",
                "runtime-a",
            ),
        )
        for observed in (True, None, "0x410FC241", -1, 1 << 32):
            with self.subTest(observed=repr(observed)):
                support = server._ResolvedGenericSetupSupport(
                    candidate, Path("unused-datasheet.pdf"), "a" * 64
                )
                with (
                    patch.object(server, "_profile_repository", repository),
                    patch.object(server, "_resolve_setup_support", return_value=support),
                    patch.object(
                        server,
                        "resolve_device_support_geometry",
                        return_value=SimpleNamespace(flash_start=0),
                    ),
                    patch.object(server.target_control, "open_session", return_value=handle),
                    patch.object(
                        server.target_control,
                        "read_memory",
                        return_value=cast(int, observed),
                    ),
                    patch.object(server.target_control, "close_session") as close_session,
                    patch.dict(server._setup_attachment_overrides, clear=True),
                ):
                    outcome = server._setup_connection_phase(context)
                self.assertEqual(outcome.code, "setup/live-connect-failed")
                self.assertIn("identity observation is malformed", outcome.agent_prompt.casefold())
                repository.commit_core.assert_not_called()
                repository.commit_optional.assert_not_called()
                close_session.assert_called_once_with(handle)

    def test_exact_compatible_and_unavailable_are_distinct(self) -> None:
        exact = observe_live_identity(
            _handle(_board(capability="exact")),
            read_memory=lambda _handle, _address, _width: 0x1234,
            configured_part_number="PART-A",
        )
        compatible = observe_live_identity(
            _handle(_board()),
            read_memory=lambda _handle, _address, _width: 0x1234,
            configured_part_number="PART-A",
        )
        unavailable = observe_live_identity(
            _handle(
                SimpleNamespace(
                    provider_id="pyocd", target="target-a", provider_support_identity="support-a"
                )
            ),
            read_memory=lambda _handle, _address, _width: 0,
            configured_part_number="PART-A",
        )

        self.assertEqual(
            (exact.capability, exact.comparison_status, exact.exact_live_part_number),
            ("exact", "matched", "PART-A"),
        )
        self.assertEqual(
            (
                compatible.capability,
                compatible.comparison_status,
                compatible.exact_live_part_number,
            ),
            ("compatible", "compatible", None),
        )
        self.assertEqual(
            (unavailable.capability, unavailable.comparison_status, unavailable.evidence["kind"]),
            ("unavailable", "unavailable", "unavailable"),
        )

    def test_exact_and_compatible_mismatches_are_verified_contradictions(self) -> None:
        for capability in ("exact", "compatible"):
            with (
                self.subTest(capability=capability),
                self.assertRaises(LiveIdentityContradiction) as raised,
            ):
                observe_live_identity(
                    _handle(_board(capability=capability)),
                    read_memory=lambda _handle, _address, _width: 0,
                    configured_part_number="PART-A",
                )
            self.assertIsInstance(raised.exception, TargetStateError)
            self.assertEqual(raised.exception.code, "identity-contradiction")

    def test_configured_read_failure_is_not_a_contradiction(self) -> None:
        for capability in ("exact", "compatible"):
            with (
                self.subTest(capability=capability),
                self.assertRaises(LiveIdentityObservationError) as raised,
            ):
                observe_live_identity(
                    _handle(_board(capability=capability)),
                    read_memory=lambda _handle, _address, _width: (_ for _ in ()).throw(
                        OSError("transport read failed")
                    ),
                    configured_part_number="PART-A",
                )
            self.assertNotIsInstance(raised.exception, LiveIdentityContradiction)
            self.assertEqual(raised.exception.code, "identity-observation/read-failed")

    def test_malformed_observed_identity_values_are_blocking_observation_failures(self) -> None:
        for capability in ("exact", "compatible"):
            for observed in (True, None, "0x1234", -1, 1 << 32):
                with self.subTest(capability=capability, observed=repr(observed)):

                    def malformed_read(
                        _handle: TargetSessionHandle, _address: int, _width_bits: int
                    ) -> int:
                        return cast(int, observed)

                    with self.assertRaises(LiveIdentityObservationError) as raised:
                        observe_live_identity(
                            _handle(_board(capability=capability)),
                            read_memory=malformed_read,
                            configured_part_number="PART-A",
                        )
                    self.assertNotIsInstance(raised.exception, LiveIdentityContradiction)
                    self.assertEqual(raised.exception.code, "identity-observation/read-failed")

    def test_observation_failure_blocks_map_build_before_region_backend(self) -> None:
        for capability in ("exact", "compatible"):
            regions_called = False

            def regions_for(_handle: TargetSessionHandle) -> tuple[PhysicalMemoryRegion, ...]:
                nonlocal regions_called
                regions_called = True
                return ()

            with (
                self.subTest(capability=capability),
                self.assertRaises(LiveIdentityObservationError),
            ):
                build_document(
                    board_id="board-a",
                    handle=_handle(_board(capability=capability)),
                    regions_for=regions_for,
                    read_memory=lambda _handle, _address, _width: (_ for _ in ()).throw(
                        OSError("identity read lost")
                    ),
                    configured_part_number="PART-A",
                )
            self.assertFalse(regions_called)

    def test_provider_typed_compatible_and_unavailable_identity_remain_honest(self) -> None:
        compatible = observe_live_identity(
            _handle(
                _board(provider_id="recipe-provider"),
                live_identity={
                    "capability": "compatible",
                    "part_number": None,
                    "provenance": "provider read",
                    "support_identity": "support-a",
                    "evidence": {"query": "identity"},
                },
            ),
            read_memory=None,
            configured_part_number="PART-A",
        )
        unavailable = observe_live_identity(
            _handle(
                _board(provider_id="recipe-provider"),
                live_identity={
                    "capability": "unavailable",
                    "part_number": None,
                    "provenance": "provider read",
                    "support_identity": "support-a",
                    "evidence": {"reason": "provider exposes no identity query"},
                },
            ),
            read_memory=None,
            configured_part_number="PART-A",
        )

        self.assertEqual(compatible.comparison_status, "compatible")
        self.assertIsNone(compatible.exact_live_part_number)
        self.assertEqual(unavailable.evidence["reason"], "provider exposes no identity query")

    def test_map_schema_binds_complete_identity_evidence_and_rejects_v1(self) -> None:
        handle = _handle(_board())
        regions = (
            PhysicalMemoryRegion(
                0,
                0x100,
                True,
                True,
                True,
                "physical_flash",
                "flash",
                "provider",
                "session-a",
            ),
        )
        document = build_document(
            board_id="board-a",
            handle=handle,
            regions_for=lambda _handle: regions,
            read_memory=lambda _handle, _address, _width: 0x1234,
            configured_part_number="PART-A",
        )
        self.assertEqual(document["schema_version"], 2)
        checked = validate_document(document, board_id="board-a")
        changed = dict(checked)
        changed_identity = dict(checked["identity"])
        changed_evidence = dict(changed_identity["evidence"])
        changed_evidence["mask"] = 0xFF
        changed_identity["evidence"] = changed_evidence
        changed["identity"] = changed_identity
        self.assertNotEqual(map_digest(changed), checked["digest"])
        old = dict(checked)
        old["schema_version"] = 1
        with self.assertRaisesRegex(SafetyAuthorityError, "unsupported"):
            validate_document(old, board_id="board-a")

    def test_map_binding_preserves_identity_failure_kind(self) -> None:
        from tempfile import TemporaryDirectory
        from pathlib import Path
        import json

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            from firmware_mcp.firmstore.store import FirmStore

            store = FirmStore(root)
            handle = _handle(_board(capability="exact"))
            regions = (
                PhysicalMemoryRegion(
                    0,
                    0x100,
                    True,
                    True,
                    True,
                    "physical_flash",
                    "flash",
                    "provider",
                    "session-a",
                ),
            )
            document = build_document(
                board_id="board-a",
                handle=handle,
                regions_for=lambda _handle: regions,
                read_memory=lambda _handle, _address, _width: 0x1234,
                configured_part_number="PART-A",
            )
            path = store.layout.safety_board("board-a") / "memory-map.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(document), encoding="utf-8")
            profiles = ProfileRepository(store)
            profile = SimpleNamespace(
                safety_ref=path.relative_to(root).as_posix(), mcu_part_number="PART-A"
            )
            for read, phrase, cause in (
                (
                    lambda _handle, _address, _width: (_ for _ in ()).throw(OSError("lost")),
                    "observation failed",
                    LiveIdentityObservationError,
                ),
                (lambda _handle, _address, _width: 0, "contradicts", LiveIdentityContradiction),
            ):
                with self.subTest(phrase=phrase):
                    authority = SafetyAuthority(
                        store,
                        profiles,
                        lambda _handle: regions,
                        read,
                    )
                    with (
                        patch.object(profiles, "load", return_value=profile),
                        self.assertRaisesRegex(SafetyAuthorityError, phrase) as raised,
                    ):
                        authority.binding("board-a", handle)
                    self.assertIsInstance(raised.exception.__cause__, cause)

    def test_malformed_identity_value_keeps_map_backed_status_structured(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import json

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            from firmware_mcp.firmstore.store import FirmStore

            store = FirmStore(root)
            board = _board(capability="exact")
            handle = _handle(board)
            regions = (
                PhysicalMemoryRegion(
                    0,
                    0x100,
                    True,
                    True,
                    True,
                    "physical_flash",
                    "flash",
                    "provider",
                    "session-a",
                ),
            )
            document = build_document(
                board_id="board-a",
                handle=handle,
                regions_for=lambda _handle: regions,
                read_memory=lambda _handle, _address, _width: 0x1234,
                configured_part_number="PART-A",
            )
            map_path = store.layout.safety_board("board-a") / "memory-map.json"
            map_path.parent.mkdir(parents=True)
            map_path.write_text(json.dumps(document), encoding="utf-8")
            profile = SimpleNamespace(
                board=board,
                mcu_part_number="PART-A",
                safety_ref=map_path.relative_to(root).as_posix(),
            )
            profiles = ProfileRepository(store)
            authority = SafetyAuthority(
                store,
                profiles,
                lambda _handle: regions,
                lambda _handle, _address, _width: cast(int, None),
            )
            connection = SimpleNamespace(handle=handle, connection_id="connection-a")
            inventory = SimpleNamespace(serial_ports=())
            with (
                patch.object(profiles, "load", return_value=profile),
                patch.object(server, "_profile_repository", profiles),
                patch.object(server, "_safety_authority", authority),
                patch.object(
                    server,
                    "connection_manager",
                    SimpleNamespace(maybe_connection=lambda _board_id: connection),
                ),
                patch.object(server, "_validation_inventory", return_value=inventory),
                patch.object(
                    server._attachment_cache,
                    "resolve",
                    return_value=SimpleNamespace(reused=False, port_path=None),
                ),
                patch.object(server.target_control, "read_memory", return_value=cast(int, None)),
            ):
                with self.assertRaisesRegex(SafetyAuthorityError, "observation failed"):
                    authority.binding("board-a", handle)
                status = server._get_setup_status("board-a")
            self.assertEqual(status["identity_comparison_status"], "unavailable")
            self.assertEqual(
                cast(dict[str, object], status["identity_evidence"])["kind"],
                "observation-failed",
            )
            self.assertEqual(cast(dict[str, object], status["safety_map"])["state"], "stale")
            self.assertFalse(cast(bool, status["ready_for_flash"]))

    def test_status_and_guard_keep_observation_failure_distinct(self) -> None:
        from tempfile import TemporaryDirectory
        from pathlib import Path
        import json

        board = _board(capability="exact")
        profile = SimpleNamespace(
            mcu_part_number="PART-A", safety_ref="safety/board-a/memory-map.json", board=board
        )
        connection = SimpleNamespace(handle=_handle(board), connection_id="connection-a")
        manager = SimpleNamespace(maybe_connection=lambda _board_id: connection)
        inventory = SimpleNamespace(serial_ports=())
        with (
            patch.object(
                server, "_profile_repository", SimpleNamespace(load=lambda _board_id: profile)
            ),
            patch.object(server, "connection_manager", manager),
            patch.object(
                server,
                "_safety_authority",
                SimpleNamespace(
                    binding=Mock(
                        side_effect=SafetyAuthorityError(
                            "identity observation failed before comparison"
                        )
                    )
                ),
            ),
            patch.object(server, "_validation_inventory", return_value=inventory),
            patch.object(
                server._attachment_cache,
                "resolve",
                return_value=SimpleNamespace(reused=False, port_path=None),
            ),
            patch.object(
                server.target_control,
                "read_memory",
                side_effect=OSError("wire disconnected"),
            ),
        ):
            status = server._get_setup_status("board-a")
        self.assertEqual(status["identity_comparison_status"], "unavailable")
        self.assertEqual(
            cast(dict[str, object], status["identity_evidence"])["kind"], "observation-failed"
        )
        self.assertNotEqual(cast(dict[str, object], status["safety_map"])["state"], "current")
        self.assertFalse(cast(bool, status["ready_for_flash"]))

        with (
            patch.object(
                server, "_profile_repository", SimpleNamespace(load=lambda _board_id: profile)
            ),
            patch.object(server, "connection_manager", manager),
            patch.object(
                server,
                "_safety_authority",
                SimpleNamespace(
                    binding=Mock(
                        side_effect=SafetyAuthorityError("identity contradicts live target")
                    )
                ),
            ),
            patch.object(server, "_validation_inventory", return_value=inventory),
            patch.object(
                server._attachment_cache,
                "resolve",
                return_value=SimpleNamespace(reused=False, port_path=None),
            ),
            patch.object(server.target_control, "read_memory", return_value=0),
        ):
            contradicted = server._get_setup_status("board-a")
        self.assertEqual(contradicted["identity_comparison_status"], "contradicted")
        self.assertEqual(
            cast(dict[str, object], contradicted["identity_evidence"])["kind"], "contradiction"
        )
        self.assertNotEqual(cast(dict[str, object], contradicted["safety_map"])["state"], "current")
        self.assertFalse(cast(bool, contradicted["ready_for_flash"]))

        compatible_board = _board(capability="compatible")
        compatible_profile = SimpleNamespace(
            mcu_part_number="PART-A",
            safety_ref="safety/board-a/memory-map.json",
            board=compatible_board,
        )
        compatible_connection = SimpleNamespace(
            handle=_handle(compatible_board), connection_id="connection-a"
        )
        with (
            patch.object(
                server,
                "_profile_repository",
                SimpleNamespace(load=lambda _board_id: compatible_profile),
            ),
            patch.object(
                server,
                "connection_manager",
                SimpleNamespace(maybe_connection=lambda _board_id: compatible_connection),
            ),
            patch.object(
                server,
                "_safety_authority",
                SimpleNamespace(
                    binding=Mock(side_effect=SafetyAuthorityError("identity conflict"))
                ),
            ),
            patch.object(server, "_validation_inventory", return_value=inventory),
            patch.object(
                server._attachment_cache,
                "resolve",
                return_value=SimpleNamespace(reused=False, port_path=None),
            ),
            patch.object(server.target_control, "read_memory", return_value=0),
        ):
            compatible_status = server._get_setup_status("board-a")
        self.assertEqual(compatible_status["identity_comparison_status"], "contradicted")
        self.assertEqual(
            cast(dict[str, object], compatible_status["identity_evidence"])["kind"],
            "contradiction",
        )

        with TemporaryDirectory() as temporary:
            firmware_path = Path(temporary) / "firmware.hex"
            firmware = b":020000040000FA\n:0100000000FF\n:00000001FF\n"
            firmware_path.write_bytes(firmware)
            evidence_path = Path(temporary) / "target.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "artifact_sha256": server.parse_flash_image_bytes(
                            firmware_path, firmware
                        ).sha256,
                        "part_number": "PART-A",
                        "provenance": "test fixture",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(server, "_handle", return_value=connection.handle),
                patch.object(
                    server,
                    "_safety_authority",
                    SimpleNamespace(validate_flash_role=lambda *_args: {"roles": ["application"]}),
                ),
                patch.object(
                    server, "_profile_repository", SimpleNamespace(load=lambda _board_id: profile)
                ),
                patch.object(
                    server,
                    "observe_live_identity",
                    side_effect=LiveIdentityObservationError("transport read failed"),
                ),
            ):
                with self.assertRaises(server.GuardError) as raised:
                    server._guard_classification(
                        "flash_firmware",
                        "board-a",
                        {
                            "firmware_path": str(firmware_path),
                            "flash_role": "application",
                            "artifact_target_evidence_path": str(evidence_path),
                            "halt_after_reset": False,
                        },
                        None,
                    )
            self.assertEqual(raised.exception.code, "guard/identity-observation-read-failed")

    def test_guard_safety_binding_preserves_identity_failure_kind(self) -> None:
        board = _board(capability="exact")
        connection = SimpleNamespace(handle=_handle(board), connection_id="connection-a")
        manager = SimpleNamespace(maybe_connection=lambda _board_id: connection)

        for cause, expected_code in (
            (LiveIdentityContradiction("different target"), "guard/live-identity-contradiction"),
            (
                LiveIdentityObservationError("identity register transport failure"),
                "guard/identity-observation-read-failed",
            ),
        ):

            def binding(*_args: object, error: TargetStateError = cause) -> dict[str, object]:
                try:
                    raise error
                except TargetStateError as exc:
                    raise SafetyAuthorityError("identity binding failed") from exc

            with (
                patch.object(server, "connection_manager", manager),
                patch.object(server, "_safety_authority", SimpleNamespace(binding=binding)),
            ):
                with self.assertRaises(server.GuardError) as raised:
                    server._guard_safety_binding("board-a")
            self.assertEqual(raised.exception.code, expected_code)


if __name__ == "__main__":
    unittest.main()
