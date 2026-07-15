"""Mutation-focused convergence watcher for the first R10 pass."""

from __future__ import annotations

from dataclasses import dataclass

from pyocd_debug_mcp.services.session_runtime import (
    SessionRecord,
    ToolEvent,
    ToolOutcome,
    WatcherBlocked,
)

FLASH_TOOL = "flash_firmware"
UART_TOOL = "read_serial"
RECOVER_TOOL = "unlock_recover"


@dataclass(frozen=True)
class BlockDecision:
    action_family: str
    code: str
    message: str


class ConvergenceWatcher:
    """Track repeated mutation failures and block obviously non-converging loops."""

    def ensure_allowed(self, session: SessionRecord, action_family: str) -> None:
        blocked = session.blocked_actions.get(action_family)
        if blocked is None:
            return
        raise WatcherBlocked(
            blocked["code"],
            blocked["message"],
            action_family=action_family,
            session_id=session.session_id,
        )

    def observe_event(self, session: SessionRecord, event: ToolEvent) -> BlockDecision | None:
        signature = self._signature(event)
        if signature is None:
            return None

        action_family = str(signature[0])
        threshold = self._threshold_for(action_family)
        streak = self._matching_streak(session, action_family, signature)
        if streak < threshold:
            return None

        if action_family == FLASH_TOOL:
            code = "watch/flash-repetition"
            message = "Repeated identical flash failures detected. Disconnect and reconnect before trying again."
        elif action_family == UART_TOOL:
            code = "watch/uart-miss-repetition"
            message = "Repeated identical UART misses detected. Disconnect and reconnect before trying again."
        elif action_family == RECOVER_TOOL:
            code = "watch/recover-repetition"
            message = "Repeated identical recover failures detected. Disconnect and reconnect before trying again."
        else:
            return None

        return BlockDecision(action_family=action_family, code=code, message=message)

    def _threshold_for(self, action_family: str) -> int:
        if action_family == FLASH_TOOL:
            return 2
        if action_family == UART_TOOL:
            return 3
        if action_family == RECOVER_TOOL:
            return 2
        raise ValueError(f"Unsupported action family: {action_family}")

    def _matching_streak(
        self,
        session: SessionRecord,
        action_family: str,
        target_signature: tuple[object, ...],
    ) -> int:
        count = 0
        for event in reversed(session.events):
            if event.tool_name != action_family:
                continue
            if event.outcome_kind == ToolOutcome.SUCCESS:
                break
            signature = self._signature(event)
            if signature is None:
                continue
            if signature == target_signature:
                count += 1
                continue
            break
        return count

    def _signature(self, event: ToolEvent) -> tuple[object, ...] | None:
        if event.tool_name == FLASH_TOOL:
            if event.outcome_kind not in {ToolOutcome.FAILED, ToolOutcome.REFUSED}:
                return None
            artifact_identity = event.normalized_args.get(
                "artifact_sha256"
            ) or event.normalized_args.get("artifact_path")
            if artifact_identity is None or event.error_code is None:
                return None
            return (
                FLASH_TOOL,
                event.board_id,
                artifact_identity,
                event.error_code,
            )

        if event.tool_name == UART_TOOL:
            if event.outcome_kind != ToolOutcome.FAILED:
                return None
            if event.error_code != "uart/no-match":
                return None
            return (
                UART_TOOL,
                event.board_id,
                event.normalized_args.get("port"),
                event.normalized_args.get("baudrate"),
                event.normalized_args.get("expected_text"),
                event.error_code,
            )

        if event.tool_name == RECOVER_TOOL:
            if event.outcome_kind not in {ToolOutcome.FAILED, ToolOutcome.REFUSED}:
                return None
            if event.error_code in {None, "recover/confirmation-required"}:
                return None
            return (
                RECOVER_TOOL,
                event.board_id,
                event.error_code,
            )

        return None
