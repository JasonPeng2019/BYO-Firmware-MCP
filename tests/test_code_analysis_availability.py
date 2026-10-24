"""Board-independent availability of the five public code-analysis tools.

Contract "Availability and exclusions" and "Public MCP tools": every name is an
ordinary ``TierRoute(name, "metadata", "code-analysis")`` registration in both
profiles, callable with no board, no-setup, setup-lite, setup-full, revoked or
unvalidated identities and an unrelated corrupt policy, without consulting any
hardware backend, probe inventory, board-policy reader/writer or unlock
operation.  Schemas are exact and closed; the analysis JSON stays one intact
text block with any due check-in appended separately.  No hardware is used.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import unittest
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest import mock

from pyocd_debug_mcp.capabilities.matrix import ROUTE_BY_NAME, TierRoute, applicable_route_names
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier

from tests.code_analysis_support import (
    ANALYSIS_TOOLS,
    FAKE_CLANGD,
    TOOL_OPERATIONS,
    FakeClangd,
    assert_envelope,
    assert_failure,
    configure_server_program,
    fixed_resolver,
    mcp_blocks,
    mcp_envelope,
    mcp_refusal,
    raw_range,
    simple_project,
    temporary_directory,
)
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    confirmed_lite_policy,
    known_good_full_policy,
    tiered_test_server,
)

TIER_STATES = (
    "no_board",
    "no_setup",
    "setup_lite",
    "setup_full",
    "revoked_unvalidated",
    "corrupt_policy",
)
PROFILES = {"personal": True, "professional": False}
FULL_BOARD = "legacy_full"
POSITION = {"line": 5, "column": 25}
EXPECTED_SCHEMAS: dict[str, dict[str, Any]] = {
    "get_code_analysis_status": {},
    "find_code_definition": {
        "file_path": None,
        "line": None,
        "column": None,
        "timeout_seconds": 30,
    },
    "find_code_references": {
        "file_path": None,
        "line": None,
        "column": None,
        "timeout_seconds": 30,
        "include_declaration": False,
        "max_results": 100,
    },
    "get_code_hover": {"file_path": None, "line": None, "column": None, "timeout_seconds": 30},
    "get_code_diagnostics": {"file_path": None, "max_results": 100, "timeout_seconds": 30},
}
FORBIDDEN_ARGUMENTS = {
    "board_id": "board",
    "project_root": "C:/elsewhere",
    "runtime_root": "C:/runtime",
    "executable": str(FAKE_CLANGD),
    "clangd_path": str(FAKE_CLANGD),
    "tier": "setup-full",
    "command": "clangd --help",
    "flags": ["-DX"],
    "query_driver": "C:/gcc.exe",
    "method": "workspace/executeCommand",
    "content": "int unsaved;",
}
ECHO = {"uri": "$request_uri", "range": {"start": "$request_position", "end": "$request_position"}}
OK_SCENARIO = {
    "diagnostics_mode": "versioned",
    "diagnostics": [],
    "responses": {
        "textDocument/definition": {
            "result": {"uri": "$request_uri", "range": raw_range((2, 4), (2, 10))}
        },
        "textDocument/references": {"result": [ECHO]},
        "textDocument/hover": {
            "result": {"contents": {"kind": "markdown", "value": "int helper(int)"}}
        },
    },
}


class RecordingPolicyRepository(CapabilityPolicyRepository):
    """Real repository that records every board-policy read or write."""

    def __init__(self, project_root: Path) -> None:
        super().__init__(project_root)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def resolve(self, board_id: str):  # type: ignore[no-untyped-def]
        self._record("resolve", board_id)
        return super().resolve(board_id)

    def commit(self, board_id: str, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        self._record("commit", board_id)
        return super().commit(board_id, *args, **kwargs)

    def downgrade_to_no_setup(self, board_id: str):  # type: ignore[no-untyped-def]
        self._record("downgrade_to_no_setup", board_id)
        return super().downgrade_to_no_setup(board_id)

    def mark_setup_incomplete(self, board_id: str):  # type: ignore[no-untyped-def]
        self._record("mark_setup_incomplete", board_id)
        return super().mark_setup_incomplete(board_id)


def seed_tier_state(root: Path, state: str) -> None:
    """Write the board-policy state with the real repository, before the server."""

    repository = CapabilityPolicyRepository(root)
    if state == "no_setup":
        repository.resolve("raw_board")
    elif state == "setup_lite":
        repository.commit(
            "lite_board",
            Tier.SETUP_LITE,
            map_snapshot=confirmed_lite_policy("lite_board"),
            evidence={"source": "confirmed"},
        )
    elif state == "setup_full":
        profile, memory_map = known_good_full_policy()
        repository.commit(
            FULL_BOARD,
            Tier.SETUP_FULL,
            profile_snapshot=profile,
            map_snapshot=memory_map,
            evidence={"identity": "exact"},
        )
    elif state == "revoked_unvalidated":
        repository.commit(
            "lite_board",
            Tier.SETUP_LITE,
            map_snapshot=confirmed_lite_policy("lite_board"),
            evidence={"source": "confirmed"},
        )
        repository.mark_setup_incomplete("lite_board")
        from pyocd_debug_mcp.firmstore.cache import CACHE_SCHEMA_VERSION
        from pyocd_debug_mcp.firmstore.store import FirmStore

        attachments = FirmStore(root).layout.cache_artifact("attachments.json")
        attachments.parent.mkdir(parents=True, exist_ok=True)
        attachments.write_text(
            json.dumps(
                {
                    "schema_version": CACHE_SCHEMA_VERSION,
                    "records": [
                        {
                            "board_id": "lite_board",
                            "probe_family": "cmsis-dap",
                            "probe_usb_serial": "REVOKED-PROBE",
                            "uart_usb_serial": "REVOKED-UART",
                            "uart_vid": 4660,
                            "uart_pid": 22136,
                            "confirmed": False,
                            "confirmed_at": "2026-01-01T00:00:00+00:00",
                            "revoked_at": "2026-02-01T00:00:00+00:00",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    elif state == "corrupt_policy":
        corrupted = repository.board_root("corrupt_board")
        corrupted.mkdir(parents=True)
        (corrupted / "current.json").write_text("not-json", encoding="utf-8")
    elif state != "no_board":
        raise AssertionError(state)


def firm_digest(root: Path) -> dict[str, bytes]:
    firm = root / ".firm"
    if not firm.is_dir():
        return {}
    return {
        path.relative_to(firm).as_posix(): path.read_bytes()
        for path in sorted(firm.rglob("*"))
        if path.is_file() and path.relative_to(firm).parts[0] != "code-analysis"
    }


@contextlib.contextmanager
def analysis_server(root: Path, *, narrative_logging: bool = True) -> Generator[Any, None, None]:
    import pyocd_debug_mcp.monitor.tools as monitor_tools

    with (
        mock.patch.object(monitor_tools, "NARRATIVE_LOGGING", narrative_logging),
        tiered_test_server(root) as server,
    ):
        yield server


@contextlib.contextmanager
def forbidden_board_io(server: Any) -> Generator[list[str], None, None]:
    """Make every probe-inventory/serial/unlock entry point fail loudly and record."""

    touched: list[str] = []

    def forbidden(name: str):  # type: ignore[no-untyped-def]
        def call(*args: Any, **kwargs: Any) -> Any:
            touched.append(name)
            raise AssertionError(f"code analysis consulted {name}")

        return call

    unlock = mock.MagicMock(name="unlock_coordinator")
    with contextlib.ExitStack() as stack:
        for name in (
            "_run_cmd",
            "list_connected_probes_detailed",
            "resolve_probe_for_board_cli",
            "list_serial_ports",
            "resolve_serial_port",
        ):
            if hasattr(server, name):
                stack.enter_context(mock.patch.object(server, name, forbidden(name)))
        stack.enter_context(mock.patch.object(server, "_unlock_coordinator", unlock))
        yield touched
        if unlock.mock_calls:
            touched.append(f"_unlock_coordinator: {unlock.mock_calls}")


def query_arguments(root: Path, tool: str) -> dict[str, Any]:
    if tool == "get_code_analysis_status":
        return {}
    arguments: dict[str, Any] = {"file_path": str(root / "src" / "main.c"), "timeout_seconds": 10}
    if tool != "get_code_diagnostics":
        arguments.update(POSITION)
    return arguments


class AnalysisCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = temporary_directory("byo-ca-avail-")
        self.base = directory.__enter__()
        self.addCleanup(directory.__exit__, None, None, None)

    def project(self, name: str = "project") -> Path:
        return simple_project(self.base / name).root

    def fake(self, name: str = "fake") -> FakeClangd:
        return FakeClangd(self.base / name, OK_SCENARIO)


class RouteMatrixAndInventory(unittest.TestCase):
    def test_every_tool_is_an_ordinary_metadata_route(self) -> None:
        for name in ANALYSIS_TOOLS:
            with self.subTest(name=name):
                self.assertEqual(
                    ROUTE_BY_NAME.get(name), TierRoute(name, "metadata", "code-analysis")
                )

    def test_both_profiles_keep_all_five_tools(self) -> None:
        for profile, narrative_logging in PROFILES.items():
            with self.subTest(profile=profile):
                self.assertTrue(
                    set(ANALYSIS_TOOLS)
                    <= set(applicable_route_names(narrative_logging=narrative_logging))
                )


class PublicRegistration(AnalysisCase):
    def test_registered_inventory_equals_the_matrix_in_both_profiles(self) -> None:
        for profile, narrative_logging in PROFILES.items():
            with (
                self.subTest(profile=profile),
                analysis_server(
                    self.project(profile), narrative_logging=narrative_logging
                ) as server,
            ):
                # Full registered inventory equals the matrix (the existing
                # tiered-core idiom); analysis tools are also advertised by
                # default, unlike hidden/locked hardware tools.
                registered = set(server.mcp._tool_manager._tools)
                self.assertSetEqual(
                    set(applicable_route_names(narrative_logging=narrative_logging)), registered
                )
                listed = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
                self.assertTrue(set(ANALYSIS_TOOLS) <= listed, sorted(set(ANALYSIS_TOOLS) - listed))
                self.assertFalse(set(ANALYSIS_TOOLS) & set(server.LAYER2_ACTIONS))

    def test_schemas_are_exact_closed_and_annotated_read_only(self) -> None:
        with analysis_server(self.project()) as server:
            tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
        for name, expected in EXPECTED_SCHEMAS.items():
            with self.subTest(name=name):
                schema = tools[name].inputSchema
                properties = schema.get("properties", {})
                self.assertEqual(set(properties), set(expected), schema)
                self.assertIs(schema.get("additionalProperties"), False, schema)
                required = {key for key, default in expected.items() if default is None}
                self.assertEqual(set(schema.get("required", [])), required, schema)
                for key, default in expected.items():
                    if default is not None:
                        self.assertEqual(
                            properties[key].get("default"), default, (key, properties[key])
                        )
                annotations = tools[name].annotations
                self.assertIsNotNone(annotations, name)
                self.assertIs(annotations.readOnlyHint, True)
                self.assertIs(annotations.destructiveHint, False)
                self.assertIs(annotations.idempotentHint, True)
                self.assertIn(
                    "saved", (tools[name].description or "").lower(), "saved-files-only description"
                )


class EveryTierStateWithoutBoardIo(AnalysisCase):
    def _exercise(self, state: str, narrative_logging: bool) -> None:
        root = self.project(f"{state}-{int(narrative_logging)}")
        seed_tier_state(root, state)
        before = firm_digest(root)
        fake = self.fake(f"fake-{state}-{int(narrative_logging)}")
        backend = DeterministicTargetBackend()
        with analysis_server(root, narrative_logging=narrative_logging) as server:
            repository = RecordingPolicyRepository(root)
            server.configure_tiered_test_seams(backend=backend, policy_repository=repository)
            service = configure_server_program(server, fake=fake)
            self.addCleanup(service.shutdown)
            with forbidden_board_io(server) as touched:
                for tool in ANALYSIS_TOOLS:
                    envelope = mcp_envelope(server, tool, query_arguments(root, tool))
                    assert_envelope(self, envelope, TOOL_OPERATIONS[tool], status="ok")
            service.shutdown()
        self.assertEqual(touched, [], "analysis consulted board I/O")
        self.assertEqual(backend.calls, [], "analysis reached the hardware backend")
        self.assertEqual(repository.calls, [], "analysis read or wrote board policy")
        self.assertEqual(firm_digest(root), before, "board state under .firm was rewritten")

    def test_all_tools_in_every_tier_state_and_profile(self) -> None:
        for profile, narrative_logging in PROFILES.items():
            for state in TIER_STATES:
                with self.subTest(profile=profile, state=state):
                    self._exercise(state, narrative_logging)


class ArgumentRefusals(AnalysisCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.project()
        self.fake_peer = self.fake()
        context = analysis_server(self.root)
        self.server = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.resolver = fixed_resolver(FAKE_CLANGD)
        service = configure_server_program(
            self.server, fake=self.fake_peer, program_resolver=self.resolver
        )
        self.addCleanup(service.shutdown)

    def _assert_refused(self, tool: str, arguments: dict[str, Any]) -> None:
        outcome = mcp_refusal(self.server, tool, arguments)
        if isinstance(outcome, dict):
            assert_failure(
                self, outcome, TOOL_OPERATIONS[tool], "error", "analysis/invalid-argument"
            )
        else:
            self.assertTrue(outcome, "an SDK/schema refusal must explain itself")
        self.assertEqual(self.fake_peer.starts(), [], f"{tool} {arguments} started clangd")

    def test_unknown_or_authority_arguments_are_refused_before_resolution(self) -> None:
        for tool in ANALYSIS_TOOLS:
            for key, value in FORBIDDEN_ARGUMENTS.items():
                with self.subTest(tool=tool, argument=key):
                    outcome = mcp_refusal(
                        self.server, tool, {**query_arguments(self.root, tool), key: value}
                    )
                    self.assertIsInstance(outcome, str, f"{tool} accepted {key}: {outcome}")
        self.assertEqual(self.resolver.calls, [], "refused calls must not resolve a program")
        self.assertEqual(self.fake_peer.starts(), [])

    def test_strict_types_and_limits_are_never_coerced(self) -> None:
        position_tools = ("find_code_definition", "find_code_references", "get_code_hover")
        bad_values: list[tuple[str, str, Any]] = []
        for tool in position_tools:
            bad_values += [
                (tool, "line", "5"),
                (tool, "line", 5.0),
                (tool, "line", True),
                (tool, "line", 0),
                (tool, "column", -1),
                (tool, "timeout_seconds", True),
                (tool, "timeout_seconds", "10"),
                (tool, "timeout_seconds", 0),
                (tool, "timeout_seconds", -2.5),
            ]
        for tool in ("find_code_references", "get_code_diagnostics"):
            bad_values += [
                (tool, "max_results", 0),
                (tool, "max_results", 1001),
                (tool, "max_results", "5"),
                (tool, "max_results", True),
                (tool, "max_results", 5.0),
            ]
        bad_values += [
            ("find_code_references", "include_declaration", "true"),
            ("find_code_references", "include_declaration", 1),
            ("get_code_diagnostics", "timeout_seconds", float("nan")),
        ]
        for tool, key, value in bad_values:
            with self.subTest(tool=tool, key=key, value=repr(value)):
                self._assert_refused(tool, {**query_arguments(self.root, tool), key: value})

    def test_uri_and_network_paths_are_invalid_arguments(self) -> None:
        source = self.root / "src" / "main.c"
        for value in (source.as_uri(), "\\\\server\\share\\main.c", "//server/share/main.c"):
            for tool in ("find_code_definition", "get_code_diagnostics"):
                with self.subTest(tool=tool, value=value):
                    self._assert_refused(
                        tool, {**query_arguments(self.root, tool), "file_path": value}
                    )


class MissingProgram(AnalysisCase):
    def test_missing_program_changes_responses_not_discovery(self) -> None:
        root = self.project()
        with analysis_server(root) as server:
            service = configure_server_program(
                server, program_resolver=fixed_resolver(None, version=None, origin=None)
            )
            self.addCleanup(service.shutdown)
            listed = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
            self.assertTrue(set(ANALYSIS_TOOLS) <= listed)
            status = mcp_envelope(server, "get_code_analysis_status", {})
            assert_failure(self, status, "status", "unavailable", "analysis/executable-unavailable")
            for tool in ANALYSIS_TOOLS[1:]:
                with self.subTest(tool=tool):
                    envelope = mcp_envelope(server, tool, query_arguments(root, tool))
                    assert_failure(
                        self,
                        envelope,
                        TOOL_OPERATIONS[tool],
                        "unavailable",
                        "analysis/executable-unavailable",
                    )


class CheckInBlockSeparation(AnalysisCase):
    """The due check-in is a separate block; the analysis JSON stays intact."""

    def _drive_to_boundary(self, tool: str) -> None:
        from pyocd_debug_mcp.monitor.counters import CHECKIN_CADENCE
        from pyocd_debug_mcp.monitor.transport import TestTransport

        root = self.project()
        fake = self.fake()
        with analysis_server(root) as server:
            service = configure_server_program(server, fake=fake)
            self.addCleanup(service.shutdown)
            monitor = server._monitor
            transport = TestTransport()
            monitor._default_transport = lambda workspace=None: transport  # type: ignore[method-assign]
            monitor.boot()
            try:
                asyncio.run(
                    server.mcp.call_tool("initialization_handshake", {"workspace_path": str(root)})
                )
                while monitor._counters.snapshot().total < CHECKIN_CADENCE - 1:
                    asyncio.run(server.mcp.call_tool("server_health_check", {}))
                self.assertEqual(monitor._counters.snapshot().total, CHECKIN_CADENCE - 1)
                blocks = mcp_blocks(server, tool, query_arguments(root, tool))
            finally:
                monitor.closeout("code-analysis-checkin-test")
        texts = [block.text for block in blocks]
        self.assertGreaterEqual(len(texts), 2, f"check-in must be a separate block: {texts}")
        envelope = assert_envelope(self, json.loads(texts[0]), TOOL_OPERATIONS[tool], status="ok")
        self.assertNotIn("[routine check-in due]", json.dumps(envelope))
        self.assertTrue(any("[routine check-in due]" in text for text in texts[1:]), texts)

    def test_status_at_the_check_in_boundary(self) -> None:
        self._drive_to_boundary("get_code_analysis_status")

    def test_query_at_the_check_in_boundary(self) -> None:
        self._drive_to_boundary("find_code_definition")


if __name__ == "__main__":
    unittest.main()
