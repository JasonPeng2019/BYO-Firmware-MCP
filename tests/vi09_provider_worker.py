"""Production provider protocol entry with test-owned electrical observations."""

from __future__ import annotations

import json
import os

from pyocd.probe.aggregator import PROBE_CLASSES

from tests.vi09_provider_fixture import CMSISDAPProbe, STLinkProbe, event

config = json.loads(os.environ["VI09_CHILD_CONFIG"])
STLinkProbe.devices = [STLinkProbe(uid, "ST-Link simulated endpoint") for uid in config["stlink"]]
CMSISDAPProbe.devices = [
    CMSISDAPProbe(uid, "CMSIS-DAP simulated endpoint") for uid in config.get("cmsisdap", [])
]
PROBE_CLASSES.clear()
PROBE_CLASSES.update(stlink=STLinkProbe, cmsisdap=CMSISDAPProbe)

from pyocd_debug_mcp.adapters.provider_worker import main  # noqa: E402

event("child_entry", file=__file__)
main()
