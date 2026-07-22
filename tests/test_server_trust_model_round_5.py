"""Round-five regressions for removal of convergence retry lockouts."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pyocd_debug_mcp.services.session_runtime import (
    ActionContext,
    PolicyRefusal,
    SessionRecord,
    ToolOutcome,
)
from pyocd_debug_mcp.services.uart_capture import UARTCaptureResult, UARTExchangeResult
from pyocd_debug_mcp.tools.flash import FlashToolServices, build_flash_handlers
from pyocd_debug_mcp.tools.serial import (
    SerialToolServices,
    read_serial,
    serial_exchange,
    write_serial,
)


class ConvergenceLockoutRemovalTests(unittest.TestCase):
    @staticmethod
    def _runtime() -> SessionRecord:
        root = Path("test-run")
        return SessionRecord(
            session_id="session",
            board_id="board",
            connection_id="connection",
            probe_uid="probe",
            route_used="test-route",
            created_at="now",
            run_root=root,
            log_path=root / "events.jsonl",
            summary_path=root / "session.json",
        )

    def test_repeated_flash_refusals_and_failures_reach_resolver_or_backend_and_log_every_attempt(
        self,
    ) -> None:
        refusal_events: list[dict[str, Any]] = []
        refusal_resolver_calls = 0
        runtime = self._runtime()

        def refuse_resolve(*_args: object) -> object:
            nonlocal refusal_resolver_calls
            refusal_resolver_calls += 1
            raise PolicyRefusal("flash/invalid-artifact", "artifact remains invalid")

        refusal_services = FlashToolServices(
            runtime_for=lambda _board: runtime,
            active_session_id=lambda _board: "session",
            duration_ms=lambda _started: 1,
            record_event=lambda *args, **kwargs: refusal_events.append(kwargs),
            format_refusal=lambda refusal, **_kwargs: f"Refused [{refusal.code}]",
            action_context=lambda tool, board: ActionContext("test", tool, board),
            maybe_handle_for=lambda _board: None,
            handle_for=lambda _board: object(),
            resolve_request=refuse_resolve,
            flash_target=lambda *_args: Path("unused"),
            error_code=lambda _exc: "flash/backend",
        )
        refusal_handler = build_flash_handlers(refusal_services)["flash_application"]
        for _ in range(3):
            self.assertIn("flash/invalid-artifact", refusal_handler("board", "same.elf"))
        self.assertEqual(refusal_resolver_calls, 3)
        self.assertEqual(
            [event["outcome_kind"] for event in refusal_events],
            [ToolOutcome.REFUSED, ToolOutcome.REFUSED, ToolOutcome.REFUSED],
        )

        failure_events: list[dict[str, Any]] = []
        backend_calls = 0
        runtime = self._runtime()
        request = SimpleNamespace(
            artifact_path=Path("same.elf"),
            identity=SimpleNamespace(as_log_fields=lambda: {"artifact_sha256": "same"}),
        )

        def fail_backend(*_args: object) -> Path:
            nonlocal backend_calls
            backend_calls += 1
            raise RuntimeError("programmer backend failed")

        failure_services = FlashToolServices(
            runtime_for=lambda _board: runtime,
            active_session_id=lambda _board: "session",
            duration_ms=lambda _started: 1,
            record_event=lambda *args, **kwargs: failure_events.append(kwargs),
            format_refusal=lambda refusal, **_kwargs: str(refusal),
            action_context=lambda tool, board: ActionContext("test", tool, board),
            maybe_handle_for=lambda _board: None,
            handle_for=lambda _board: object(),
            resolve_request=lambda *_args: request,
            flash_target=fail_backend,
            error_code=lambda _exc: "flash/backend-failed",
        )
        failure_handler = build_flash_handlers(failure_services)["flash_application"]
        for _ in range(3):
            with self.assertRaisesRegex(RuntimeError, "programmer backend failed"):
                failure_handler("board", "same.elf")
        self.assertEqual(backend_calls, 3)
        self.assertEqual(
            [event["outcome_kind"] for event in failure_events],
            [ToolOutcome.FAILED, ToolOutcome.FAILED, ToolOutcome.FAILED],
        )

    def test_repeated_uart_misses_do_not_block_later_attempts_and_refusals_still_log(self) -> None:
        events: list[dict[str, Any]] = []
        capture_calls = 0
        write_calls = 0
        exchange_calls = 0
        runtime = self._runtime()
        handle = SimpleNamespace(
            board=SimpleNamespace(default_baudrate=115200),
            metadata=SimpleNamespace(route_used="test-route"),
        )

        def capture(*_args: object, **_kwargs: object) -> UARTCaptureResult:
            nonlocal capture_calls
            capture_calls += 1
            return UARTCaptureResult("no output", "marker", 0, 0.1)

        def write(*_args: object, **_kwargs: object) -> object:
            nonlocal write_calls
            write_calls += 1
            return SimpleNamespace(bytes_written=2, duration_seconds=0.1)

        def exchange(*_args: object, **_kwargs: object) -> UARTExchangeResult:
            nonlocal exchange_calls
            exchange_calls += 1
            return UARTExchangeResult("ok", "ok", 2, 0.1, 1)

        services = SerialToolServices(
            runtime_for=lambda _board: runtime,
            active_session_id=lambda _board: "session",
            duration_ms=lambda _started: 1,
            record_event=lambda *args, **kwargs: events.append(kwargs),
            format_refusal=lambda refusal, **_kwargs: f"Refused [{refusal.code}]",
            handle_for=lambda _board: handle,
            resolve_port=lambda _handle, **_kwargs: SimpleNamespace(device="COM_TEST"),
            capture_uart=capture,
            write_uart=write,
            exchange_uart=exchange,
            reset_target=lambda _handle: None,
            no_board_config_message="no board",
        )
        for _ in range(4):
            self.assertIn("did not match", read_serial(services, "board", "marker"))
        self.assertEqual(capture_calls, 4)
        self.assertIn("wrote", write_serial(services, "board", "go"))
        self.assertIn(
            "matched",
            serial_exchange(
                services,
                "board",
                [{"text": "go", "expected_text": "ok", "line_ending": "lf"}],
                read_seconds=1.0,
            ),
        )
        self.assertEqual((write_calls, exchange_calls), (1, 1))
        self.assertIn("uart/invalid-baudrate", read_serial(services, "board", baudrate=0))
        self.assertEqual(capture_calls, 4)
        self.assertEqual(len(events), 7)
        self.assertEqual(
            [event["outcome_kind"] for event in events[:4]],
            [ToolOutcome.FAILED] * 4,
        )
        self.assertEqual(events[-1]["outcome_kind"], ToolOutcome.REFUSED)
