"""M08-T independent boundary: a simulated Cortex-M4 behind a simulated MEM-AP/probe.

Only the probe transport, the MEM-AP view of memory/SCS/FPB registers and a
deterministic Thumb executor are simulated. Installed pyOCD SoCTarget, CortexM
(halt/step/resume/register transfer), FPB, software breakpoint provider and
BreakpointManager run on top of it, and the production PyOCDSWDInterface
open/step/breakpoint/close code runs unchanged: the adapter below replaces only
pyOCD probe enumeration (``_choose_session``).

Instruction effects come from decoding the measured encoding at PC; there is no
PC+2 rule. A single step that meets an FPB match or BKPT reports DFSR.BKPT and
executes nothing, as on Armv7-M (DDI0403E C1.6). Fault knobs model only the
transport/core boundary: a no-op step request, a PC change without a step, a
non-clearable debug cause, a core that never re-halts, transfer faults, dropped
restoration writes and a hung transport.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pyocd.core.exceptions import TransferError, TransferFaultError
from pyocd.core.memory_map import FlashRegion, MemoryMap, RamRegion
from pyocd.core.options_manager import OptionsManager
from pyocd.core.soc_target import SoCTarget
from pyocd.coresight.cortex_m import CortexM
from pyocd.coresight.fpb import FPB
from pyocd.utility.notification import Notifier

from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface

FLASH_BASE = 0x08000000
FLASH_SIZE = 0x100000
RAM_BASE = 0x20000000
RAM_SIZE = 0x18000
SCS_BASE = 0xE000E000
FPB_BASE = 0xE0002000
FP_COMP0 = FPB_BASE + 8
CODE_COMPARATORS = 6
LITERAL_COMPARATORS = 2
PROBE_UID = "M08T-SIM-0001"

# First 256 bytes of the current firmware-v2 STM32L476 fixture.bin (5692 bytes,
# sha256 below): vectors, Default_Handler, repair_step_probe and Reset_Handler's
# loop. The evidence runner re-reads the ROOT file and checks this prefix.
FIRMWARE_V2_STM32_SHA256 = "2cf2c9005fbe869dd9cefdfa1b9f3ed10412100e63b0f4f6f2054a04a59dd9c5"
FIRMWARE_V2_STM32_PREFIX = bytes.fromhex(
    "00000120650000084d0000084d0000084d0000084d0000084d00000800000000"
    "0000000000000000000000004d0000084d000008000000004d0000084d000008"
    "651400080c0000003000000080b56f46ffe700bffde7d4d480b56f4600bf00bf"
    "80bdd4d480b56f4684b040f20000c2f200000390ffe7039840f20401c2f20001"
    "884228d2ffe7039900200860ffe703980090011d0a4601920a460292002818bf"
    "0120002918bf0121884208d1ffe702980099884203d3ffe7012048b9ffe7029a"
    "009940f24000c0f6000000f017f8ffe701980390cfe740f20001c2f200010020"
    "c0f201300860ffe7fff7b6ff40f20001c2f20001086801300860f5e780b56f46"
)
REPAIR_STEP_PROBE = 0x08000058  # B580 push {r7, lr}
LOOP_CALL = 0x080000E8  # F7FF FFB6 bl repair_step_probe
COUNTER = 0x20000000  # repair_counter

DHCSR, DFSR, DCRSR, DCRDR, DEMCR = (
    CortexM.DHCSR, CortexM.DFSR, CortexM.DCRSR, CortexM.DCRDR, CortexM.DEMCR)
C_DEBUGEN, C_HALT, C_STEP, C_MASKINTS, C_PMOV = (
    CortexM.C_DEBUGEN, CortexM.C_HALT, CortexM.C_STEP, CortexM.C_MASKINTS, CortexM.C_PMOV)
CONTROL_MASK = 0x6F  # C_DEBUGEN, C_HALT, C_STEP, C_MASKINTS, C_SNAPSTALL, C_PMOV
PPB_IDENTITY = {
    CortexM.CPUID: 0x410FC241,  # Cortex-M4 r0p1, Armv7-M
    CortexM.CLIDR: 0,
    CortexM.ISAR3: 0x01111131,
    CortexM.MPU_TYPE: 0x00000800,
    CortexM.MVFR0: 0x10110021,  # FPv4 single precision
    CortexM.MVFR2: 0,
}
T_BIT = 1 << 24
FAULT_KNOBS = frozenset(
    {"step_mode", "drop_after_execute", "fail_flush_after_execute", "sticky_dfsr"})


class SimulationLimit(AssertionError):
    """The executor met an encoding outside this fixture; fail loudly, never guess."""


class SimulatedCortexM4:
    """Silicon model: memory banks, SCS debug registers, FPBv1 and a Thumb core."""

    def __init__(self, *, staged: dict[int, int] | None = None, run_limit: int = 4096) -> None:
        self.flash = bytearray(b"\xff" * FLASH_SIZE)
        self.flash[: len(FIRMWARE_V2_STM32_PREFIX)] = FIRMWARE_V2_STM32_PREFIX
        self.ram = bytearray(RAM_SIZE)
        for address, halfword in (staged or {}).items():
            self._store(address, 2, halfword, core=True)
        self.run_limit = run_limit
        self.regs: dict[int, int] = {index: 0 for index in range(17)}
        self.regs.update({18: 0, 20: 0, 33: 0})
        self.halted = False
        self.control = 0
        self.dfsr = 0
        self.sticky_dfsr = 0
        self.demcr = 0
        self.dcrdr = 0
        self.retired = False
        self.fp_enable = 0
        self.comparators = [0] * (CODE_COMPARATORS + LITERAL_COMPARATORS)
        self.ppb: dict[int, int] = {}
        self.journal: list[tuple[Any, ...]] = []
        self.executed: list[dict[str, Any]] = []
        self.step_requests = 0
        self.flushes = 0
        # Fault knobs (all single-purpose; armed by a test immediately before use).
        self.step_mode = "execute"  # execute|noop|pc_only|stall|transfer_error|hang
        self.drop_after_execute: set[int] = set()
        self.fail_flush_after_execute = False
        self._pending_flush_error: Exception | None = None
        self.snapshot_path: Path | None = None
        self.enter_main_loop()

    # ----- deterministic starting state ------------------------------------------------
    def enter_main_loop(self, counter: int = 0x03010000) -> None:
        """Running in Reset_Handler's loop, as after push/mov r7/sub sp in its prologue."""
        self.regs.update({0: counter, 1: COUNTER, 7: 0x2000FFF8, 13: 0x2000FFE8,
                          14: 0xFFFFFFFF, 15: LOOP_CALL, 16: T_BIT})
        self._store(COUNTER, 4, counter, core=True)
        self.halted = False

    # ----- memory -------------------------------------------------------------------------
    @staticmethod
    def bank_of(address: int) -> str | None:
        if FLASH_BASE <= address < FLASH_BASE + FLASH_SIZE:
            return "flash"
        if RAM_BASE <= address < RAM_BASE + RAM_SIZE:
            return "ram"
        return None

    def _locate(self, address: int, size: int) -> tuple[bytearray, int]:
        bank = self.bank_of(address)
        if bank is None or self.bank_of(address + size - 1) != bank:
            raise TransferFaultError("simulated AHB fault", fault_address=address, length=size)
        memory, base = (self.flash, FLASH_BASE) if bank == "flash" else (self.ram, RAM_BASE)
        return memory, address - base

    def _load(self, address: int, size: int) -> int:
        memory, offset = self._locate(address, size)
        return int.from_bytes(memory[offset: offset + size], "little")

    def _store(self, address: int, size: int, value: int, *, core: bool) -> None:
        memory, offset = self._locate(address, size)
        if not core and self.bank_of(address) == "flash":
            # The AP cannot program flash; record the attempted code write anyway.
            self.journal.append(("flash-write-ignored", address, value, size))
            return
        memory[offset: offset + size] = (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little")

    def code_writes(self) -> list[tuple[Any, ...]]:
        """Every AP write aimed at a code bank (flash or RAM), dropped or not."""
        return [row for row in self.journal
                if row[0] in {"w", "w-dropped", "flash-write-ignored"}
                and self.bank_of(row[1]) is not None]

    def flash_writes(self) -> list[tuple[Any, ...]]:
        return [row for row in self.code_writes() if self.bank_of(row[1]) == "flash"]

    # ----- AP transfers -------------------------------------------------------------------
    def ap_read(self, address: int, size_bits: int) -> int:
        size = size_bits // 8
        if address >= 0xE0000000:
            value = self._read_ppb(address)
        else:
            value = self._load(address, size)
        self.journal.append(("r", address, value, size_bits))
        return value

    def ap_write(self, address: int, value: int, size_bits: int) -> None:
        size = size_bits // 8
        if self.executed_since_arm and address in self.drop_after_execute:
            self.journal.append(("w-dropped", address, value, size_bits))
            return
        self.journal.append(("w", address, value, size_bits))
        if address >= 0xE0000000:
            self._write_ppb(address, value)
        else:
            self._store(address, size, value, core=False)
        if address == DHCSR or FPB_BASE <= address < FP_COMP0 + 4 * len(self.comparators):
            self.write_snapshot()

    def flush(self) -> None:
        self.flushes += 1
        self.journal.append(("flush", self.flushes))
        if self._pending_flush_error is not None:
            error, self._pending_flush_error = self._pending_flush_error, None
            raise error

    @property
    def executed_since_arm(self) -> bool:
        return self._armed and any(row["armed"] for row in self.executed)

    def arm(self, **knobs: Any) -> None:
        """Arm faults for the next public operation; later executions are marked armed."""
        for name, value in knobs.items():
            if name not in FAULT_KNOBS:
                raise AttributeError(name)
            setattr(self, name, value)
        for row in self.executed:
            row["armed"] = False
        self._armed = True

    def disarm(self) -> None:
        """Return to an ordinary core so cleanup is observed without injected faults."""
        self.step_mode = "execute"
        self.drop_after_execute = set()
        self.fail_flush_after_execute = False
        self.sticky_dfsr = 0
        self._armed = False

    _armed = False

    # ----- SCS / FPB ----------------------------------------------------------------------
    def dhcsr(self) -> int:
        value = self.control
        if self.halted:
            value |= C_HALT | CortexM.S_HALT | CortexM.S_REGRDY
        if self.retired:
            value |= CortexM.S_RETIRE_ST
        return value

    def _read_ppb(self, address: int) -> int:
        if address == DHCSR:
            value = self.dhcsr()
            self.retired = False  # S_RETIRE_ST clears on read
            return value
        if address == DFSR:
            return self.dfsr | self.sticky_dfsr
        if address == DCRDR:
            return self.dcrdr
        if address == DEMCR:
            return self.demcr
        if address in PPB_IDENTITY:
            return PPB_IDENTITY[address]
        if address == FPB_BASE:
            return self.fp_enable | (CODE_COMPARATORS << 4) | (LITERAL_COMPARATORS << 8)
        if FP_COMP0 <= address < FP_COMP0 + 4 * len(self.comparators):
            return self.comparators[(address - FP_COMP0) // 4]
        return self.ppb.get(address, 0)

    def _write_ppb(self, address: int, value: int) -> None:
        if address == DHCSR:
            self._write_dhcsr(value)
        elif address == DFSR:
            self.dfsr &= ~value  # W1C; sticky bits model a cause that cannot be cleared
        elif address == DCRSR:
            self._register_transfer(value)
        elif address == DCRDR:
            self.dcrdr = value
        elif address == DEMCR:
            self.demcr = value
        elif address == FPB_BASE:
            if value & 2:  # FP_CTRL.KEY must be written as 1
                self.fp_enable = value & 1
        elif FP_COMP0 <= address < FP_COMP0 + 4 * len(self.comparators):
            self.comparators[(address - FP_COMP0) // 4] = value
        else:
            self.ppb[address] = value

    def _register_transfer(self, value: int) -> None:
        selector = value & 0x7F
        if not self.halted:
            return  # S_REGRDY stays clear while running
        if selector == 17:
            selector = 13  # MSP is the active SP in Thread mode here
        if value & (1 << 16):
            data = self.dcrdr & ~3 if selector == 13 else self.dcrdr
            self.regs[selector] = data
        else:
            self.dcrdr = self.regs.get(selector, 0)

    def _write_dhcsr(self, value: int) -> None:
        if value >> 16 != 0xA05F:
            return  # writes without DBGKEY are ignored
        control = value & CONTROL_MASK
        if control & C_DEBUGEN and not control & C_HALT and control & C_STEP and self.halted:
            self.step_requests += 1
            if self.step_mode == "transfer_error":
                self.step_mode = "execute"
                raise TransferError("simulated SWD transfer fault on the DHCSR step request")
            if self.step_mode == "hang":
                self.write_snapshot(note="transport hung in DHCSR step request")
                while True:  # a probe that never returns; the parent must kill us
                    time.sleep(3600)
        was_halted = self.halted
        if not was_halted:
            control = (control & ~C_MASKINTS) | (self.control & C_MASKINTS)
        self.control = control
        if not control & C_DEBUGEN:
            self.control = 0
            self.halted = False
            return
        if control & C_HALT:
            if not was_halted:
                self.halted = True
                self.dfsr |= CortexM.DFSR_HALTED
            return
        if not was_halted:
            return
        if control & C_STEP:
            self._single_step()
        else:
            self._run()

    def _single_step(self) -> None:
        mode = self.step_mode
        if mode == "noop":  # re-enters debug halt with no instruction and no cause
            self.control |= C_HALT
            return
        if mode == "pc_only":  # PC changes without a step completing
            self.regs[15] = (self.regs[15] + 2) & 0xFFFFFFFF
            self.control |= C_HALT
            return
        if mode == "stall":  # core leaves debug state and never re-halts
            self.halted = False
            return
        if self._debug_event_at(self.regs[15]):
            self.control |= C_HALT
            return
        self._execute_one(via="step")
        self.dfsr |= CortexM.DFSR_HALTED
        self.control |= C_HALT

    def _run(self) -> None:
        self.halted = False
        for _ in range(self.run_limit):
            if self._debug_event_at(self.regs[15]):
                self.halted = True
                self.control |= C_HALT
                return
            self._execute_one(via="run")

    def _debug_event_at(self, pc: int) -> bool:
        """FPBv1 match or BKPT at PC: halt with DFSR.BKPT before execution."""
        if self.fp_enable and pc < 0x20000000:
            for comp in self.comparators[:CODE_COMPARATORS]:
                replace = comp >> 30
                if comp & 1 and (comp & 0x1FFFFFFC) == (pc & 0x1FFFFFFC) and (
                    replace == 3 or (replace == 1 and not pc & 2) or (replace == 2 and pc & 2)
                ):
                    self.dfsr |= CortexM.DFSR_BKPT
                    return True
        if self.bank_of(pc) is not None and self._load(pc, 2) >> 8 == 0xBE:
            self.dfsr |= CortexM.DFSR_BKPT
            return True
        return False

    # ----- Thumb executor -----------------------------------------------------------------
    def _reg(self, index: int, pc: int) -> int:
        return (pc + 4) if index == 15 else self.regs[index]

    def _set_flags_nz(self, result: int, carry: int | None = None,
                      overflow: int | None = None) -> None:
        psr = self.regs[16] & 0x0FFFFFFF
        psr |= (result >> 31 & 1) << 31 | (1 if result == 0 else 0) << 30
        old = self.regs[16]
        psr |= (old >> 29 & 1 if carry is None else carry) << 29
        psr |= (old >> 28 & 1 if overflow is None else overflow) << 28
        self.regs[16] = psr

    def _execute_one(self, *, via: str) -> None:
        pc = self.regs[15]
        hw1 = self._load(pc, 2)
        hw2 = self._load(pc + 2, 2) if hw1 >> 11 in (0b11101, 0b11110, 0b11111) else None
        next_pc = pc + (4 if hw2 is not None else 2)
        regs = self.regs
        if hw2 is None and hw1 & 0xFE00 == 0xB400:
            listed = [r for r in range(8) if hw1 >> r & 1] + ([14] if hw1 & 0x100 else [])
            sp = regs[13] - 4 * len(listed)
            for offset, register in enumerate(listed):
                self._store(sp + 4 * offset, 4, regs[register], core=True)
            regs[13] = sp
            mnemonic = "push"
        elif hw2 is None and hw1 & 0xFE00 == 0xBC00:
            listed = [r for r in range(8) if hw1 >> r & 1] + ([15] if hw1 & 0x100 else [])
            sp = regs[13]
            values = [self._load(sp + 4 * offset, 4) for offset in range(len(listed))]
            regs[13] = sp + 4 * len(listed)
            for register, value in zip(listed, values):
                if register == 15:
                    if not value & 1:
                        raise SimulationLimit("POP to PC without Thumb bit (INVSTATE)")
                    next_pc = value & ~1
                else:
                    regs[register] = value
            mnemonic = "pop"
        elif hw2 is None and hw1 & 0xFF00 == 0x4600:
            destination = (hw1 >> 4 & 8) | (hw1 & 7)
            value = self._reg(hw1 >> 3 & 0xF, pc)
            if destination == 15:
                next_pc = value & ~1
            else:
                regs[destination] = value & ~3 if destination == 13 else value
            mnemonic = "mov"
        elif hw2 is None and hw1 == 0xBF00:
            mnemonic = "nop"
        elif hw2 is None and hw1 & 0xF800 == 0xE000:
            offset = (hw1 & 0x7FF) << 1
            offset -= 0x1000 if offset & 0x800 else 0
            next_pc = (pc + 4 + offset) & 0xFFFFFFFF
            mnemonic = "b"
        elif hw2 is None and hw1 & 0xF800 == 0x2000:
            regs[hw1 >> 8 & 7] = hw1 & 0xFF
            self._set_flags_nz(hw1 & 0xFF)
            mnemonic = "movs"
        elif hw2 is None and hw1 & 0xF800 == 0x3000:
            register = hw1 >> 8 & 7
            before, immediate = regs[register], hw1 & 0xFF
            total = before + immediate
            result = total & 0xFFFFFFFF
            overflow = int(not (before ^ immediate) & 0x80000000
                           and bool((before ^ result) & 0x80000000))
            regs[register] = result
            self._set_flags_nz(result, carry=int(total > 0xFFFFFFFF), overflow=overflow)
            mnemonic = "adds"
        elif hw2 is None and hw1 & 0xF000 == 0x6000:
            immediate, base, register = (hw1 >> 6 & 0x1F) * 4, hw1 >> 3 & 7, hw1 & 7
            address = regs[base] + immediate
            if hw1 & 0x0800:
                regs[register] = self._load(address, 4)
                mnemonic = "ldr"
            else:
                self._store(address, 4, regs[register], core=True)
                mnemonic = "str"
        elif hw2 is not None and hw1 & 0xF800 == 0xF000 and hw2 & 0xD000 == 0xD000:
            sign = hw1 >> 10 & 1
            i1 = 1 - ((hw2 >> 13 & 1) ^ sign)
            i2 = 1 - ((hw2 >> 11 & 1) ^ sign)
            offset = sign << 24 | i1 << 23 | i2 << 22 | (hw1 & 0x3FF) << 12 | (hw2 & 0x7FF) << 1
            offset -= 1 << 25 if sign else 0
            regs[14] = (pc + 4) | 1
            next_pc = (pc + 4 + offset) & 0xFFFFFFFF
            mnemonic = "bl"
        elif hw2 is not None and hw1 & 0xFB70 == 0xF240 and not hw2 & 0x8000:
            immediate = ((hw1 & 0xF) << 12 | (hw1 >> 10 & 1) << 11
                         | (hw2 >> 12 & 7) << 8 | (hw2 & 0xFF))
            register = hw2 >> 8 & 0xF
            if hw1 & 0x0080:
                regs[register] = (regs[register] & 0xFFFF) | immediate << 16
                mnemonic = "movt"
            else:
                regs[register] = immediate
                mnemonic = "movw"
        else:
            encoding = f"{hw1:04X}" + (f" {hw2:04X}" if hw2 is not None else "")
            raise SimulationLimit(f"unsupported Thumb encoding {encoding} at 0x{pc:08X}")
        regs[15] = next_pc
        self.retired = True
        self.executed.append({
            "pc": pc, "encoding": f"{hw1:04X}" + (f" {hw2:04X}" if hw2 is not None else ""),
            "mnemonic": mnemonic, "next_pc": next_pc, "via": via, "armed": self._armed,
            "journal_index": len(self.journal),
        })
        if self.fail_flush_after_execute and self._armed:
            self.fail_flush_after_execute = False
            self._pending_flush_error = TransferError(
                "simulated deferred SWD transfer fault reported after the step executed")

    # ----- observation ---------------------------------------------------------------------
    def word(self, address: int) -> int:
        return self._load(address, 4)

    def halfword(self, address: int) -> int:
        return self._load(address, 2)

    def state(self) -> dict[str, Any]:
        return {
            "halted": self.halted,
            "dhcsr": self.dhcsr(),
            "dfsr": self.dfsr | self.sticky_dfsr,
            "demcr": self.demcr,
            "registers": {str(key): value for key, value in sorted(self.regs.items())},
            "fp_ctrl_enable": self.fp_enable,
            "comparators": list(self.comparators),
            "step_requests": self.step_requests,
            "executed": list(self.executed),
            "flash_writes": [list(row) for row in self.flash_writes()],
            "code_writes": [list(row) for row in self.code_writes()],
            "counter": self.word(COUNTER),
            "flushes": self.flushes,
        }

    def write_snapshot(self, note: str | None = None) -> None:
        if self.snapshot_path is None:
            return
        payload = {"pid": os.getpid(), "note": note, **self.state()}
        temporary = self.snapshot_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        os.replace(temporary, self.snapshot_path)


class SimulatedAP:
    """MEM-AP transfer surface used by pyOCD CortexM/FPB and production."""

    short_description = "M08-T simulated MEM-AP#0"
    ap_num = 0

    def __init__(self, model: SimulatedCortexM4) -> None:
        self.model = model

    def read_memory(self, addr: int, transfer_size: int = 32, now: bool = True) -> Any:
        value = self.model.ap_read(addr, transfer_size)
        return value if now else (lambda: value)

    def write_memory(self, addr: int, data: int, transfer_size: int = 32) -> None:
        self.model.ap_write(addr, data, transfer_size)

    def read32(self, addr: int, now: bool = True) -> Any:
        return self.read_memory(addr, 32, now)

    def read16(self, addr: int, now: bool = True) -> Any:
        return self.read_memory(addr, 16, now)

    def read8(self, addr: int, now: bool = True) -> Any:
        return self.read_memory(addr, 8, now)

    def write32(self, addr: int, value: int) -> None:
        self.write_memory(addr, value, 32)

    def write16(self, addr: int, value: int) -> None:
        self.write_memory(addr, value, 16)

    def write8(self, addr: int, value: int) -> None:
        self.write_memory(addr, value, 8)

    def read_memory_block8(self, addr: int, size: int) -> list[int]:
        return [self.model.ap_read(addr + offset, 8) for offset in range(size)]

    def read_memory_block32(self, addr: int, size: int) -> list[int]:
        return [self.model.ap_read(addr + 4 * offset, 32) for offset in range(size)]

    def write_memory_block8(self, addr: int, data: Any) -> None:
        for offset, value in enumerate(data):
            self.model.ap_write(addr + offset, value, 8)

    def write_memory_block32(self, addr: int, data: Any) -> None:
        for offset, value in enumerate(data):
            self.model.ap_write(addr + 4 * offset, value, 32)

    def set_cacheable(self, cacheable: bool = True) -> None:
        self.model.journal.append(("set-cacheable", cacheable))


class SimulatedProbe:
    def __init__(self, model: SimulatedCortexM4, unique_id: str) -> None:
        self.model = model
        self.unique_id = unique_id
        self.description = "M08-T simulated MEM-AP probe"
        self.vendor_name = "M08-T"
        self.product_name = "simulated MEM-AP"
        self.associated_board_info = None
        self.is_open = True

    def flush(self) -> None:
        self.model.flush()

    def assert_reset(self, asserted: bool) -> None:
        self.model.journal.append(("assert-reset", bool(asserted)))

    def close(self) -> None:
        self.is_open = False
        self.model.journal.append(("probe-close",))


class SimulatedSession(Notifier):
    """Session surface pyOCD targets need: options, probe, notifications, target."""

    def __init__(self, model: SimulatedCortexM4, unique_id: str, options: dict[str, Any] | None,
                 *, writable_flash: bool = False) -> None:
        super().__init__()
        self.options = OptionsManager()
        self.options.add_front(dict(options or {}))
        self.probe = SimulatedProbe(model, unique_id)
        self.board = SimpleNamespace(name="M08-T simulated Cortex-M4")
        self.delegate = None
        self.target: SoCTarget | None = None
        self.model = model
        self.writable_flash = writable_flash
        self.closed = False

    def open(self) -> None:
        memory_map = MemoryMap(
            FlashRegion(start=FLASH_BASE, length=FLASH_SIZE, blocksize=0x800, is_boot_memory=True,
                        access="rwx" if self.writable_flash else "rx"),
            RamRegion(start=RAM_BASE, length=RAM_SIZE),
        )
        soc = SoCTarget(self, memory_map)
        ap = SimulatedAP(self.model)
        core = CortexM(self, ap, soc.memory_map, 0, None, SCS_BASE)
        soc.add_core(core)
        fpb = FPB(ap, addr=FPB_BASE)
        core.add_child(fpb)
        core.init()
        fpb.init()
        self.target = soc
        if self.options.get("connect_mode") == "halt":
            soc.halt()

    def close(self) -> None:
        try:
            if self.target is not None:
                self.target.disconnect(self.options.get("resume_on_disconnect"))
        finally:
            self.probe.close()
            self.closed = True


class SimulatedProbeAdapter(PyOCDSWDInterface):
    """Production adapter; only pyOCD probe enumeration selects the simulated probe."""

    def __init__(self, model: SimulatedCortexM4, *, unique_id: str = PROBE_UID,
                 writable_flash: bool = False) -> None:
        super().__init__()
        self.model = model
        self.unique_id = unique_id
        self.writable_flash = writable_flash
        self.sessions: list[SimulatedSession] = []

    def _choose_session(self, *, probe_uid: str | None, options: dict[str, Any] | None) -> Any:
        session = SimulatedSession(self.model, self.unique_id, options,
                                   writable_flash=self.writable_flash)
        self.sessions.append(session)
        return session


def module_identities(
    prefixes: tuple[str, ...] = ("pyocd_debug_mcp", "pyocd.", "tests."),
) -> list[dict]:
    rows = []
    for name, module in sorted(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if (name == "__main__" or name.startswith(prefixes)) and file:
            path = Path(file).resolve()
            if path.is_file():
                data = path.read_bytes()
                rows.append({"module": name, "path": str(path), "bytes": len(data),
                             "sha256": hashlib.sha256(data).hexdigest()})
    return rows


def worker_main(argv: list[str]) -> None:
    """Real provider worker over the simulated probe: ``<step_mode> <observation_dir>``.

    ``step_mode`` is consulted only by a DHCSR single-step request, so the open,
    breakpoint and resume traffic before the public step meets an ordinary core.
    """
    import contextlib

    with contextlib.redirect_stdout(sys.stderr):  # stdout carries the worker protocol
        from pyocd_debug_mcp.adapters import provider_worker, provider_worker_runtime

        step_mode, observation = argv[0], Path(argv[1])
        observation.mkdir(parents=True, exist_ok=True)
        model = SimulatedCortexM4()
        model.step_mode = step_mode
        model.snapshot_path = observation / f"worker-{os.getpid()}-state.json"
        adapter = SimulatedProbeAdapter(model)
        provider_worker_runtime.PyOCDSWDInterface = lambda: adapter
        # Snapshot before the production close path can terminate this child.
        (observation / f"worker-{os.getpid()}-imports.json").write_text(
            json.dumps(module_identities(), indent=1) + "\n", encoding="utf-8")
        model.write_snapshot(note="worker ready")
    provider_worker.main()


if __name__ == "__main__":
    worker_main(sys.argv[1:])
