"""M16-T public clients and synthetic I/O, independent of M16-I fixtures.

The retained publisher pack is real support data. The PDF research, inventory,
register observations and programming boundary are synthetic host inputs, never
vendor identity evidence or proof of a physical target. Policy is always produced
by registered public setup/allocation/admission calls.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import patch

from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
from pyocd_debug_mcp.setup_flow import device_support
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from tests.b02_public_support import elf_fixture
from tests.m05_public_support import NULL_PLAN, PublicSetup
from tests.m14_independent_support import EraseProbe
from tests.m15_independent_evidence import capture_imports
from tests.m15_independent_support import selected
from tests.test_m05_lite_validation import advertised_validation
from tests.tiered_acceptance_support import isolated_project, public_text, tiered_test_server


PACK_SHA = "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"
NAME = "M16 independent partition"
RAM = 0x20000000
RAM_VALUE = 0x6D143A72


def research(root, family):
    """Different widths/masks/UID layouts exercise general structured research."""
    stm = family == "pack"
    layout = (
        [
            (0xE0042000, 32, "device", 0x415, 0xFFF, None),
            (0x1FFF75E0, 16, "flash_size", None, 0xFFFF, 1024),
            (0x1FFF7590, 32, "unique", None, 0xFFFFFFFF, None),
            (0x1FFF7594, 32, "unique", None, 0xFFFFFFFF, None),
            (0x1FFF7598, 32, "unique", None, 0xFFFFFFFF, None),
        ]
        if stm
        else [
            (0x10000100, 32, "device", 0x52840, 0xFFFFFFFF, None),
            (0x10000110, 32, "flash_size", None, 0xFFFFFFFF, 1024),
            (0x10000060, 32, "unique", None, 0xFFFFFFFF, None),
            (0x10000064, 32, "unique", None, 0xFFFFFFFF, None),
        ]
    )
    observations = (
        [0x10000415, 1024, 0x137C9A21, 0x8412936B, 0x6A452780]
        if stm
        else [0x52840, 1024, 0x137C9A21, 0x8412936B]
    )
    source = root / (family + "-synthetic-research.pdf")
    source.write_bytes(
        b"%PDF-1.4\n% M16-T SYNTHETIC host research, NOT an official vendor document.\n"
        + json.dumps(dict(layout=layout, observations=observations)).encode()
    )
    base, sector = (0x08000000, 0x800) if stm else (0, 0x1000)
    request = dict(
        start=base + 0x80000,
        end=base + 0x80000 + 2 * sector,
        source_path=str(source),
        source_url="https://example.invalid/m16-t/synthetic-reference.pdf",
        source_pages=[1],
        source_note="Synthetic factory table and bounded user layout, section 1.",
        identity_coverage=(
            "Synthetic STM32L476 group/size/UID/backend coverage; package suffix unproven."
            if stm
            else "Synthetic nRF52840 model/size/UID/backend coverage; package suffix unproven."
        ),
        registers=[
            dict(
                address=a,
                width_bits=w,
                role=r,
                expected=e,
                mask=m,
                unit_bytes=u,
                read_only=True,
                factory=True,
                source_pages=[1],
                source_note=f"Synthetic factory table section 1: {r} {a:#x} ({w} bits).",
            )
            for a, w, r, e, m, u in layout
        ],
    )
    return SimpleNamespace(
        request=request,
        source=source,
        base=base,
        sector=sector,
        values={row[0]: value for row, value in zip(layout, observations)},
        widths={row[0]: row[1] for row in layout},
    )


class PartitionProbe(EraseProbe):
    def __init__(self, root, family, factory):
        super().__init__(root, "stm32l476rgtx" if family == "pack" else "nrf52840")
        self.factory = factory
        self.provider = "stlink" if family == "pack" else "jlink"
        self.on_factory_read = None

    def open(self, **kwargs):
        handle = super().open(**kwargs)
        # EraseProbe's original Nordic fixture reports jlink; this synthetic
        # inventory also exercises a real ST-Link assignment/provider binding.
        return replace(
            handle,
            metadata=replace(handle.metadata, probe_family=self.provider),
        )

    def read_memory(self, handle, address, width_bits, **kwargs):
        if address in self.factory.values:
            self.calls.append(("factory-read", handle.probe_uid, address, width_bits))
            assert width_bits == self.factory.widths[address]
            value = self.factory.values[address]
            if self.on_factory_read:
                self.on_factory_read(address)
            if isinstance(value, Exception):
                raise value
            return value
        if address == self.factory.base and address not in self.programmed:
            self.calls.append(("read", handle.probe_uid, address, width_bits))
            return RAM + 0x1000
        return super().read_memory(handle, address, width_bits, **kwargs)


class Fixture:
    def __init__(self, test, root, family):
        self.test, self.root, self.family = test, root, family
        self.factory = research(root, family)
        self.backend = PartitionProbe(root, family, self.factory)
        self.wire, self.events = [], []
        self.stack = None
        self.board = None
        uid = "m16-t-synthetic-" + family
        inventory = ValidationInventory(
            probes=(ValidationProbe(uid, uid, "stlink" if family == "pack" else "jlink", uid),)
        )
        self.inventory = SimpleNamespace(
            snapshot=lambda: snapshot_from_validation_inventory(inventory),
            validation_inventory=lambda: inventory,
        )

    def start(self):
        self.stop()
        self.stack = ExitStack()
        # These storage constants predate per-test server imports. Relocate
        # them exactly as M15 does; the public flow still creates every byte
        # of the manifest, binding and authoritative generation.
        self.stack.enter_context(
            patch.object(device_support, "MANIFEST_PATH", self.root / ".firm/packs/manifest.yaml")
        )
        self.stack.enter_context(
            patch.object(device_support, "PACKS_DIR", self.root / ".firm/packs/files")
        )
        self.server = self.stack.enter_context(tiered_test_server(self.root))
        self.server.configure_tiered_test_seams(backend=self.backend)
        self.stack.enter_context(patch.object(self.server, "_hardware_inventory", self.inventory))
        self.flow = PublicSetup(self.server, call=self.call)
        self.events.append(dict(start_run=self.server.server_run.run_id))

    def stop(self):
        if self.stack:
            try:
                for board in self.server.connection_manager.assigned_board_ids():
                    self.call("disconnect", dict(board_id=board))
                capture_imports()
            finally:
                self.stack.close()
                self.stack = None

    def call(self, name, arguments):
        row = dict(tool=name, arguments=copy.deepcopy(arguments), run=self.server.server_run.run_id)
        self.wire.append(row)
        try:
            row["reply"] = public_text(self.server, name, arguments)
            return row["reply"]
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            raise

    def setup(self):
        route = self.flow.overview(NAME)["routes"][0]
        self.board = route["board_id"]
        part = "STM32L476RGT6" if self.family == "pack" else "nRF52840-QIAA"
        _, plan = self.flow.accept(route, self.factory.source, part, tier="setup-full")
        pending = self.flow.start(plan)
        self.test.assertEqual(pending["status"], "setup_research_required", pending)
        if self.family == "pack":
            pack = (
                Path(__file__).resolve().parents[1]
                / "fallback-support-evidence/results/hil-artifacts/.firm/packs/files/Keil.STM32L4xx_DFP.3.1.0.pack"
            )
            self.test.assertEqual(hashlib.sha256(pack.read_bytes()).hexdigest(), PACK_SHA)
            # Copy support bytes only; public research creates the registry and binding.
            local = self.root / pack.name
            shutil.copyfile(pack, local)
            response = copy.deepcopy(pending["accepted_response"]["response"])
            response.update(
                pack_id="Keil.STM32L4xx_DFP",
                version="3.1.0",
                filename=local.name,
                url="https://www.keil.com/pack/Keil.STM32L4xx_DFP.3.1.0.pack",
                source_path=str(local),
                official_sha256=PACK_SHA,
                evidence=[
                    dict(
                        source="Retained official Keil 3.1.0 pack",
                        claim="STM32L476RGTx supports selected order code; no live package claim.",
                    )
                ],
                reasoning_summary="Replay retained publisher PDSC/FLM bytes through ordinary pack research.",
            )
        else:
            response = self.flow.target_answer(pending, "nrf52840")
        answered = self.flow.answer(self.board, pending, response)
        self.test.assertEqual(answered["status"], "setup_continuation_accepted", answered)
        result = self.flow.fix(plan)
        self.test.assertEqual(result["status"], "setup_completed", result)
        self.test.assertEqual(self.validate()["status"], "validation_passed")

    def validate(self):
        return advertised_validation(self.flow, self.board)

    def admit(self, request=None):
        self.flow.json(
            "load_setup_tool", dict(board_id=self.board, tool_name="board_safety_refresh")
        )
        return self.flow.json(
            "board_safety_refresh",
            dict(
                board_id=self.board,
                user_bootloader=copy.deepcopy(
                    request if request is not None else self.factory.request
                ),
            ),
        )

    def caps(self):
        return self.flow.json("get_capabilities", dict(board_id=self.board))

    def allocate(self, offset=0):
        artifact = elf_fixture(self.root / f"app-{offset:x}.elf", base=self.factory.base + offset)
        self.flow.call("flash_application-plan", NULL_PLAN)
        plan = self.flow.plan("flash_application", self.board, dict(artifact=str(artifact)))
        self.test.assertEqual(plan["status"], "plan_accepted", plan)
        result = self.flow.call(
            "flash_application", dict(board_id=self.board, artifact=str(artifact))
        )
        self.test.assertIn("256 matching addressed bytes", result)
        self.test.assertEqual(self.validate()["status"], "validation_passed")

    def record(self):
        return selected(self.root, self.board)

    def reconnect(self):
        overview = self.flow.overview(NAME)
        self.test.assertEqual(overview["status"], "setup_routes_ready", overview)
        self.test.assertEqual(overview["routes"][0]["board_id"], self.board, overview)
        return self.flow.call("connect", dict(board_id=self.board))

    def refresh(self):
        self.flow.json(
            "load_setup_tool", dict(board_id=self.board, tool_name="board_safety_refresh")
        )
        return self.flow.json("board_safety_refresh", dict(board_id=self.board))


@contextmanager
def full_fixture(test, family="builtin"):
    with isolated_project() as root:
        f = Fixture(test, root, family)
        destination = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / test.id() / family
        destination.mkdir(parents=True, exist_ok=True)
        try:
            f.start()
            f.setup()
            yield f
        finally:
            # Preserve failures before cleanup, including damaged captured sources.
            if (root / ".firm").exists():
                shutil.copytree(root / ".firm", destination / ".firm", dirs_exist_ok=True)
            for path in root.glob("*"):
                if path.is_file():
                    shutil.copyfile(path, destination / path.name)
            try:
                f.stop()
            finally:
                (destination / "wire.json").write_text(
                    json.dumps(f.wire, indent=2), encoding="utf-8"
                )
                (destination / "probe.json").write_text(
                    json.dumps(f.backend.calls, indent=2, default=str), encoding="utf-8"
                )
                (destination / "events.json").write_text(
                    json.dumps(f.events, indent=2), encoding="utf-8"
                )


@contextmanager
def partitioned_recovery_corpus(test):
    """Run existing independent M15 oracles on genuinely admitted partitions."""
    from tests.m15_independent_support import corpus

    with corpus(test) as f:
        factory = research(f.root, "builtin")
        original = f.backend.read_memory

        def read(handle, address, width_bits, **kwargs):
            if address in factory.values:
                f.backend.calls.append(("factory-read", handle.probe_uid, address, width_bits))
                assert width_bits == factory.widths[address]
                return factory.values[address]
            return original(handle, address, width_bits, **kwargs)

        with patch.object(f.backend, "read_memory", read):
            f.flow.json("load_setup_tool", dict(board_id=f.board, tool_name="board_safety_refresh"))
            result = f.flow.json(
                "board_safety_refresh", dict(board_id=f.board, user_bootloader=factory.request)
            )
            test.assertEqual(result["status"], "safety_refresh_completed", result)
            test.assertEqual(advertised_validation(f.flow, f.board)["status"], "validation_passed")
            f.saved = selected(f.root, f.board)
            f.backup.write_bytes(f.saved.data)
            f.factory = factory
            test.assertIsNotNone(f.saved.document["map_snapshot"]["bootloader_policy"])
            yield f
