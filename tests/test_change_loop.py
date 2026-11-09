"""Routing, artifact selection, truthful flash state and recovery cleanup."""

from __future__ import annotations
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.adapters.swd_process import _validate_result
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.services.connections import BoardNotConnectedError, ConnectionManager
import argparse
from contextlib import redirect_stdout
from pyocd_debug_mcp import native_build, server
from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.services.session_runtime import ActionContext, SessionRecord, ToolOutcome
from pyocd_debug_mcp.tools.flash import FlashToolServices, build_flash_handlers
from pyocd_debug_mcp.firmstore.profiles import ProfileError


def _board() -> BoardConfig:
    return BoardConfig(
        board_id="profile-board",
        display_name="profile board",
        mcu_family="test",
        probe_family="test",
        pyocd_target="profile-target",
        probe_type="test",
        probe_hint_terms=(),
        serial_hint_terms=(),
        test_addr=0,
        source_path=Path("profile.yaml"),
    )


class ChangeLoopSpecTests(unittest.TestCase):
    def test_cl001_native_reset_connect_cannot_restore_ambient_probe_or_target(self) -> None:
        """The adapter is a second routing boundary, not merely the server's delegate."""
        from pyocd_debug_mcp.adapters import swd_pyocd

        session = SimpleNamespace(
            probe=SimpleNamespace(unique_id=None, assert_reset=Mock()),
            target=SimpleNamespace(
                halt=Mock(), get_state=Mock(return_value=SimpleNamespace(name="HALTED"))
            ),
            open=Mock(),
        )
        interface = swd_pyocd.PyOCDSWDInterface()
        with (
            patch.dict(
                "os.environ",
                {"PYOCD_PROBE_UID": "ambient-probe", "PYOCD_TARGET": "ambient-target"},
                clear=False,
            ),
            patch.object(interface, "_choose_session", return_value=session) as choose,
            patch.object(interface, "_verify_session_pack_source"),
        ):
            handle = interface.connect_under_reset(board=None, unique_id=None, target=None)

        self.assertIsNone(choose.call_args.kwargs["probe_uid"])
        self.assertNotIn("target_override", choose.call_args.kwargs["options"])
        self.assertIsNone(handle.probe_uid)
        self.assertIsNone(handle.target_override)

    def test_cl001_ordinary_open_retains_ambient_compatibility(self) -> None:
        from pyocd_debug_mcp.adapters import swd_pyocd

        session = SimpleNamespace(probe=SimpleNamespace(unique_id=None), open=Mock())
        interface = swd_pyocd.PyOCDSWDInterface()
        with (
            patch.dict(
                "os.environ",
                {"PYOCD_PROBE_UID": "ambient-probe", "PYOCD_TARGET": "ambient-target"},
                clear=False,
            ),
            patch.object(interface, "_choose_session", return_value=session) as choose,
            patch.object(interface, "_open_and_verify_session"),
        ):
            handle = interface.open(board=None, unique_id=None, target=None)

        self.assertEqual(choose.call_args.kwargs["probe_uid"], "ambient-probe")
        self.assertEqual(choose.call_args.kwargs["options"]["target_override"], "ambient-target")
        self.assertEqual(handle.target_override, "ambient-target")

    def test_cl001_reset_connect_has_no_ambient_routing_authority(self) -> None:
        """Reset attach must pass only profile/action routing to the backend."""
        from pyocd_debug_mcp import server

        manager = ConnectionManager()
        board = _board()
        handle = TargetSessionHandle(None, board, "profile-probe", "fake", "profile-target")
        runtime = SimpleNamespace(session_id="runtime")
        assignment = SimpleNamespace(runtime_session=runtime)
        control = SimpleNamespace(connect_under_reset=Mock(return_value=handle))
        with (
            patch.dict(
                "os.environ",
                {
                    "PYOCD_BOARD_CONFIG": "ambient.yaml",
                    "PYOCD_PROBE_UID": "ambient-probe",
                    "PYOCD_TARGET": "ambient-target",
                },
                clear=False,
            ),
            patch.object(server, "connection_manager", manager),
            patch.object(server, "resolve_board_config", return_value=board) as resolve_board,
            patch.object(
                server, "_resolve_probe_uid_for_connect", return_value="profile-probe"
            ) as resolve_uid,
            patch.object(server, "target_control", control),
            patch.object(server, "_promote_open_session", return_value=assignment),
            patch.object(server, "_record_event"),
            patch.object(
                server, "session_metadata", return_value=SimpleNamespace(route_used="fake")
            ),
            patch.object(
                server._profile_repository, "load", side_effect=server.ProfileError("absent")
            ),
        ):
            reply = server._connect_under_reset_impl("profile-board", None, None)

        self.assertIn("target halted", reply)
        resolve_board.assert_called_once_with(
            "profile-board", None, allow_environment_overrides=False
        )
        resolve_uid.assert_called_once_with(board, None, allow_environment_override=False)
        self.assertEqual(control.connect_under_reset.call_args.kwargs["unique_id"], "profile-probe")
        self.assertEqual(control.connect_under_reset.call_args.kwargs["target"], "profile-target")

    def test_cl002_multiple_native_images_are_candidates_not_selected_artifacts(self) -> None:
        from pyocd_debug_mcp import native_build

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("z.elf", "a.elf", "z.map", "a.map"):
                (root / name).write_bytes(b"map" if name.endswith(".map") else b"elf")
            with patch.object(
                native_build, "_is_loadable_elf", side_effect=lambda p: p.suffix == ".elf"
            ):
                artifacts = native_build._artifact_paths(root)

        self.assertIsNone(artifacts["elf"])
        self.assertIsNone(artifacts["map"])
        self.assertEqual(artifacts["artifact_selection"], "explicit_declaration_required")
        self.assertEqual([Path(p).name for p in artifacts["elf_candidates"]], ["a.elf", "z.elf"])
        self.assertEqual([Path(p).name for p in artifacts["map_candidates"]], ["a.map", "z.map"])
        self.assertIn("--artifact-elf", str(artifacts["artifact_selection_remedy"]))

    def test_cl002_one_sided_ambiguity_clears_all_selection_in_helper_and_evidence(self) -> None:
        from pyocd_debug_mcp import native_build

        for names, ambiguous_role in (
            (("one.elf", "one.map", "two.map"), "map_candidates"),
            (("one.elf", "two.elf", "one.map"), "elf_candidates"),
        ):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project, build = root / "project", root / "build"
                project.mkdir()
                build.mkdir()
                for name in names:
                    (build / name).write_bytes(b"elf" if name.endswith(".elf") else b"map")
                with patch.object(
                    native_build, "_is_loadable_elf", side_effect=lambda p: p.suffix == ".elf"
                ):
                    artifacts = native_build._artifact_paths(build)
                    self.assertEqual(
                        {role: artifacts[role] for role in ("elf", "hex", "map")},
                        {"elf": None, "hex": None, "map": None},
                    )
                    args = native_build.build_parser().parse_args(
                        [
                            "--project-dir",
                            str(project),
                            "--build-dir",
                            str(build),
                            "--",
                            "native-build",
                        ]
                    )
                    output = io.StringIO()
                    with (
                        contextlib.redirect_stdout(output),
                        patch.object(
                            native_build, "run_owned", return_value=SimpleNamespace(returncode=0)
                        ),
                    ):
                        self.assertEqual(native_build.run_build(args), 0)
                evidence = json.loads(output.getvalue())
                self.assertEqual(
                    {role: evidence["artifacts"][role] for role in ("elf", "hex", "map")},
                    {"elf": None, "hex": None, "map": None},
                )
                self.assertEqual(
                    evidence["artifacts"]["artifact_selection"], "explicit_declaration_required"
                )
                self.assertEqual(len(evidence["artifacts"][ambiguous_role]), 2)
                singleton_role = (
                    "map_candidates" if ambiguous_role == "elf_candidates" else "elf_candidates"
                )
                self.assertEqual(len(artifacts[singleton_role]), 1)
                self.assertEqual(
                    evidence["artifacts"][singleton_role],
                    artifacts[singleton_role],
                )

    def test_cl003_flash_state_is_validated_across_process_boundary(self) -> None:
        for state in ("running", "halted", "reset_state_unconfirmed"):
            with self.subTest(state=state):
                self.assertEqual(_validate_result("flash", state, {}), state)
        for invalid in (None, "RUNNING", "unknown"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _validate_result("flash", invalid, {})

    def test_cl003_final_reset_transport_loss_is_successful_but_unconfirmed(self) -> None:
        from pyocd.core.exceptions import TransferError
        from pyocd_debug_mcp.adapters import swd_pyocd

        target = SimpleNamespace(
            reset_and_halt=Mock(side_effect=[None, None]),
            reset=Mock(side_effect=TransferError("link dropped")),
            get_state=Mock(),
        )
        session = SimpleNamespace(target=target)
        interface = swd_pyocd.PyOCDSWDInterface()
        handle = TargetSessionHandle(session, _board(), "probe", "fake", None)
        with patch.object(swd_pyocd, "FileProgrammer") as programmer:
            self.assertEqual(
                interface.flash(handle, Path("firmware.hex"), halt_after_reset=False),
                "reset_state_unconfirmed",
            )
        programmer.return_value.program.assert_called_once_with("firmware.hex")
        target.get_state.assert_not_called()

    def test_cl003_any_final_reset_failure_after_programming_is_unconfirmed(self) -> None:
        """Only pre-program/programmer errors are fatal; final reset is observationally uncertain."""
        for error_type in (OSError, RuntimeError):
            with self.subTest(error_type=error_type.__name__):
                from pyocd_debug_mcp.adapters import swd_pyocd

                target = SimpleNamespace(
                    reset_and_halt=Mock(),
                    reset=Mock(side_effect=error_type("reset transport vanished")),
                    get_state=Mock(),
                )
                handle = TargetSessionHandle(
                    SimpleNamespace(target=target), _board(), "probe", "fake", None
                )
                with patch.object(swd_pyocd, "FileProgrammer") as programmer:
                    self.assertEqual(
                        swd_pyocd.PyOCDSWDInterface().flash(
                            handle, Path("firmware.hex"), halt_after_reset=False
                        ),
                        "reset_state_unconfirmed",
                    )
                programmer.return_value.program.assert_called_once_with("firmware.hex")
                target.get_state.assert_not_called()

    def test_cl003_programmer_failure_remains_fatal_not_a_success_state(self) -> None:
        for error_type in (OSError, RuntimeError):
            with self.subTest(error_type=error_type.__name__):
                from pyocd_debug_mcp.adapters import swd_pyocd
                from pyocd_debug_mcp.target_errors import TargetConnectionError

                target = SimpleNamespace(reset_and_halt=Mock(), reset=Mock(), get_state=Mock())
                handle = TargetSessionHandle(
                    SimpleNamespace(target=target), _board(), "probe", "fake", None
                )
                with patch.object(swd_pyocd, "FileProgrammer") as programmer:
                    programmer.return_value.program.side_effect = error_type("verify failed")
                    with self.assertRaisesRegex(TargetConnectionError, "verify failed"):
                        swd_pyocd.PyOCDSWDInterface().flash(
                            handle, Path("firmware.hex"), halt_after_reset=False
                        )
                target.reset.assert_not_called()
                target.get_state.assert_not_called()

    def test_cl003_only_an_observed_final_state_is_reported(self) -> None:
        from pyocd_debug_mcp.adapters import swd_pyocd

        target = SimpleNamespace(
            reset_and_halt=Mock(),
            reset=Mock(),
            get_state=Mock(return_value=SimpleNamespace(name="HALTED")),
        )
        interface = swd_pyocd.PyOCDSWDInterface()
        handle = TargetSessionHandle(
            SimpleNamespace(target=target), _board(), "probe", "fake", None
        )
        for observed, halt, expected in (("HALTED", True, "halted"), ("RUNNING", False, "running")):
            with self.subTest(observed=observed), patch.object(swd_pyocd, "FileProgrammer"):
                target.get_state.return_value = SimpleNamespace(name=observed)
                self.assertEqual(
                    interface.flash(handle, Path("firmware.hex"), halt_after_reset=halt), expected
                )

        target.get_state.side_effect = OSError("state unreadable")
        with patch.object(swd_pyocd, "FileProgrammer"):
            self.assertEqual(
                interface.flash(handle, Path("firmware.hex"), halt_after_reset=False),
                "reset_state_unconfirmed",
            )

    def test_cl003_flash_tool_logs_and_explains_unconfirmed_state_without_running_claim(
        self,
    ) -> None:
        from pyocd_debug_mcp.services.session_runtime import (
            SessionRecord,
        )

        root = Path("test-run")
        runtime = SessionRecord(
            "session",
            "profile-board",
            "connection",
            "probe",
            "fake",
            "now",
            root,
            root / "events",
            root / "summary",
        )
        events: list[dict[str, object]] = []
        request = SimpleNamespace(
            artifact_path=Path("firmware.elf"),
            identity=SimpleNamespace(as_log_fields=lambda: {"artifact_sha256": "digest"}),
        )
        services = FlashToolServices(
            runtime_for=lambda _board: runtime,
            active_session_id=lambda _board: "session",
            duration_ms=lambda _started: 1,
            record_event=lambda *_args, **kwargs: events.append(kwargs),
            format_refusal=lambda refusal, **_kwargs: str(refusal),
            action_context=lambda tool, board: ActionContext("test", tool, board),
            maybe_handle_for=lambda _board: None,
            handle_for=lambda _board: object(),
            resolve_request=lambda *_args: request,
            flash_target=lambda *_args: (Path("firmware.elf"), "reset_state_unconfirmed"),
            error_code=lambda _exc: "flash/backend",
        )
        reply = build_flash_handlers(services)["flash_application"]("profile-board", "firmware.elf")

        self.assertIn("reset state is unconfirmed", reply)
        self.assertIn("reconnect and check target state", reply)
        self.assertNotIn("target left running", reply)
        self.assertEqual(events[-1]["outcome_kind"], ToolOutcome.SUCCESS)
        self.assertEqual(
            events[-1]["details"],
            {"target_state": "reset_state_unconfirmed", "safety_map_checked": True},
        )

    def test_cl004_recovery_finalization_revokes_assignment_even_when_close_fails(self) -> None:
        from pyocd_debug_mcp import server

        manager = ConnectionManager()
        handle = TargetSessionHandle(None, _board(), "probe", "fake", None)
        from pyocd_debug_mcp.services.session_runtime import SessionRecord

        root = Path("test-run")
        runtime = SessionRecord(
            "runtime",
            "profile-board",
            "connection",
            "probe",
            "fake",
            "now",
            root,
            root / "events",
            root / "summary",
        )
        manager.assign("profile-board", handle, runtime)
        gate = SimpleNamespace(clear=Mock())
        assignments = SimpleNamespace(clear_board=Mock())
        plans = SimpleNamespace(invalidate_board=Mock())
        control = SimpleNamespace(close_session=Mock(side_effect=OSError("worker stuck")))
        sessions = SimpleNamespace(mark_recover_completed=Mock(), close_session=Mock())
        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "gate_manager", gate),
            patch.object(server, "assignment_store", assignments),
            patch.object(server, "plan_engine", plans),
            patch.object(server, "target_control", control),
            patch.object(server, "_session_store", sessions),
        ):
            with self.assertRaisesRegex(RuntimeError, "worker close"):
                server._finalize_unlock_recovery("profile-board")

        with self.assertRaises(BoardNotConnectedError):
            manager.handle_for("profile-board")
        sessions.mark_recover_completed.assert_called_once_with(runtime)
        sessions.close_session.assert_called_once_with(runtime)
        gate.clear.assert_called_once()
        assignments.clear_board.assert_called_once_with("profile-board")
        plans.invalidate_board.assert_called_once()


def _write_loadable_elf(path: Path) -> None:
    """Write the smallest ELF64 file accepted by the helper's structural check."""

    content = bytearray(64 + 56)
    content[:7] = b"\x7fELF\x02\x01\x01"
    content[16:18] = (2).to_bytes(2, "little")
    content[18:20] = (40).to_bytes(2, "little")
    content[20:24] = (1).to_bytes(4, "little")
    content[32:40] = (64).to_bytes(8, "little")
    content[52:54] = (64).to_bytes(2, "little")
    content[54:56] = (56).to_bytes(2, "little")
    content[56:58] = (1).to_bytes(2, "little")
    content[64:68] = (1).to_bytes(4, "little")  # PT_LOAD
    path.write_bytes(content)


class NativeBuildAmbiguityRegressionTests(unittest.TestCase):
    def test_multi_image_discovery_exposes_candidates_without_selecting_or_authorizing_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("z.elf", "a.elf"):
                _write_loadable_elf(root / name)
            for name in ("z.map", "a.map"):
                (root / name).write_text("linker map", encoding="utf-8")

            artifacts = native_build._artifact_paths(root)

            self.assertIsNone(artifacts["elf"])
            self.assertIsNone(artifacts["map"])
            self.assertIsNone(artifacts["hex"])
            self.assertEqual(artifacts["artifact_selection"], "explicit_declaration_required")
            self.assertEqual(
                artifacts["elf_candidates"],
                [str((root / "a.elf").resolve()), str((root / "z.elf").resolve())],
            )
            self.assertEqual(
                artifacts["map_candidates"],
                [str((root / "a.map").resolve()), str((root / "z.map").resolve())],
            )
            self.assertIn("--artifact-elf", str(artifacts["artifact_selection_remedy"]))
            selected = {role: artifacts[role] for role in ("elf", "hex", "map")}
            assurance = native_build._artifact_assurance(selected, declared=False)
            self.assertIsNone(assurance["elf"])
            self.assertIsNone(assurance["map"])

    def test_explicit_artifacts_restore_selected_fields_after_ambiguous_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_loadable_elf(root / "a.elf")
            _write_loadable_elf(root / "b.elf")
            (root / "a.map").write_text("map", encoding="utf-8")
            (root / "b.map").write_text("map", encoding="utf-8")

            selected = native_build._validate_declared_artifacts(
                root,
                {"elf": "b.elf", "map": "b.map", "hex": None},
                expected_root=root.resolve(),
            )

            self.assertEqual(selected["elf"], str((root / "b.elf").resolve()))
            self.assertEqual(selected["map"], str((root / "b.map").resolve()))

    def test_one_sided_ambiguity_clears_all_selected_fields_in_helper_and_build_evidence(
        self,
    ) -> None:
        for ambiguous_role in ("elf", "map"):
            with (
                self.subTest(ambiguous_role=ambiguous_role),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                project = root / "project"
                build = root / "build"
                project.mkdir()
                build.mkdir()
                elf_names = ("a.elf", "b.elf") if ambiguous_role == "elf" else ("a.elf",)
                map_names = ("a.map", "b.map") if ambiguous_role == "map" else ("a.map",)
                for name in elf_names:
                    _write_loadable_elf(build / name)
                for name in map_names:
                    (build / name).write_text("linker map", encoding="utf-8")

                artifacts = native_build._artifact_paths(build)
                self.assertEqual(
                    {role: artifacts[role] for role in ("elf", "hex", "map")},
                    {"elf": None, "hex": None, "map": None},
                )
                self.assertEqual(artifacts["artifact_selection"], "explicit_declaration_required")
                self.assertEqual(
                    artifacts["elf_candidates"],
                    [str((build / name).resolve()) for name in elf_names],
                )
                self.assertEqual(
                    artifacts["map_candidates"],
                    [str((build / name).resolve()) for name in map_names],
                )
                singleton_candidates = (
                    artifacts["map_candidates"]
                    if ambiguous_role == "elf"
                    else artifacts["elf_candidates"]
                )
                self.assertEqual(len(singleton_candidates), 1)

                args = argparse.Namespace(
                    project_dir=str(project),
                    build_dir=str(build),
                    command=["fake-build"],
                    cwd=None,
                    env=[],
                    offline=False,
                    timeout_seconds=1,
                    artifact=[],
                    artifact_elf=None,
                    artifact_hex=None,
                    artifact_map=None,
                )
                output = io.StringIO()
                with (
                    patch.object(
                        native_build, "run_owned", return_value=SimpleNamespace(returncode=0)
                    ),
                    redirect_stdout(output),
                ):
                    self.assertEqual(native_build.run_build(args), 0)

                evidence = json.loads(output.getvalue())
                self.assertEqual(
                    {role: evidence["artifacts"][role] for role in ("elf", "hex", "map")},
                    {"elf": None, "hex": None, "map": None},
                )
                self.assertEqual(
                    evidence["artifacts"]["artifact_selection"], "explicit_declaration_required"
                )
                self.assertEqual(
                    evidence["artifacts"]["elf_candidates"],
                    [str((build / name).resolve()) for name in elf_names],
                )
                self.assertEqual(
                    evidence["artifacts"]["map_candidates"],
                    [str((build / name).resolve()) for name in map_names],
                )
                self.assertEqual(
                    evidence["artifacts"]["map_candidates"], artifacts["map_candidates"]
                )
                self.assertEqual(
                    evidence["artifacts"]["elf_candidates"], artifacts["elf_candidates"]
                )


class ResetConnectRoutingRegressionTests(unittest.TestCase):
    @staticmethod
    def _board() -> BoardConfig:
        return BoardConfig("board", "Board", "mcu", "probe", "profile-target", "probe", (), (), 0)

    def test_reset_connect_ignores_ambient_uid_and_target_but_normal_resolution_keeps_compatibility(
        self,
    ) -> None:
        board = self._board()
        manager = ConnectionManager()
        handle = TargetSessionHandle(None, board, "profile-uid", "route", "profile-target")
        repository = Mock()
        repository.load.side_effect = ProfileError("no stored profile")
        repository.store.layout.board_profile.return_value.is_file.return_value = False
        connect = Mock(return_value=handle)
        assignment = SimpleNamespace(runtime_session=object())

        with (
            patch.dict(
                "os.environ",
                {"PYOCD_PROBE_UID": "ambient-uid", "PYOCD_TARGET": "ambient-target"},
                clear=False,
            ),
            patch.object(server, "connection_manager", manager),
            patch.object(server, "_profile_repository", repository),
            patch.object(server, "resolve_board_config", return_value=board) as resolve_board,
            patch.object(
                server, "_resolve_probe_uid_for_connect", return_value="profile-uid"
            ) as resolve_uid,
            patch.object(server.target_control, "connect_under_reset", connect),
            patch.object(server, "_promote_open_session", return_value=assignment),
            patch.object(server, "_record_event"),
        ):
            server._connect_under_reset_impl("board", None, None)

        resolve_board.assert_called_once_with("board", None, allow_environment_overrides=False)
        resolve_uid.assert_called_once_with(board, None, allow_environment_override=False)
        self.assertEqual(connect.call_args.kwargs["unique_id"], "profile-uid")
        self.assertEqual(connect.call_args.kwargs["target"], "profile-target")
        with patch.dict("os.environ", {"PYOCD_PROBE_UID": "ambient-uid"}, clear=False):
            self.assertEqual(server._resolve_probe_uid_for_connect(None, None), "ambient-uid")

    def test_adapter_reset_connect_does_not_reintroduce_ambient_routing_but_open_does(self) -> None:
        class Probe:
            unique_id = "chosen-probe"

            @staticmethod
            def assert_reset(_asserted: bool) -> None:
                pass

        class Target:
            @staticmethod
            def halt() -> None:
                pass

            @staticmethod
            def get_state() -> object:
                return SimpleNamespace(name="HALTED")

        class Session:
            probe = Probe()
            target = Target()

            @staticmethod
            def open() -> None:
                pass

        interface = swd_pyocd.PyOCDSWDInterface()
        session = Session()
        with (
            patch.dict(
                "os.environ",
                {"PYOCD_PROBE_UID": "ambient-uid", "PYOCD_TARGET": "ambient-target"},
                clear=False,
            ),
            patch.object(interface, "_choose_session", return_value=session) as choose,
            patch.object(interface, "_verify_session_pack_source"),
            patch.object(swd_pyocd, "verified_pack_for_target", return_value=None),
        ):
            reset_handle = interface.connect_under_reset(board=None, unique_id=None, target=None)

        self.assertIsNone(choose.call_args.kwargs["probe_uid"])
        self.assertNotIn("target_override", choose.call_args.kwargs["options"])
        self.assertIsNone(reset_handle.target_override)

        with (
            patch.dict(
                "os.environ",
                {"PYOCD_PROBE_UID": "ambient-uid", "PYOCD_TARGET": "ambient-target"},
                clear=False,
            ),
            patch.object(interface, "_choose_session", return_value=session) as choose,
            patch.object(interface, "_verify_session_pack_source"),
            patch.object(swd_pyocd, "verified_pack_for_target", return_value=None),
        ):
            ordinary_handle = interface.open(board=None, unique_id=None, target=None)

        self.assertEqual(choose.call_args.kwargs["probe_uid"], "ambient-uid")
        self.assertEqual(ordinary_handle.target_override, "ambient-target")


class RecoveryCleanupRegressionTests(unittest.TestCase):
    def test_successful_recovery_closes_both_resources_and_requires_fresh_connection(self) -> None:
        manager = ConnectionManager()
        handle = TargetSessionHandle(None, None, "probe", "route", None)
        root = Path("test-run")
        runtime = SessionRecord(
            "session",
            "board",
            "connection",
            "probe",
            "route",
            "now",
            root,
            root / "events",
            root / "summary",
        )
        manager.assign("board", handle, runtime)
        gate = Mock()
        assignments = Mock()
        plans = Mock()
        sessions = Mock()

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "gate_manager", gate),
            patch.object(server, "assignment_store", assignments),
            patch.object(server, "plan_engine", plans),
            patch.object(server, "_session_store", sessions),
            patch.object(server.target_control, "close_session") as close_target,
        ):
            server._finalize_unlock_recovery("board")

        with self.assertRaises(BoardNotConnectedError):
            manager.handle_for("board")
        close_target.assert_called_once_with(handle)
        sessions.mark_recover_completed.assert_called_once_with(runtime)
        sessions.close_session.assert_called_once_with(runtime)
        gate.clear.assert_called_once_with(
            "board", "target recovery completed; reconnect and validate required"
        )
        assignments.clear_board.assert_called_once_with("board")
        plans.invalidate_board.assert_called_once_with("board", "target recovery completed")

    def test_recovery_revokes_assignment_before_cleanup_failure(self) -> None:
        manager = ConnectionManager()
        handle = TargetSessionHandle(None, None, "probe", "route", None)
        root = Path("test-run")
        runtime = SessionRecord(
            "session",
            "board",
            "connection",
            "probe",
            "route",
            "now",
            root,
            root / "events",
            root / "summary",
        )
        manager.assign("board", handle, runtime)
        gate = Mock()
        assignments = Mock()
        plans = Mock()
        sessions = Mock()

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "gate_manager", gate),
            patch.object(server, "assignment_store", assignments),
            patch.object(server, "plan_engine", plans),
            patch.object(server, "_session_store", sessions),
            patch.object(
                server.target_control, "close_session", side_effect=OSError("worker close failed")
            ),
            self.assertRaisesRegex(RuntimeError, "worker close failed"),
        ):
            server._finalize_unlock_recovery("board")

        self.assertIsNone(manager.maybe_connection("board"))
        gate.clear.assert_called_once()
        assignments.clear_board.assert_called_once_with("board")
        plans.invalidate_board.assert_called_once()
        sessions.mark_recover_completed.assert_called_once_with(runtime)
        sessions.close_session.assert_called_once_with(runtime)
