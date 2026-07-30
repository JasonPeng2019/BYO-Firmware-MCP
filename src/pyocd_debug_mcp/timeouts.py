"""Timeout budgets used by the standalone BYO/server runtime."""

from __future__ import annotations

from dataclasses import dataclass

# PROJECT-DEFINED (runtime command ceiling for probe/vendor enumeration helpers).
DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS = 30.0

# PROJECT-DEFINED (hard per-hook ceiling for one agent-authored discovery hook).
# Lives here, not in `discovery_hooks`, because `kernel/operations.py` needs it to size
# the operation budget and `kernel/__init__` imports operations -- so importing
# `discovery_hooks` from operations would be a cycle. `discovery_hooks` re-exports it.
MAX_HOOK_TIMEOUT_SECONDS = 60.0

# PROJECT-DEFINED (long setup command ceiling; allows uv sync without unbounded waits).
SETUP_COMMAND_TIMEOUT_SECONDS = 900.0

# PROJECT-DEFINED (local MCP startup should fail before any tool-level timeout applies).
MCP_STARTUP_TIMEOUT_SECONDS = 30.0

# PROJECT-DEFINED (explicit pyOCD operation ceilings; mirrors/tightens pyOCD defaults).
PYOCD_STEP_TIMEOUT_SECONDS = 2.0
PYOCD_RESET_HALT_TIMEOUT_SECONDS = 2.0
PYOCD_DAP_RECOVER_TIMEOUT_SECONDS = 2.0
PYOCD_CORE_RECOVER_TIMEOUT_SECONDS = 2.0
PYOCD_FLASH_INIT_TIMEOUT_SECONDS = 5.0
PYOCD_FLASH_PROGRAM_TIMEOUT_SECONDS = 10.0
PYOCD_FLASH_ERASE_SECTOR_TIMEOUT_SECONDS = 10.0
PYOCD_FLASH_ERASE_ALL_TIMEOUT_SECONDS = 240.0
PYOCD_FLASH_ANALYZER_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ServerTimeoutConfig:
    step_instruction_seconds: float = PYOCD_STEP_TIMEOUT_SECONDS
    reset_halt_seconds: float = PYOCD_RESET_HALT_TIMEOUT_SECONDS
    dap_recover_seconds: float = PYOCD_DAP_RECOVER_TIMEOUT_SECONDS
    core_recover_seconds: float = PYOCD_CORE_RECOVER_TIMEOUT_SECONDS
    flash_init_seconds: float = PYOCD_FLASH_INIT_TIMEOUT_SECONDS
    flash_program_seconds: float = PYOCD_FLASH_PROGRAM_TIMEOUT_SECONDS
    flash_erase_sector_seconds: float = PYOCD_FLASH_ERASE_SECTOR_TIMEOUT_SECONDS
    flash_erase_all_seconds: float = PYOCD_FLASH_ERASE_ALL_TIMEOUT_SECONDS
    flash_analyzer_seconds: float = PYOCD_FLASH_ANALYZER_TIMEOUT_SECONDS

    def to_record(self) -> dict[str, float]:
        return {
            "step_instruction_seconds": self.step_instruction_seconds,
            "reset_halt_seconds": self.reset_halt_seconds,
            "dap_recover_seconds": self.dap_recover_seconds,
            "core_recover_seconds": self.core_recover_seconds,
            "flash_init_seconds": self.flash_init_seconds,
            "flash_program_seconds": self.flash_program_seconds,
            "flash_erase_sector_seconds": self.flash_erase_sector_seconds,
            "flash_erase_all_seconds": self.flash_erase_all_seconds,
            "flash_analyzer_seconds": self.flash_analyzer_seconds,
        }

    def pyocd_options(self) -> dict[str, object]:
        return {
            "cpu.step.instruction.timeout": self.step_instruction_seconds,
            "reset.halt_timeout": self.reset_halt_seconds,
            "reset.dap_recover.timeout": self.dap_recover_seconds,
            "reset.core_recover.timeout": self.core_recover_seconds,
            "flash.timeout.init": self.flash_init_seconds,
            "flash.timeout.program": self.flash_program_seconds,
            "flash.timeout.erase_sector": self.flash_erase_sector_seconds,
            "flash.timeout.erase_all": self.flash_erase_all_seconds,
            "flash.timeout.analyzer": self.flash_analyzer_seconds,
        }


def default_server_timeout_config() -> ServerTimeoutConfig:
    return ServerTimeoutConfig()


def subprocess_timeout_stream_text(value: object) -> str:
    """Normalize subprocess timeout output to text for diagnostics."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
