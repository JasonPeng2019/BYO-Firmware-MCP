"""Runtime-supported CPU-register policy and safety-checked peripheral writes."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from pyocd_debug_mcp.kernel.operations import wrap_layer2_response


_ORDINARY_REGISTER = re.compile(r"(?:r(?:[0-9]|1[0-2])|[sdq][0-9]+)", re.IGNORECASE)
_EXECUTION_REGISTERS = frozenset(
    {
        "r13",
        "r14",
        "r15",
        "sp",
        "lr",
        "pc",
        "xpsr",
        "apsr",
        "ipsr",
        "epsr",
        "msp",
        "psp",
        "msplim",
        "psplim",
        "primask",
        "basepri",
        "basepri_max",
        "faultmask",
        "control",
        "cfbp",
        "fpscr",
    }
)
_PROHIBITED_TERMS = (
    "secure",
    "security",
    "provision",
    "lifecycle",
    "option",
    "otp",
    "fuse",
    "protect",
    "lock",
    "auth",
    "key",
    "sau",
)


@dataclass(frozen=True, slots=True)
class RegisterToolServices:
    supported_registers: Callable[[str], tuple[str, ...]]
    read_register: Callable[[str, str], str]
    write_register: Callable[[str, str, int], str]
    masked_register_write: Callable[[str, int, int, int], str]
    check_register_write: Callable[[str, int], None] | None = None


class RegisterPreconditionError(ValueError):
    """A register request failed a Layer-2 check before execution began."""


def _normalize_name(name: str) -> str:
    return name.strip().casefold()


def _refusal(code: str, message: str) -> str:
    return wrap_layer2_response(f"Refused [{code}]: {message}")


def _is_prohibited(name: str) -> bool:
    return name.endswith(("_ns", "_s")) or any(term in name for term in _PROHIBITED_TERMS)


def _validate_supported(
    services: RegisterToolServices,
    board_id: str,
    name: str,
    *,
    execution_state: bool,
) -> tuple[str | None, str | None]:
    normalized = _normalize_name(name)
    if _is_prohibited(normalized):
        return None, _refusal(
            "register/prohibited",
            f"Register '{name}' is security/provisioning-related and unavailable.",
        )
    expected_class = (
        normalized in _EXECUTION_REGISTERS
        if execution_state
        else _ORDINARY_REGISTER.fullmatch(normalized) is not None
    )
    if not expected_class:
        class_name = "execution-state" if execution_state else "ordinary CPU/FP"
        return None, _refusal(
            "register/wrong-class",
            f"Register '{name}' is not in the {class_name} register class.",
        )
    supported = {item.casefold() for item in services.supported_registers(board_id)}
    if normalized not in supported:
        return None, _refusal(
            "register/unsupported",
            f"Register '{name}' is not supported by the connected core.",
        )
    return normalized, None


def _parse_value(
    value: str | int,
    field_name: str,
    *,
    maximum: int | None = 0xFFFFFFFF,
) -> tuple[int | None, str | None]:
    if isinstance(value, bool):
        return None, _refusal("register/invalid-value", f"{field_name} must not be boolean.")
    try:
        parsed = value if isinstance(value, int) else int(value, 0)
    except ValueError:
        return None, _refusal(
            "register/invalid-value",
            f"{field_name} must be hexadecimal or decimal.",
        )
    if parsed < 0 or (maximum is not None and parsed > maximum):
        limit = (
            "a non-negative integer"
            if maximum is None
            else f"an unsigned {maximum.bit_length()}-bit value"
        )
        return None, _refusal(
            "register/invalid-value",
            f"{field_name} must fit in {limit}.",
        )
    return parsed, None


def _register_maximum(normalized_name: str) -> int:
    if normalized_name.startswith("q"):
        return (1 << 128) - 1
    if normalized_name.startswith("d"):
        return (1 << 64) - 1
    return 0xFFFFFFFF


def _raise_precondition(refusal: str | None) -> None:
    if refusal is not None:
        raise RegisterPreconditionError(refusal.splitlines()[0])


def validate_guarded_register_call(
    services: RegisterToolServices,
    action_name: str,
    board_id: str,
    arguments: dict[str, object],
) -> None:
    """Run Task-7 register checks before a guarded call consumes budget."""

    if action_name in {"write_cpu_register", "set_execution_state"}:
        name = arguments.get("name")
        value = arguments.get("value")
        if (
            not isinstance(name, str)
            or isinstance(value, bool)
            or not isinstance(value, (str, int))
        ):
            raise RegisterPreconditionError(
                "Refused [register/invalid-value]: name and value have invalid types."
            )
        normalized, refusal = _validate_supported(
            services,
            board_id,
            name,
            execution_state=action_name == "set_execution_state",
        )
        _raise_precondition(refusal)
        assert normalized is not None
        _, refusal = _parse_value(value, "value", maximum=_register_maximum(normalized))
        _raise_precondition(refusal)
        return

    if action_name != "register_write":
        return
    parsed: dict[str, int] = {}
    for field_name in ("address", "mask", "value"):
        value = arguments.get(field_name)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise RegisterPreconditionError(
                f"Refused [register/invalid-value]: {field_name} has an invalid type."
            )
        parsed_value, refusal = _parse_value(value, field_name)
        _raise_precondition(refusal)
        assert parsed_value is not None
        parsed[field_name] = parsed_value
    if parsed["address"] % 4:
        raise RegisterPreconditionError(
            "Refused [register/unaligned]: address must be 32-bit aligned."
        )
    if parsed["mask"] == 0:
        raise RegisterPreconditionError(
            "Refused [register/empty-mask]: mask must affect at least one bit."
        )


def build_register_handlers(
    services: RegisterToolServices,
) -> dict[str, Callable[..., str]]:
    """Build the revised CPU and peripheral register action handlers."""

    def read_cpu_register(board_id: str, name: str) -> str:
        """Read one runtime-supported ordinary CPU or floating-point register."""

        normalized, refusal = _validate_supported(services, board_id, name, execution_state=False)
        if refusal is not None:
            return refusal
        assert normalized is not None
        return wrap_layer2_response(services.read_register(board_id, normalized))

    def read_execution_state(board_id: str, name: str) -> str:
        """Read one runtime-supported control-flow or execution-state register."""

        normalized, refusal = _validate_supported(services, board_id, name, execution_state=True)
        if refusal is not None:
            return refusal
        assert normalized is not None
        return wrap_layer2_response(services.read_register(board_id, normalized))

    def write_cpu_register(board_id: str, name: str, value: str | int) -> str:
        """Write one ordinary R0-R12 or floating-point register under a fixed plan."""

        normalized, refusal = _validate_supported(services, board_id, name, execution_state=False)
        if refusal is not None:
            return refusal
        assert normalized is not None
        parsed, refusal = _parse_value(value, "value", maximum=_register_maximum(normalized))
        if refusal is not None:
            return refusal
        assert parsed is not None
        return wrap_layer2_response(services.write_register(board_id, normalized, parsed))

    def set_execution_state(board_id: str, name: str, value: str | int) -> str:
        """Set one control-flow/mode register under a permission-carrying fixed plan."""

        normalized, refusal = _validate_supported(services, board_id, name, execution_state=True)
        if refusal is not None:
            return refusal
        assert normalized is not None
        parsed, refusal = _parse_value(value, "value", maximum=_register_maximum(normalized))
        if refusal is not None:
            return refusal
        assert parsed is not None
        return wrap_layer2_response(services.write_register(board_id, normalized, parsed))

    def register_write(
        board_id: str,
        address: str | int,
        mask: str | int,
        value: str | int,
    ) -> str:
        """Apply a fixed-plan masked write inside mapped non-prohibited peripheral space."""

        parsed_address, refusal = _parse_value(address, "address")
        if refusal is not None:
            return refusal
        parsed_mask, refusal = _parse_value(mask, "mask")
        if refusal is not None:
            return refusal
        parsed_value, refusal = _parse_value(value, "value")
        if refusal is not None:
            return refusal
        assert parsed_address is not None and parsed_mask is not None and parsed_value is not None
        if parsed_address % 4:
            return _refusal("register/unaligned", "address must be 32-bit aligned.")
        if parsed_mask == 0:
            return _refusal("register/empty-mask", "mask must affect at least one bit.")
        if services.check_register_write is not None:
            services.check_register_write(board_id, parsed_address)
        result = services.masked_register_write(
            board_id,
            parsed_address,
            parsed_mask,
            parsed_value,
        )
        return wrap_layer2_response(f"{result}\nSafety map: mapped peripheral write allowed.")

    return {
        "read_cpu_register": read_cpu_register,
        "read_execution_state": read_execution_state,
        "write_cpu_register": write_cpu_register,
        "set_execution_state": set_execution_state,
        "register_write": register_write,
    }
