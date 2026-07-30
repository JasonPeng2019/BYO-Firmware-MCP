"""Hook output is configuration, never evidence, and never authority.

The most important test in this file is
`NoHookConfigurationTests` -- with no hook configuration present, every code path must
behave as it did before this feature existed. Everything else here is a boundary that
must hold even when a hook *is* configured and lying.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from pyocd_debug_mcp import discovery_failures, discovery_hooks, server
from pyocd_debug_mcp.discovery_failures import (
    PROBE_OPEN_FAILED,
    UART_OPEN_FAILED,
    carries_hook_contract,
    hook_failure,
    no_native_probe_failure,
    no_native_uart_failure,
    open_failure_payload,
    selection_disappeared_failure,
    unsupported_provider_failure,
)
from pyocd_debug_mcp.discovery_hooks import DiscoveryHookError, parse_hook_output
from pyocd_debug_mcp.firmstore.store import (
    PERSISTED_AUTHORITY_KEYS,
    FirmStore,
    PersistedAuthorityError,
    ensure_no_persisted_authority,
)
from pyocd_debug_mcp.hardware_inventory import (
    HardwareInventoryService,
    snapshot_from_validation_inventory,
)
from pyocd_debug_mcp.kernel import operations as ops
from pyocd_debug_mcp.probe_inventory import EMPTY_NATIVE_PROBE_LISTING
from tests.discovery_hook_fixtures import single_spec, source_without_docstrings


class HookOutputCannotInjectConfigurationTests(unittest.TestCase):
    """A hook names hardware. It may not name anything else."""

    FORBIDDEN_FIELDS = (
        "target",
        "pyocd_target",
        "pack",
        "pack_path",
        "pack_sha256",
        "connection_policy",
        "board_id",
        "flash_range",
        "memory_map",
        "regions",
        "permission",
        "plan",
        "gate",
        "active_gate",
        "unlocked_tools",
        "remaining_calls",
        "safety_ref",
        "identity_capability",
        "probe_family",
        "baudrate",
    )

    def test_no_extra_top_level_field_is_accepted(self) -> None:
        for field in self.FORBIDDEN_FIELDS:
            with self.subTest(field=field):
                document = {
                    "schema_version": 1,
                    "kind": "probe",
                    "probes": [
                        {"provider": "cmsisdap", "unique_id": "U1", "description": "P"}
                    ],
                    field: "injected",
                }
                with self.assertRaises(DiscoveryHookError) as caught:
                    parse_hook_output(document, expected_kind="probe")
                self.assertIn(field, str(caught.exception))

    def test_no_extra_probe_row_field_is_accepted(self) -> None:
        for field in self.FORBIDDEN_FIELDS:
            with self.subTest(field=field):
                document = {
                    "schema_version": 1,
                    "kind": "probe",
                    "probes": [
                        {
                            "provider": "cmsisdap",
                            "unique_id": "U1",
                            "description": "P",
                            field: "injected",
                        }
                    ],
                }
                with self.assertRaises(DiscoveryHookError):
                    parse_hook_output(document, expected_kind="probe")

    def test_no_extra_uart_row_field_is_accepted(self) -> None:
        for field in ("baudrate", "board_id", "plan", "flash_range"):
            with self.subTest(field=field):
                document = {
                    "schema_version": 1,
                    "kind": "uart",
                    "uarts": [{"port_path": "COM7", "description": "U", field: 1}],
                }
                with self.assertRaises(DiscoveryHookError):
                    parse_hook_output(document, expected_kind="uart")

    def test_every_persisted_authority_key_is_rejected_in_hook_output(self) -> None:
        for key in sorted(PERSISTED_AUTHORITY_KEYS):
            with self.subTest(key=key):
                document = {
                    "schema_version": 1,
                    "kind": "probe",
                    "probes": [
                        {"provider": "cmsisdap", "unique_id": "U1", "description": "P"}
                    ],
                    key: {"forged": True},
                }
                with self.assertRaises(DiscoveryHookError):
                    parse_hook_output(document, expected_kind="probe")

    def test_the_parsed_output_model_exposes_only_hardware_naming_fields(self) -> None:
        output = parse_hook_output(
            discovery_hooks.PROBE_OUTPUT_SCHEMA_EXAMPLE, expected_kind="probe"
        )

        self.assertEqual(
            set(output.probes[0].__slots__), {"provider", "unique_id", "description"}
        )

    def test_the_parsed_uart_model_exposes_only_endpoint_naming_fields(self) -> None:
        output = parse_hook_output(
            discovery_hooks.UART_OUTPUT_SCHEMA_EXAMPLE, expected_kind="uart"
        )

        self.assertEqual(
            set(output.uarts[0].__slots__),
            {"port_path", "description", "serial_number", "vid", "pid"},
        )

    def test_a_hook_row_cannot_carry_authority_through_the_inventory(self) -> None:
        row = snapshot_from_validation_inventory(
            cast(Any, SimpleNamespace(probes=(), serial_ports=()))
        )

        self.assertEqual(row.probes, ())
        for name in ("plan", "permission", "gate", "target", "safety_ref"):
            self.assertNotIn(name, discovery_hooks.HookProbeRow.__slots__)

    def test_hook_documents_never_pass_the_persisted_authority_guard(self) -> None:
        """Anything routed through FirmStore inherits this refusal."""

        with self.assertRaises(PersistedAuthorityError):
            ensure_no_persisted_authority({"active_plan": {"id": "x"}})
        # A legitimate hook document passes, so the guard is not vacuous.
        ensure_no_persisted_authority(discovery_hooks.PROBE_OUTPUT_SCHEMA_EXAMPLE)
        ensure_no_persisted_authority(discovery_hooks.MANIFEST_SCHEMA_EXAMPLE)


class FirmStoreBoundaryTests(unittest.TestCase):
    def test_firmstore_names_the_hook_directory_but_cannot_write_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FirmStore(Path(directory))
            layout = store.ensure_layout()

            self.assertTrue(layout.discovery_hooks.is_dir())
            self.assertFalse(hasattr(store, "write_hook"))
            self.assertFalse(hasattr(store, "write_discovery_hook"))

    def test_the_hook_directory_is_inside_the_gitignored_firm_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = FirmStore(Path(directory)).ensure_layout()

            self.assertTrue(layout.discovery_hooks.is_relative_to(layout.root))
            self.assertEqual(layout.root.name, ".firm")

    def test_the_repository_gitignores_the_firm_root(self) -> None:
        ignore = Path(__file__).resolve().parents[1] / ".gitignore"

        self.assertIn("/.firm/", ignore.read_text(encoding="utf-8"))


class OpenFailureBoundaryTests(unittest.TestCase):
    """A backend-open failure must never loop the agent back to discovery."""

    def test_probe_open_failed_carries_no_hook_contract_call(self) -> None:
        payload = open_failure_payload(
            PROBE_OPEN_FAILED, detail="pyOCD could not open the probe.", identity="U1"
        )

        self.assertNotIn("hook_contract_call", payload)
        self.assertNotIn("refresh_call", payload)
        self.assertFalse(carries_hook_contract(payload))

    def test_uart_open_failed_carries_no_hook_contract_call(self) -> None:
        payload = open_failure_payload(UART_OPEN_FAILED, detail="Port busy.")

        self.assertNotIn("hook_contract_call", payload)
        self.assertFalse(carries_hook_contract(payload))

    def test_open_failure_payloads_never_mention_the_discovery_tools(self) -> None:
        for code in (PROBE_OPEN_FAILED, UART_OPEN_FAILED):
            with self.subTest(code=code):
                payload = open_failure_payload(code, detail="nope")
                text = json.dumps(payload)

                self.assertNotIn(discovery_failures.CONTRACT_TOOL, text)
                self.assertNotIn(discovery_failures.REFRESH_TOOL, text)
                self.assertIn("do not call the discovery hook tools", str(payload["agent_prompt"]))

    def test_open_failure_remedies_are_terminal_action_checks(self) -> None:
        payload = open_failure_payload(PROBE_OPEN_FAILED, detail="nope")

        remedies = " ".join(cast("list[str]", payload["remedies"]))
        self.assertIn("driver", remedies)
        self.assertIn("other process", remedies)
        self.assertIn("firmware", remedies)

    def test_only_open_failure_codes_are_accepted_by_the_constructor(self) -> None:
        with self.assertRaises(ValueError):
            open_failure_payload("discovery/no-native-probe", detail="x")

    def test_unsupported_provider_offers_no_hook_contract_because_none_can_help(self) -> None:
        failure = unsupported_provider_failure(
            "nosuchprovider", registered_providers=("cmsisdap", "jlink")
        )

        payload = failure.to_payload()
        self.assertIsNone(failure.hook_contract_call)
        self.assertFalse(carries_hook_contract(payload))
        self.assertIn("no amount of hook repair", str(payload["agent_prompt"]))

    def test_selection_disappeared_reroutes_and_forbids_substitution(self) -> None:
        failure = selection_disappeared_failure("The selected probe is gone.")

        payload = failure.to_payload()
        self.assertIn("setup_overview", str(payload["agent_prompt"]))
        self.assertIn("Do not substitute", str(payload["agent_prompt"]))
        self.assertFalse(carries_hook_contract(payload))

    def test_discovery_failures_that_should_offer_a_contract_do(self) -> None:
        """The negative assertions above are only meaningful if the positive holds."""

        probe = no_native_probe_failure(hooks_available=True).to_payload()
        uart = no_native_uart_failure(hooks_available=True).to_payload()

        self.assertTrue(carries_hook_contract(probe))
        self.assertTrue(carries_hook_contract(uart))

    def test_hook_failures_offer_both_a_contract_and_a_refresh(self) -> None:
        for code in (
            discovery_failures.DISCOVERY_HOOK_FAILED,
            discovery_failures.DISCOVERY_HOOK_TIMEOUT,
            discovery_failures.DISCOVERY_HOOK_OUTPUT_INVALID,
            discovery_failures.DISCOVERY_HOOK_SOURCE_CHANGED,
        ):
            with self.subTest(code=code):
                payload = hook_failure(code, "probe", hook_diagnostics=()).to_payload()

                self.assertIn("hook_contract_call", payload)
                self.assertIn("refresh_call", payload)

    def test_the_locked_environment_diagnostic_precedes_the_hook_suggestion(self) -> None:
        """A driver problem is likelier than an unenumerable device, and no hook fixes it."""

        prompt = no_native_probe_failure(hooks_available=True).message

        self.assertLess(
            prompt.index("uv run --locked"),
            prompt.index(discovery_failures.CONTRACT_TOOL),
        )

    def test_no_native_probe_prompt_denies_it_is_a_naming_ambiguity(self) -> None:
        self.assertIn(
            "not a board-naming ambiguity",
            no_native_probe_failure(hooks_available=True).message,
        )


class GateAndAuthorityTests(unittest.TestCase):
    def test_no_failure_payload_in_the_family_stamps_or_mentions_a_gate_grant(self) -> None:
        payloads = [
            no_native_probe_failure(hooks_available=True).to_payload(),
            no_native_uart_failure(hooks_available=True).to_payload(),
            hook_failure(
                discovery_failures.DISCOVERY_HOOK_FAILED, "probe", hook_diagnostics=()
            ).to_payload(),
            unsupported_provider_failure("x", registered_providers=()).to_payload(),
            selection_disappeared_failure("gone").to_payload(),
            open_failure_payload(PROBE_OPEN_FAILED, detail="nope"),
            open_failure_payload(UART_OPEN_FAILED, detail="nope"),
        ]

        for payload in payloads:
            with self.subTest(code=payload["code"]):
                ensure_no_persisted_authority(payload)
                for key in ("gate", "gate_open", "identity_capability", "validation_id"):
                    self.assertNotIn(key, payload)

    def test_the_discovery_tools_never_reach_the_validation_stamp(self) -> None:
        from pyocd_debug_mcp.tools import discovery as discovery_tools

        source = source_without_docstrings(discovery_tools)

        for forbidden in (
            "_stamp_validation_session",
            "gate_manager",
            "assignment_store",
            "plan_engine",
            "permission_store",
            "target_control",
            "open_session",
        ):
            self.assertNotIn(forbidden, source, f"{forbidden} is reachable from a hook tool")

    def test_the_hook_execution_module_touches_no_authority_state(self) -> None:
        # Docstrings stripped: this module deliberately *explains* that hook state does
        # not live on ServerRun, and a raw text scan would flag the explanation.
        source = source_without_docstrings(discovery_hooks)

        for forbidden in (
            "gate_manager",
            "assignment_store",
            "plan_engine",
            "permission_store",
            "ServerRun",
            "clear_authority",
        ):
            self.assertNotIn(forbidden, source)

    def test_retry_tickets_are_not_stored_on_the_server_run(self) -> None:
        """clear_authority() would wipe them, and they are not authority."""

        from pyocd_debug_mcp.kernel.run_state import ServerRun

        fields = set(ServerRun.__slots__)

        self.assertEqual(
            fields,
            {"run_id", "started_at", "plans", "permissions", "assignments", "gates"},
        )

    def test_clearing_authority_does_not_clear_hook_configuration(self) -> None:
        store = discovery_hooks.HookSnapshotStore()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = single_spec(root, "probe", ["probe"])
            store.replace(
                discovery_hooks.DiscoveryHookSnapshot("m" * 64, (spec,), "now")
            )

            server.server_run.clear_authority()

            self.assertEqual(len(store.current().hooks), 1)


class NoHookConfigurationTests(unittest.TestCase):
    """With no hook configuration, behavior and schemas must be unchanged.

    The guide calls this the most important regression test in the set.
    """

    def setUp(self) -> None:
        ops.reset_eligible_hook_count_provider()
        self.addCleanup(ops.reset_eligible_hook_count_provider)

    def test_an_empty_snapshot_reports_no_hooks_for_either_kind(self) -> None:
        snapshot = discovery_hooks.EMPTY_SNAPSHOT

        self.assertFalse(snapshot.has_hooks_for("probe"))
        self.assertFalse(snapshot.has_hooks_for("uart"))
        self.assertEqual(snapshot.eligible_counts(), {"probe": 0, "uart": 0})

    def test_no_manifest_yields_an_empty_snapshot_and_runs_nothing(self) -> None:
        launched: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            hooks = discovery_hooks.load_hook_snapshot(Path(directory), environ={})
            service = HardwareInventoryService(
                native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
                native_uarts=lambda: cast("Any", []),
                hook_snapshot=lambda: hooks,
                run_hooks=lambda snapshot, kind: (launched.append(kind) or ()),
            )

            snapshot = service.snapshot()

        self.assertEqual(launched, [])
        self.assertEqual(snapshot.probes, ())
        self.assertEqual(snapshot.uarts, ())
        self.assertEqual(snapshot.hook_diagnostics, ())
        self.assertEqual(snapshot.hook_manifest_sha256, "")

    def test_every_operation_budget_is_unchanged_with_no_hooks(self) -> None:
        expected = {
            "read_serial": ({"read_seconds": 3}, ops.DEFAULT_OPERATION_TIMEOUT_SECONDS),
            "write_serial": ({"timeout_seconds": 2}, ops.DEFAULT_OPERATION_TIMEOUT_SECONDS),
            "serial_exchange": (
                {"read_seconds": 2, "steps": [{"write": "a"}]},
                ops.DEFAULT_OPERATION_TIMEOUT_SECONDS,
            ),
            "get_discovery_hook_contract": (
                {"kind": "probe"},
                ops.DEFAULT_OPERATION_TIMEOUT_SECONDS,
            ),
        }

        for tool, (arguments, value) in expected.items():
            with self.subTest(tool=tool):
                self.assertEqual(ops.operation_timeout_seconds(tool, arguments), value)

    def test_the_probe_inventory_budget_matches_the_pre_hook_formula(self) -> None:
        from pyocd_debug_mcp.kernel.processes import MAX_OWNED_PROCESS_CLEANUP_SECONDS
        from pyocd_debug_mcp.probe_families import configured_probe_cli_commands
        from pyocd_debug_mcp.timeouts import DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS

        expected = (
            ops.DEFAULT_OPERATION_TIMEOUT_SECONDS
            + len(configured_probe_cli_commands())
            * (DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS + MAX_OWNED_PROCESS_CLEANUP_SECONDS)
            + ops.CANCELLATION_CLEANUP_GRACE_SECONDS
        )

        self.assertEqual(ops.operation_timeout_seconds("setup_overview", {}), expected)

    def test_the_hook_budget_addend_is_exactly_zero(self) -> None:
        self.assertEqual(ops._hook_budget("probe"), 0.0)
        self.assertEqual(ops._hook_budget("uart"), 0.0)
        self.assertEqual(ops._hook_budget("probe", "uart"), 0.0)

    def test_no_native_probe_diagnostics_are_synthesized_from_nothing(self) -> None:
        self.assertEqual(EMPTY_NATIVE_PROBE_LISTING.probes, ())
        self.assertIsNone(EMPTY_NATIVE_PROBE_LISTING.exit_code)
        self.assertFalse(EMPTY_NATIVE_PROBE_LISTING.timed_out)
        self.assertFalse(EMPTY_NATIVE_PROBE_LISTING.available)

    def test_the_live_tool_surface_still_advertises_every_pre_hook_tool(self) -> None:
        names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}

        for expected in (
            "connect",
            "disconnect",
            "setup_overview",
            "board_validate",
            "get_setup_status",
            "load_setup_tool",
        ):
            with self.subTest(tool=expected):
                self.assertIn(expected, names)

    def test_the_plan_gated_serial_tools_keep_their_hidden_locked_policy(self) -> None:
        """These are gated hardware actions; the hook work must not have exposed them."""

        for name in ("read_serial", "write_serial", "serial_exchange"):
            with self.subTest(tool=name):
                definition = server.tool_registry._definitions[name]
                self.assertTrue(definition.hidden_by_default)
                self.assertTrue(definition.locked_by_default)

    def test_the_two_new_tools_are_the_only_additions_to_the_surface(self) -> None:
        names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}

        self.assertIn(discovery_failures.CONTRACT_TOOL, names)
        self.assertIn(discovery_failures.REFRESH_TOOL, names)

    def test_the_legacy_serial_fallback_registry_still_loads_at_import(self) -> None:
        """PYOCD_SERIAL_FALLBACK_REGISTRY behavior is preserved for one release."""

        from pyocd_debug_mcp import serial_resolver

        self.assertTrue(hasattr(serial_resolver, "SERIAL_FALLBACKS"))
        self.assertTrue(hasattr(serial_resolver, "parse_nrfjprog_com_output"))
        self.assertTrue(hasattr(serial_resolver, "parse_stm32_programmer_list_output"))

    def test_resolve_serial_port_still_consults_the_vendor_registry(self) -> None:
        import inspect

        from pyocd_debug_mcp import serial_resolver

        source = inspect.getsource(serial_resolver.resolve_serial_port)

        self.assertIn("SERIAL_FALLBACKS", source)

    def test_the_vendor_parsers_are_untouched_and_still_parse_their_formats(self) -> None:
        from pyocd_debug_mcp.serial_resolver import (
            parse_nrfjprog_com_output,
            parse_stm32_programmer_list_output,
        )

        nordic = parse_nrfjprog_com_output("683710208  COM7  VCOM0\n")
        self.assertEqual(nordic[0].port, "COM7")
        self.assertEqual(nordic[0].probe_serial, "683710208")

        stlink = parse_stm32_programmer_list_output(
            # The section header is matched with `startswith`, so the single space
            # between "=====" and "UART Interface" is load-bearing.
            "===== DFU Interface =====\n"
            "===== UART Interface =====\n"
            "ST-LINK SN  : 066EFF505057\n"
            "Port        : COM5\n"
        )
        self.assertEqual(stlink[0].port, "COM5")


class NativePreferenceTests(unittest.TestCase):
    def test_native_inventory_is_preferred_when_both_sources_see_a_device(self) -> None:
        from pyocd_debug_mcp.hardware_inventory import ProbeRow

        def row(provenance: tuple[str, ...], description: str, sha: str | None) -> ProbeRow:
            return ProbeRow(
                provider="jlink",
                probe_id="U1",
                unique_id="U1",
                row_id=f"row-{provenance[0]}",
                description=description,
                stable_identity="U1",
                provenance=provenance,
                hook_source_sha256=sha,
                identity_scope="stable",
                snapshot_id="snap",
            )

        merged = HardwareInventoryService._merge_probe_rows(
            [row(("native",), "Native description", None)],
            [row(("hook:h",), "Hook description", "a" * 64)],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].description, "Native description")
        self.assertEqual(merged[0].provenance[0], "native")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
