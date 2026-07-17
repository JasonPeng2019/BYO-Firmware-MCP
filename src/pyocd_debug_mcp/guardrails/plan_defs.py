"""Declarative source of truth for every Layer-1 plan tool."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final


class BudgetMode(str, Enum):
    FIXED = "fixed"
    FLEXIBLE = "flexible"


class PermissionMode(str, Enum):
    NONE = "none"
    REQUIRED = "required"
    FRESH_ONE_TIME = "fresh-one-time"


class SafetyMode(str, Enum):
    SESSION = "session"
    VALIDATED_READ = "validated-read"
    FRESH_WRITE = "fresh-write"


class FieldType(str, Enum):
    TEXT = "text"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"
    JSON = "json"
    TEXT_OR_INTEGER = "text-or-integer"


@dataclass(frozen=True, slots=True)
class FieldDefinition:
    name: str
    field_type: FieldType
    description: str
    nullable: bool = False
    choices: tuple[object, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    min_items: int | None = None


@dataclass(frozen=True, slots=True)
class PlanDefinition:
    action_name: str
    plan_tool_name: str
    purpose: str
    action_fields: tuple[FieldDefinition, ...]
    budget_mode: BudgetMode
    permission_mode: PermissionMode
    safety_mode: SafetyMode
    timeout_seconds: float
    extra_instructions: str
    paired_actions: tuple[str, ...] = ()
    max_plan_cycles_per_board: int | None = None

    @property
    def common_fields(self) -> tuple[FieldDefinition, ...]:
        return COMMON_PLAN_FIELDS

    @property
    def plan_fields(self) -> tuple[FieldDefinition, ...]:
        fields = self.common_fields + (ACTION_PARAMETERS_FIELD,)
        if self.permission_mode is not PermissionMode.NONE:
            fields += (USER_PERMISSION_FIELD,)
        return fields

    @property
    def plan_field_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.plan_fields)

    @property
    def null_fields(self) -> tuple[FieldDefinition, ...]:
        """The exact universal first-call envelope, including NULL permission."""

        return self.common_fields + (ACTION_PARAMETERS_FIELD, USER_PERMISSION_FIELD)

    @property
    def null_field_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.null_fields)

    @property
    def call_fields(self) -> tuple[FieldDefinition, ...]:
        """Published MCP fields accepted across initialization and populated calls."""

        return self.null_fields

    def render_null_response(self, permission_disclosure: str | None = None) -> str:
        return _render_null_response(self, permission_disclosure)


COMMON_PLAN_FIELDS: Final = (
    FieldDefinition("board_id", FieldType.TEXT, "Stable logical board identifier."),
    FieldDefinition("hypothesis", FieldType.TEXT, "Non-empty concrete hypothesis."),
    FieldDefinition("strategy", FieldType.TEXT, "Non-empty evaluated strategy."),
    FieldDefinition("hypothesis_made", FieldType.BOOLEAN, "Must be true."),
    FieldDefinition("strategy_evaluated", FieldType.BOOLEAN, "Must be true."),
    FieldDefinition(
        "expected_fail_return",
        FieldType.TEXT,
        "Non-empty expected failure result or signal.",
    ),
    FieldDefinition(
        "expected_success_return",
        FieldType.TEXT,
        "Non-empty expected success result or signal.",
    ),
    FieldDefinition("max_calls", FieldType.INTEGER, "Primary call budget."),
    FieldDefinition("max_calls_buffer", FieldType.INTEGER, "Additional bounded call budget."),
)
USER_PERMISSION_FIELD: Final = FieldDefinition(
    "user_permission",
    FieldType.TEXT,
    "Structured permission value handled by the permission provider.",
    nullable=True,
)
ACTION_PARAMETERS_FIELD: Final = FieldDefinition(
    "action_parameters",
    FieldType.OBJECT,
    "One JSON object containing exactly the underlying action parameters, frozen verbatim.",
)


def _field(
    name: str,
    field_type: FieldType,
    description: str,
    *,
    nullable: bool = False,
    choices: tuple[object, ...] = (),
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
    min_items: int | None = None,
) -> FieldDefinition:
    return FieldDefinition(
        name,
        field_type,
        description,
        nullable,
        choices,
        minimum,
        maximum,
        exclusive_minimum,
        min_items,
    )


_DEFINITIONS = (
    PlanDefinition(
        "board_setup",
        "board_setup-plan",
        "Create or repair one logical board profile and its safety evidence.",
        (
            _field("mode", FieldType.TEXT, "Exactly setup or repair.", choices=("setup", "repair")),
            _field("connection_id", FieldType.TEXT, "Intended enumerated physical connection."),
            _field("display_name", FieldType.TEXT, "User-provided familiar board name."),
            _field("mcu_part_number", FieldType.TEXT, "Exact user-provided MCU part number."),
            _field("serial_baudrate", FieldType.INTEGER, "Positive UART baud rate.", minimum=1),
        ),
        BudgetMode.FIXED,
        PermissionMode.REQUIRED,
        SafetyMode.SESSION,
        300.0,
        "Do not guess hardware choices or rewrite the user-supplied MCU part number.",
        paired_actions=("board_fix_setup",),
        max_plan_cycles_per_board=3,
    ),
    PlanDefinition(
        "connect_override",
        "connect_override-plan",
        "Connect using explicitly reviewed exceptional identifiers without changing profiles.",
        (
            _field("probe_uid", FieldType.TEXT, "Manual stable probe identifier.", nullable=True),
            _field("target_override", FieldType.TEXT, "Manual pyOCD target.", nullable=True),
            _field(
                "external_board_config",
                FieldType.TEXT,
                "External board configuration path.",
                nullable=True,
            ),
        ),
        BudgetMode.FLEXIBLE,
        PermissionMode.NONE,
        SafetyMode.SESSION,
        30.0,
        "Override values are run-scoped and never silently update a profile.",
    ),
    PlanDefinition(
        "write_cpu_register",
        "write_cpu_register-plan",
        "Write an ordinary general-purpose or floating-point CPU register.",
        (
            _field("name", FieldType.TEXT, "Supported ordinary CPU register name."),
            _field("value", FieldType.TEXT_OR_INTEGER, "Exact hexadecimal or decimal value."),
        ),
        BudgetMode.FIXED,
        PermissionMode.NONE,
        SafetyMode.FRESH_WRITE,
        30.0,
        "Control-flow, security, and provisioning registers are excluded.",
    ),
    PlanDefinition(
        "set_execution_state",
        "set_execution_state-plan",
        "Change a CPU control-flow or execution-mode register.",
        (
            _field("name", FieldType.TEXT, "Supported execution-state register name."),
            _field("value", FieldType.TEXT_OR_INTEGER, "Exact hexadecimal or decimal value."),
        ),
        BudgetMode.FIXED,
        PermissionMode.REQUIRED,
        SafetyMode.FRESH_WRITE,
        30.0,
        "Permission does not make unsupported or security-related registers writable.",
    ),
    PlanDefinition(
        "read_memory_address",
        "read_memory_address-plan",
        "Read a mapped address or bounded memory block when symbol access is unsuitable.",
        (
            _field("address", FieldType.TEXT_OR_INTEGER, "Exact address."),
            _field(
                "width",
                FieldType.INTEGER,
                "Transfer width: 8, 16, or 32 bits.",
                choices=(8, 16, 32),
            ),
            _field(
                "length",
                FieldType.INTEGER,
                "Optional block length up to 64 KiB.",
                nullable=True,
                minimum=1,
                maximum=65536,
            ),
        ),
        BudgetMode.FLEXIBLE,
        PermissionMode.NONE,
        SafetyMode.VALIDATED_READ,
        30.0,
        "Prefer read_memory_symbol when debug metadata identifies the value.",
    ),
    PlanDefinition(
        "write_memory",
        "write_memory-plan",
        "Write one symbol-backed value or an explicitly justified mapped RAM address.",
        (
            _field("symbol_or_address", FieldType.TEXT_OR_INTEGER, "Exact symbol or address."),
            _field("value", FieldType.JSON, "Exact JSON-representable value."),
            _field(
                "width",
                FieldType.INTEGER,
                "Transfer width: 8, 16, or 32 bits.",
                choices=(8, 16, 32),
            ),
            _field("allow_address_fallback", FieldType.BOOLEAN, "Explicit raw-address fallback."),
            _field("reason", FieldType.TEXT, "Reason symbol access is unsuitable.", nullable=True),
        ),
        BudgetMode.FIXED,
        PermissionMode.NONE,
        SafetyMode.FRESH_WRITE,
        30.0,
        "Prefer symbols; raw addresses require fallback=true and a concrete reason.",
    ),
    PlanDefinition(
        "set_breakpoint",
        "set_breakpoint-plan",
        "Set a breakpoint at one mapped executable symbol or address.",
        (_field("symbol_or_address", FieldType.TEXT_OR_INTEGER, "Exact symbol or address."),),
        BudgetMode.FIXED,
        PermissionMode.NONE,
        SafetyMode.FRESH_WRITE,
        30.0,
        "The resolved location must be executable and supported by the connected core.",
    ),
    PlanDefinition(
        "flash_application",
        "flash_application-plan",
        "Flash a validated artifact wholly inside the application partition.",
        (
            _field("artifact", FieldType.TEXT, "Local ELF or HEX artifact path."),
            _field(
                "target_address",
                FieldType.TEXT_OR_INTEGER,
                "Optional explicit load address.",
                nullable=True,
            ),
        ),
        BudgetMode.FIXED,
        PermissionMode.NONE,
        SafetyMode.FRESH_WRITE,
        120.0,
        "No caller-supplied allowed ranges are accepted.",
    ),
    PlanDefinition(
        "flash_bootloader",
        "flash_bootloader-plan",
        "Flash a validated artifact wholly inside the bootloader partition.",
        (
            _field("artifact", FieldType.TEXT, "Local ELF or HEX artifact path."),
            _field(
                "target_address",
                FieldType.TEXT_OR_INTEGER,
                "Optional explicit load address.",
                nullable=True,
            ),
        ),
        BudgetMode.FIXED,
        PermissionMode.REQUIRED,
        SafetyMode.FRESH_WRITE,
        120.0,
        "Permission is partition-specific and never authorizes application or prohibited ranges.",
    ),
    PlanDefinition(
        "register_write",
        "register_write-plan",
        "Apply one masked value to a documented peripheral register.",
        (
            _field("address", FieldType.TEXT_OR_INTEGER, "Exact documented register address."),
            _field("mask", FieldType.TEXT_OR_INTEGER, "Exact documented field mask."),
            _field("value", FieldType.TEXT_OR_INTEGER, "Exact value to apply."),
        ),
        BudgetMode.FIXED,
        PermissionMode.NONE,
        SafetyMode.FRESH_WRITE,
        30.0,
        "Security, provisioning, option-byte, OTP, and lifecycle registers are unavailable.",
    ),
    PlanDefinition(
        "reset_and_halt",
        "reset_and_halt-plan",
        "Reset the selected board and halt immediately at startup.",
        (),
        BudgetMode.FLEXIBLE,
        PermissionMode.NONE,
        SafetyMode.SESSION,
        30.0,
        "This reset does not unlock a protected target.",
    ),
    PlanDefinition(
        "connect_under_reset",
        "connect_under_reset-plan",
        "Attach while physical reset is asserted, then halt and release reset.",
        (
            _field("probe_uid", FieldType.TEXT, "Stable probe identifier.", nullable=True),
            _field("target_override", FieldType.TEXT, "Exact target override.", nullable=True),
        ),
        BudgetMode.FLEXIBLE,
        PermissionMode.NONE,
        SafetyMode.SESSION,
        30.0,
        "Fail clearly when the probe has no wired reset-line support; do not degrade silently.",
    ),
    PlanDefinition(
        "target_unlock",
        "target_unlock-plan",
        "Perform one documented destructive vendor recovery operation.",
        (
            _field(
                "recovery_mechanism",
                FieldType.TEXT,
                "Exact documented vendor recovery mechanism; NULL requests research when unknown.",
                nullable=True,
            ),
        ),
        BudgetMode.FIXED,
        PermissionMode.FRESH_ONE_TIME,
        SafetyMode.SESSION,
        300.0,
        "Erase facts are server-derived from the current safety map. First submit with "
        "user_permission=NULL to receive the plan-id-bound disclosure, then resubmit every "
        "other field unchanged with user_permission=one-time. Full-session permission never applies.",
    ),
    PlanDefinition(
        "read_serial",
        "read_serial-plan",
        "Capture bounded UART output from the selected board.",
        (
            _field("expected_text", FieldType.TEXT, "Optional expected text.", nullable=True),
            _field(
                "read_seconds",
                FieldType.NUMBER,
                "Positive bounded capture duration.",
                minimum=0,
                exclusive_minimum=True,
            ),
            _field("baudrate", FieldType.INTEGER, "Positive baud rate.", nullable=True, minimum=1),
            _field("port", FieldType.TEXT, "Current serial port path.", nullable=True),
            _field("reset_on_open", FieldType.BOOLEAN, "Reset after opening the port."),
        ),
        BudgetMode.FLEXIBLE,
        PermissionMode.NONE,
        SafetyMode.VALIDATED_READ,
        30.0,
        "A port path is runtime-only; it is never persisted as attachment identity.",
    ),
    PlanDefinition(
        "write_serial",
        "write_serial-plan",
        "Send bounded UTF-8 text over the selected board's UART.",
        (
            _field("text", FieldType.TEXT, "Exact text to send."),
            _field("baudrate", FieldType.INTEGER, "Positive baud rate.", nullable=True, minimum=1),
            _field("port", FieldType.TEXT, "Current serial port path.", nullable=True),
            _field("append_newline", FieldType.BOOLEAN, "Append one newline when true."),
            _field(
                "timeout_seconds",
                FieldType.NUMBER,
                "Positive bounded write timeout.",
                minimum=0,
                exclusive_minimum=True,
            ),
        ),
        BudgetMode.FLEXIBLE,
        PermissionMode.NONE,
        SafetyMode.FRESH_WRITE,
        30.0,
        "The text and all transport parameters are bound exactly by the plan.",
    ),
)


PLAN_DEFINITIONS: Final = MappingProxyType(
    {definition.action_name: definition for definition in _DEFINITIONS}
)
PLAN_TOOL_DEFINITIONS: Final = MappingProxyType(
    {definition.plan_tool_name: definition for definition in _DEFINITIONS}
)


@dataclass(frozen=True, slots=True)
class _PromptGuidance:
    use_when: str
    preconditions: str
    warnings: str
    soft_guardrails: tuple[str, ...]
    exit: str
    example_parameters: dict[str, object]
    special: str = ""


_UART_PROTOCOL = (
    "You cannot see the board. Prints are your eyes. When testing or diagnosing via UART: "
    "(1) instrument heavily at every decision point, state transition, ISR entry/exit, and "
    "suspect branch; (2) track every injected print in a workspace markdown file with source "
    "file such as uart_debug_prints.md, source file, function, line, exact unique greppable tag "
    "such as [TRC-01], and what it proves; "
    "(3) treat captured tags as hardware observations and reconstruct what actually ran, not "
    "what the code should have done; (4) budget multiple captures and remember that changed "
    "instrumentation requires rebuild, board_safety_refresh after relinking, and reflash; "
    "(5) at task completion remove every tracked print, grep for the tags until there are zero "
    "hits, rebuild and flash clean firmware, then delete or clear the tracking file."
)


_GUIDANCE: Final = MappingProxyType(
    {
        "board_setup": _PromptGuidance(
            "Use only for a user-named board with no matching profile, or to repair a matching "
            "profile whose setup is incomplete or failed. Call load_setup_tool first. A healthy "
            "existing profile must use board_validate, not setup again merely because this is a "
            "new Server Run.",
            "The server verifies load_setup_tool and the one-to-one name-to-connection assignment. "
            "One plan uniquely permits one board_setup call and one paired board_fix_setup call; "
            "board_fix_setup has no separate plan prompt.",
            "Setup touches live hardware but is bounded to non-destructive reads; it never flashes "
            "or erases. Relay setup_needs_user_input and setup_research_required choices in plain "
            "conversation without exposing JSON, continuation identifiers, or internal field names.",
            (
                "Confirm the user explicitly named this board during this session.",
                "Confirm no healthy profile has the same display name; validate it if one does.",
                "Use the user's exact MCU part number—never guess, normalize, or correct it.",
                "Be ready to relay probe, port, and build choices instead of selecting silently.",
            ),
            "Both setup actions relock when the workflow completes; proceed to board_validate.",
            {
                "mode": "setup",
                "connection_id": "probe:001",
                "display_name": "left controller",
                "mcu_part_number": "STM32L476RGT6",
                "serial_baudrate": 115200,
            },
            "PAIRED ALLOWANCE: if the first setup call fails, its one paired board_fix_setup call "
            "is already authorized even under one-time permission. A further attempt requires a "
            "replacement plan and, for one-time permission, a fresh user prompt.",
        ),
        "connect_override": _PromptGuidance(
            "Use only after normal connect or validation resolution fails and you can state the "
            "exact failure. It is for exceptional manual probe unique_id, pyOCD target, logical "
            "board definition, or external board configuration. Never use it to conceal a "
            "hardware/profile mismatch the user should correct.",
            "The plan and logical board must match. Manual probe_uid, target_override, and "
            "external_board_config values are run-scoped and never rewrite a profile.",
            "An incorrect target override can produce undefined debug behavior on a mismatched chip.",
            (
                "State which normal resolution step failed and its exact error.",
                "Confirm the target override agrees with the user's exact part number.",
                "Take the probe unique ID from server enumeration; never guess it.",
            ),
            "The resulting session behaves like a normal connection; disconnect cleanly when done.",
            {
                "probe_uid": "066EFF534951877567174226",
                "target_override": "stm32l476rgtx",
                "external_board_config": None,
            },
        ),
        "write_cpu_register": _PromptGuidance(
            "Use while halted to patch an ordinary R0-R12 or supported floating-point register. "
            "Read it first with read_cpu_register. Do not use this for PC, SP/MSP/PSP, LR, xPSR, "
            "CONTROL, PRIMASK, BASEPRI, FAULTMASK, or security/provisioning registers; use "
            "set_execution_state for supported execution-state registers.",
            "The server verifies a validated session, open fresh gate, exact plan match, and the "
            "connected core's supported ordinary-register class.",
            "A wrong value corrupts in-flight computation, normally recoverable by reset.",
            ("Halt the core first.", "Confirm the register is in the ordinary allowed class.", "Confirm the value fits its width.", "State the readback expected afterward."),
            "Read the register back, then deliberately resume or reset.",
            {"name": "r0", "value": "0x00000001"},
        ),
        "set_execution_state": _PromptGuidance(
            "Use only for a deliberate write to PC, SP/MSP/PSP, LR, xPSR, CONTROL, PRIMASK, "
            "BASEPRI, FAULTMASK, or a related supported execution-state register. reset_and_run is "
            "always available and safer for restarting; use write_cpu_register for ordinary data "
            "registers.",
            "The server verifies a validated session, open fresh gate, permission, exact plan match, "
            "and the connected core's supported execution-state register class.",
            "This can redirect execution, corrupt the stack, mask interrupts, or fault the CPU; it "
            "is usually recoverable with reset.",
            ("Explain why reset or a breakpoint cannot achieve the goal.", "State the reset_and_halt or reset_and_run recovery step.", "Derive an address or stack value from symbols or a verified read, never guesswork.", "Ask the user plainly and carry the grant only in user_permission."),
            "Verify with read_execution_state/get_state and leave a deliberate run or halt state.",
            {"name": "pc", "value": "0x08008231"},
        ),
        "read_memory_address": _PromptGuidance(
            "Prefer symbol access whenever source code or debug symbols identify the intended "
            "variable (find_symbol and read_memory_symbol are always available and need no plan). "
            "Use raw addresses only for dynamically allocated, pointer-derived, stack, optimized-out, "
            "or otherwise unsymbolized memory.",
            "The server verifies a validated session and full containment in a mapped region; unknown "
            "memory is denied and block length is capped at 64 KiB.",
            "Some peripheral registers have clear-on-read or other side effects.",
            ("Try find_symbol first and record why it was insufficient.", "Identify the mapped region containing the address.", "For peripherals, check the reference manual for read side effects."),
            "No board-state cleanup is required.",
            {"address": "0x20000400", "width": 32, "length": 64},
        ),
        "write_memory": _PromptGuidance(
            "Use to change one variable for a bounded hypothesis test. Prefer a symbol. A raw address "
            "requires allow_address_fallback=true plus a concrete reason and is RAM-only. Do not use "
            "this for peripheral registers or flash.",
            "The server verifies a validated session, open gate, fingerprint freshness, exact plan, "
            "and full mapped-RAM containment. A raw address without the flag is rejected with 'Try a "
            "symbol first.' Prohibited and unknown regions are denied.",
            "A wrong memory write can crash the application; recover by reset or reflash.",
            ("Attempt and show symbol resolution first.", "Confirm the value and width match the variable type and size.", "State a reversal or recovery step.", "State the readback that will confirm the write."),
            "Read back the location and remember the board may now behave differently by design.",
            {"symbol_or_address": "motor_speed_target", "value": "0x12C", "width": 32, "allow_address_fallback": False, "reason": None},
        ),
        "set_breakpoint": _PromptGuidance(
            "Breakpoint-and-step debugging is escalation, not the first move. First use tagged UART "
            "print instrumentation and read_serial to reconstruct flow. Use a breakpoint only when "
            "prints failed or cannot work—for example before UART initialization, timing perturbation, "
            "no UART path, or instruction-level inspection after logs localized the fault. "
            "remove_breakpoint is always available and needs no plan.",
            "The server resolves the symbol/address and verifies a mapped executable region supported "
            "by the target breakpoint mechanism.",
            "Breakpoint resources are finite, and a forgotten breakpoint can halt the board later.",
            ("Record the print-based attempt and why escalation is necessary.", "Use a symbol-derived address or justify the explicit address.", "State the expected halt/PC and the state to inspect.", "Name remove_breakpoint in the strategy and perform it afterward."),
            "Remove the breakpoint and leave the core in a deliberate state.",
            {"symbol_or_address": "uart_rx_handler"},
        ),
        "flash_application": _PromptGuidance(
            "Use only to deploy a rebuilt application artifact. Never use it for a bootloader or "
            "arbitrary flash. The server checks every loadable segment, required erase sector, entry "
            "point, vector table, target identity, and optional explicit target address against the "
            "application partition.",
            "A validated session and open fresh gate are required. After rebuilding/relinking, run "
            "board_safety_refresh first or fingerprint freshness closes the gate.",
            "Cancellation lets an in-progress flash finish safely. A wrong-but-contained image may run "
            "incorrectly; recover by flashing a correct application.",
            ("Build the artifact freshly from the stated configuration.", "Confirm the named board is the artifact's intended board.", "State the observable post-flash behavior and how UART or another read will verify it.", "If the linker changed, rebuild the safety map instead of flashing immediately."),
            "The target runs after flashing; verify the planned observable behavior.",
            {"artifact": "firmware/app/build/firmware.elf", "target_address": None},
        ),
        "flash_bootloader": _PromptGuidance(
            "Use only for an artifact intended for the bootloader partition. Application, prohibited, "
            "ROM-bootloader, and unknown regions are rejected. Prefer flash_application for ordinary "
            "firmware changes.",
            "A validated session, open fresh gate, permission, current fingerprint, target identity, "
            "and full bootloader segment/erase/entry/vector containment are required.",
            "A bad bootloader can make the application unbootable. It remains recoverable over SWD, "
            "but explain that consequence before requesting permission.",
            ("Confirm a bootloader flash is truly necessary.", "Use the bootloader build artifact, not the application build.", "Tell the user plainly what a failed bootloader means.", "State the post-flash check, such as a bootloader UART banner."),
            "The target runs after flashing; verify the bootloader behavior.",
            {"artifact": "firmware/boot/build/bootloader.elf", "target_address": None},
        ),
        "register_write": _PromptGuidance(
            "Use for one documented peripheral/configuration register read-modify-write. The complete "
            "word must be in a mapped peripheral window. Flash security, option bytes, OTP, debug "
            "protection, provisioning, and lifecycle registers are unavailable—no plan can authorize "
            "them, and calling an address a register does not make it one.",
            "The server verifies a validated session, open fresh gate, exact plan, and peripheral "
            "classification with prohibited ranges overriding all other classifications.",
            "A wrong peripheral write can wedge clocks or pins; reset normally recovers it.",
            ("Read the reference-manual/datasheet section.", "Read the SVD or machine register description.", "Confirm register name, address, and field agree in both sources.", "Compute the mask and name bits deliberately left untouched.", "Account for side effects such as write-1-to-clear fields."),
            "Read back where safe/readable and note the expected behavior change.",
            {"address": "0x48000014", "mask": "0x20", "value": "0x20"},
        ),
        "reset_and_halt": _PromptGuidance(
            "Use when firmware fails immediately and startup state must be inspected. reset_and_run is "
            "always available and needs no plan for an ordinary restart. This action is not an unlock "
            "and never changes target security.",
            "The server verifies the validated session and exact board/plan match.",
            "The core remains halted, so firmware does not run until explicitly resumed or reset.",
            ("Confirm reset_and_run is insufficient and explain why.", "Set any needed breakpoint before resuming.", "State exactly what startup state will be inspected."),
            "Resume or call reset_and_run; do not leave the board silently halted.",
            {},
        ),
        "connect_under_reset": _PromptGuidance(
            "Use after normal attach fails because firmware sleeps, reconfigures debug pins/clocks, or "
            "crashes too early. It asserts physical reset, attaches over SWD, halts, and releases reset. "
            "It is not an unlock; a protected target still requires target_unlock.",
            "The server verifies the board/plan and that the selected probe has a wired, supported reset "
            "line; unsupported hardware fails clearly without a fallback.",
            "Physical reset stops the running firmware without warning.",
            ("Record the exact normal-attach failure.", "Confirm probe/reset-line wiring and support.", "Have a deliberate next step for the halted core."),
            "The core is halted after attach; finish in a deliberate run or halt state.",
            {"probe_uid": None, "target_override": None},
        ),
        "target_unlock": _PromptGuidance(
            "Use only for a server-confirmed locked target after every non-destructive route is exhausted. "
            "It performs one typed documented vendor recovery operation, usually mass erase. Reset tools "
            "never unlock and arbitrary security/provisioning writes are never accepted.",
            "The server binds the plan to current run, board, target, probe, connection, safety-map "
            "fingerprint, recovery mechanism, and complete erase disclosure.",
            "Recovery erases exactly the disclosed ranges. If the device only supports full-chip erase, "
            "all nonvolatile application, bootloader, configuration, and user data is lost.",
            ("Confirm locked state from a server result, not inference.", "List the exhausted non-destructive alternatives.", "Ensure the user understands every erased range and what must be reflashed.", "Prepare the post-recovery reflash and board_validate steps."),
            "The gate stays closed after recovery until board_validate passes again.",
            {"recovery_mechanism": "nrf_pyocd_unlock"},
            "TWO-PHASE APPROVAL: first submit the complete populated plan with user_permission=null. "
            "The server returns permission_required with exact live identity, mechanism, mass-erase "
            "flag, all ranges/banks/sectors, all-nonvolatile status, expected losses, and plan_id. Relay "
            "that in plain language and obtain explicit approval. Then resubmit the complete plan "
            "unchanged except user_permission='one-time'. Any change to plan, target, probe, map, or "
            "erase facts invalidates approval. Research can identify a mechanism but never authorizes "
            "execution. Full-session and prior approval never cover mass erase.",
        ),
        "read_serial": _PromptGuidance(
            "Use for bounded UART boot text, logs, or injected diagnostic prints. It is not a continuous "
            "monitor; each capture is bounded by read_seconds. expected_text=null is the right choice for "
            "exploratory captures so one exact plan can cover varied output.",
            "The server verifies a validated session, exact plan/board/parameters, a positive duration "
            "and baud rate, and the resolved runtime port.",
            "A halted core emits no UART. Opening some ports can trigger auto-reset; use reset_on_open "
            "deliberately.",
            ("Confirm the core is running when output is expected.", "Choose a capture window long enough for the event.", "Install and track diagnostic prints before planning captures.", "Budget honestly for every observation and retry.", _UART_PROTOCOL),
            "The server owns port cleanup; if instrumentation was used, perform the full tracked-print cleanup.",
            {"expected_text": None, "read_seconds": 5.0, "baudrate": None, "port": None, "reset_on_open": False},
        ),
        "write_serial": _PromptGuidance(
            "Use to send bounded UTF-8 input to a firmware CLI or test menu. The exact text is frozen; a "
            "different command requires a replacement plan. Pair it with planned read_serial observation.",
            "The server verifies a validated session, exact plan/board/parameters, non-empty text, and "
            "positive baud rate and timeout.",
            "Input reaches live firmware. Recognize and avoid destructive commands implemented by the "
            "firmware's own erase or reset menus.",
            ("Confirm firmware is ready for this input.", "Check whether append_newline is required.", "Plan read_serial to observe the response.", _UART_PROTOCOL),
            "The server owns port cleanup; if instrumentation was used, perform the full tracked-print cleanup.",
            {"text": "test motor", "baudrate": None, "port": None, "append_newline": True, "timeout_seconds": 1.0},
        ),
    }
)


def _render_null_response(
    definition: PlanDefinition,
    permission_disclosure: str | None,
) -> str:
    guidance = _GUIDANCE[definition.action_name]
    action_lines = "\n".join(
        f"- {field.name} ({field.field_type.value}{' or NULL' if field.nullable else ''}): "
        f"{field.description}"
        for field in definition.action_fields
    ) or "- No action-specific parameters; action_parameters must be the empty object {}."
    action_names = ", ".join(field.name for field in definition.action_fields) or "none"
    plan_lines = "\n".join(
        f"- {field.name}: {field.description}" for field in definition.plan_fields
    )
    permission = {
        PermissionMode.NONE: (
            "None. Omit user_permission entirely from a populated plan. Including it—even as "
            "NULL—makes a populated plan malformed. It appears only as NULL in the universal "
            "first all-NULL initialization call."
        ),
        PermissionMode.REQUIRED: (
            "Required. Ask the user plainly before submitting. Conversation is never permission; "
            "the grant counts only as user_permission='one-time' or 'full-session'. One-time covers "
            "the fixed 1,0 action allowance. Full-session covers only this tool and board for this "
            "Server Run; later plans may use user_permission=null when that grant is active."
        ),
        PermissionMode.FRESH_ONE_TIME: (
            "Fresh one-time permission is required for every destructive recovery. Full-session, a "
            "prior approval, another board's approval, and conversational assent never apply. Follow "
            "the two-phase disclosure flow below."
        ),
    }[definition.permission_mode]
    if permission_disclosure:
        permission = f"{permission} Current permission state: {permission_disclosure}"
    budget = (
        "Submit exactly max_calls=1 and max_calls_buffer=0; every other budget is rejected. "
        "The one call is consumed when execution starts even if it fails, times out, is cancelled, "
        "or returns nothing useful. Pre-execution refusal consumes nothing. Exhaustion relocks the "
        "action and a complete replacement plan is required."
        if definition.budget_mode is BudgetMode.FIXED
        else "Set max_calls to the attempts the strategy genuinely expects (1..20) and "
        "max_calls_buffer to bounded leeway (0..10). Every started attempt consumes one call, "
        "including empty, inconclusive, failed, timed-out, or cancelled results; pre-execution "
        "refusal consumes nothing. Exhaustion relocks the action and requires a complete replacement plan."
    )
    example: dict[str, object] = {
        "board_id": "left_controller",
        "hypothesis": f"Concrete board evidence indicates {definition.action_name} will test the suspected cause.",
        "strategy": f"Run the exact {definition.action_name} action, inspect the stated result, and follow the recovery or cleanup path on failure.",
        "hypothesis_made": True,
        "strategy_evaluated": True,
        "expected_fail_return": "A concrete refusal, value, or board state that disproves the hypothesis.",
        "expected_success_return": "The concrete value or board state that confirms the hypothesis.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "action_parameters": guidance.example_parameters,
    }
    if definition.budget_mode is BudgetMode.FLEXIBLE:
        example["max_calls"] = 3
        example["max_calls_buffer"] = 1
    if definition.permission_mode is not PermissionMode.NONE:
        example["user_permission"] = (
            None if definition.permission_mode is PermissionMode.FRESH_ONE_TIME else "one-time"
        )
    validation_permission = (
        "user_permission is required unless a reusable full-session grant is active"
        if definition.permission_mode is PermissionMode.REQUIRED
        else "user_permission must be omitted"
        if definition.permission_mode is PermissionMode.NONE
        else "user_permission must be NULL for disclosure and exactly one-time for unchanged approval"
    )
    soft = "\n".join(
        f"{index}. {item}" for index, item in enumerate(guidance.soft_guardrails, start=1)
    )
    special = f"\n{guidance.special}" if guidance.special else ""
    budget_label = "fixed" if definition.budget_mode is BudgetMode.FIXED else "flexible"
    return (
        f"Plan initialization for {definition.plan_tool_name}.\n\n"
        f"[MECHANISM]\nThe real action {definition.action_name} is hidden and locked. This "
        f"response is step 1. To unlock it, call {definition.plan_tool_name} again with every "
        "field populated as described below. A valid plan unlocks the action for the named board "
        "with exactly the planned parameters. Plans are immutable; any change requires a complete "
        "new plan call, which atomically replaces the old plan. Submit only the plan JSON object as "
        "the tool arguments—no prose, Markdown, wrapper key, flattened action fields, or extra fields.\n\n"
        f"[PURPOSE]\nPurpose: {definition.purpose}\n\n"
        f"[USE-WHEN / NOT-WHEN]\n{guidance.use_when}\n\n"
        "[PLAN-FIELDS]\nRequired plan fields:\n"
        f"{plan_lines}\n"
        "board_id: the assigned logical board id. hypothesis: concrete belief and why this action "
        "tests it. strategy: how the result will be used and the success/failure branches. "
        "hypothesis_made and strategy_evaluated: literal true. expected_fail_return and "
        "expected_success_return: concrete observable results. max_calls and max_calls_buffer: the "
        "budget described below. action_parameters: one nested JSON object containing the exact "
        "underlying action arguments. user_permission: present only for permission-locked populated "
        "plans. The first call is the universal envelope with every one of these fields NULL.\n\n"
        f"[ACTION-PARAMETERS]\nUnderlying action parameters:\nNested action_parameters must contain exactly these keys ({action_names}) "
        "and their values are frozen verbatim:\n"
        f"{action_lines}\n\n"
        "[VALIDATION]\nA populated call must be exactly one JSON object with every required "
        "envelope field, the exact nested action_parameters schema, correct JSON types, no unknown "
        f"or extra fields, and a valid budget; {validation_permission}. hypothesis_made and "
        "strategy_evaluated must be true, and all reasoning/result text must be concrete rather than "
        "empty or boilerplate. Malformed submissions list missing/invalid fields, create or replace no "
        "plan, consume no budget, do not count as initialization, and leave an active plan untouched. "
        "A changed field requires a complete replacing plan.\n\n"
        f"[BUDGET]\nBudget: {budget_label}. {budget}\n\n"
        f"[PERMISSION]\nPermission: {permission}\n\n"
        f"[PRECONDITIONS]\nSafety/session policy: {definition.safety_mode.value}; default timeout "
        f"{definition.timeout_seconds:g}s. {guidance.preconditions} Execution still checks active/current plan, "
        "exact board and parameters, remaining calls, assignment/session validity, and every applicable "
        "validation, gate, and fingerprint condition; refusal names the remedy.\n\n"
        f"[WARNINGS]\n{guidance.warnings}{special}\nExtra instructions: "
        f"{definition.extra_instructions}\n\n"
        f"[SOFT-GUARDRAILS — confirm before submitting]\n{soft}\n\n"
        f"[EXIT]\n{guidance.exit}\n\n"
        "[EXAMPLE-PLAN]\nIllustrative values only; replace them with real reasoning and exact action "
        "arguments, then submit only this JSON object:\n"
        f"{json.dumps(example, ensure_ascii=False, indent=2)}"
    )
PAIRED_ACTION_DEFINITIONS: Final = MappingProxyType(
    {
        paired_action: definition
        for definition in _DEFINITIONS
        for paired_action in definition.paired_actions
    }
)

if len(PLAN_DEFINITIONS) != len(_DEFINITIONS):  # pragma: no cover - import-time invariant
    raise RuntimeError("Duplicate action in plan definitions")
if len(PLAN_TOOL_DEFINITIONS) != len(_DEFINITIONS):  # pragma: no cover
    raise RuntimeError("Duplicate plan tool in plan definitions")


def definition_for_action(action_name: str) -> PlanDefinition:
    try:
        return PLAN_DEFINITIONS[action_name]
    except KeyError:
        try:
            return PAIRED_ACTION_DEFINITIONS[action_name]
        except KeyError as exc:
            raise KeyError(f"No plan definition for action '{action_name}'") from exc


def definition_for_plan_tool(plan_tool_name: str) -> PlanDefinition:
    try:
        return PLAN_TOOL_DEFINITIONS[plan_tool_name]
    except KeyError as exc:
        raise KeyError(f"Unknown plan tool '{plan_tool_name}'") from exc
