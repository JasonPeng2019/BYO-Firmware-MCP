"""Adversarial regression tests for CL-001 through CL-004.

These assertions deliberately test the public hand-off boundaries so a partial
implementation cannot satisfy the plan merely by changing wording.
"""

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
        from pyocd_debug_mcp.adapters import swd_pyocd

        target = SimpleNamespace(
            reset_and_halt=Mock(),
            reset=Mock(side_effect=OSError("reset transport vanished")),
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
        from pyocd_debug_mcp.adapters import swd_pyocd
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        target = SimpleNamespace(reset_and_halt=Mock(), reset=Mock(), get_state=Mock())
        handle = TargetSessionHandle(
            SimpleNamespace(target=target), _board(), "probe", "fake", None
        )
        with patch.object(swd_pyocd, "FileProgrammer") as programmer:
            programmer.return_value.program.side_effect = OSError("verify failed")
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
        with patch.object(swd_pyocd, "FileProgrammer"):
            self.assertEqual(
                interface.flash(handle, Path("firmware.hex"), halt_after_reset=True), "halted"
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
            ActionContext,
            SessionRecord,
            ToolOutcome,
        )
        from pyocd_debug_mcp.tools.flash import FlashToolServices, build_flash_handlers

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


if __name__ == "__main__":
    unittest.main()
