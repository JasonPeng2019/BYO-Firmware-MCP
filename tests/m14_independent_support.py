"""M14-T public client and fail-closed probe; no implementer fixture imports."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle, TargetSessionMetadata
from pyocd_debug_mcp.flash_image import FlashResult, derive_flash_image
from pyocd_debug_mcp.safety.map_build import GenericSafetyMapDocument
from tests.m05_public_support import NULL_PLAN, PublicSetup
from tests.test_m05_lite_validation import advertised_validation
from tests.test_tiered_acceptance_lite import _setup_inventory
from tests.tiered_acceptance_support import isolated_project, tiered_test_server


RAM = 0x20000000


def imports(destination):
    rows = []
    for name, module in sorted(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if name.startswith(("pyocd_debug_mcp", "tests.", "pyocd.")) and file:
            path = Path(file).resolve()
            if path.is_file():
                data = path.read_bytes()
                rows.append(
                    {
                        "module": name,
                        "path": str(path),
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
    destination.write_text(json.dumps(rows, indent=2), encoding="utf-8")


class EraseProbe:
    """Only exercised I/O exists. Unexpected programming/register/reset calls fail."""

    def __init__(self, root, target):
        self.root = root
        self.target = target
        self.calls = []
        self.at_entry = []
        self.error = None
        self.recovery_supported = True
        self.serial = 0
        self.programmed = {}

    def open(self, **kwargs):
        self.calls.append(("open", kwargs))
        actual_target = kwargs.get("target") or kwargs["board"].pyocd_target
        assert actual_target == self.target, kwargs
        self.serial += 1
        uid = kwargs["unique_id"]
        return TargetSessionHandle(
            session=object(),
            board=kwargs["board"],
            probe_uid=uid,
            route_used="independent-fake-probe",
            target_override=self.target,
            metadata=TargetSessionMetadata(
                "M14 synthetic board",
                "M14 synthetic probe",
                "jlink",
                uid,
                self.target,
                "independent-fake-probe",
                self.target,
                f"probe-open-{self.serial}",
            ),
        )

    def close(self, handle):
        self.calls.append(("close", handle.probe_uid))

    def get_state(self, handle):
        self.calls.append(("state", handle.probe_uid))
        return "halted"

    def read_memory(self, handle, address, width_bits, **kwargs):
        self.calls.append(("read", handle.probe_uid, address, width_bits))
        assert width_bits == 32
        if address == 0xE000ED00:
            return 0x410FC241
        if all(address + offset in self.programmed for offset in range(width_bits // 8)):
            return int.from_bytes(
                bytes(self.programmed[address + offset] for offset in range(width_bits // 8)),
                "little",
            )
        if address == 0:
            return RAM + 0x1000  # Public setup's non-mutating connectivity read.
        if address == RAM:
            return 0x6D143A72
        raise AssertionError(f"Unexpected probe read {address:#x}")

    def supports_recovery(self, handle, mechanism):
        self.calls.append(("supports", handle.probe_uid, mechanism))
        return self.recovery_supported and mechanism == "backend_mass_erase"

    def check_flash_support(self, handle, firmware, *, base_address=None, artifact_sha256=None):
        self.calls.append(("program-support", handle.probe_uid, str(firmware)))
        image = derive_flash_image(firmware, base_address=base_address)
        assert image.byte_count > 0

    def flash(self, handle, firmware, *, halt_after_reset, base_address=None, artifact_sha256=None):
        self.calls.append(("program", handle.probe_uid, str(firmware)))
        image = derive_flash_image(firmware, base_address=base_address)
        # The test fake models the I/O boundary only. Policy allocation and
        # disclosure are produced by the real public M02/M14 services.
        for start, data in image.runs:
            self.programmed.update({start + offset: byte for offset, byte in enumerate(data)})
        return FlashResult.for_image(image, "halted")

    def recover(self, handle):
        # Read actual persisted authority at the first destructive boundary.
        path = self.root / ".agent-workspace/runtime/manual-permissions/mass-erase.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        self.at_entry.append(state)
        self.calls.append(("erase", handle.probe_uid))
        assert state["state"] == "consumed", state
        assert state["grant_id"] == "fresh-independent-grant" and state["claim_id"], state
        assert not path.with_suffix(".claim").exists()
        if self.error:
            raise self.error


def erase_fields(board, permission=None):
    return {
        "board_id": board,
        "hypothesis": "The disclosed whole-device recovery is intentionally requested.",
        "hypothesis_made": True,
        "strategy": "Spend one fresh exact human grant on one backend attempt.",
        "strategy_evaluated": True,
        "expected_fail_return": "Uncertain failure consumes authority and revokes readiness.",
        "expected_success_return": "Recovery disconnects and requires fresh validation.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "action_parameters": {"recovery_mechanism": "backend_mass_erase"},
        "user_permission": permission,
    }


def disclose(f):
    f.flow.call("target_unlock-plan", NULL_PLAN)
    return f.flow.json("target_unlock-plan", erase_fields(f.board))


def grant(f, disclosure, **overrides):
    binding = disclosure["manual_grant"]
    fields = {
        "action": "mass-erase",
        "grant_id": "fresh-independent-grant",
        "board_id": f.board,
        "policy_digest": binding["policy_digest"],
        "binding_digest": binding["binding_digest"],
        **overrides,
    }
    f.server._manual_permissions.write_unlocked_grant(**fields)


def approve(f):
    return f.flow.json("target_unlock-plan", erase_fields(f.board, "one-time"))


def attempt(f):
    return f.flow.call(
        "target_unlock", {"board_id": f.board, "recovery_mechanism": "backend_mass_erase"}
    )


def connect(f):
    # Disconnect clears run-local assignment. Reassign from current public
    # inventory so a profile reconnect never falls through to real probe CLI.
    overview = f.flow.overview("Independent M14 generic erase")
    assert overview["routes"][0]["board_id"] == f.board, overview
    return f.flow.call("connect", {"board_id": f.board})


def safe_read(f):
    f.flow.call("read_memory_address-plan", NULL_PLAN)
    plan = f.flow.plan(
        "read_memory_address", f.board, {"address": RAM, "width": 32, "length": None}
    )
    return f.flow.execute(plan["stable_client_fallback"])


def safe_value(f):
    batch = safe_read(f)
    assert batch["status"] == "batch_completed", batch
    return int(batch["completed"][0]["result"].splitlines()[0], 16)


@contextmanager
def ordinary_full(test, *, target="nrf52840"):
    """Generate real FULL policy through public onboarding and installed target facts."""
    with isolated_project() as root, tiered_test_server(root) as server:
        backend = EraseProbe(root, target)
        server.configure_tiered_test_seams(backend=backend)
        f = SimpleNamespace(root=root, server=server, backend=backend, wire=[])

        def call(name, args):
            from tests.tiered_acceptance_support import public_text

            row = {"tool": name, "arguments": args}
            f.wire.append(row)
            try:
                row["reply"] = public_text(server, name, args)
                return row["reply"]
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
                raise

        f.flow = PublicSetup(server, call=call)
        with patch.object(server, "_hardware_inventory", _setup_inventory()):
            try:
                from pyocd_debug_mcp.setup_flow import board_catalog

                test.assertEqual(board_catalog._CATALOG, {})
                route = f.flow.overview("Independent M14 generic erase")["routes"][0]
                f.board = route["board_id"]
                pdf = root / "synthetic.pdf"
                pdf.write_bytes(b"%PDF-1.4\n% Synthetic host evidence; no physical claim\n")
                part = {"nrf52840": "nRF52840-QIAA", "nrf52832": "nRF52832-QFAA"}[target]
                _, plan = f.flow.accept(route, pdf, part, tier="setup-full")
                pending = f.flow.start(plan)
                test.assertEqual(pending["status"], "setup_research_required", pending)
                f.flow.answer(f.board, pending, f.flow.target_answer(pending, target))
                completed = f.flow.fix(plan)
                test.assertEqual(completed["status"], "setup_completed", completed)
                test.assertEqual(
                    advertised_validation(f.flow, f.board)["status"], "validation_passed"
                )
                f.map = server._safety_repository.load_current(f.board)
                test.assertIsInstance(f.map, GenericSafetyMapDocument)
                test.assertEqual(board_catalog._CATALOG, {})
                yield f
            finally:
                for board in server.connection_manager.assigned_board_ids():
                    call("disconnect", {"board_id": board})
                evidence = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / test.id() / target
                evidence.mkdir(parents=True, exist_ok=True)
                (evidence / "wire.json").write_text(json.dumps(f.wire, indent=2), encoding="utf-8")
                (evidence / "probe.json").write_text(
                    json.dumps(backend.calls, default=str, indent=2), encoding="utf-8"
                )
                (evidence / "backend-entry.json").write_text(
                    json.dumps(backend.at_entry, indent=2), encoding="utf-8"
                )
                imports(evidence / "imports.json")
                if (root / ".firm").exists():
                    shutil.copytree(root / ".firm", evidence / ".firm", dirs_exist_ok=True)
                manual = root / ".agent-workspace/runtime/manual-permissions"
                if manual.exists():
                    shutil.copytree(manual, evidence / "manual", dirs_exist_ok=True)
