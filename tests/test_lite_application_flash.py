"""VI01A: public lite flash and real full-tier generic allocation contracts."""

from __future__ import annotations

import hashlib
from functools import wraps
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.capabilities.lite import normalize_lite_confirmation
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.capabilities.routing import TierRouter
from pyocd_debug_mcp.safety.enforce import SafetyPolicyError
from pyocd_debug_mcp.safety.map_build import require_reconciled_authority
from pyocd_debug_mcp.setup_flow.preflight import PreflightDecision, ProbeCandidate, SetupUserInput
from pyocd_debug_mcp.setup_flow.setup import SetupPhaseContext
from pyocd_debug_mcp.setup_flow.validate import (
    ValidationBackend,
    ValidationInventory,
    ValidationProbe,
)
from test_lite_protective_overlaps import FLASH, RAM, cortex_elf, region
from tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    public_text,
    tiered_test_server,
)


def observe(test, **data):
    destination = os.environ.get("VI01A_OBSERVATIONS")
    if destination:
        with Path(destination).open("a", encoding="utf-8") as output:
            output.write(json.dumps({"test": test.id(), **data}, default=str) + "\n")


class PublicActions:
    def invoke(self, name, parameters):
        plan = name + "-plan"
        public_text(
            self.server,
            plan,
            dict.fromkeys(
                (
                    "board_id",
                    "hypothesis",
                    "strategy",
                    "hypothesis_made",
                    "strategy_evaluated",
                    "expected_fail_return",
                    "expected_success_return",
                    "max_calls",
                    "max_calls_buffer",
                    "action_parameters",
                    "user_permission",
                )
            ),
        )
        args = dict(
            board_id=self.board_id,
            hypothesis="The current committed policy permits this artifact",
            strategy="Execute one safe public operation against a simulated hardware backend",
            hypothesis_made=True,
            strategy_evaluated=True,
            expected_fail_return="The actual safety contract refuses before hardware effects",
            expected_success_return="Programming completes with normal finalization",
            max_calls=1,
            max_calls_buffer=0,
            action_parameters=parameters,
        )
        if name == "flash_bootloader":
            args["user_permission"] = "one-time"
        accepted = json.loads(public_text(self.server, plan, args))
        self.assertEqual(accepted["status"], "plan_accepted", accepted)
        try:
            response = public_text(self.server, name, {"board_id": self.board_id, **parameters})
        except ToolError as exc:
            response = str(exc)
        observe(
            self,
            stage="public-action",
            action=name,
            response=response,
            backend_calls=self.backend.calls,
        )
        return response


class LiteApplicationFlashTests(PublicActions, unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(isolated_project())
        self.server = self.enterContext(tiered_test_server(self.root))
        self.board_id = "vi01a_lite"
        self.backend = DeterministicTargetBackend()
        self.server.configure_tiered_test_seams(
            backend=self.backend, auto_target_resolver=lambda _: "nrf52840"
        )
        self.handle = self.backend.open(board=None, unique_id="vi01a-lite-probe", target="nrf52840")
        self.server._promote_open_session(self.board_id, self.handle, gate_reason="VI01A simulated")
        self.addCleanup(self.server.disconnect, self.board_id)
        self.artifact = cortex_elf(self.root / "application.elf", FLASH)

    def commit(self, rows, sectors=None):
        confirmation = normalize_lite_confirmation(
            self.board_id,
            {
                "decision": "confirm-or-correct",
                "regions": [region("ram", RAM, RAM + 0x100), *rows],
                "flash": {
                    "backend_target": "nrf52840",
                    "erase_sectors": sectors
                    if sectors is not None
                    else [{"start": FLASH, "end": FLASH + 0x100}],
                },
                "recovery": None,
            },
        )
        committed = CapabilityPolicyRepository(self.root).commit(
            self.board_id,
            Tier.SETUP_LITE,
            map_snapshot=confirmation.map_snapshot,
            evidence=confirmation.evidence,
        )
        repository = CapabilityPolicyRepository(self.root)
        resolved = TierRouter(repository).require_safe("flash_application", self.board_id)
        self.assertEqual(resolved.generation_id, committed.generation_id)
        self.assertEqual(resolved.policy_digest, committed.policy_digest)
        self.assertEqual(resolved.map_snapshot, confirmation.map_snapshot)
        self.server.configure_tiered_test_seams(policy_repository=repository)
        observe(
            self,
            stage="commit-reopen-router",
            generation=resolved.generation_id,
            digest=resolved.policy_digest,
            snapshot=resolved.map_snapshot,
        )
        return resolved

    def test_public_lite_application_programs_and_binds_artifact_without_full_allocation(self):
        before = self.commit([region("application_flash", FLASH, FLASH + 0x100)])
        self.server._check_flash_safety("flash_application", self.board_id, self.artifact)
        start = len(self.backend.calls)
        response = self.invoke("flash_application", {"artifact": str(self.artifact)})
        self.assertIn("Flashed", response)
        self.assertIn("target left halted", response)
        self.assertEqual(
            self.backend.calls[start:],
            [
                ("flash", "vi01a-lite-probe", self.artifact.resolve(), False),
                ("release_reset", "vi01a-lite-probe"),
            ],
        )
        self.assertEqual(
            self.server._current_symbol_artifacts[self.board_id],
            (self.artifact.resolve(), hashlib.sha256(self.artifact.read_bytes()).hexdigest()),
        )
        after = CapabilityPolicyRepository(self.root).resolve(self.board_id)
        self.assertEqual(after.generation_id, before.generation_id)
        self.assertEqual(after.map_snapshot, before.map_snapshot)
        self.assertNotIn(self.board_id, self.server._pending_generic_allocations)

    def test_public_lite_bootloader_still_programs_and_finalizes(self):
        self.commit([region("bootloader", FLASH, FLASH + 0x100)])
        start = len(self.backend.calls)
        response = self.invoke("flash_bootloader", {"artifact": str(self.artifact)})
        self.assertIn("Flashed", response)
        self.assertEqual(
            self.backend.calls[start:],
            [
                ("flash", "vi01a-lite-probe", self.artifact.resolve(), False),
                ("release_reset", "vi01a-lite-probe"),
            ],
        )

    def test_public_lite_prohibited_role_and_sector_refusals_precede_all_effects(self):
        app = region("application_flash", FLASH, FLASH + 0x100)
        cases = [
            ([app, region("prohibited", FLASH + 7, FLASH + 8)], None, "not fully confirmed"),
            ([app, region("bootloader", FLASH, FLASH + 0x10)], None, "not fully confirmed"),
            ([app, region("bootloader", FLASH + 0x80, FLASH + 0x100)], None, "not fully confirmed"),
            ([app], [{"start": FLASH, "end": FLASH + 8}], "do not fully cover"),
            (
                [region("application_flash", FLASH, FLASH + 0x100, writable=False)],
                None,
                "not fully confirmed",
            ),
        ]
        for rows, sectors, message in cases:
            with self.subTest(rows=rows, sectors=sectors):
                self.commit(rows, sectors)
                before = list(self.backend.calls)
                response = self.invoke("flash_application", {"artifact": str(self.artifact)})
                self.assertIn(message, response)
                self.assertNotIn("No active full safety snapshot", response)
                self.assertEqual(self.backend.calls, before)

    def test_no_setup_is_refused_in_public_route_and_both_allocation_helpers(self):
        repository = CapabilityPolicyRepository(self.root)
        repository.commit(self.board_id, Tier.NO_SETUP)
        self.server.configure_tiered_test_seams(policy_repository=repository)
        before = list(self.backend.calls)
        try:
            response = public_text(
                self.server,
                "flash_application",
                {"board_id": self.board_id, "artifact": str(self.artifact)},
            )
        except ToolError as exc:
            response = str(exc)
        self.assertIn("no safety map", response)
        for call in (
            lambda: self.server._stage_generic_allocation(
                self.board_id, self.artifact, self.handle
            ),
            lambda: self.server._prepare_generic_allocation(
                "flash_application", self.board_id, self.artifact, self.handle
            ),
        ):
            with self.assertRaises(SafetyPolicyError) as caught:
                call()
            self.assertEqual(caught.exception.code, "safety/map-refresh-required")
        self.assertEqual(self.backend.calls, before)


class PublicationObservingBackend(DeterministicTargetBackend):
    def __init__(self, root, board_id):
        super().__init__(read_value=0x410FC240, supported_targets=frozenset({"stm32l476rgtx"}))
        self.root = root
        self.board_id = board_id
        self.policies_at_programming = []

    def flash(self, handle, firmware, *, halt_after_reset):
        state = CapabilityPolicyRepository(self.root).resolve(self.board_id)
        self.policies_at_programming.append(state)
        return super().flash(handle, firmware, halt_after_reset=halt_after_reset)


def fresh_full_project(function):
    """Select the firmware project before import-time pack paths are captured."""

    @wraps(function)
    def execute(self):
        if os.environ.get("VI01A_FULL_PROJECT"):
            return function(self)
        with isolated_project() as project:
            environment = dict(
                os.environ,
                VI01A_FULL_PROJECT=str(project),
                BYO_MCP_ARTIFACT_ROOT=str(project),
                BYO_MCP_MONITOR_ROOT=str(project / "monitor-import"),
            )
            destination = os.environ.get("VI01A_OBSERVATIONS")
            ledger_root = os.environ.get("BYO_FINAL_CHILD_EVIDENCE")
            # The interpreter still starts with the project selected before any
            # product import. The real loader/result now records the inner body.
            child_evidence = Path(ledger_root or project / "child-evidence") / function.__name__
            child_evidence.mkdir(parents=True, exist_ok=False)
            command = [
                sys.executable, "-B", str(Path(__file__).with_name("final_acceptance_suite.py")),
                "--worker", type(self).__module__.removeprefix("tests."), "--select", self.id(),
                "--evidence", str(child_evidence),
            ] if ledger_root else [sys.executable, "-B", "-m", "unittest", "-v", self.id()]
            receipt = {
                "command": command, "cwd": str(project), "outer_forwarder": self.id(),
                "inner_selected_id": self.id(), "accounting": "Forwarder and child are not independent coverage",
                "artifact_root_before_import": environment["BYO_MCP_ARTIFACT_ROOT"],
                "phase": "dispatch", "evidence": str(child_evidence),
                "started_monotonic": time.monotonic(),
            }
            command_path = child_evidence / "command.json"
            command_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            result = subprocess.run(
                command,
                cwd=project,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            (child_evidence / "stdout.log").write_bytes(result.stdout)
            (child_evidence / "stderr.log").write_bytes(result.stderr)
            receipt.update(phase="completed", exit_code=result.returncode,
                           completed_monotonic=time.monotonic(),
                           stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),
                           stderr_sha256=hashlib.sha256(result.stderr).hexdigest())
            command_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            if destination:
                output = Path(destination).parent / (function.__name__ + ".child.log")
                output.write_bytes(result.stdout + result.stderr)
                output.with_suffix(".command.json").write_text(
                    json.dumps(
                        {
                            **receipt,
                        },
                        indent=2,
                    )
                    + "\n"
                )
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode("utf-8", errors="replace"))
            if ledger_root:
                self.assertTrue((child_evidence / "result.json").is_file(), "Missing actual child ledger")
                actual = json.loads((child_evidence / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(actual["loaded"], [self.id()])
                if actual["methods"].get(self.id()) == "SKIP":
                    self.skipTest("Actual selected child skipped; see " + str(child_evidence))
                self.assertEqual(actual["methods"].get(self.id()), "PASS", actual)

    return execute


class FullGenericAllocationTests(PublicActions, unittest.TestCase):
    def setUp(self):
        project = os.environ.get("VI01A_FULL_PROJECT")
        if project is None:
            return
        self.root = Path(project).resolve()
        self.server = self.enterContext(tiered_test_server(self.root))
        self.board_id = "vi01a_full"
        self.backend = PublicationObservingBackend(self.root, self.board_id)
        self.server.configure_tiered_test_seams(backend=self.backend)
        source = (
            Path(__file__).resolve().parents[1]
            / "fallback-support-evidence/results/hil-artifacts/.firm"
        )
        shutil.copytree(source / "packs", self.root / ".firm/packs")
        datasheet = self.root / "STM32L476RG.pdf"
        shutil.copyfile(next((source / "evidence/datasheets").glob("*.pdf")), datasheet)
        probe = ProbeCandidate("full-probe", "ST-Link", "stlink", "full-serial")
        connection_id = self.server.probe_connection_id("stlink", "full-serial")
        self.server._setup_tiers[self.board_id] = Tier.SETUP_FULL
        self.server.assignment_store.assign(connection_id, self.board_id)
        user = SetupUserInput(
            self.board_id,
            connection_id,
            "VI01A full STM32L4",
            "STM32L476RGT6",
            None,
            datasheet_path=str(datasheet),
            requires_uart=False,
        )
        context = SetupPhaseContext(
            "continuation",
            "attempt",
            "setup",
            user,
            PreflightDecision(
                "preflight_ready",
                "setup/preflight-ready",
                "ready",
                selected_probe=probe,
                selected_target="stm32l476rgtx",
            ),
            {},
        )
        # Only hardware inventory/reads/open/close are simulated. Setup, evidence
        # replay, safety generation, repository publication and gate decisions run.
        with patch.object(
            self.server._board_validator,
            "_backend",
            ValidationBackend(
                inventory=lambda: ValidationInventory(
                    probes=(ValidationProbe("full-probe", "ST-Link", "stlink", "full-serial"),)
                ),
                target_supported=self.server._validation_target_supported,
                connect=self.server._validation_connect,
                read_memory=self.server._validation_read,
                capture_serial=self.server._validation_capture,
                close=self.server._validation_close,
            ),
        ):
            for phase in (
                self.server._setup_connection_phase,
                self.server._setup_validation_phase,
                self.server._setup_safety_research_phase,
                self.server._setup_safety_map_phase,
                self.server._setup_commit_phase,
            ):
                outcome = phase(context)
                self.assertTrue(outcome.verified, (outcome.agent_prompt, outcome.details))
        self.addCleanup(self.server.disconnect, self.board_id)
        self.state = CapabilityPolicyRepository(self.root).resolve(self.board_id)
        self.server.configure_tiered_test_seams(
            policy_repository=CapabilityPolicyRepository(self.root)
        )
        self.assertEqual(
            TierRouter(CapabilityPolicyRepository(self.root))
            .state_for(self.board_id)
            .generation_id,
            self.state.generation_id,
        )
        self.document = self.server._active_full_document(self.board_id)
        require_reconciled_authority(self.document)
        from pyocd_debug_mcp.setup_flow import device_support

        self.assertEqual(
            device_support.MANIFEST_PATH, self.server._profile_repository.store.layout.pack_manifest
        )
        self.assertEqual(self.document.deployment_policy, {"kind": "none"})
        self.assertIsNone(self.document.partitions.application)
        self.assertTrue(self.document.geometry.erase_available)
        self.handle = self.server._handle(self.board_id)
        self.artifact = cortex_elf(
            self.root / "full.elf", self.document.geometry.physical_flash[0].start
        )
        observe(
            self,
            stage="full-real-setup",
            generation=self.state.generation_id,
            snapshot=self.document.to_document(),
            source="actual copied pack/datasheet fixture",
            server_origin=self.server.__file__,
            authority_manifest=str(device_support.MANIFEST_PATH),
        )

    def stage(self):
        self.server._stage_generic_allocation(self.board_id, self.artifact, self.handle)
        self.assertIn(self.board_id, self.server._pending_generic_allocations)

    @fresh_full_project
    def test_public_full_allocation_publishes_proof_before_programming(self):
        response = self.invoke("flash_application", {"artifact": str(self.artifact)})
        self.assertIn("Flashed", response)
        self.assertEqual(len(self.backend.policies_at_programming), 1)
        at_programming = self.backend.policies_at_programming[0]
        self.assertNotEqual(at_programming.generation_id, self.state.generation_id)
        self.assertEqual(
            at_programming.map_snapshot["deployment_policy"]["kind"],
            "artifact_application_allocation",
        )
        proof = at_programming.map_snapshot["deployment_policy"]
        self.assertEqual(proof["creation_map_digest"], self.document.canonical_digest)
        self.assertEqual(
            proof["creation_artifact_digest"],
            hashlib.sha256(self.artifact.read_bytes()).hexdigest(),
        )
        active = self.server._active_full_document(self.board_id)
        self.assertEqual(
            self.server._safety_repository.load_current(self.board_id).canonical_digest,
            active.canonical_digest,
        )
        self.assertEqual(
            self.server.gate_manager.snapshot(self.board_id).safety_map.map_digest,
            active.canonical_digest,
        )
        self.assertNotIn(self.board_id, self.server._pending_generic_allocations)

    @fresh_full_project
    def test_full_prepare_requires_the_staged_allocation_proof(self):
        before = list(self.backend.calls)
        with self.assertRaises(SafetyPolicyError) as caught:
            self.server._prepare_generic_allocation(
                "flash_application", self.board_id, self.artifact, self.handle
            )
        self.assertEqual(caught.exception.code, "safety/allocation-proof-missing")
        self.assertEqual(self.backend.calls, before)

    @fresh_full_project
    def test_full_prepare_rejects_changed_artifact(self):
        self.stage()
        data = self.artifact.read_bytes()
        self.artifact.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        self.assert_stale_prepare()

    @fresh_full_project
    def test_full_prepare_rejects_changed_committed_map(self):
        self.stage()
        document = self.document.to_document()
        document["regions"][0]["name"] += " refreshed"
        self.server._capability_policies.commit(
            self.board_id,
            Tier.SETUP_FULL,
            profile_snapshot=self.state.profile_snapshot,
            map_snapshot=document,
            evidence=self.state.evidence,
            expected_state=self.state,
        )
        require_reconciled_authority(self.server._active_full_document(self.board_id))
        self.assert_stale_prepare()

    @fresh_full_project
    def test_full_prepare_rejects_replaced_connection(self):
        self.stage()
        prior = self.server.connection_manager.clear(self.board_id)
        self.server.connection_manager.assign(
            self.board_id,
            prior.handle,
            prior.runtime_session,
            connection_id="probeid:stlink:replacement",
        )
        self.assert_stale_prepare()

    def assert_stale_prepare(self):
        before = list(self.backend.calls)
        with self.assertRaises(SafetyPolicyError) as caught:
            self.server._prepare_generic_allocation(
                "flash_application", self.board_id, self.artifact, self.handle
            )
        self.assertEqual(caught.exception.code, "safety/allocation-proof-stale")
        self.assertEqual(self.backend.calls, before)
        self.assertNotIn(self.board_id, self.server._pending_generic_allocations)


if __name__ == "__main__":
    unittest.main()
