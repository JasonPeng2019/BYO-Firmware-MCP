from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from firmware_mcp.adapters.debug_interface import TargetSessionHandle
from firmware_mcp.services import target_control
from firmware_mcp.target_errors import TargetConnectionError, TargetStateError


class _Backend:
    def __init__(self, states: list[str], *, error: Exception | None = None) -> None:
        self.states = states
        self.error = error

    def get_state(self, _handle: TargetSessionHandle) -> str:
        if self.error is not None:
            raise self.error
        return self.states.pop(0)

    def halt(self, _handle: TargetSessionHandle) -> None:
        return None

    def resume(self, _handle: TargetSessionHandle) -> None:
        return None

    def step(self, _handle: TargetSessionHandle) -> None:
        return None

    def reset(self, _handle: TargetSessionHandle) -> None:
        return None

    def reset_and_halt(self, _handle: TargetSessionHandle) -> None:
        return None

    def read_core_register(self, _handle: TargetSessionHandle, name: str) -> int:
        self.last_register = name
        return 0x20000010


class TargetControlPostconditionTests(unittest.TestCase):
    handle = TargetSessionHandle(SimpleNamespace(), None, None, "test", None)

    def test_resume_reports_a_legitimate_immediate_halt_as_evidence(self) -> None:
        backend = _Backend(["HALTED"])
        with patch.object(target_control, "_BACKEND", backend):
            self.assertEqual(target_control.resume(self.handle), "HALTED")

    def test_halt_and_step_require_observed_halted_state(self) -> None:
        with patch.object(target_control, "_BACKEND", _Backend(["RUNNING"])):
            with self.assertRaisesRegex(TargetStateError, "not HALTED"):
                target_control.halt(self.handle)
        with patch.object(target_control, "_BACKEND", _Backend(["RUNNING"])):
            with self.assertRaisesRegex(TargetStateError, "not HALTED"):
                target_control.step(self.handle)

    def test_step_returns_real_pc_only_after_halt_postcondition(self) -> None:
        backend = _Backend(["HALTED"])
        with patch.object(target_control, "_BACKEND", backend):
            self.assertEqual(target_control.step(self.handle), ("HALTED", 0x20000010))
        self.assertEqual(backend.last_register, "pc")

    def test_transport_loss_while_observing_postcondition_is_not_success(self) -> None:
        with patch.object(
            target_control, "_BACKEND", _Backend([], error=TargetConnectionError("lost"))
        ):
            with self.assertRaisesRegex(TargetConnectionError, "lost"):
                target_control.reset(self.handle, halt_after=True)


if __name__ == "__main__":
    unittest.main()
