"""AT06: setup-lite keeps operator-confirmed evidence partial, persistent, and honest."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.policy import (
    CapabilityPolicyError,
    CapabilityPolicyRepository,
    Tier,
)
from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
from pyocd_debug_mcp.safety.regions import AddressRange
from pyocd_debug_mcp.setup_flow.preflight import PreflightInventory, ProbeCandidate
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe

from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    assert_lite_raw_warning,
    confirmed_lite_policy,
    fixture,
    isolated_project,
    public_call,
    public_text,
    tiered_test_server,
)


BOARD = "lite_board"
_PROBE_UID = "LITE-ACCEPTANCE-PROBE"


def _batch_payload(text: str) -> dict[str, object]:
    """Decode the public batch body while retaining its human safe-exit reminder."""

    payload = json.loads(text.split("\n", 1)[0])
    if not isinstance(payload, dict):
        raise AssertionError(f"batch response must be an object: {payload!r}")
    return payload


def _child_setup_response(batch: dict[str, object]) -> dict[str, object]:
    """Extract the JSON setup result returned by the public one-child fallback."""

    completed = batch.get("completed")
    if not isinstance(completed, list) or len(completed) != 1:
        raise AssertionError(f"setup fallback did not run exactly one child: {batch!r}")
    child = completed[0]
    if not isinstance(child, dict) or child.get("tool_name") not in {
        "board_setup",
        "board_fix_setup",
    }:
        raise AssertionError(f"unexpected setup fallback child: {child!r}")
    result = child.get("result")
    # The nested MCP dispatcher serializes its FastMCP text content rather
    # than flattening it.  Both shapes are public batch output forms.
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        result = result[0].get("text")
    if not isinstance(result, str):
        raise AssertionError(f"setup child did not return JSON text: {child!r}")
    payload = json.loads(result)
    if not isinstance(payload, dict):
        raise AssertionError(f"setup child response must be an object: {payload!r}")
    return payload


def _phase_codes(response: dict[str, object]) -> set[str]:
    observed = response.get("observed")
    records = observed.get("phase_records") if isinstance(observed, dict) else None
    if not isinstance(records, list):
        return set()
    return {
        str(record["code"])
        for record in records
        if isinstance(record, dict) and isinstance(record.get("code"), str)
    }


def _minimal_local_pdf(root: Path, name: str) -> Path:
    """Make local evidence bytes only; this is not a profile/map authority fixture."""

    path = root / name
    path.write_bytes(b"%PDF-1.4\n% tiered acceptance local datasheet\n")
    return path


def _setup_inventory() -> SimpleNamespace:
    """One stable synthetic probe inventory, consumed through public setup_overview."""

    inventory = ValidationInventory(
        probes=(
            ValidationProbe(
                _PROBE_UID,
                "Tiered acceptance J-Link",
                "jlink",
                _PROBE_UID,
            ),
        )
    )
    snapshot = snapshot_from_validation_inventory(inventory)
    return SimpleNamespace(
        snapshot=lambda: snapshot,
        validation_inventory=lambda: inventory,
    )


def _ready_setup_preflight() -> PreflightInventory:
    """A server-selected exact target with intentionally absent support authority.

    The visible overview remains responsible for assigning the probe.  This
    deterministic dependency merely prevents a target-research branch from
    obscuring the tier-specific connection prerequisite under test.
    """

    return PreflightInventory(
        probes=(
            ProbeCandidate(
                _PROBE_UID,
                "Tiered acceptance J-Link",
                "jlink",
                _PROBE_UID,
            ),
        ),
        built_in_targets=("nrf52840",),
        exact_detected_targets=("nrf52840",),
    )


def _accepted_setup_plan(
    testcase: unittest.TestCase,
    server: object,
    *,
    display_name: str,
    target_tier: str,
    datasheet: Path,
) -> tuple[str, dict[str, object], dict[str, object]]:
    """Walk the visible overview/load/plan protocol and return its exact fallback."""

    overview = json.loads(public_text(server, "setup_overview", {"board_names": [display_name]}))
    testcase.assertEqual(overview["status"], "setup_routes_ready")
    routes = overview.get("routes")
    testcase.assertIsInstance(routes, list)
    testcase.assertEqual(len(routes), 1)
    route = routes[0]
    testcase.assertIsInstance(route, dict)
    board_id = route.get("board_id")
    template = route.get("plan_action_parameters_template")
    testcase.assertIsInstance(board_id, str)
    testcase.assertIsInstance(template, dict)
    assert isinstance(board_id, str)
    assert isinstance(template, dict)

    loaded = json.loads(
        public_text(
            server,
            "load_setup_tool",
            {"board_id": board_id, "tool_name": "board_setup-plan"},
        )
    )
    testcase.assertEqual(loaded["status"], "setup_tool_loaded")
    # The all-NULL initialisation is intentionally routed through MCP even
    # though it returns operator-facing text rather than a JSON data envelope.
    public_text(
        server,
        "board_setup-plan",
        {
            "board_id": None,
            "hypothesis": None,
            "strategy": None,
            "hypothesis_made": None,
            "strategy_evaluated": None,
            "expected_fail_return": None,
            "expected_success_return": None,
            "max_calls": None,
            "max_calls_buffer": None,
            "action_parameters": None,
            "user_permission": None,
        },
    )
    action = {
        **template,
        "target_tier": target_tier,
        "mcu_part_number": "NO-PACK-PART",
        "requires_uart": False,
        "serial_baudrate": None,
        "serial_id": None,
        "datasheet_path": str(datasheet),
    }
    accepted = json.loads(
        public_text(
            server,
            "board_setup-plan",
            {
                "board_id": board_id,
                "hypothesis": (
                    "The local datasheet and the operator-confirmed containment facts are "
                    "sufficient for the requested setup-lite policy."
                ),
                "strategy": (
                    "Run the one bounded setup attempt, preserve its exact continuation, "
                    "and use the paired repair only after the server accepts confirmation."
                ),
                "hypothesis_made": True,
                "strategy_evaluated": True,
                "expected_fail_return": (
                    "setup-full reports the missing verified device-support prerequisite"
                ),
                "expected_success_return": "setup-lite commits only confirmed local geometry",
                "max_calls": 1,
                "max_calls_buffer": 0,
                "action_parameters": action,
                "user_permission": "one-time",
            },
        )
    )
    testcase.assertEqual(accepted["status"], "plan_accepted")
    fallback = accepted.get("stable_client_fallback")
    testcase.assertIsInstance(fallback, dict)
    assert isinstance(fallback, dict)
    testcase.assertEqual(fallback.get("tool_name"), "action_batch")
    return board_id, action, accepted


class TieredLiteEvidenceAcceptanceTests(unittest.TestCase):
    def test_lite_requires_at_least_one_confirmed_map_dependent_capability(self) -> None:
        """AT06: an empty proposal/uncertain parse can never be committed as lite."""

        with isolated_project() as root:
            repository = CapabilityPolicyRepository(root)
            with self.assertRaisesRegex(CapabilityPolicyError, "(confirmed|invalid)"):
                repository.commit(
                    BOARD,
                    Tier.SETUP_LITE,
                    map_snapshot={"board_id": BOARD, "regions": []},
                    evidence={"proposal": "uncertain"},
                )

    def test_confirmed_geometry_and_provenance_survive_restart_without_asserting_exact_identity(
        self,
    ) -> None:
        """AT06/C05: operator corrections persist verbatim and lite remains trusted-not-proven."""

        confirmation = fixture("lite_confirmation.json")
        with isolated_project() as root:
            repository = CapabilityPolicyRepository(root)
            committed = repository.commit(
                BOARD,
                Tier.SETUP_LITE,
                map_snapshot={
                    "board_id": BOARD,
                    "regions": confirmation["regions"],
                    "flash": confirmation["flash"],
                },
                evidence={
                    "proposed_regions": [],
                    "confirmed_regions": confirmation["regions"],
                    "provenance": {"source_pages": [42, 51], "operator_correction": True},
                },
            )
            restarted = CapabilityPolicyRepository(root).resolve(BOARD)
            self.assertEqual(restarted.tier, Tier.SETUP_LITE)
            self.assertEqual(restarted.policy_digest, committed.policy_digest)
            self.assertEqual(restarted.map_snapshot, committed.map_snapshot)
            self.assertEqual(restarted.evidence, committed.evidence)

            with tiered_test_server(root) as server:
                server.configure_tiered_test_seams(policy_repository=repository)
                payload = public_call(server, "get_capabilities", {"board_id": BOARD})
                self.assertEqual(payload["tier"], "setup-lite")
                self.assertEqual(
                    payload["identity"], {"assertion": "trusted-not-proven", "capability": None}
                )
                self.assertNotEqual(payload["identity"].get("capability"), "exact")
                self.assertEqual(payload["policy_digest"], committed.policy_digest)

    def test_lite_raw_backend_failure_still_instructs_human_display_and_preserves_confirmed_policy(
        self,
    ) -> None:
        """AT06/AT08: error handling cannot hide the deliberate containment-bypass warning."""

        confirmation = fixture("lite_confirmation.json")
        with isolated_project() as root, tiered_test_server(root) as server:
            repository = CapabilityPolicyRepository(root)
            repository.commit(
                BOARD,
                Tier.SETUP_LITE,
                map_snapshot=confirmed_lite_policy(BOARD),
                evidence={"confirmed_regions": confirmation["regions"]},
            )
            backend = DeterministicTargetBackend(
                read_error=RuntimeError("fake target is unreachable")
            )
            server.configure_tiered_test_seams(
                backend=backend,
                auto_target_resolver=lambda _board: "nrf52840",
                policy_repository=repository,
            )
            # This is a deterministic live-session fixture only.  Its policy
            # remains the real persisted setup-lite policy, and the behavior
            # under test below crosses the public MCP raw handler.
            handle = backend.open(board=None, unique_id="lite-probe", target="nrf52840")
            server._promote_open_session(BOARD, handle, gate_reason="acceptance fixture")
            try:
                payload = public_call(
                    server,
                    "read_memory_raw",
                    {"board_id": BOARD, "address": 0x20000000},
                )
                self.assertEqual(payload["status"], "error")
                assert_lite_raw_warning(self, payload, "read_memory_raw")
                persisted = CapabilityPolicyRepository(root).resolve(BOARD)
                self.assertEqual(persisted.tier, Tier.SETUP_LITE)
                self.assertEqual(persisted.evidence, {"confirmed_regions": confirmation["regions"]})
            finally:
                server.disconnect(BOARD)

    def test_public_lite_safe_memory_remains_plan_gated_while_raw_is_warned(self) -> None:
        """AT06: partial map evidence cannot silently satisfy a safe-operation prerequisite."""

        with isolated_project() as root, tiered_test_server(root) as server:
            confirmation = fixture("lite_confirmation.json")
            policies = CapabilityPolicyRepository(root)
            policies.commit(
                BOARD,
                Tier.SETUP_LITE,
                map_snapshot={
                    "board_id": BOARD,
                    "regions": confirmation["regions"],
                    "flash": confirmation["flash"],
                },
                evidence={"confirmed_regions": confirmation["regions"]},
            )
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(
                backend=backend,
                auto_target_resolver=lambda _board: "nrf52840",
                policy_repository=policies,
            )
            handle = backend.open(board=None, unique_id="lite-probe", target="nrf52840")
            server._promote_open_session(BOARD, handle, gate_reason="acceptance fixture")
            try:
                for address in (0x20000000, 0x00000000):
                    with self.subTest(address=hex(address)):
                        raw = public_call(
                            server,
                            "read_memory_raw",
                            {"board_id": BOARD, "address": address},
                        )
                        self.assertEqual(raw["status"], "ok")
                        assert_lite_raw_warning(self, raw, "read_memory_raw")
                before_safe = list(backend.calls)
                with self.assertRaisesRegex(Exception, "read_memory_address-plan"):
                    asyncio.run(
                        server.mcp.call_tool(
                            "read_memory_address",
                            {"board_id": BOARD, "address": 0x20000000},
                        )
                    )
                self.assertEqual(
                    [call for call in backend.calls if call[0] == "read_memory"],
                    [call for call in before_safe if call[0] == "read_memory"],
                    "safe route must not reach a target before its public plan prerequisite",
                )
                guide = {
                    "board_id": None,
                    "hypothesis": None,
                    "strategy": None,
                    "hypothesis_made": None,
                    "strategy_evaluated": None,
                    "expected_fail_return": None,
                    "expected_success_return": None,
                    "max_calls": None,
                    "max_calls_buffer": None,
                    "action_parameters": None,
                    "user_permission": None,
                }
                public_text(server, "read_memory_address-plan", guide)

                def read_once(address: int) -> dict[str, object]:
                    accepted = json.loads(
                        public_text(
                            server,
                            "read_memory_address-plan",
                            {
                                "board_id": BOARD,
                                "hypothesis": "The requested word belongs to the confirmed map.",
                                "strategy": "Read exactly one 32-bit word through the returned fallback.",
                                "hypothesis_made": True,
                                "strategy_evaluated": True,
                                "expected_fail_return": "The server reports containment refusal.",
                                "expected_success_return": "The bounded mapped word is returned.",
                                "max_calls": 1,
                                "max_calls_buffer": 0,
                                "action_parameters": {
                                    "address": address,
                                    "width": 32,
                                    "length": 4,
                                },
                            },
                        )
                    )
                    self.assertEqual(accepted["status"], "plan_accepted")
                    fallback = accepted["stable_client_fallback"]
                    self.assertIsInstance(fallback, dict)
                    assert isinstance(fallback, dict)
                    batch = _batch_payload(
                        public_text(server, fallback["tool_name"], fallback["arguments"])
                    )
                    self.assertEqual(batch["status"], "batch_completed", batch)
                    completed = batch["completed"]
                    self.assertIsInstance(completed, list)
                    assert isinstance(completed, list)
                    child = completed[0]
                    self.assertIsInstance(child, dict)
                    assert isinstance(child, dict)
                    self.assertEqual(child["tool_name"], "read_memory_address")
                    result = child["result"]
                    self.assertIsInstance(result, str)
                    decoded = json.loads(str(result).split("\n", 1)[0])
                    self.assertIsInstance(decoded, dict)
                    return decoded

                contained = read_once(0x20000000)
                self.assertEqual(contained["status"], "ok")
                self.assertEqual(contained["operation"], "read_memory_address")
                refused = read_once(0x40000000)
                self.assertEqual(refused["status"], "refused")
                self.assertEqual(refused["operation"], "read_memory_address")
                self.assertIn("map", refused["message"])
            finally:
                server.disconnect(BOARD)

    def test_public_lite_cpu_register_read_remains_direct_and_does_not_consult_map_geometry(
        self,
    ) -> None:
        """AT05/AT06: map-independent CPU inspection gains no new PDF/map gate."""

        with isolated_project() as root, tiered_test_server(root) as server:
            policies = CapabilityPolicyRepository(root)
            policies.commit(
                BOARD,
                Tier.SETUP_LITE,
                map_snapshot=confirmed_lite_policy(BOARD),
                evidence={"confirmed_regions": fixture("lite_confirmation.json")["regions"]},
            )
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(backend=backend, policy_repository=policies)
            handle = backend.open(board=None, unique_id="lite-probe", target="nrf52840")
            server._promote_open_session(BOARD, handle, gate_reason="acceptance fixture")
            try:
                text = public_text(server, "read_cpu_register", {"board_id": BOARD, "name": "r0"})
                self.assertIn("0x00000000", text)
                self.assertTrue(
                    any(call[0] == "read_core_register" for call in backend.calls),
                    "the direct non-map register read must reach the connected backend",
                )
                self.assertFalse(
                    any(call[0].startswith("read_memory") for call in backend.calls),
                    "CPU inspection must not make a containment-map memory read",
                )
            finally:
                server.disconnect(BOARD)

    def test_public_lite_read_only_flash_is_raw_default_and_safe_denial_never_reaches_backend(
        self,
    ) -> None:
        """AT05/AT06: discovery and containment both require confirmed flash writability.

        The real persisted operator confirmation deliberately names application
        and bootloader flash as readable but not writable.  A safe route cannot
        advertise either partition, nor may an accepted safe-plan attempt reach
        the target just because discovery made the route look usable.
        """

        with isolated_project() as root, tiered_test_server(root) as server:
            policy = confirmed_lite_policy(BOARD)
            flash = policy["flash"]
            self.assertIsInstance(flash, dict)
            assert isinstance(flash, dict)
            # The production confirmation format records sector sizes.  This
            # execution-only fixture gives the existing safe containment path
            # its required explicit end boundary without changing the
            # read-only partition authority under test.
            flash["erase_sectors"] = [{"start": 0x00004000, "end": 0x00040000}]
            policies = CapabilityPolicyRepository(root)
            policies.commit(
                BOARD,
                Tier.SETUP_LITE,
                map_snapshot=policy,
                evidence={"confirmed_regions": fixture("lite_confirmation.json")["regions"]},
            )
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(backend=backend, policy_repository=policies)
            handle = backend.open(board=None, unique_id="lite-probe", target="nrf52840")
            server._promote_open_session(BOARD, handle, gate_reason="acceptance fixture")
            try:
                capabilities = public_call(server, "get_capabilities", {"board_id": BOARD})
                families = {
                    row["family"]: row
                    for row in capabilities["capabilities"]
                    if isinstance(row, dict) and isinstance(row.get("family"), str)
                }
                for family in ("flash-application", "flash-bootloader"):
                    with self.subTest(family=family, oracle="discovery"):
                        route = families[family]
                        self.assertTrue(route["available"], route)
                        self.assertFalse(route["safe"], route)
                        self.assertEqual(route["preferred_tool"], "flash_raw", route)
                        self.assertEqual(route["default_tool"], "flash_raw", route)

                def runtime_evidence(start: int) -> SimpleNamespace:
                    """Only avoid an ELF parser dependency; containment remains real."""

                    return SimpleNamespace(
                        initial_stack_pointer=0x20000080,
                        loadable_segments=(
                            SimpleNamespace(load_range=AddressRange(start, start + 16)),
                        ),
                        hex_ranges=(),
                        reset_handler=start + 9,
                        entry_point=start + 9,
                        vector_table=start,
                    )

                for action, start, permission in (
                    ("flash_application", 0x00004000, None),
                    ("flash_bootloader", 0x0003F000, "one-time"),
                ):
                    with self.subTest(action=action, oracle="safe-denial-before-io"):
                        artifact = root / f"read-only-{action}.elf"
                        artifact.write_bytes(b"test-only parser seam artifact")
                        plan_tool = f"{action}-plan"
                        null_plan = {
                            "board_id": None,
                            "hypothesis": None,
                            "strategy": None,
                            "hypothesis_made": None,
                            "strategy_evaluated": None,
                            "expected_fail_return": None,
                            "expected_success_return": None,
                            "max_calls": None,
                            "max_calls_buffer": None,
                            "action_parameters": None,
                            "user_permission": None,
                        }
                        public_text(server, plan_tool, null_plan)
                        populated_plan: dict[str, object] = {
                            "board_id": BOARD,
                            "hypothesis": (
                                "The proposed artifact must be refused because the confirmed "
                                "partition has no write authority."
                            ),
                            "strategy": (
                                "Exercise the exact plan-bound public safe action and verify "
                                "the target is untouched on containment refusal."
                            ),
                            "hypothesis_made": True,
                            "strategy_evaluated": True,
                            "expected_fail_return": "Lite containment refuses the read-only flash.",
                            "expected_success_return": "No flash backend call is made.",
                            "max_calls": 1,
                            "max_calls_buffer": 0,
                            "action_parameters": {"artifact": str(artifact)},
                        }
                        if permission is not None:
                            populated_plan["user_permission"] = permission
                        accepted = json.loads(public_text(server, plan_tool, populated_plan))
                        self.assertEqual(accepted["status"], "plan_accepted", accepted)
                        before = list(backend.calls)
                        with patch.object(
                            server._safety_policy,
                            "_extract_runtime_evidence",
                            return_value=runtime_evidence(start),
                        ):
                            # Safe-action refusals are surfaced by FastMCP as
                            # a public tool error (rather than the raw route's
                            # versioned envelope).  Keep this at the public
                            # dispatcher boundary so a lower-level safety
                            # helper cannot accidentally make the assertion
                            # pass without the plan/action lifecycle.
                            with self.assertRaisesRegex(
                                Exception, "not fully confirmed by this setup-lite map"
                            ):
                                asyncio.run(
                                    server.mcp.call_tool(
                                        action,
                                        {"board_id": BOARD, "artifact": str(artifact)},
                                    )
                                )
                        self.assertEqual(
                            backend.calls,
                            before,
                            "safe flash refusal must occur before any backend I/O",
                        )
            finally:
                server.disconnect(BOARD)

    def test_public_lite_escalation_uses_local_confirmation_when_full_support_is_unavailable(
        self,
    ) -> None:
        """AT06/C05: full's pack prerequisite cannot suppress a valid lite confirmation path.

        This follows the real visible setup-overview -> loaded plan -> accepted
        stable-client fallback -> continuation -> paired-repair protocol.  It
        deliberately has an exact preflight target but no reviewed binding or
        installed pack for ``NO-PACK-PART``.  That absence must block setup-full
        with a named prerequisite, while setup-lite may attach non-destructively
        and commit only the operator-confirmed partial map.
        """

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(backend=backend)
            with (
                patch.object(server, "_hardware_inventory", _setup_inventory()),
                patch.object(
                    server._setup_workflow,
                    "inventory_provider",
                    side_effect=lambda _input: _ready_setup_preflight(),
                ),
            ):
                lite_board, _action, accepted = _accepted_setup_plan(
                    self,
                    server,
                    display_name="Local-confirmed lite board",
                    target_tier="setup-lite",
                    datasheet=_minimal_local_pdf(root, "lite-local.pdf"),
                )
                fallback = accepted["stable_client_fallback"]
                assert isinstance(fallback, dict)
                initial = CapabilityPolicyRepository(root).resolve(lite_board)
                setup_batch = _batch_payload(
                    public_text(server, str(fallback["tool_name"]), fallback["arguments"])
                )
                self.assertEqual(setup_batch["status"], "batch_completed", setup_batch)
                pending = _child_setup_response(setup_batch)
                self.assertEqual(pending["status"], "setup_research_required", pending)
                self.assertIn("setup/lite-confirmation-required", _phase_codes(pending))
                self.assertEqual(initial.tier, Tier.NO_SETUP)
                waiting = CapabilityPolicyRepository(root).resolve(lite_board)
                self.assertTrue(waiting.setup_incomplete)
                self.assertEqual(waiting.tier, Tier.NO_SETUP)

                confirmation = fixture("lite_confirmation.json")
                continuation = json.loads(
                    public_text(
                        server,
                        "continue_setup",
                        {
                            "board_id": lite_board,
                            "continuation_id": pending["continuation_id"],
                            "response": {
                                key: value
                                for key, value in confirmation.items()
                                if key != "schema_version"
                            },
                        },
                    )
                )
                self.assertEqual(continuation["accepted"], "lite_confirmation")
                paired = accepted["paired_action_fallbacks"]
                self.assertIsInstance(paired, list)
                self.assertEqual(len(paired), 1)
                repair_call = paired[0]["call"]
                self.assertIsInstance(repair_call, dict)
                assert isinstance(repair_call, dict)
                repaired_batch = _batch_payload(
                    public_text(
                        server,
                        str(repair_call["tool_name"]),
                        repair_call["arguments"],
                    )
                )
                self.assertEqual(repaired_batch["status"], "batch_completed", repaired_batch)
                completed = _child_setup_response(repaired_batch)
                self.assertEqual(completed["status"], "setup_completed", completed)
                lite = public_call(server, "get_capabilities", {"board_id": lite_board})
                self.assertEqual(lite["tier"], "setup-lite")
                self.assertEqual(
                    lite["identity"],
                    {"assertion": "trusted-not-proven", "capability": None},
                )
                self.assertEqual(
                    CapabilityPolicyRepository(root).resolve(lite_board).map_snapshot,
                    confirmed_lite_policy(lite_board),
                )

                full_board, _full_action, full_accepted = _accepted_setup_plan(
                    self,
                    server,
                    display_name="Missing-support full board",
                    target_tier="setup-full",
                    datasheet=_minimal_local_pdf(root, "full-local.pdf"),
                )
                full_fallback = full_accepted["stable_client_fallback"]
                assert isinstance(full_fallback, dict)
                full_batch = _batch_payload(
                    public_text(
                        server,
                        str(full_fallback["tool_name"]),
                        full_fallback["arguments"],
                    )
                )
                self.assertEqual(full_batch["status"], "batch_completed", full_batch)
                full_failure = _child_setup_response(full_batch)
                self.assertEqual(full_failure["status"], "setup_blocked", full_failure)
                self.assertIn("setup/reviewed-support-not-found", _phase_codes(full_failure))
                full_state = CapabilityPolicyRepository(root).resolve(full_board)
                self.assertEqual(full_state.tier, Tier.NO_SETUP)
                self.assertTrue(full_state.setup_incomplete)

    def test_public_failed_lite_escalation_marks_incomplete_without_replacing_prior_policy(
        self,
    ) -> None:
        """AT06: a failed post-preflight escalation never publishes staged setup evidence."""

        board_id = "prior_lite"
        with isolated_project() as root, tiered_test_server(root) as server:
            policies = CapabilityPolicyRepository(root)
            before = policies.commit(
                board_id,
                Tier.SETUP_LITE,
                map_snapshot=confirmed_lite_policy(board_id),
                evidence={"confirmed_regions": fixture("lite_confirmation.json")["regions"]},
            )
            backend = DeterministicTargetBackend(open_error=RuntimeError("fixture cable lost"))
            server.configure_tiered_test_seams(backend=backend, policy_repository=policies)
            with (
                patch.object(server, "_hardware_inventory", _setup_inventory()),
                patch.object(
                    server._setup_workflow,
                    "inventory_provider",
                    side_effect=lambda _input: _ready_setup_preflight(),
                ),
            ):
                routed_board, _action, accepted = _accepted_setup_plan(
                    self,
                    server,
                    display_name="Prior lite",
                    target_tier="setup-lite",
                    datasheet=_minimal_local_pdf(root, "failure-local.pdf"),
                )
                self.assertEqual(routed_board, board_id)
                fallback = accepted["stable_client_fallback"]
                assert isinstance(fallback, dict)
                batch = _batch_payload(
                    public_text(server, str(fallback["tool_name"]), fallback["arguments"])
                )
                self.assertEqual(batch["status"], "batch_completed", batch)
                failed = _child_setup_response(batch)
                self.assertEqual(failed["status"], "setup_connection_failed", failed)
                self.assertIn("setup/live-connect-failed", _phase_codes(failed))

            after = CapabilityPolicyRepository(root).resolve(board_id)
            self.assertEqual(after.tier, Tier.SETUP_LITE)
            self.assertTrue(after.setup_incomplete)
            self.assertEqual(after.map_snapshot, before.map_snapshot)
            self.assertEqual(after.evidence, before.evidence)
            self.assertNotEqual(after.generation_id, before.generation_id)
            self.assertFalse(
                (root / ".firm" / "boards" / f"{board_id}.yaml").exists(),
                "a failed escalation must not revive an uncommitted profile artifact",
            )
            self.assertFalse(
                (root / ".firm" / "safety" / board_id / "memory_map.yaml").exists(),
                "a failed escalation must not activate a staged full-style safety map",
            )


if __name__ == "__main__":
    unittest.main()
