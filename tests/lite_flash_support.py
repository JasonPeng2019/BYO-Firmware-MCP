"""Shared lite flash support fixtures; no collected cases."""

from __future__ import annotations
import shutil
import unittest
from unittest.mock import patch
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
from tiered_acceptance_support import tiered_test_server
import hashlib
from functools import wraps
from xml.etree import ElementTree
import json
import os
import subprocess
import sys
import time
from mcp.server.fastmcp.exceptions import ToolError
from tiered_acceptance_support import DeterministicTargetBackend, isolated_project, public_text
import struct
from pathlib import Path


RAM = 0x20000000


FLASH = 0x08000000


def region(kind: str, start: int, end: int, **permissions: bool) -> dict[str, object]:
    return {
        "name": f"confirmed {kind}",
        "kind": kind,
        "start": hex(start),
        "end": hex(end),
        "readable": True,
        "writable": True,
        "executable": False,
        "source_pages": [42],
        "source_note": "explicit operator-confirmed geometry and access permissions",
        **permissions,
    }


def cortex_elf(path: Path, start: int) -> Path:
    """Serialize a valid ARM ELF32 load image; no compiler or policy parser seam."""

    ident = b"\x7fELF\x01\x01\x01" + b"\0" * 9
    header = ident + struct.pack(
        "<HHIIIIIHHHHHH",
        2,
        40,
        1,
        start + 9,
        52,
        0,
        0x05000000,
        52,
        32,
        1,
        40,
        0,
        0,
    )
    program = struct.pack("<8I", 1, 84, start, start, 16, 16, 5, 4)
    vector_and_code = struct.pack("<4I", RAM + 0x80, start + 9, 0xBF00BF00, 0xBF00BF00)
    path.write_bytes(header + program + vector_and_code)
    return path


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
                PYTHONPATH=os.pathsep.join(
                    (
                        str(Path(__file__).resolve().parents[1] / "src"),
                        str(Path(__file__).resolve().parent),
                        str(Path(__file__).resolve().parents[1]),
                    )
                ),
            )
            destination = os.environ.get("VI01A_OBSERVATIONS")
            ledger_root = os.environ.get("BYO_FINAL_CHILD_EVIDENCE")
            # The interpreter still starts with the project selected before any
            # product import. Standard pytest/JUnit records the actual inner body.
            child_evidence = Path(ledger_root or project / "child-evidence") / function.__name__
            child_evidence.mkdir(parents=True, exist_ok=False)
            module, case, method = self.id().rsplit(".", 2)
            selector = str(Path(__file__).with_name(module.split(".")[-1] + ".py"))
            command = [
                sys.executable,
                "-B",
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "--strict-markers",
                "-m",
                "not live",
                "-v",
                selector + "::" + case + "::" + method,
                "--junitxml=" + str(child_evidence / "result.xml"),
            ]
            receipt = {
                "command": command,
                "cwd": str(project),
                "outer_forwarder": self.id(),
                "inner_selected_id": self.id(),
                "accounting": "Forwarder and child are not independent coverage",
                "artifact_root_before_import": environment["BYO_MCP_ARTIFACT_ROOT"],
                "phase": "dispatch",
                "evidence": str(child_evidence),
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
            receipt.update(
                phase="completed",
                exit_code=result.returncode,
                completed_monotonic=time.monotonic(),
                stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),
                stderr_sha256=hashlib.sha256(result.stderr).hexdigest(),
            )
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
            self.assertEqual(
                result.returncode,
                0,
                (result.stdout + result.stderr).decode("utf-8", errors="replace"),
            )
            cases = ElementTree.parse(child_evidence / "result.xml").findall(".//testcase")
            self.assertTrue(cases, "Selected child collected no product case")
            if all(case.find("skipped") is not None for case in cases):
                self.skipTest("Actual selected child skipped; see " + str(child_evidence))

    return execute


class FullGenericAllocationCase(PublicActions, unittest.TestCase):
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

    def assert_stale_prepare(self):
        before = list(self.backend.calls)
        with self.assertRaises(SafetyPolicyError) as caught:
            self.server._prepare_generic_allocation(
                "flash_application", self.board_id, self.artifact, self.handle
            )
        self.assertEqual(caught.exception.code, "safety/allocation-proof-stale")
        self.assertEqual(self.backend.calls, before)
        self.assertNotIn(self.board_id, self.server._pending_generic_allocations)
