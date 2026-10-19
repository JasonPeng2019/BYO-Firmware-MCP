"""Independent M14-S-01 low-level host boundary; no probe discovery or target init.

The installed target methods and Timeout run unchanged. Only CTRL-AP registers,
flash algorithm I/O and the clock are controlled. No repair-I test is imported.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import MethodType, SimpleNamespace
from unittest.mock import patch

from pyocd.core.exceptions import TransferError
from pyocd.core.soc_target import SoCTarget
from pyocd.target.family import target_nRF52 as nordic
from pyocd.utility import timeout


class ReturnBoundary:
    def __init__(self, outcome, before_primitive=lambda: None):
        self.outcome = outcome
        self.before_primitive = before_primitive
        self.events = []
        self.clock = 100.0
        self.target = SimpleNamespace(ctrl_ap=self)
        self.target.mass_erase = MethodType(nordic.NRF52.mass_erase, self.target)
        if outcome == "void":
            flash = SimpleNamespace(
                is_erase_all_supported=True,
                Operation=SimpleNamespace(ERASE="erase"),
                init=lambda op: self.events.append(["flash-init", op]),
                erase_all=self.chip_erase,
                cleanup=lambda: self.events.append(["flash-cleanup"]),
            )
            self.target.selected_core = SimpleNamespace(node_name="core0")
            self.target.memory_map = SimpleNamespace(
                iter_matching_regions=lambda **kw: [SimpleNamespace(flash=flash)]
            )
            self.target.call_delegate = self.delegate
            self.target.session = SimpleNamespace(target=self.target)
            self.target.mass_erase = MethodType(SoCTarget.mass_erase, self.target)

    def delegate(self, name, **kwargs):
        assert name == "mass_erase" and kwargs == {"target": self.target}
        self.events.append(["delegate", name])
        return False  # Run the installed default CHIP implementation.

    def chip_erase(self):
        self.before_primitive()
        self.events.append(["chip-erase"])

    def write_reg(self, address, value):
        if (address, value) == (nordic.CTRL_AP_ERASEALL, nordic.CTRL_AP_ERASEALL_ERASE):
            self.before_primitive()
        self.events.append(["write", address, value])

    def read_reg(self, address):
        assert address == nordic.CTRL_AP_ERASEALLSTATUS, address
        self.events.append(["poll", address])
        if self.outcome == "exception":
            raise TransferError("independent CTRL-AP transport loss")
        return (
            nordic.CTRL_AP_ERASEALLSTATUS_BUSY
            if self.outcome == "timeout"
            else nordic.CTRL_AP_ERASEALLSTATUS_READY
        )

    def sleep(self, seconds):
        self.events.append(["sleep", seconds])
        self.clock += nordic.MASS_ERASE_TIMEOUT + 1

    @property
    def attempts(self):
        return sum(
            row == ["write", nordic.CTRL_AP_ERASEALL, nordic.CTRL_AP_ERASEALL_ERASE]
            or row == ["chip-erase"]
            for row in self.events
        )

    @contextmanager
    def controlled_clock(self):
        with (
            patch.object(timeout, "time", lambda: self.clock),
            patch.object(nordic, "sleep", self.sleep),
        ):
            yield
