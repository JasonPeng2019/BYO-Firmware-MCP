"""M15 host fixture: real public FULL setup; only inventory and target I/O are controlled."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import patch

from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
from pyocd_debug_mcp.setup_flow import device_support
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from tests import m02_independent_support as boundary
from tests.m05_public_support import NULL_PLAN, PublicSetup
from tests.tiered_acceptance_support import isolated_project, public_text, tiered_test_server

BOARD = "m15_host"
UID = "m15-controlled-probe"
RAM = 0x20000018
RAM_VALUE = 0x7153AB09


class MemoryBoundary(boundary.MemoryBoundary):
    def read_memory(self, address, width=32, **kwargs):
        self.calls.append(("read-memory", address, width))
        if 0x20000000 <= address < 0x20018000:
            assert width == 32
            return RAM_VALUE
        return super().read_memory(address, width, **kwargs)


class Adapter(boundary.BoundaryAdapter):
    def open(self, **kwargs):
        self.target.calls.append(("open", kwargs))
        return super().open(**kwargs)

    def close(self, handle):
        self.target.calls.append(("close",))


class PublicRecoveryFixture:
    def __init__(self, root):
        self.root = root
        self.target = MemoryBoundary()
        self.wire = []
        self.runs = []
        self.session_stack = None
        inventory = ValidationInventory(
            probes=(ValidationProbe(UID, "Controlled ST-Link", "stlink", UID),)
        )
        self.inventory = SimpleNamespace(
            snapshot=lambda: snapshot_from_validation_inventory(inventory),
            validation_inventory=lambda: inventory,
        )

    def start(self):
        self.stop()
        self.session_stack = ExitStack()
        self.server = self.session_stack.enter_context(tiered_test_server(self.root))
        self.server.configure_tiered_test_seams(backend=Adapter(self.target))
        self.session_stack.enter_context(
            patch.object(self.server, "_hardware_inventory", self.inventory)
        )
        self.session_stack.enter_context(
            patch.object(
                self.server,
                "_validation_inventory",
                lambda *_: self.inventory.validation_inventory(),
            )
        )
        self.flow = PublicSetup(self.server, call=self.call)
        self.runs.append(self.server.server_run.run_id)
        return self.server

    def stop(self):
        if self.session_stack is not None:
            try:
                if self.server.connection_manager.maybe_connection(BOARD) is not None:
                    self.call("disconnect", {"board_id": BOARD})
            finally:
                self.session_stack.close()
                self.session_stack = None

    def call(self, tool, arguments):
        row = {"run": self.server.server_run.run_id, "tool": tool, "arguments": arguments}
        self.wire.append(row)
        try:
            row["reply"] = public_text(self.server, tool, arguments)
            return row["reply"]
        except Exception as exc:
            row["exception"] = f"{type(exc).__name__}: {exc}"
            raise

    def full_setup(self, *, ownership=True):
        source = (
            Path(boundary.__file__).resolve().parents[1]
            / "fallback-support-evidence/results/hil-artifacts/.firm"
        )
        shutil.copytree(source / "packs", self.root / ".firm/packs")
        datasheet = self.root / "STM32L476RG.pdf"
        shutil.copyfile(next((source / "evidence/datasheets").glob("*.pdf")), datasheet)
        route = self.flow.overview("M15 host")["routes"][0]
        assert route["board_id"] == BOARD, route
        _, accepted = self.flow.accept(route, datasheet, "STM32L476RGT6", tier="setup-full")
        reply = self.flow.start(accepted)
        assert reply["status"] == "setup_completed", reply
        self.validate()
        if ownership:
            artifact = boundary.write_artifact(
                self.root / "owned.elf", ((boundary.BASE, boundary.fixture_runs()[0][1][:512]),)
            )
            self.flow.call("flash_application-plan", NULL_PLAN)
            accepted = self.flow.plan("flash_application", BOARD, {"artifact": str(artifact)})
            assert accepted["status"] == "plan_accepted", accepted
            reply = self.call("flash_application", {"board_id": BOARD, "artifact": str(artifact)})
            assert "512 matching addressed bytes" in reply, reply

    def validate(self):
        self.flow.overview("M15 host")
        loaded = self.flow.json(
            "load_setup_tool", {"board_id": BOARD, "tool_name": "board_validate"}
        )
        reply = self.flow.json(loaded["next_call"]["tool"], loaded["next_call"]["arguments"])
        assert reply["status"] == "validation_passed", reply
        return reply

    def lose_generation(self):
        pointer = self.root / ".firm/capabilities" / BOARD / "current.json"
        raw_pointer = pointer.read_bytes()
        selected = json.loads(raw_pointer)
        generation = pointer.parent / "generations" / (selected["generation_id"] + ".json")
        backup = self.root / "original-backup" / generation.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        generation.replace(backup)
        (backup.parent / "current.json").write_bytes(raw_pointer)
        return pointer, generation, backup

    def read_plan(self):
        self.flow.call("read_memory_address-plan", NULL_PLAN)
        return self.flow.plan(
            "read_memory_address", BOARD, {"address": RAM, "width": 32, "length": None}
        )


@contextmanager
def public_fixture(test):
    with isolated_project() as root, ExitStack() as stack:
        fixture = PublicRecoveryFixture(root)
        stack.enter_context(
            patch.object(device_support, "MANIFEST_PATH", root / ".firm/packs/manifest.yaml")
        )
        stack.enter_context(patch.object(device_support, "PACKS_DIR", root / ".firm/packs/files"))
        stack.enter_context(
            patch.object(boundary.adapter, "FileProgrammer", side_effect=fixture.target.programmer)
        )
        fixture.start()
        try:
            yield fixture
        finally:
            fixture.stop()
            if evidence_root := os.environ.get("BYO_MANAGED_TEST_EVIDENCE"):
                destination = Path(evidence_root) / test.id()
                shutil.copytree(root, destination, dirs_exist_ok=True)
                (destination / "wire.json").write_text(
                    json.dumps(fixture.wire, indent=2), encoding="utf-8"
                )
                (destination / "target.json").write_text(
                    json.dumps(fixture.target.calls, indent=2, default=str), encoding="utf-8"
                )
                (destination / "server-runs.json").write_text(
                    json.dumps(fixture.runs), encoding="utf-8"
                )
