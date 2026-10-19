"""Independent memory/AP boundary; pyOCD selection, FPB and SW providers are real.

Writable flash is deliberate: it lets the installed manager's AUTO fallback
actually call the software provider and attempt a code write. All AP/code writes
are journalled before effects or injected transfer failures.
"""

from types import SimpleNamespace

from pyocd.core.memory_map import FlashRegion, MemoryMap, RamRegion
from pyocd.core.target import Target
from pyocd.coresight.fpb import FPB
from pyocd.debug.breakpoints.manager import BreakpointManager
from pyocd.debug.breakpoints.software import SoftwareBreakpointProvider

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface


class MemoryTarget:
    def __init__(self, capacity=3, flash_start=0x1000):
        self.memory_map = MemoryMap(
            FlashRegion(start=flash_start, length=0x4000, blocksize=0x1000, access="rwx"),
            RamRegion(start=0x20000000, length=0x1000, access="rwx"),
        )
        self.flash_start = flash_start
        self.words = {0xE0002000: (capacity & 15) << 4 | (capacity & 0x70) << 8}
        self.writes = []
        self.reads = []
        self.requests = []
        self.flushes = 0
        self.fail_flushes = set()
        self.fail_after_comparator_write = False
        self.drop_comparator_clear = False
        self.drop_restore = False
        self.fail_raw_reads = False
        self.session = SimpleNamespace(
            subscribe=lambda *args: None, options={"force_sw_bkpt": False}
        )
        self.ap = self
        self.selected_core_or_raise = self
        self.fpb = FPB(self, addr=0xE0002000)
        self.fpb.init()
        self.software = SoftwareBreakpointProvider(self)
        self.bp_manager = BreakpointManager(self)
        self.bp_manager.add_provider(self.fpb)
        self.bp_manager.add_provider(self.software)
        self.writes.clear()

    def read_memory(self, address, transfer_size=32, now=True):
        self.reads.append((address, transfer_size, now))
        if self.fail_raw_reads:
            raise RuntimeError("injected AP read failure")
        return self.words.get(address, 0x4770 if transfer_size == 16 else 0x47704770)

    def read16(self, address):
        return self.bp_manager.filter_memory(address, 16, self.read_memory(address, 16))

    def read32(self, address):
        return self.read_memory(address, 32)

    def write_memory(self, address, value, transfer_size=32):
        self.writes.append((address, value, transfer_size))
        comparator = 0xE0002008 <= address < 0xE0002088
        if (comparator and value == 0 and self.drop_comparator_clear) or (
            transfer_size == 16 and value != 0xBE00 and self.drop_restore
        ):
            return
        self.words[address] = value
        if comparator and value and self.fail_after_comparator_write:
            raise RuntimeError("injected failure after physical comparator programming")

    def write16(self, address, value):
        self.write_memory(address, value, 16)

    def write32(self, address, value):
        self.write_memory(address, value, 32)

    def invalidate_instruction_cache(self, address):
        pass

    def find_breakpoint(self, address):
        return self.bp_manager.find_breakpoint(address)

    def set_breakpoint(self, address, type=Target.BreakpointType.AUTO):
        self.requests.append((address, type))
        return self.bp_manager.set_breakpoint(address, type)

    def remove_breakpoint(self, address):
        self.bp_manager.remove_breakpoint(address)

    def flush(self):
        self.flushes += 1
        if self.flushes in self.fail_flushes:
            raise RuntimeError("injected target flush failure")

    @property
    def code_writes(self):
        # Classify by the boundary's physical banks, independently of a changed
        # provider map or transfer width. The complete write journal is retained.
        return [row for row in self.writes
                if self.flash_start <= row[0] < self.flash_start + 0x4000
                or 0x20000000 <= row[0] < 0x20001000]

    @property
    def comparators(self):
        return {bp.comp_register_addr: self.words[bp.comp_register_addr]
                for bp in self.fpb.hw_breakpoints}

    def get_state(self):
        return Target.State.HALTED


def handle_for(target):
    return TargetSessionHandle(
        SimpleNamespace(target=target, board=SimpleNamespace(name="M07 boundary"),
                        probe=SimpleNamespace(unique_id="m07-test", description="memory AP")),
        None, "m07-test", "m07-independent-boundary", "cortex_m"
    )


class BoundaryAdapter(PyOCDSWDInterface):
    """Replace only probe connection; all breakpoint transactions stay production."""

    def __init__(self, target):
        super().__init__()
        self.target = target

    def open(self, **kwargs):
        return handle_for(self.target)

    def close(self, handle):
        pass

    def release_reset(self, handle):
        pass
