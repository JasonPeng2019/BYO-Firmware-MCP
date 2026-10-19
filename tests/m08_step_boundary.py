"""Host-only AP/core boundary running installed pyOCD stepping and BP providers."""
from types import SimpleNamespace

from pyocd.core.target import Target
from pyocd.core.exceptions import CoreRegisterAccessError
from pyocd.coresight.cortex_m import CortexM
from pyocd.debug.breakpoints.manager import BreakpointManager
from pyocd.utility.notification import Notifier

from tests.m07_breakpoint_boundary import MemoryTarget


class StepTarget(MemoryTarget):
    """Model instruction effects only at the DHCSR/AP boundary; no probe access."""

    def __init__(self, *, instruction=0xB580, mode="execute", pc=0x08000000):
        self.drop_bp_restore = False
        super().__init__(capacity=3, flash_start=0x08000000)
        notifier = Notifier()
        self.session = SimpleNamespace(
            subscribe=notifier.subscribe, notify=notifier.notify,
            options={"force_sw_bkpt": False, "cpu.step.instruction.timeout": 0.005},
        )
        self.bp_manager = BreakpointManager(self)
        self.bp_manager.add_provider(self.fpb)
        self.bp_manager.add_provider(self.software)
        self.core_number = 0
        self._run_token = 0
        self.mode = mode
        self.registers = {"pc": pc, "sp": 0x20000800, "r7": 7, "lr": 0x08000041}
        self.words[pc] = instruction
        self.dhcsr = CortexM.C_DEBUGEN | CortexM.C_HALT | CortexM.S_HALT
        self.dfsr = CortexM.DFSR_BKPT
        self.executed = 0
        self.events = []

    def read_memory(self, address, transfer_size=32, now=True):
        if address == CortexM.DHCSR:
            self.reads.append((address, transfer_size, now))
            value = self.dhcsr
            self.dhcsr &= ~CortexM.S_RETIRE_ST
            return value
        if address == CortexM.DFSR:
            return self.dfsr
        return super().read_memory(address, transfer_size, now)

    def write_memory(self, address, value, transfer_size=32):
        if address == CortexM.DFSR:
            self.dfsr &= ~value
            return
        if address == CortexM.DHCSR:
            self.events.append(("dhcsr", value))
            was_halted = bool(self.dhcsr & CortexM.S_HALT)
            self.dhcsr = (self.dhcsr & ~0xFFFF) | (value & 0xFFFF)
            if value & CortexM.C_HALT:
                self.dhcsr |= CortexM.S_HALT
                if not was_halted:
                    self.dfsr |= CortexM.DFSR_HALTED
                return
            if value & CortexM.C_STEP:
                if self.mode == "error":
                    raise RuntimeError("injected step transfer failure")
                pc = self.registers["pc"]
                hit = any(bp.enabled and bp.addr == pc and self.words[bp.comp_register_addr]
                          for bp in self.fpb.hw_breakpoints)
                if hit or self.words[pc] == 0xBE00:
                    self.dfsr = CortexM.DFSR_BKPT
                elif self.mode == "running":
                    self.dhcsr &= ~CortexM.S_HALT
                    return
                else:
                    self.executed += 1
                    if self.words[pc] == 0xB580:  # controlled PUSH {r7, lr}
                        self.registers["sp"] -= 8
                    if self.words[pc] != 0xE7FE:  # controlled B .
                        self.registers["pc"] += 2
                    # Deliberately leave S_RETIRE_ST zero: its exact timing is
                    # not a portable per-instruction completion contract.
                    self.dfsr = CortexM.DFSR_HALTED
                self.dhcsr |= CortexM.C_HALT | CortexM.S_HALT
            return
        if self.drop_bp_restore and self.executed and address in self.comparators and value:
            self.writes.append((address, value, transfer_size))
            return
        super().write_memory(address, value, transfer_size)

    clear_debug_cause_bits = CortexM.clear_debug_cause_bits

    def step(self, disable_interrupts=True, start=0, end=0, hook_cb=None):
        self.events.append(("step", disable_interrupts, start, end))
        if self.mode in {"noop", "pc-only"}:
            if self.mode == "pc-only":
                self.registers["pc"] += 2
            return
        CortexM.step(self, disable_interrupts, start, end, hook_cb)

    def get_state(self):
        return Target.State.HALTED if self.dhcsr & CortexM.S_HALT else Target.State.RUNNING

    def read_core_register(self, name):
        if not self.dhcsr & CortexM.S_HALT:
            raise CoreRegisterAccessError("register access requires a halted core")
        return self.registers[name]

    @property
    def core_registers(self):
        return SimpleNamespace(by_name=self.registers)

    def invalidate_instruction_cache(self, address):
        self.events.append(("invalidate", address))

    def halt(self):
        self.write_memory(CortexM.DHCSR, CortexM.DBGKEY | CortexM.C_DEBUGEN | CortexM.C_HALT)


if __name__ == "__main__":
    # Substitute only the target/open boundary in a real process worker. The
    # production bootstrap, RPC loop, adapter step and result validation run.
    import contextlib
    import sys

    with contextlib.redirect_stdout(sys.stderr):
        from pyocd_debug_mcp.adapters import provider_worker, provider_worker_runtime
        from tests.m07_breakpoint_boundary import BoundaryAdapter

        boundary = BoundaryAdapter(StepTarget(mode=sys.argv[1], instruction=int(sys.argv[2], 0)))
        provider_worker_runtime.PyOCDSWDInterface = lambda: boundary
        if len(sys.argv) > 3:
            # Snapshot before the production close path can terminate this child
            # without running atexit. The argv names its exact external
            # destination; observation never changes provider behavior.
            import hashlib
            import json
            import os
            from pathlib import Path

            rows = []
            for name, module in sorted(sys.modules.items()):
                file = getattr(module, "__file__", None)
                if (name == "__main__" or name.startswith(("pyocd_debug_mcp", "pyocd.", "tests."))) and file:
                    path = Path(file).resolve()
                    if path.is_file():
                        data = path.read_bytes()
                        rows.append({"module": name, "path": str(path), "bytes": len(data),
                                     "sha256": hashlib.sha256(data).hexdigest()})
            destination = Path(sys.argv[3])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / f"worker-{os.getpid()}.json").write_text(json.dumps(rows, indent=2) + "\n")
    provider_worker.main()
