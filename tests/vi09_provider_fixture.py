"""Electrical seams for VI09; pyOCD registration, Session, Board and targets are real."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

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


    @property
    def unique_id(self):
        return self.uid

    @property
    def description(self):
        return self.label


    @property
    def capabilities(self):
        return set()


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


    def assert_reset(self, asserted):
        self.resets.append(asserted)
        event("reset", uid=self.uid, asserted=asserted)


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
