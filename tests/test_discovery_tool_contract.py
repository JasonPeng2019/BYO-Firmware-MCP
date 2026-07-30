"""Schema drift and tool registration policy for the two discovery tools.

Both cases here are load-bearing in a way nothing else in the suite covers:

* **Schema drift.** The server hands an agent an example manifest and then judges what
  the agent writes back with a separate code path. If those two disagree, the agent gets
  a rejection loop it cannot debug from the outside.
* **Registration policy.** If either tool is hidden, locked, Layer-2 wrapped, or lax
  about arguments, the fallback is *unreachable from a client* rather than merely buggy.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pyocd_debug_mcp import discovery_hooks, server
from pyocd_debug_mcp.discovery_failures import CONTRACT_TOOL, REFRESH_TOOL
from pyocd_debug_mcp.discovery_hooks import (
    load_hook_snapshot,
    parse_hook_output,
    parse_manifest_document,
)
from pyocd_debug_mcp.tools.discovery import (
    DiscoveryRetryStore,
    DiscoveryToolServices,
    build_discovery_handlers,
)


def _flat(text: str | None) -> str:
    """Collapse a docstring to one line so assertions ignore wrapping."""

    return " ".join((text or "").split())


def _call(name: str, arguments: dict[str, Any]) -> Any:
    result = asyncio.run(server.mcp.call_tool(name, arguments))
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, list):
        result = getattr(result[0], "text", result[0])
    return json.loads(result)


class SchemaDriftTests(unittest.TestCase):
    """The contract the server hands out must be one it accepts back."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.handlers = build_discovery_handlers(
            DiscoveryToolServices(
                hook_root=lambda: self.root,
                load_snapshot=lambda: load_hook_snapshot(self.root, environ={}),
                current_snapshot=lambda: discovery_hooks.EMPTY_SNAPSHOT,
                replace_snapshot=lambda snapshot: snapshot,
                retry_store=DiscoveryRetryStore("run-test"),
                registered_providers=lambda: ("cmsisdap", "jlink", "stlink"),
                run_hooks=lambda snapshot, kind: (),
            )
        )

    def contract(self, kind: str) -> dict[str, Any]:
        return json.loads(self.handlers[CONTRACT_TOOL](kind))

    def test_returned_manifest_schema_passes_the_real_manifest_validator(self) -> None:
        for kind in ("probe", "uart"):
            with self.subTest(kind=kind):
                document = self.contract(kind)["manifest_schema"]

                declarations = parse_manifest_document(document)

                self.assertTrue(declarations)

    def test_returned_output_schema_passes_the_real_output_parser(self) -> None:
        for kind in ("probe", "uart"):
            with self.subTest(kind=kind):
                document = self.contract(kind)["output_schema"]

                output = parse_hook_output(document, expected_kind=kind)

                self.assertEqual(output.kind, kind)

    def test_returned_example_manifest_passes_the_real_manifest_validator(self) -> None:
        for kind in ("probe", "uart"):
            with self.subTest(kind=kind):
                example = self.contract(kind)["example"]["manifest"]

                declarations = parse_manifest_document(example)

                self.assertEqual(len(declarations), 1)
                self.assertEqual(declarations[0].kind, kind)

    def test_returned_example_survives_a_real_json_round_trip(self) -> None:
        """The agent receives this as JSON text, not as Python objects."""

        for kind in ("probe", "uart"):
            with self.subTest(kind=kind):
                example = self.contract(kind)["example"]["manifest"]
                round_tripped = json.loads(json.dumps(example))

                self.assertTrue(parse_manifest_document(round_tripped))

    def test_the_returned_example_actually_loads_and_runs_end_to_end(self) -> None:
        """Write exactly what the server handed out, then let the server load it."""

        for kind in ("probe", "uart"):
            with self.subTest(kind=kind):
                root = Path(tempfile.mkdtemp())
                contract = json.loads(self.handlers[CONTRACT_TOOL](kind))
                example = contract["example"]
                (root / example["hook_filename"]).write_text(
                    example["hook_source"], encoding="utf-8"
                )
                (root / example["manifest_filename"]).write_text(
                    json.dumps(example["manifest"]), encoding="utf-8"
                )

                snapshot = load_hook_snapshot(root, environ={})

                self.assertEqual(len(snapshot.hooks), 1)
                execution = discovery_hooks.execute_hook(snapshot.hooks[0])
                self.assertTrue(execution.ok, execution.failure_detail)
                assert execution.output is not None
                rows = (
                    execution.output.probes if kind == "probe" else execution.output.uarts
                )
                self.assertTrue(rows, "the published example hook returned no rows")

    def test_declared_limits_match_the_values_actually_enforced(self) -> None:
        limits = self.contract("probe")["limits"]

        self.assertEqual(
            limits["max_timeout_seconds"], discovery_hooks.MAX_HOOK_TIMEOUT_SECONDS
        )
        self.assertEqual(limits["max_rows"], discovery_hooks.MAX_HOOK_ROWS)
        self.assertEqual(limits["max_field_characters"], discovery_hooks.MAX_FIELD_CHARS)
        self.assertEqual(limits["max_stdout_bytes"], discovery_hooks.MAX_HOOK_STDOUT_BYTES)
        self.assertEqual(limits["max_hooks"], discovery_hooks.MAX_HOOKS_PER_MANIFEST)

    def test_declared_timeout_ceiling_is_actually_rejected_one_above(self) -> None:
        ceiling = self.contract("probe")["limits"]["max_timeout_seconds"]
        document = json.loads(json.dumps(self.contract("probe")["manifest_schema"]))
        document["hooks"][0]["timeout_seconds"] = ceiling + 1

        with self.assertRaises(discovery_hooks.DiscoveryHookError):
            parse_manifest_document(document)

    def test_declared_runners_are_exactly_the_supported_runners(self) -> None:
        self.assertEqual(
            self.contract("probe")["supported_runners"],
            sorted(discovery_hooks.SUPPORTED_RUNNERS),
        )

    def test_declared_platform_matches_the_one_platform_function(self) -> None:
        contract = self.contract("probe")

        self.assertEqual(contract["operating_system"], discovery_hooks.current_platform())
        self.assertIn(contract["operating_system"], contract["supported_platforms"])
        self.assertEqual(
            contract["platform_guidance"],
            discovery_hooks.PLATFORM_GUIDANCE[discovery_hooks.current_platform()],
        )

    def test_the_returned_manifest_example_targets_the_current_platform(self) -> None:
        """An example an agent cannot run on this OS is not a usable example."""

        for kind in ("probe", "uart"):
            with self.subTest(kind=kind):
                contract = self.contract(kind)
                entry = contract["example"]["manifest"]["hooks"][0]
                self.assertIn(contract["operating_system"], entry["platforms"])

    def test_hook_root_is_the_servers_own_value_not_a_guess(self) -> None:
        self.assertEqual(self.contract("probe")["hook_root"], str(self.root))

    def test_probe_contract_lists_registered_providers_and_uart_does_not(self) -> None:
        self.assertEqual(
            self.contract("probe")["pyocd_providers"], ["cmsisdap", "jlink", "stlink"]
        )
        self.assertNotIn("pyocd_providers", self.contract("uart"))

    def test_an_unsupported_kind_is_refused_with_the_supported_list(self) -> None:
        payload = json.loads(self.handlers[CONTRACT_TOOL]("flash"))

        self.assertEqual(payload["status"], "discovery_contract_rejected")
        self.assertEqual(payload["code"], "discovery/unsupported-kind")
        self.assertEqual(payload["supported_kinds"], ["probe", "uart"])

    def test_kind_is_accepted_case_insensitively(self) -> None:
        payload = json.loads(self.handlers[CONTRACT_TOOL]("PROBE"))

        self.assertEqual(payload["status"], "discovery_hook_contract")
        self.assertEqual(payload["kind"], "probe")


class LiveServerContractTests(unittest.TestCase):
    """The same guarantees, through the real registered server tools."""

    def test_probe_contract_providers_come_from_pyocd_probe_classes(self) -> None:
        from pyocd_debug_mcp.probe_inventory import registered_provider_ids

        payload = _call(CONTRACT_TOOL, {"kind": "probe"})

        self.assertEqual(payload["pyocd_providers"], sorted(registered_provider_ids()))

    def test_live_contract_schemas_pass_the_real_validators(self) -> None:
        for kind in ("probe", "uart"):
            with self.subTest(kind=kind):
                payload = _call(CONTRACT_TOOL, {"kind": kind})

                self.assertTrue(parse_manifest_document(payload["manifest_schema"]))
                self.assertEqual(
                    parse_hook_output(payload["output_schema"], expected_kind=kind).kind,
                    kind,
                )

    def test_live_hook_root_is_under_the_projects_firm_directory(self) -> None:
        payload = _call(CONTRACT_TOOL, {"kind": "probe"})

        root = Path(payload["hook_root"])
        self.assertEqual(root.name, "discovery_hooks")
        self.assertEqual(root.parent.name, ".firm")


class RegistrationPolicyTests(unittest.TestCase):
    """Any one of these being wrong makes the fallback unreachable from a client."""

    TOOLS = (CONTRACT_TOOL, REFRESH_TOOL)

    def test_both_tools_are_listed_with_no_board_connected(self) -> None:
        names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}

        for name in self.TOOLS:
            with self.subTest(tool=name):
                self.assertIn(name, names)

    def test_neither_tool_is_hidden(self) -> None:
        for name in self.TOOLS:
            with self.subTest(tool=name):
                definition = server.tool_registry._definitions[name]
                self.assertFalse(definition.hidden_by_default)

    def test_neither_tool_is_locked_and_neither_has_a_prerequisite(self) -> None:
        for name in self.TOOLS:
            with self.subTest(tool=name):
                definition = server.tool_registry._definitions[name]
                self.assertFalse(definition.locked_by_default)
                self.assertIsNone(definition.prerequisite)

    def test_neither_tool_is_wrapped_as_a_hardware_action(self) -> None:
        """A hook failure must not be reported through the Layer-2 failure envelope."""

        layer2 = set(getattr(server.mcp, "_layer2_tools", set()))

        for name in self.TOOLS:
            with self.subTest(tool=name):
                self.assertNotIn(name, layer2)

    def test_both_tools_reject_an_unrecognized_keyword_argument(self) -> None:
        """FastMCP drops unknown fields silently unless this is actually wired."""

        for name, arguments in (
            (CONTRACT_TOOL, {"kind": "probe", "executable": True}),
            (REFRESH_TOOL, {"retry_id": None, "executable": True}),
        ):
            with self.subTest(tool=name):
                with self.assertRaises(Exception) as caught:
                    _call(name, arguments)
                self.assertIn("executable", str(caught.exception))

    def test_both_tools_are_callable_with_no_prior_unlock(self) -> None:
        contract = _call(CONTRACT_TOOL, {"kind": "probe"})
        self.assertEqual(contract["status"], "discovery_hook_contract")

        refresh = _call(REFRESH_TOOL, {})
        self.assertIn(
            refresh["status"],
            {"discovery_hooks_absent", "discovery_hooks_refreshed", "discovery_hooks_partial"},
        )

    def test_neither_tool_is_a_guarded_setup_action(self) -> None:
        for name in self.TOOLS:
            with self.subTest(tool=name):
                self.assertNotIn(name, server.SETUP_GUARDED_ACTIONS)

    def test_live_descriptions_come_from_the_handler_docstrings(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}

        for name in self.TOOLS:
            with self.subTest(tool=name):
                handler = server.discovery_tool_handlers[name]
                description = tools[name].description
                self.assertTrue(description)
                assert description is not None
                assert handler.__doc__ is not None
                self.assertEqual(
                    description.strip().splitlines()[0],
                    handler.__doc__.strip().splitlines()[0],
                )

    def test_contract_description_tells_the_agent_it_authors_the_hook(self) -> None:
        handler = server.discovery_tool_handlers[CONTRACT_TOOL]
        assert handler.__doc__ is not None

        description = _flat(handler.__doc__)

        self.assertIn("Write the hook and manifest yourself", description)
        self.assertIn("It executes nothing", description)

    def test_refresh_description_states_it_takes_no_path_or_code(self) -> None:
        handler = server.discovery_tool_handlers[REFRESH_TOOL]
        assert handler.__doc__ is not None

        description = _flat(handler.__doc__)

        self.assertIn("no path, no arguments, and no code", description)
        self.assertIn("grants nothing", description)
        self.assertIn("opens no probe and no serial port", description)

    def test_the_two_tools_are_the_only_discovery_tools_registered(self) -> None:
        self.assertEqual(set(server.discovery_tool_handlers), set(self.TOOLS))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
