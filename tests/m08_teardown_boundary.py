"""C01 teardown fault injection at the MEM-AP, with installed Session.close."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, cast

from pyocd.core.exceptions import TransferError
from pyocd.core.session import Session

from tests.m08_independent_core import (
    FP_COMP0, PROBE_UID, SimulatedCortexM4, SimulatedProbeAdapter, SimulatedSession,
    module_identities,
)

RAM_CODE = 0x20004000


class TeardownFaultModel(SimulatedCortexM4):
    """Lose target access when teardown restores the installed owned BP location.

    The step request, when selected, fails once as in the independent C01 case;
    its temporary BP restoration succeeds. The later teardown transfer fails
    persistently. Nothing above the AP (including production cleanup) is mocked.
    """

    def __init__(self, *, ram: bool, after_step: bool = False) -> None:
        super().__init__()
        self.location = RAM_CODE if ram else FP_COMP0
        self.removed_value = 0xB580 if ram else 0
        self.installed = False
        self.lose_on_remove = True
        self.after_step = after_step
        self.transport_lost = False
        if after_step:
            self.step_mode = "transfer_error"

    def ap_write(self, address: int, value: int, size_bits: int) -> None:
        if (self.lose_on_remove and self.installed and address == self.location
                and value == self.removed_value and (not self.after_step or self.step_requests)):
            self.transport_lost = True
            self.journal.append(("teardown-transfer-error", address, value, size_bits))
            self.write_snapshot(note="persistent target loss during breakpoint teardown")
            raise TransferError("simulated persistent teardown transfer fault")
        super().ap_write(address, value, size_bits)
        if address == self.location and value != self.removed_value:
            self.installed = True

    def ap_read(self, address: int, size_bits: int) -> int:
        if self.transport_lost and address == self.location:
            raise TransferError("simulated teardown state readback unavailable")
        return super().ap_read(address, size_bits)

    def state(self) -> dict[str, Any]:
        state = super().state()
        state["ram_instruction"] = self.halfword(RAM_CODE)
        state["transport_lost"] = self.transport_lost
        return state


class NativeCloseSession(SimulatedSession):
    """Retain the independent target model, using installed pyOCD session teardown.

    The simulated probe has no physical wire protocol. Board.uninit delegates
    to the actual SoCTarget.disconnect, as a real board does. In particular the
    real Session.disconnect suppression of pyOCD errors remains in effect.
    """

    def close(self) -> None:
        if self.closed:
            return
        target = self.target
        assert target is not None
        previous_cwd = Path.cwd()
        native = cast(Any, Session(None, auto_open=False, options={
            "no_config": True, "project_dir": os.environ["BYO_MCP_ARTIFACT_ROOT"],
        }))
        native._probe = self.probe
        setattr(self.probe, "wire_protocol", None)
        native._board = SimpleNamespace(
            target=target,
            uninit=lambda: target.disconnect(self.options.get("resume_on_disconnect")),
        )
        native._closed = False
        native._inited = True
        try:
            native.close()
        finally:
            os.chdir(previous_cwd)
            self.closed = True
            self.model.write_snapshot(note="installed Session.close finished")


class NativeCloseAdapter(SimulatedProbeAdapter):
    def _choose_session(self, *, probe_uid: str | None, options: dict[str, Any] | None) -> Any:
        session = NativeCloseSession(self.model, self.unique_id, options,
                                     writable_flash=self.writable_flash)
        self.sessions.append(session)
        return session


def worker_main(mode: str, observation: Path) -> None:
    with contextlib.redirect_stdout(sys.stderr):
        from pyocd_debug_mcp.adapters import provider_worker, provider_worker_runtime

        observation.mkdir(parents=True, exist_ok=True)
        model = TeardownFaultModel(ram=mode.endswith("ram"), after_step=mode.startswith("step"))
        model.lose_on_remove = not mode.startswith("ordinary")
        model.snapshot_path = observation / f"worker-{os.getpid()}-state.json"
        adapter = NativeCloseAdapter(model, unique_id=PROBE_UID)
        provider_worker_runtime.PyOCDSWDInterface = lambda: adapter
        (observation / f"worker-{os.getpid()}-imports.json").write_text(
            json.dumps(module_identities(), indent=2) + "\n", encoding="utf-8")
        model.write_snapshot(note="teardown worker ready")
    provider_worker.main()


if __name__ == "__main__":
    worker_main(sys.argv[1], Path(sys.argv[2]))
