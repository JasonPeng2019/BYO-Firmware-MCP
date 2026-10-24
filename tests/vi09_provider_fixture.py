"""Electrical seams for VI09; pyOCD registration, Session, Board and targets are real."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from pyocd.board.board import Board
from pyocd.core.target import Target
from pyocd.probe.debug_probe import DebugProbe

from pyocd_debug_mcp.adapters import provider_worker_runtime, swd_pyocd
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionMetadata

EVENTS = []


def event(kind, **facts):
    record = {"kind": kind, **facts}
    EVENTS.append(record)
    destination = os.environ.get("VI09_CHILD_EVENTS")
    if destination:
        with Path(destination).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")


class ElectricalBoard(Board):
    def __init__(self, session):
        # Keep actual target/pack construction, including _pack_device provenance.
        super().__init__(session, target=session.options.get("target_override") or "cortex_m")
        self.target.read_memory = self.read_memory
        self.target.get_state = lambda: Target.State.HALTED
        self.target.halt = lambda: event("halt")

    def init(self):
        event("board_init")

    def uninit(self):
        event("board_uninit")

    @staticmethod
    def read_memory(address, transfer_size=32, now=True):
        del transfer_size, now
        event("read_memory", address=address)
        return 0x410FC241 if address == 0xE000ED00 else 0


class ElectricalProbe(DebugProbe):
    devices = []
    provider = "fixture"

    def __init__(self, uid, description=None):
        super().__init__()
        self.uid = uid
        self.label = description or f"{self.provider} simulated endpoint"
        self.opens = 0
        self.closes = 0
        self.resets = []
        self.fail_opens = 0
        self.opened = False

    @classmethod
    def get_probe_with_id(cls, unique_id, is_explicit=False):
        event("fast_lookup", provider=cls.provider, uid=unique_id, explicit=is_explicit)
        return next((p for p in cls.devices if p.uid == unique_id), None)

    @classmethod
    def get_all_connected_probes(cls, unique_id=None, is_explicit=False):
        event("enumerate", provider=cls.provider, uid=unique_id, explicit=is_explicit)
        # The actual DebugProbe contract permits ignoring the optional filter.
        return list(cls.devices)

    @property
    def unique_id(self):
        return self.uid

    @property
    def description(self):
        return self.label

    @property
    def vendor_name(self):
        return "VI09 simulated electrical boundary"

    @property
    def product_name(self):
        return self.label

    @property
    def capabilities(self):
        return set()

    @property
    def supported_wire_protocols(self):
        return (DebugProbe.Protocol.SWD,)

    @property
    def wire_protocol(self):
        return None

    @property
    def is_open(self):
        return self.opened

    def create_associated_board(self):
        return ElectricalBoard(self.session)

    def open(self):
        self.opens += 1
        event(
            "probe_open",
            provider=self.provider,
            uid=self.uid,
            no_config=self.session.options.get("no_config"),
            auto_unlock=self.session.options.get("auto_unlock"),
            user_script=self.session.options.get("user_script"),
            project_dir=self.session.project_dir,
            target=self.session.options.get("target_override"),
            frequency=self.session.options.get("frequency"),
            connect_mode=self.session.options.get("connect_mode"),
        )
        if self.fail_opens:
            self.fail_opens -= 1
            raise RuntimeError("No emulator with serial number")
        self.opened = True

    def close(self):
        self.closes += 1
        self.opened = False
        event("probe_close", provider=self.provider, uid=self.uid)

    def set_clock(self, frequency):
        event("clock", frequency=frequency)

    def assert_reset(self, asserted):
        self.resets.append(asserted)
        event("reset", uid=self.uid, asserted=asserted)


# Siblings are essential: production metadata uses registered-class isinstance.
class STLinkProbe(ElectricalProbe):
    devices = []
    provider = "stlink"


class CMSISDAPProbe(ElectricalProbe):
    devices = []
    provider = "cmsisdap"


class JLinkProbe(ElectricalProbe):
    devices = []
    provider = "jlink"


class MetadataAdapter(swd_pyocd.PyOCDSWDInterface):
    """In-process native opens with the real worker's metadata conversion."""

    def open(self, **kwargs):
        handle = super().open(**kwargs)
        return replace(
            handle, metadata=TargetSessionMetadata(**provider_worker_runtime._metadata(handle))
        )
