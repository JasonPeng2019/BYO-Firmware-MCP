"""Live public-contract coverage for the atomic Slice 2B surface."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from jsonschema import ValidationError, validate as validate_json_schema

from firmware_mcp import server
from firmware_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from firmware_mcp.target_errors import TargetStateError


DIRECT_TOOLS = {
    "refresh_safety_map",
    "get_setup_overview",
    "setup_board",
    "repair_board_setup",
    "continue_board_setup",
    "validate_board",
    "get_setup_status",
    "connect_board",
    "disconnect_board",
    "get_board_info",
    "get_target_state",
    "halt_target",
    "resume_target",
    "step_target",
    "reset_target",
    "read_cpu_register",
    "write_cpu_register",
    "write_peripheral_register",
    "read_memory",
    "write_memory",
    "find_symbol",
    "set_breakpoint",
    "remove_breakpoint",
    "build_firmware",
    "collect_build_artifacts",
    "flash_firmware",
    "read_serial",
    "write_serial",
    "exchange_serial",
    "wait_duration",
    "recover_target",
}

GUARD_TOOLS = {
    "request_hardware_permission",
    "get_hardware_permission",
    "revoke_hardware_permission",
    "create_hardware_plan",
    "get_hardware_plan",
    "cancel_hardware_plan",
}

FINAL_TOOLS = DIRECT_TOOLS | GUARD_TOOLS

GUARDED_ACTIONS = {
    "refresh_safety_map",
    "setup_board",
    "repair_board_setup",
    "continue_board_setup",
    "validate_board",
    "connect_board",
    "get_target_state",
    "halt_target",
    "resume_target",
    "step_target",
    "reset_target",
    "read_cpu_register",
    "write_cpu_register",
    "write_peripheral_register",
    "read_memory",
    "write_memory",
    "set_breakpoint",
    "remove_breakpoint",
    "flash_firmware",
    "read_serial",
    "write_serial",
    "exchange_serial",
    "recover_target",
}

REMOVED_TOOLS = {
    "initialization_handshake",
    "setup_overview",
    "board_setup",
    "board_fix_setup",
    "continue_setup",
    "board_validate",
    "connect",
    "connect_override",
    "connect_under_reset",
    "disconnect",
    "get_state",
    "halt",
    "resume",
    "step",
    "reset_and_halt",
    "reset_and_run",
    "read_execution_state",
    "set_execution_state",
    "register_write",
    "read_memory_address",
    "read_memory_symbol",
    "flash_application",
    "flash_bootloader",
    "serial_exchange",
    "wait",
    "target_unlock",
}


class Slice2BPublicContractTests(unittest.TestCase):
    @staticmethod
    def _raw(tool: object):
        function = getattr(tool, "fn")
        return getattr(function, "_guarded_raw_handler", function)

    @staticmethod
    def _profile(
        board_id: str,
        display_name: str,
        *,
        complete: bool,
    ) -> SimpleNamespace:
        document: dict[str, object] = {}
        if complete:
            document["datasheet_sha256"] = "a" * 64
        return SimpleNamespace(
            board_id=board_id,
            display_name=display_name,
            mcu_part_number="EXACT-PART",
            to_document=lambda: document,
        )

    def _overview(
        self,
        profile: SimpleNamespace | None,
        display_name: str,
    ) -> dict[str, object]:
        inventory = ValidationInventory(
            probes=(ValidationProbe("probe-1", "Probe one", "provider", "probe-1"),)
        )
        with (
            patch.object(
                server._profile_repository,
                "load_all",
                return_value=[] if profile is None else [profile],
            ),
            patch.object(server, "_validation_inventory", return_value=inventory),
            patch.object(server, "_replace_setup_assignments"),
        ):
            return dict(server._setup_overview([display_name], {display_name: "probe:probe-1"}))

    def _overview_route(
        self,
        profile: SimpleNamespace | None,
        display_name: str,
    ) -> dict[str, object]:
        result = self._overview(profile, display_name)
        routes = cast(list[dict[str, object]], result["routes"])
        self.assertEqual(len(routes), 1)
        return routes[0]

    def test_exact_final_tools_are_the_only_manager_tools(self) -> None:
        manager_tools = set(server.mcp._tool_manager._tools)
        self.assertEqual(manager_tools, FINAL_TOOLS)
        self.assertTrue(REMOVED_TOOLS.isdisjoint(manager_tools))
        self.assertEqual(set(server.tool_registry.advertised()), FINAL_TOOLS)

    def test_guarded_actions_require_plan_ids_without_hiding_visible_tools(self) -> None:
        before = set(server.mcp._tool_manager._tools)
        for name, tool in server.mcp._tool_manager._tools.items():
            required = set(tool.parameters.get("required", []))
            with self.subTest(tool=name):
                if name in GUARDED_ACTIONS:
                    self.assertIn("plan_id", required)
                elif name not in {"get_hardware_plan", "cancel_hardware_plan"}:
                    self.assertNotIn("plan_id", required)
        self.assertEqual(set(server.mcp._tool_manager._tools), before)

    def test_final_schemas_use_the_shared_vocabulary(self) -> None:
        properties = {
            name: set(tool.parameters["properties"])
            for name, tool in server.mcp._tool_manager._tools.items()
        }
        for name in GUARDED_ACTIONS:
            self.assertIn("plan_id", properties[name])
        for name in DIRECT_TOOLS - GUARDED_ACTIONS:
            self.assertNotIn("plan_id", properties[name])
        self.assertEqual(
            properties["connect_board"],
            {"board_id", "probe_id", "target", "board_config_path", "under_reset", "plan_id"},
        )
        self.assertEqual(properties["reset_target"], {"board_id", "halt_after_reset", "plan_id"})
        self.assertEqual(properties["read_cpu_register"], {"board_id", "register_name", "plan_id"})
        self.assertEqual(
            properties["write_cpu_register"],
            {"board_id", "register_name", "value", "verify", "plan_id"},
        )
        self.assertEqual(
            properties["read_memory"],
            {"board_id", "address", "width_bits", "length_bytes", "plan_id"},
        )
        self.assertEqual(
            properties["write_memory"],
            {"board_id", "address", "value", "width_bits", "verify", "plan_id"},
        )
        self.assertEqual(properties["find_symbol"], {"board_id", "query", "elf_path"})
        self.assertEqual(
            properties["set_breakpoint"], {"board_id", "address", "elf_path", "plan_id"}
        )
        self.assertEqual(
            properties["refresh_safety_map"],
            {"board_id", "layout_path", "application_elf_path", "plan_id"},
        )
        self.assertEqual(properties["remove_breakpoint"], {"board_id", "address", "plan_id"})
        self.assertEqual(
            properties["flash_firmware"],
            {
                "board_id",
                "firmware_path",
                "flash_role",
                "halt_after_reset",
                "artifact_target_evidence_path",
                "plan_id",
            },
        )
        self.assertEqual(
            properties["read_serial"],
            {
                "board_id",
                "expected_text",
                "timeout_seconds",
                "baud",
                "port",
                "reset_on_open",
                "plan_id",
            },
        )
        self.assertEqual(
            properties["write_serial"],
            {"board_id", "text", "baud", "port", "line_ending", "timeout_seconds", "plan_id"},
        )
        self.assertIn("timeout_seconds", properties["exchange_serial"])
        self.assertIn("baud", properties["exchange_serial"])
        forbidden = {"baudrate", "read_seconds", "ready_seconds", "append_newline", "probe_uid"}
        self.assertTrue(all(forbidden.isdisjoint(fields) for fields in properties.values()))

    def test_each_live_tool_description_teaches_its_schema(self) -> None:
        for name, tool in server.mcp._tool_manager._tools.items():
            with self.subTest(tool=name):
                description = tool.description.casefold()
                for heading in (
                    "**what**",
                    "**when**",
                    "**parameters**",
                    "**returns**",
                    "**failures and recovery**",
                ):
                    self.assertIn(heading, description)
                for parameter in tool.parameters["properties"]:
                    self.assertIn(parameter.casefold(), description)
                self.assertIn("example", description)
                fields = tool.parameters["properties"]
                if {"timeout_seconds", "duration_seconds"} & set(fields):
                    self.assertIn("seconds", description)
                if "baud" in fields:
                    self.assertIn("baud", description)
                if "address" in fields:
                    self.assertIn("address", description)
                if "width_bits" in fields:
                    self.assertIn("bits", description)

    def test_start_here_resource_is_real_and_uses_only_final_routes(self) -> None:
        resources = server.mcp._resource_manager._resources
        self.assertEqual(set(resources), {"firmware://start-here"})
        resource = resources["firmware://start-here"]
        self.assertEqual(resource.mime_type, "text/markdown")
        content = asyncio.run(resource.read())
        self.assertIsInstance(content, str)
        assert isinstance(content, str)
        lowered = content.casefold()
        for phase in ("detect", "configure", "build", "flash", "verify", "debug"):
            self.assertIn(phase, lowered)
        for name in (
            "get_setup_overview",
            "setup_board",
            "connect_board",
            "build_firmware",
            "flash_firmware",
            "recover_target",
            "read_serial",
            "request_hardware_permission",
            "create_hardware_plan",
            "get_hardware_permission",
        ):
            self.assertIn(name, content)
        self.assertIn("destructive-once", content)
        for legacy in REMOVED_TOOLS:
            self.assertNotIn(f"`{legacy}`", content)

    def test_public_docs_are_portable_and_do_not_teach_removed_routes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        documents = (
            root / "README.md",
            root / "SERVER_GUIDE.md",
            root / "docs" / "architecture.md",
            root / "docs" / "client-contract.md",
        )
        for document in documents:
            with self.subTest(document=document.name):
                content = document.read_text(encoding="utf-8")
                for legacy in REMOVED_TOOLS:
                    self.assertNotIn(f"`{legacy}`", content)
                self.assertNotIn("C:\\Users", content)
                self.assertNotIn("Jason", content)
                self.assertNotRegex(content, r"\bCOM[0-9]+\b")

    def test_permission_fallback_docs_describe_the_returned_self_contained_argv(self) -> None:
        root = Path(__file__).resolve().parents[1]
        documents = {
            "firmware://start-here": server.firmware_start_here(),
            "README.md": (root / "README.md").read_text(encoding="utf-8"),
            "SERVER_GUIDE.md": (root / "SERVER_GUIDE.md").read_text(encoding="utf-8"),
            "docs/client-contract.md": (root / "docs" / "client-contract.md").read_text(
                encoding="utf-8"
            ),
        }
        for name, content in documents.items():
            with self.subTest(document=name):
                self.assertIn("approval_argv", content)
                self.assertIn("-m firmware_mcp.server approve-hardware", content)
                self.assertNotIn("byo-firmware-mcp approve-hardware", content)

    def test_public_workflow_teaches_how_to_complete_a_fresh_board_template(self) -> None:
        root = Path(__file__).resolve().parents[1]
        documents = {
            "firmware://start-here": server.firmware_start_here(),
            "README.md": (root / "README.md").read_text(encoding="utf-8"),
            "SERVER_GUIDE.md": (root / "SERVER_GUIDE.md").read_text(encoding="utf-8"),
            "docs/client-contract.md": (root / "docs" / "client-contract.md").read_text(
                encoding="utf-8"
            ),
        }
        required_guidance = (
            "next_call",
            "only when it exists",
            "template_status=non_executable",
            "required_user_facts",
            "arguments_template",
            "complete `setup_board`",
            "do not invent values",
        )
        for name, document in documents.items():
            with self.subTest(document=name):
                lowered = " ".join(document.casefold().split())
                for guidance in required_guidance:
                    self.assertIn(guidance, lowered)
                self.assertTrue(
                    "partial template" in lowered or "not directly invokable" in lowered,
                    "the partial setup template must be described as non-executable",
                )

    def test_setup_routes_and_build_guidance_use_only_live_schemas(self) -> None:
        new_route = self._overview_route(None, "New board")
        repair_route = self._overview_route(
            self._profile("repair_board", "Repair board", complete=False), "Repair board"
        )
        validate_route = self._overview_route(
            self._profile("validate_board", "Validate board", complete=True), "Validate board"
        )

        self.assertEqual(new_route["next_tool"], "setup_board")
        self.assertNotIn("next_call", new_route)
        self.assertEqual(new_route["template_status"], "non_executable")
        template = new_route["arguments_template"]
        self.assertIsInstance(template, dict)
        assert isinstance(template, dict)
        setup_tool = server.mcp._tool_manager.get_tool("setup_board")
        self.assertIsNotNone(setup_tool)
        assert setup_tool is not None
        setup_schema = setup_tool.parameters
        self.assertEqual(set(template), set(setup_schema["properties"]) - {"plan_id"})
        self.assertIsNone(template["mcu_part_number"])
        self.assertIsNone(template["requires_uart"])
        self.assertIsNone(template["datasheet_path"])
        self.assertIsNone(template["baud"])
        self.assertIsNone(template["serial_id"])
        with self.assertRaises(ValidationError):
            validate_json_schema(template, setup_schema)

        for route in (repair_route, validate_route):
            next_call = route["next_call"]
            self.assertIsInstance(next_call, dict)
            assert isinstance(next_call, dict)
            tool_name = next_call["tool"]
            self.assertEqual(route["next_tool"], tool_name)
            self.assertIn(tool_name, server.mcp._tool_manager._tools)
            arguments = next_call["arguments"]
            self.assertIsInstance(arguments, dict)
            assert isinstance(arguments, dict)
            next_tool = server.mcp._tool_manager.get_tool(tool_name)
            self.assertIsNotNone(next_tool)
            assert next_tool is not None
            self.assertEqual(
                set(arguments),
                set(next_tool.parameters["properties"]) - {"plan_id"},
            )
            # A route can supply diagnostic action arguments but never an agent-constructed
            # plan ID; it is intentionally not an executable guarded invocation.
            self.assertNotIn("plan_id", arguments)

        self.assertEqual(repair_route["next_tool"], "repair_board_setup")
        self.assertEqual(validate_route["next_tool"], "validate_board")

        guidance = self._overview(None, "Build board")["build_guidance"]
        self.assertIsInstance(guidance, dict)
        assert isinstance(guidance, dict)
        self.assertEqual(guidance["tool"], "build_firmware")
        template = guidance["arguments_template"]
        self.assertIsInstance(template, dict)
        assert isinstance(template, dict)
        build_tool = server.mcp._tool_manager.get_tool("build_firmware")
        self.assertIsNotNone(build_tool)
        assert build_tool is not None
        self.assertEqual(set(template), set(build_tool.parameters["properties"]))
        guidance_text = str(guidance).casefold()
        self.assertIn("artifacts=[]", guidance_text)
        self.assertNotIn("exactly one", guidance_text)
        self.assertNotIn("ambiguous output", guidance_text)
        self.assertNotIn("must declare", guidance_text)

    def test_setup_overview_teaches_and_accepts_familiar_name_assignments(self) -> None:
        tool = server.mcp._tool_manager.get_tool("get_setup_overview")
        self.assertIsNotNone(tool)
        assert tool is not None
        description = tool.description
        self.assertIn("each familiar name appearing in `board_names`", description)
        self.assertIn('`board_names=["left"]`', description)
        self.assertIn('`connection_assignments={"left": "probe:probe-1"}`', description)
        self.assertNotIn("maps board IDs", description)

        inventory = ValidationInventory(
            probes=(ValidationProbe("probe-1", "Probe one", "provider", "probe-1"),)
        )
        with (
            patch.object(server._profile_repository, "load_all", return_value=[]),
            patch.object(server, "_validation_inventory", return_value=inventory),
            patch.object(server, "_replace_setup_assignments") as replace_assignments,
        ):
            overview = server._setup_overview(["left"], {"left": "probe:probe-1"})

        self.assertEqual(overview["status"], "setup_routes_ready")
        routes = cast(list[dict[str, object]], overview["routes"])
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["display_name"], "left")
        self.assertEqual(routes[0]["board_id"], "left")
        replace_assignments.assert_called_once_with(
            {"probe:probe-1": "left"}, "setup overview assignment replaced"
        )

    def test_final_reset_handler_reports_final_boolean_semantics_and_event_arguments(self) -> None:
        tool = server.mcp._tool_manager.get_tool("reset_target")
        self.assertIsNotNone(tool)
        assert tool is not None

        for halt_after_reset, observed in ((False, "RUNNING"), (True, "HALTED")):
            with self.subTest(halt_after_reset=halt_after_reset):
                events: list[tuple[str, dict[str, object]]] = []
                with (
                    patch.object(server.connection_manager, "lock_for", return_value=nullcontext()),
                    patch.object(server, "_handle", return_value=SimpleNamespace()),
                    patch.object(server, "_runtime_for", return_value=None),
                    patch.object(server.target_control, "reset", return_value=observed),
                    patch.object(
                        server,
                        "_record_event",
                        side_effect=lambda tool_name, arguments, **_kwargs: events.append(
                            (tool_name, dict(arguments))
                        ),
                    ),
                ):
                    response = self._raw(tool)("board-a", halt_after_reset)

                self.assertIn(f"halt_after_reset={str(halt_after_reset).lower()}", response)
                self.assertIn(f"observed_state={observed}", response)
                self.assertNotIn("reset_and_halt", response)
                self.assertNotIn("reset_and_run", response)
                self.assertEqual(
                    events,
                    [
                        (
                            "reset_target",
                            {"board_id": "board-a", "halt_after_reset": halt_after_reset},
                        )
                    ],
                )

    def test_final_reset_failure_uses_boolean_vocabulary_in_client_and_event_evidence(self) -> None:
        tool = server.mcp._tool_manager.get_tool("reset_target")
        self.assertIsNotNone(tool)
        assert tool is not None

        events: list[tuple[str, dict[str, object], dict[str, object]]] = []
        backend = SimpleNamespace(reset_and_halt=Mock())
        with (
            patch.object(server.connection_manager, "lock_for", return_value=nullcontext()),
            patch.object(server, "_handle", return_value=SimpleNamespace()),
            patch.object(server, "_runtime_for", return_value=None),
            patch.object(server.target_control, "_BACKEND", backend),
            patch.object(server.target_control, "get_state", return_value="RUNNING"),
            patch.object(
                server,
                "_record_event",
                side_effect=lambda tool_name, arguments, **kwargs: events.append(
                    (tool_name, dict(arguments), dict(kwargs["details"]))
                ),
            ),
        ):
            with self.assertRaisesRegex(
                TargetStateError,
                r"halt_after_reset=true; observed_state=RUNNING; expected_state=HALTED",
            ) as raised:
                self._raw(tool)("board-a", True)

        backend.reset_and_halt.assert_called_once()
        message = str(raised.exception)
        self.assertIn("halt_after_reset=true", message)
        self.assertIn("observed_state=RUNNING", message)
        self.assertIn("expected_state=HALTED", message)
        self.assertEqual(len(events), 1)
        tool_name, arguments, details = events[0]
        self.assertEqual(tool_name, "reset_target")
        self.assertEqual(arguments, {"board_id": "board-a", "halt_after_reset": True})
        self.assertEqual(details["message"], message)
        for client_evidence in (message, str(details)):
            lowered = client_evidence.casefold()
            for legacy in ("reset_and_halt", "reset_and_run", "reset-and-halt", "reset-and-run"):
                self.assertNotIn(legacy, lowered)

    def test_client_visible_setup_and_recovery_guidance_has_no_removed_route(self) -> None:
        client_output_sources = (
            inspect.getsource(server._setup_overview),
            inspect.getsource(server._get_setup_status),
            inspect.getsource(server._setup_continue),
            inspect.getsource(server.get_board_info),
            inspect.getsource(server.reset),
        )
        removed_client_routes = (
            '"board_setup"',
            '"board_fix_setup"',
            "`connect`",
            "reset_and_halt",
            "reset_and_run",
            "reset-and-halt",
            "reset-and-run",
        )
        for source in client_output_sources:
            for removed in removed_client_routes:
                self.assertNotIn(removed, source)

    def test_production_wiring_has_no_legacy_migration_aliases(self) -> None:
        production_source = inspect.getsource(server)
        for alias in (
            "_legacy_connect",
            "_legacy_disconnect",
            "_legacy_get_board_info",
            "_legacy_get_state",
            "_legacy_halt",
            "_legacy_resume",
            "_legacy_step",
            "_legacy_reset",
            "_legacy_read_core_register",
            "_legacy_write_core_register",
        ):
            with self.subTest(alias=alias):
                self.assertNotIn(alias, production_source)

    def test_final_peripheral_handler_logs_its_final_operation_name(self) -> None:
        tool = server.mcp._tool_manager.get_tool("write_peripheral_register")
        self.assertIsNotNone(tool)
        assert tool is not None
        event_names: list[str] = []
        with (
            patch.object(server.connection_manager, "lock_for", return_value=nullcontext()),
            patch.object(server, "_handle", return_value=SimpleNamespace()),
            patch.object(server, "_runtime_for", return_value=None),
            patch.object(server, "_require_physical_access"),
            patch.object(server, "_require_safety_access"),
            patch.object(server.target_control, "write_memory"),
            patch.object(
                server,
                "_record_event",
                side_effect=lambda tool_name, *_args, **_kwargs: event_names.append(tool_name),
            ),
        ):
            result = self._raw(tool)("board-a", "0x40000000", "0xffffffff", "0x12345678", 32, False)

        self.assertIn("verification=not_requested", result)
        self.assertEqual(event_names, ["write_peripheral_register"])


if __name__ == "__main__":
    unittest.main()
