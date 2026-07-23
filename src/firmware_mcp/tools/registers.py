"""Runtime-supported CPU-register and live-verified peripheral writes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from firmware_mcp.kernel.operations import wrap_layer2_response


@dataclass(frozen=True, slots=True)
class RegisterToolServices:
    supported_registers: Callable[[str], tuple[str, ...]]
    read_register: Callable[[str, str], str]
    write_register: Callable[[str, str, int, bool], str]
    masked_register_write: Callable[[str, int, int, int, bool], str]


def _normalize_name(name: str) -> str:
    return name.strip().casefold()


def _refusal(code: str, message: str) -> str:
    return wrap_layer2_response(f"Invalid [{code}]: {message}")


def _validate_supported(
    services: RegisterToolServices,
    board_id: str,
    name: str,
) -> tuple[str | None, str | None]:
    normalized = _normalize_name(name)
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


def build_register_handlers(
    services: RegisterToolServices,
) -> dict[str, Callable[..., str]]:
    """Build the revised CPU and peripheral register action handlers."""

    def read_cpu_register(board_id: str, register_name: str) -> str:
        """**What** Read one live provider-supported CPU register.

        **When** Use while connected to inspect core state.

        **Parameters** `board_id` is the board and `register_name` is a provider register name,
        for example `"pc"`.

        **Returns** The observed register value.

        **Failures and recovery** Unsupported registers or lost transport are explicit; inspect
        `get_target_state` or reconnect with `connect_board`.
        """

        normalized, refusal = _validate_supported(services, board_id, register_name)
        if refusal is not None:
            return refusal
        assert normalized is not None
        return wrap_layer2_response(services.read_register(board_id, normalized))

    def write_cpu_register(
        board_id: str, register_name: str, value: str | int, verify: bool = True
    ) -> str:
        """**What** Write one live provider-supported CPU register.

        **When** Use for intentional core-state changes while debugging.

        **Parameters** `board_id` is the board; `register_name` is for example `"r0"`; `value`
        is decimal or hexadecimal; `verify` requests readback (for example `true`).

        **Returns** Verified value or explicit provider-accepted/no-readback evidence.

        **Failures and recovery** Unsupported names or mismatch are reported; use
        `read_cpu_register` or reconnect with `connect_board`.
        """

        normalized, refusal = _validate_supported(services, board_id, register_name)
        if refusal is not None:
            return refusal
        assert normalized is not None
        parsed, refusal = _parse_value(value, "value", maximum=_register_maximum(normalized))
        if refusal is not None:
            return refusal
        assert parsed is not None
        return wrap_layer2_response(services.write_register(board_id, normalized, parsed, verify))

    def write_peripheral_register(
        board_id: str,
        address: str | int,
        mask: str | int,
        value: str | int,
        width_bits: int = 32,
        verify: bool = True,
    ) -> str:
        """**What** Apply a live-region-checked masked peripheral register write.

        **When** Use for provider-mapped peripheral state, including full writes to write-only
        registers.

        **Parameters** `board_id` names the board; `address` is decimal/hex (for example
        `"0x40000000"`); `mask` and `value` are integers; `width_bits` is bits (currently `32`);
        `verify` requests readback.

        **Returns** Verified masked bits or explicit unavailable/not-requested evidence.

        **Failures and recovery** Unmapped, unreadable partial RMW, unsupported width, or mismatch
        is reported; inspect `read_memory` or use a full mask with `verify=false` when appropriate.
        """

        if isinstance(width_bits, bool) or width_bits != 32:
            return _refusal(
                "register/invalid-width", "width_bits must be 32 for this provider path."
            )

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
        result = services.masked_register_write(
            board_id,
            parsed_address,
            parsed_mask,
            parsed_value,
            verify,
        )
        return wrap_layer2_response(result)

    return {
        "read_cpu_register": read_cpu_register,
        "write_cpu_register": write_cpu_register,
        "write_peripheral_register": write_peripheral_register,
    }
