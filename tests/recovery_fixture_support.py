"""Shared recovery fixture support fixtures; no collected cases."""

from __future__ import annotations
from tests.tiered_acceptance_support import DeterministicTargetBackend
import subprocess
import sys
from typing import Any, NamedTuple
from pyocd_debug_mcp.capabilities.manual_permission_helper import unlock_manual_grant
from pyocd_debug_mcp.capabilities.manual_permissions import (
    ManualPermissionError,
    ManualPermissionRepository,
)
from tests.tiered_acceptance_support import public_text
from tests.workspace_fixture_support import WORKSPACE
import json
import unittest
from pathlib import Path
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from tests.tiered_acceptance_support import known_good_full_policy


BOARD = "legacy_full"


POLICY = "a" * 64


DISCLOSURE = "b" * 64


def _native_lite_recovery_policy() -> tuple[dict[str, object], dict[str, object]]:
    """Return independent valid setup-lite evidence with its one native recovery fact."""

    profile, _full_map = known_good_full_policy()
    # A native mass-erase disclosure must account for *physical* flash and
    # its complete erase geometry.  Do not reuse the ordinary lite fixture:
    # it is intentionally partial and has executable SRAM for safe-read tests.
    lite_map: dict[str, object] = {
        "board_id": BOARD,
        "regions": [
            {
                "name": "physical flash",
                "kind": "physical_flash",
                "start": "0x00000000",
                "end": "0x00010000",
                "readable": True,
                "writable": False,
                "executable": False,
                "source_pages": [101],
                "source_note": "confirmed physical flash range",
            },
            {
                "name": "SRAM",
                "kind": "ram",
                "start": "0x20000000",
                "end": "0x20001000",
                "readable": True,
                "writable": True,
                "executable": False,
                "source_pages": [102],
                "source_note": "confirmed RAM containment range",
            },
        ],
        "flash": {
            "backend_target": "nrf52840",
            "erase_sectors": [{"start": 0, "end": 0x00010000, "bank": "main"}],
        },
    }
    lite_map["recovery"] = {
        "mechanism": "backend_mass_erase",
        "source_pages": [101],
        "source_note": "vendor recovery chapter confirms typed backend mass erase",
    }
    return profile, lite_map


def _unlock_fields(*, permission: str | None) -> dict[str, object]:
    """Use one disclosed immutable recovery request across public plan/action calls."""

    return {
        "board_id": BOARD,
        "hypothesis": "The target is locked and requires its documented recovery primitive.",
        "hypothesis_made": True,
        "strategy": "Run exactly one disclosed typed backend recovery after fresh approval.",
        "strategy_evaluated": True,
        "expected_fail_return": "The server preserves the grant as unavailable after a failed attempt.",
        "expected_success_return": "The board is disconnected and requires fresh validation.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "action_parameters": {"recovery_mechanism": "backend_mass_erase"},
        "user_permission": permission,
    }


def _unlock_null_fields() -> dict[str, object]:
    return {field: None for field in _unlock_fields(permission=None)}


def _public_json_reply(
    server: Any, tool_name: str, arguments: dict[str, object]
) -> dict[str, object]:
    """Decode a structured public tool reply that predates the common envelope."""

    response = json.loads(public_text(server, tool_name, arguments))
    if not isinstance(response, dict):
        raise AssertionError(f"{tool_name} reply must be a JSON object: {response!r}")
    return response


def _configure_public_native_lite(
    server: Any, root: Path, backend: DeterministicTargetBackend
) -> ManualPermissionRepository:
    """Install only persistent lite evidence plus a deterministic live session."""

    profile, lite_map = _native_lite_recovery_policy()
    policies = CapabilityPolicyRepository(root)
    policies.commit(
        BOARD,
        Tier.SETUP_LITE,
        profile_snapshot=profile,
        map_snapshot=lite_map,
        evidence={"confirmed": "native recovery disclosed by fixture"},
    )
    manual = ManualPermissionRepository(root, server.server_run.run_id)
    manual.reset_startup()
    server.configure_tiered_test_seams(
        backend=backend,
        policy_repository=policies,
        manual_permission_repository=manual,
    )
    handle = backend.open(board=None, unique_id="recovery-probe", target="nrf52840")
    server._promote_open_session(BOARD, handle, gate_reason="acceptance recovery fixture")
    return manual


OBSERVATIONS: dict[str, object] = {}


UNLOCK = {"board_id": BOARD, "recovery_mechanism": "backend_mass_erase"}


class Attempt(NamedTuple):
    grant_id: str
    claim_id: str
    policy_digest: str
    binding_digest: str


def _recover_calls(backend: DeterministicTargetBackend) -> int:
    return sum(1 for call in backend.calls if call[0] == "recover")


def _record_bytes(manual: ManualPermissionRepository, action: str = "mass-erase") -> bytes:
    return manual._action_path(action).read_bytes()  # noqa: SLF001 - durable protocol record


def _invoke_helper(
    project_root: Path, action: str = "mass-erase", **binding: object
) -> dict[str, object]:
    """Run the shipped workspace helper as one separate human skill invocation."""

    arguments: dict[str, object] = {"board_id": BOARD, "policy_digest": POLICY}
    if action == "mass-erase":
        arguments["binding_digest"] = DISCLOSURE
    arguments.update(binding)
    return unlock_manual_grant(project_root=project_root, action=action, **arguments)


def _disclose(testcase: unittest.TestCase, server: Any) -> dict[str, object]:
    public_text(server, "target_unlock-plan", _unlock_null_fields())
    disclosure = _public_json_reply(server, "target_unlock-plan", _unlock_fields(permission=None))
    testcase.assertEqual(disclosure["status"], "unlock_permission_requested", disclosure)
    grant = disclosure["manual_grant"]
    testcase.assertIsInstance(grant, dict, disclosure)
    assert isinstance(grant, dict)
    return grant


def _human_grant(grant: dict[str, object], **override: object) -> dict[str, object]:
    """Invoke the helper with the exact bindings the public disclosure relayed."""

    binding: dict[str, object] = {
        "policy_digest": str(grant["policy_digest"]),
        "binding_digest": str(grant["binding_digest"]),
    }
    binding.update(override)
    return _invoke_helper(Path(str(grant["project_root"])), **binding)


def _workspace_cli_grant(grant: dict[str, object]) -> dict[str, object]:
    """Run the shipped workspace command in a different process on this test project."""

    command = [
        sys.executable,
        "-B",
        str(WORKSPACE / "bin" / "manual-permission"),
        "--project-root",
        str(grant["project_root"]),
        "--action",
        "mass-erase",
        "--board-id",
        BOARD,
        "--policy-digest",
        str(grant["policy_digest"]),
        "--binding-digest",
        str(grant["binding_digest"]),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    OBSERVATIONS.setdefault("workspace_cli", []).append(
        {
            "argv": command,
            "exit": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    )
    if result.returncode != 0:
        raise AssertionError(
            f"workspace helper failed: {result.returncode}: {result.stdout} {result.stderr}"
        )
    return json.loads(result.stdout)


def _approve(server: Any) -> dict[str, object]:
    return _public_json_reply(server, "target_unlock-plan", _unlock_fields(permission="one-time"))


def _grant_and_reserve(
    testcase: unittest.TestCase,
    server: Any,
    manual: ManualPermissionRepository,
    *,
    workspace_cli: bool = False,
) -> Attempt:
    """Disclose, run a real human helper grant, and reserve it with fresh approval."""

    grant = _disclose(testcase, server)
    # A fresh human grant is only ever requested from a clean locked record.
    testcase.assertEqual(grant["state"], "locked", grant)
    testcase.assertEqual(Path(str(grant["project_root"])).resolve(), manual.project_root)
    issued = _workspace_cli_grant(grant) if workspace_cli else _human_grant(grant)
    testcase.assertEqual(issued["status"], "manual_grant_unlocked", issued)
    approved = _approve(server)
    testcase.assertEqual(approved["status"], "unlock_plan_approved", approved)
    reserved = manual.status("mass-erase")
    testcase.assertEqual(reserved.state, "reserved")
    testcase.assertEqual(reserved.grant_id, issued["grant_id"])
    testcase.assertEqual(reserved.server_run_id, manual.server_run_id)
    assert reserved.claim_id is not None
    return Attempt(
        str(issued["grant_id"]),
        reserved.claim_id,
        str(grant["policy_digest"]),
        str(grant["binding_digest"]),
    )


def _release(server: Any) -> None:
    if server.connection_manager.maybe_connection(BOARD) is not None:
        server.disconnect(BOARD)


def _assert_no_replay(
    testcase: unittest.TestCase, manual: ManualPermissionRepository, spent: Attempt
) -> None:
    """Old grant/claim replays and stale cleanup never change the current record."""

    before = _record_bytes(manual)
    with testcase.assertRaises(ManualPermissionError):
        manual.consume_mass_erase(BOARD, spent.policy_digest, spent.binding_digest, spent.claim_id)
    with testcase.assertRaises(ManualPermissionError):
        manual.reserve_mass_erase(BOARD, spent.grant_id, spent.policy_digest, spent.binding_digest)
    manual.abandon_mass_erase(BOARD, spent.claim_id)
    manual.retire_mass_erase(BOARD, spent.policy_digest, spent.binding_digest, spent.claim_id)
    testcase.assertEqual(_record_bytes(manual), before)


def _assert_closed(
    testcase: unittest.TestCase, manual: ManualPermissionRepository, spent: Attempt
) -> None:
    """A terminal grant leaves a clean locked current-run record and refuses replay."""

    record = manual.status("mass-erase")
    testcase.assertEqual(record.state, "locked", record)
    testcase.assertEqual(record.server_run_id, manual.server_run_id)
    testcase.assertIsNone(record.grant_id)
    testcase.assertIsNone(record.claim_id)
    _assert_no_replay(testcase, manual, spent)
