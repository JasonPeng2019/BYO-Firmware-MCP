"""One tier matrix guard for public and indirect hardware routes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from .matrix import TIER_ROUTE_MATRIX, raw_tool_for, safe_tool_for
from .policy import CapabilityPolicyRepository, CapabilityState, Tier


@dataclass(frozen=True, slots=True)
class TierRouteRefusal(RuntimeError):
    """A route is unavailable for the explicit policy of its named board."""

    code: str
    message: str
    remedies: tuple[str, ...] = ()

    def __str__(self) -> str:
        return self.message


class TierRouter:
    """Apply the frozen raw/safe route split before legacy plan-lock checks."""

    def __init__(self, policies: CapabilityPolicyRepository) -> None:
        self.policies = policies

    def state_for(self, board_id: str) -> CapabilityState:
        state = self.policies.resolve(board_id)
        if not state.hardware_allowed:
            raise TierRouteRefusal(
                "tier/corrupt-policy",
                f"Board '{board_id}' has a corrupt committed safety policy; repair it before hardware access.",
                ("board_safety_refresh", "board_setup"),
            )
        return state

    def require_safe(
        self, operation: str, board_id: str, *, state: CapabilityState | None = None
    ) -> CapabilityState:
        """Require a map-backed route, before the legacy plan registry runs."""

        state = state or self.state_for(board_id)
        if state.tier is Tier.NO_SETUP:
            suffix = (
                " Setup is incomplete; complete setup for a safe route."
                if state.setup_incomplete
                else ""
            )
            raise TierRouteRefusal(
                "tier/wrong-route",
                f"Board '{board_id}' has no safety map; use the raw operation or run setup.{suffix}",
                (self.raw_tool_for(operation), "board_setup-plan", "board_setup"),
            )
        return state

    def require_raw(
        self, operation: str, board_id: str, *, state: CapabilityState | None = None
    ) -> dict[str, object] | None:
        """Require a raw route and return its mandatory lite display warning."""

        state = state or self.state_for(board_id)
        if state.tier is Tier.SETUP_FULL:
            raise TierRouteRefusal(
                "tier/wrong-route",
                f"This board is fully set up; use the planned safe {self.safe_name_for(operation)}.",
                (f"{self.safe_name_for(operation)}-plan", self.safe_name_for(operation)),
            )
        if state.tier is Tier.SETUP_LITE:
            return {
                "code": "tier/lite-containment-bypassed",
                "display_to_human": True,
                "message": (
                    f"Display to the human: {operation} on {board_id} bypassed "
                    "setup-lite containment."
                ),
            }
        return None

    def raw_error_warning(
        self, operation: str, board_id: str, *, state: CapabilityState | None = None
    ) -> dict[str, object] | None:
        """Return the lite warning wording required for a failed raw attempt."""

        warning = self.require_raw(operation, board_id, state=state)
        if warning is not None:
            warning = dict(warning)
            warning["message"] = (
                f"Display to the human: {operation} on {board_id} bypassed setup-lite "
                "containment, and the attempt failed."
            )
        return warning

    def capabilities(
        self,
        board_id: str,
        *,
        project_root: Path,
        identity_capability: str | None = None,
        native_recovery_backend_available: bool | None = None,
        state: CapabilityState | None = None,
    ) -> dict[str, Any]:
        """Build the versioned, per-board discovery payload without asserting identity."""

        state = state or self.policies.resolve(board_id)
        if state.tier is None:
            return {
                "schema_version": 1,
                "status": "capability_status",
                "board_id": board_id,
                "project_root": str(Path(project_root).resolve()),
                "tier": None,
                "policy_status": "corrupt",
                "policy_digest": None,
                "setup_incomplete": False,
                "identity": {"assertion": None, "capability": None},
                "capabilities": [],
                "remedies": [
                    "Repair the committed policy with board setup before hardware access."
                ],
            }
        if state.tier is Tier.SETUP_FULL:
            identity = {
                "assertion": "proven"
                if identity_capability in {"exact", "compatible"}
                else "not-asserted",
                "capability": identity_capability
                if identity_capability in {"exact", "compatible"}
                else None,
                "proof_required": "exact-or-compatible",
            }
        elif state.tier is Tier.SETUP_LITE:
            identity = {"assertion": "trusted-not-proven", "capability": None}
        else:
            identity = {"assertion": "not-asserted", "capability": None}

        # This is deliberately code-owned rather than copied from the operator
        # Markdown matrix.  It makes discovery an honest view of the same
        # immutable snapshot used by containment and route selection.
        lite = state.tier is Tier.SETUP_LITE
        full = state.tier is Tier.SETUP_FULL
        raw_available = not full
        rows = state.map_snapshot.get("regions") if isinstance(state.map_snapshot, Mapping) else []
        regions = (
            tuple(row for row in rows if isinstance(row, Mapping)) if isinstance(rows, list) else ()
        )
        flash = state.map_snapshot.get("flash") if isinstance(state.map_snapshot, Mapping) else None

        def has_region(
            *kinds: str, readable: bool = False, writable: bool = False, executable: bool = False
        ) -> bool:
            if full:
                return True
            if not lite:
                return False
            for row in regions:
                if row.get("kind") not in kinds:
                    continue
                if readable and row.get("readable") is not True:
                    continue
                if writable and row.get("writable") is not True:
                    continue
                if executable and row.get("executable") is not True:
                    continue
                return True
            return False

        def flash_ready(kind: str) -> bool:
            if full:
                return True
            return bool(
                lite
                and has_region(kind, writable=True)
                and isinstance(flash, Mapping)
                and isinstance(flash.get("backend_target"), str)
                and bool(flash.get("backend_target", "").strip())
                and isinstance(flash.get("erase_sectors"), list)
                and bool(flash.get("erase_sectors"))
            )

        def row(
            family: str,
            safe_tool: str | None,
            raw_tool: str | None,
            safe_available: bool,
            prerequisites: list[str],
        ) -> dict[str, Any]:
            available = safe_available or (raw_available and raw_tool is not None)
            preferred = safe_tool if safe_available and safe_tool is not None else raw_tool
            return {
                "family": family,
                "available": available,
                "safe": safe_available and safe_tool is not None,
                "preferred_tool": preferred,
                "default_tool": preferred,
                "safe_tool": safe_tool if available else None,
                "raw_tool": raw_tool if raw_available else None,
                "prerequisites": prerequisites,
            }

        partitions = (
            state.map_snapshot.get("partitions")
            if isinstance(state.map_snapshot, Mapping)
            else None
        )
        app_flash = flash_ready("application_flash") and (
            not full
            or isinstance(partitions, Mapping)
            and partitions.get("application") is not None
        )
        boot_flash = flash_ready("bootloader_flash") and (
            not full or isinstance(partitions, Mapping) and partitions.get("bootloader") is not None
        )
        recovery = (
            state.map_snapshot.get("recovery") if isinstance(state.map_snapshot, Mapping) else None
        )
        documented_lite_recovery = bool(
            lite
            and isinstance(recovery, Mapping)
            and set(recovery) == {"mechanism", "source_pages", "source_note"}
            and recovery.get("mechanism") == "backend_mass_erase"
            and isinstance(recovery.get("source_note"), str)
            and recovery["source_note"].strip()
            and isinstance(recovery.get("source_pages"), list)
            and bool(recovery["source_pages"])
            and all(
                isinstance(page, int) and not isinstance(page, bool) and page > 0
                for page in recovery["source_pages"]
            )
        )
        profile = state.profile_snapshot if isinstance(state.profile_snapshot, Mapping) else {}
        documented_full_recovery = bool(
            full
            and isinstance(state.map_snapshot, Mapping)
            and state.map_snapshot.get("schema_version") == 2
            and profile.get("recover_mode") == "backend_mass_erase"
        )
        # Documentation makes a mechanism eligible for consideration, never a
        # live backend operation. The server supplies this only from the
        # current connection while holding the board operation lock.
        recovery_available = (
            documented_lite_recovery or documented_full_recovery
        ) and native_recovery_backend_available is True
        uart_note = profile.get("uart_note")
        uart_configured = full or bool(
            lite
            and isinstance(profile, Mapping)
            and (
                isinstance(profile.get("serial_baudrate"), int)
                or isinstance(uart_note, str)
                and uart_note.strip()
            )
        )
        routes = [
            row("connection", "connect", None, True, []),
            row(
                "connection-under-reset",
                "connect_under_reset",
                None,
                lite or full,
                ["active connection plan"],
            ),
            row(
                "identity-validation",
                "board_validate",
                None,
                full,
                ["setup-full policy", "live exact or compatible proof"],
            ),
            row(
                "memory-read",
                "read_memory_address",
                "read_memory_raw",
                full
                or has_region(
                    "ram",
                    "physical_ram",
                    "application_flash",
                    "bootloader",
                    "rom",
                    "peripheral",
                    "peripheral_read_only",
                    readable=True,
                ),
                ["confirmed readable region"],
            ),
            row(
                "memory-write",
                "write_memory",
                "write_memory_raw",
                full or has_region("ram", "physical_ram", writable=True),
                ["confirmed writable RAM region"],
            ),
            row(
                "register-write",
                "register_write",
                "register_write_raw",
                full or has_region("peripheral", "peripheral_write_only", writable=True),
                ["confirmed writable peripheral region"],
            ),
            row(
                "cpu-register-write",
                "write_cpu_register",
                "write_cpu_register_raw",
                lite or full,
                ["active connection and approved plan"],
            ),
            row(
                "execution-control",
                "set_execution_state",
                "set_execution_state_raw",
                lite or full,
                ["active connection and approved plan"],
            ),
            row(
                "breakpoint",
                "set_breakpoint",
                "set_breakpoint_raw",
                full
                or has_region(
                    "ram",
                    "physical_ram",
                    "application_flash",
                    "bootloader",
                    "rom",
                    executable=True,
                ),
                ["confirmed executable region"],
            ),
            row(
                "reset-halt",
                "reset_and_halt",
                "reset_and_halt_raw",
                lite or full,
                ["active connection and approved plan"],
            ),
            row(
                "flash-application",
                "flash_application",
                "flash_raw",
                app_flash,
                [
                    "confirmed application-flash region",
                    "confirmed flash backend target",
                    "confirmed erase sectors",
                ],
            ),
            row(
                "flash-bootloader",
                "flash_bootloader",
                "flash_raw",
                boot_flash,
                [
                    "confirmed bootloader region",
                    "confirmed flash backend target",
                    "confirmed erase sectors",
                ],
            ),
            row(
                "serial-read",
                "read_serial",
                "read_serial_raw",
                uart_configured,
                ["configured UART attachment and approved plan"],
            ),
            row(
                "serial-write",
                "write_serial",
                "write_serial_raw",
                uart_configured,
                ["configured UART attachment and approved plan"],
            ),
            row(
                "serial-exchange",
                "serial_exchange",
                "serial_exchange_raw",
                uart_configured,
                ["configured UART attachment and approved plan"],
            ),
            row(
                "target-recovery",
                "target_unlock",
                "target_unlock_raw",
                recovery_available,
                (
                    ["documented native recovery mechanism", "connected backend support"]
                    if lite
                    else ["setup-full recovery authority"]
                ),
            ),
        ]
        routes[-1]["recovery_mode"] = "native-safe" if recovery_available else "unavailable"
        represented = {str(route["family"]) for route in routes}
        for definition in TIER_ROUTE_MATRIX:
            family = definition.family
            if family is None or family in represented:
                continue
            # Metadata/setup and batch rows are always discoverable; direct
            # hardware rows need their normal connection/session prerequisite,
            # but they never gain identity authority from merely being listed.
            if definition.guard == "metadata":
                available = True
                prerequisites: list[str] = []
            elif definition.guard == "indirect":
                available = True
                prerequisites = [
                    "child route prerequisites" if family == "batch" else "documented prerequisite"
                ]
            else:
                available = True
                prerequisites = ["active connection"]
            routes.append(
                {
                    "family": family,
                    "available": available,
                    "safe": definition.guard == "safe" and available,
                    "preferred_tool": definition.name,
                    "default_tool": definition.name,
                    "safe_tool": definition.name if definition.guard == "safe" else None,
                    "raw_tool": definition.raw_tool if raw_available else None,
                    "prerequisites": prerequisites,
                }
            )
            represented.add(family)
        return {
            "schema_version": 1,
            "status": "capability_status",
            "board_id": board_id,
            "project_root": str(Path(project_root).resolve()),
            "tier": state.tier.value,
            "policy_status": state.status,
            "policy_digest": state.policy_digest,
            "setup_incomplete": state.setup_incomplete,
            "identity": identity,
            "capabilities": routes,
            "manual_permissions": {
                "root": str(
                    Path(project_root).resolve()
                    / ".agent-workspace"
                    / "runtime"
                    / "manual-permissions"
                ),
                "epoch_status": "unknown",
            },
            "remedies": [],
        }

    @staticmethod
    def raw_tool_for(operation: str) -> str:
        return raw_tool_for(operation)

    @staticmethod
    def safe_name_for(operation: str) -> str:
        return safe_tool_for(operation)
