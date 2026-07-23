"""Regression coverage for planning a stored route before its first connection."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Mapping, cast
from unittest.mock import patch

from firmware_mcp import server
from firmware_mcp.board_config import BoardConfig
from firmware_mcp.firmstore.profiles import BoardProfile, ProfileError
from firmware_mcp.guardrails.core import GuardCore, GuardError
from firmware_mcp.services.connections import BoardNotConnectedError, ConnectionManager
from firmware_mcp.setup_flow.setup import RunAssignmentStore


class _Profiles:
    """Small already-verified profile repository for public guard integration."""

    def __init__(self, profiles: Mapping[str, BoardProfile]) -> None:
        self.profiles = dict(profiles)

    def load(self, board_id: str) -> BoardProfile:
        try:
            return self.profiles[board_id]
        except KeyError as exc:
            raise ProfileError(f"Board profile not found: {board_id}") from exc


class _AcceptingContext:
    async def elicit(self, _message: str, _schema: object) -> SimpleNamespace:
        return SimpleNamespace(
            action="accept",
            data=server._HardwarePermissionReply(approved=True, call_budget=4),
        )


def _profile(
    root: Path,
    *,
    board_id: str = "board_a",
    provider_id: str = "pyocd",
    target: str = "nRF52840_xxAA",
) -> BoardProfile:
    support: dict[str, str]
    if provider_id.casefold() == "pyocd":
        support = {
            "kind": "resolved_builtin_target",
            "support_id": "builtin:nrf52840",
            "part_number": "nRF52840",
            "pyocd_target": target,
        }
    else:
        support = {
            "kind": "provider_recipe",
            "support_id": "recipe:LabTool:chip-x",
            "provider_id": provider_id,
            "target": target,
            "part_number": "Chip-X",
        }
    board = BoardConfig(
        board_id=board_id,
        display_name="HIL board",
        mcu_family="test",
        probe_family="test",
        provider_id=provider_id,
        target=target,
    )
    document: dict[str, object] = {
        "schema_version": 2,
        "board_id": board_id,
        "display_name": board.display_name,
        "mcu_part_number": "nRF52840" if provider_id.casefold() == "pyocd" else "Chip-X",
        "mcu_family": board.mcu_family,
        "probe_family": board.probe_family,
        "provider_id": provider_id,
        "target": target,
        "device_support": support,
    }
    return BoardProfile(
        schema_version=2,
        mcu_part_number=cast(str, document["mcu_part_number"]),
        board=board,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        safety_ref=None,
        device_support=support,
        source_path=root / f"{board_id}.json",
        _document=document,
    )


class ConnectPlanBootstrapTests(unittest.TestCase):
    """Public guard APIs may plan, but not bypass, a disconnected stored route."""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profiles = _Profiles({"board_a": _profile(self.root)})
        self.assignments = RunAssignmentStore({})
        self.assignments.assign("probe:000123", "board_a")
        self.connections = ConnectionManager()
        self.original_core = server._guard_core
        guarded_names = {
            "connect_board",
            "setup_board",
            "repair_board_setup",
            "continue_board_setup",
            "validate_board",
            "flash_firmware",
            "write_memory",
            "recover_target",
        }
        self.core = GuardCore(
            project_root=self.root,
            run_id="hil-r3",
            action_specs={name: self.original_core.action_specs[name] for name in guarded_names},
            evidence_for=server._guard_evidence,
        )
        self.patches = [
            patch.object(server, "_project_root", self.root),
            patch.object(server, "_profile_repository", self.profiles),
            patch.object(server, "assignment_store", self.assignments),
            patch.object(server, "connection_manager", self.connections),
            patch.object(server, "_guard_core", self.core),
        ]
        for replacement in self.patches:
            replacement.start()

    def tearDown(self) -> None:
        for replacement in reversed(self.patches):
            replacement.stop()
        self.temporary.cleanup()

    @staticmethod
    def _connect_arguments(
        *, probe_id: str | None = None, target: str | None = None, under_reset: bool = False
    ) -> dict[str, object]:
        return {
            "board_id": "board_a",
            "probe_id": probe_id,
            "target": target,
            "board_config_path": None,
            "under_reset": under_reset,
        }

    @classmethod
    def _connect_action(
        cls, *, probe_id: str | None = None, target: str | None = None, under_reset: bool = False
    ) -> dict[str, object]:
        return {
            "tool": "connect_board",
            "arguments": cls._connect_arguments(
                probe_id=probe_id, target=target, under_reset=under_reset
            ),
            "max_calls": 1,
        }

    def _grant(self, board_id: str = "board_a") -> str:
        requested = asyncio.run(
            server.request_hardware_permission(
                board_id=board_id,
                scope="routine-session",
                ctx=cast(Any, _AcceptingContext()),
            )
        )
        self.assertIsInstance(requested, dict)
        self.assertEqual(cast(dict[str, object], requested)["approval"], "recorded")
        grant = server.get_hardware_permission(
            cast(str, cast(dict[str, object], requested)["request_id"])
        )
        self.assertIsInstance(grant, dict)
        return cast(str, cast(dict[str, object], grant)["grant_id"])

    def _plan(
        self, action: dict[str, object], *, grant_id: str | None = None
    ) -> dict[str, object] | str:
        return server.create_hardware_plan(
            self._grant() if grant_id is None else grant_id,
            "board_a",
            "connect the returning board",
            "a session with current stored evidence",
            [action],
        )

    def test_disconnected_valid_route_plans_canonical_pyocd_assignment_and_invalidates(
        self,
    ) -> None:
        result = self._plan(self._connect_action(probe_id="PROBE:123"))
        self.assertIsInstance(result, dict)
        record = cast(dict[str, object], result)
        plan_id = cast(str, record["plan_id"])
        action = cast(list[dict[str, object]], record["actions"])[0]
        arguments = cast(dict[str, object], action["arguments"])
        self.assertEqual(arguments["probe_id"], "probe:000123")
        self.assertEqual(arguments["target"], "nRF52840_xxAA")
        self.assertFalse(self.connections.maybe_connection("board_a"))

        def fake_connect(
            board_id: str,
            probe_id: str | None = None,
            target: str | None = None,
            board_config_path: str | None = None,
            under_reset: bool = False,
        ) -> str:
            del board_id, probe_id, target, board_config_path, under_reset
            return "fake stored-route connection"

        wrapped = server._guarded_handler("connect_board", fake_connect)
        self.assertEqual(
            wrapped(
                board_id="board_a",
                probe_id="probe:000123",
                target="nRF52840_xxAA",
                board_config_path=None,
                under_reset=False,
                plan_id=plan_id,
            ),
            "fake stored-route connection",
        )
        grant_id = cast(str, record["grant_id"])
        self.assertFalse(self.core._grants[grant_id].active)
        self.assertEqual(self.core._grants[grant_id].close_reason, "connect_board-changed-evidence")

    def test_under_reset_and_pyocd_equivalents_use_the_same_stored_route(self) -> None:
        null_probe = server._connect_route_classification(
            "board_a", self._connect_arguments(under_reset=True)
        )
        self.assertEqual(
            cast(dict[str, object], null_probe["effects"])["connection_assignment"],
            "probe:000123",
        )
        classification = server._connect_route_classification(
            "board_a", self._connect_arguments(probe_id="000123", under_reset=True)
        )
        effects = cast(dict[str, object], classification["effects"])
        self.assertEqual(effects["connection_assignment"], "probe:000123")
        self.assertTrue(effects["under_reset"])

        result = self._plan(self._connect_action(probe_id="000123", under_reset=True))
        self.assertIsInstance(result, dict)
        record = cast(dict[str, object], result)
        plan_id = cast(str, record["plan_id"])
        arguments = cast(
            dict[str, object], cast(list[dict[str, object]], record["actions"])[0]["arguments"]
        )
        self.assertEqual(arguments["probe_id"], "probe:000123")
        self.assertTrue(arguments["under_reset"])
        wrapped = server._guarded_handler(
            "connect_board",
            lambda board_id, probe_id=None, target=None, board_config_path=None, under_reset=False: (
                "under-reset" if under_reset else "wrong mode"
            ),
        )
        self.assertEqual(
            wrapped(
                board_id="board_a",
                probe_id="probe:000123",
                target="nRF52840_xxAA",
                board_config_path=None,
                under_reset=True,
                plan_id=plan_id,
            ),
            "under-reset",
        )

    def test_distinct_or_generic_opaque_route_override_is_rejected_before_provider(self) -> None:
        with self.assertRaisesRegex(GuardError, "stored stable probe assignment"):
            server._connect_route_classification("board_a", self._connect_arguments(probe_id="999"))
        with self.assertRaisesRegex(GuardError, "stored verified support target"):
            server._connect_route_classification(
                "board_a", self._connect_arguments(target="wrong-target")
            )
        external_config = self._connect_arguments()
        external_config["board_config_path"] = "outside-profile.json"
        with self.assertRaisesRegex(GuardError, "stored profile"):
            server._connect_route_classification("board_a", external_config)

        self.profiles.profiles["board_a"] = _profile(
            self.root, provider_id="LabTool", target="chip-x"
        )
        self.assignments.replace({"provider:LabTool:Port-01": "board_a"})
        generic_arguments = self._connect_arguments(
            probe_id="provider:LabTool:Port-01", target="chip-x"
        )
        generic = server._connect_route_classification("board_a", generic_arguments)
        self.assertEqual(
            cast(dict[str, object], generic["effects"])["connection_assignment"],
            "provider:LabTool:Port-01",
        )
        with self.assertRaisesRegex(GuardError, "opaque"):
            server._connect_route_classification(
                "board_a",
                self._connect_arguments(probe_id="provider:labtool:Port-01", target="chip-x"),
            )

    def test_generic_preconnection_actions_need_no_live_handle_but_dynamic_actions_do(self) -> None:
        grant_id = self._grant()
        setup_actions: list[object] = [
            {
                "tool": "setup_board",
                "arguments": {
                    "board_id": "board_a",
                    "connection_id": "probe:000123",
                    "display_name": "HIL board",
                    "mcu_part_number": "nRF52840",
                    "requires_uart": False,
                    "baud": None,
                    "serial_id": None,
                    "datasheet_path": "datasheet.pdf",
                    "provider_recipe": None,
                },
                "max_calls": 1,
            },
            {
                "tool": "repair_board_setup",
                "arguments": {"board_id": "board_a"},
                "max_calls": 1,
            },
            {
                "tool": "continue_board_setup",
                "arguments": {
                    "board_id": "board_a",
                    "continuation_id": "continuation-current",
                    "response": {},
                },
                "max_calls": 1,
            },
            {
                "tool": "validate_board",
                "arguments": {"board_id": "board_a"},
                "max_calls": 1,
            },
        ]
        planned = server.create_hardware_plan(
            grant_id,
            "board_a",
            "finish setup diagnostics",
            "current setup evidence",
            setup_actions,
        )
        self.assertIsInstance(planned, dict)
        self.assertFalse(self.connections.maybe_connection("board_a"))
        for tool, arguments in (
            ("flash_firmware", {"firmware_path": "image.hex", "flash_role": "application"}),
            ("write_memory", {"address": 0x20000000, "width_bits": 32, "value": 1}),
            ("recover_target", {"mechanism": "mass-erase"}),
        ):
            with self.assertRaises(BoardNotConnectedError):
                server._guard_classification(tool, "board_a", arguments, None)

    def test_wrong_board_can_get_generic_grant_but_never_a_connect_plan(self) -> None:
        wrong_grant = self._grant("board-a")
        result = server.create_hardware_plan(
            wrong_grant,
            "board-a",
            "connect an illustrative spelling",
            "never reaches a provider",
            [
                {
                    "tool": "connect_board",
                    "arguments": {
                        "board_id": "board-a",
                        "probe_id": None,
                        "target": None,
                        "board_config_path": None,
                        "under_reset": False,
                    },
                    "max_calls": 1,
                }
            ],
        )
        self.assertIsInstance(result, str)
        self.assertIn("guard/connect-profile-missing", result)

    def test_start_here_requires_the_exact_returned_board_id_for_connect_planning(self) -> None:
        start_here = server.firmware_start_here()
        self.assertIn(
            "Copy the exact `board_id` returned by `get_setup_overview`/`get_setup_status`",
            start_here,
        )
        self.assertIn("never a substitute for that live key", start_here)

    def test_public_docs_teach_the_exact_disconnected_stored_route(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for path in (
            root / "README.md",
            root / "SERVER_GUIDE.md",
            root / "docs/client-contract.md",
        ):
            with self.subTest(path=path.name):
                content = " ".join(path.read_text(encoding="utf-8").casefold().split())
                self.assertIn("`board_id`", content)
                self.assertIn("while disconnected", content)
                self.assertTrue(
                    "board_config_path" in content or "external board-config path" in content
                )


if __name__ == "__main__":
    unittest.main()
