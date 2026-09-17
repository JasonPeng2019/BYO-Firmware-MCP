"""AT03/AT05: raw families are backend-limited, public-response shaped, and tiered."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.capabilities.routing import TierRouteRefusal, TierRouter
from pyocd_debug_mcp.tools.raw import RawToolServices, build_raw_handlers

from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    assert_lite_raw_warning,
    confirmed_lite_policy,
    isolated_project,
    known_good_full_policy,
    public_call,
    tiered_test_server,
)


class _RawBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def handle(self, board_id: str) -> str:
        self.calls.append(("handle", board_id))
        return f"handle:{board_id}"

    def read_memory(self, handle: object, address: int, width: int) -> int:
        self.calls.append(("read_memory", handle, address, width))
        return 0x12345678

    def read_block(self, handle: object, address: int, length: int) -> list[int]:
        self.calls.append(("read_block", handle, address, length))
        return [index & 0xFF for index in range(length)]

    def write_memory(self, handle: object, address: int, value: int, width: int) -> None:
        self.calls.append(("write_memory", handle, address, value, width))

    def write_register(self, handle: object, name: str, value: int) -> None:
        self.calls.append(("write_register", handle, name, value))

    def breakpoint(self, handle: object, address: int) -> None:
        self.calls.append(("set_breakpoint", handle, address))

    def reset(self, handle: object, halt_after: bool) -> None:
        self.calls.append(("reset", handle, halt_after))

    def flash(self, handle: object, artifact: Path) -> tuple[Path, str]:
        self.calls.append(("flash", handle, artifact))
        return artifact, "halted"

    def recover(self, handle: object, mechanism: str) -> str:
        self.calls.append(("recover", handle, mechanism))
        return f"recovered:{mechanism}"


class TieredRawAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._project = isolated_project()
        self.root = self._project.__enter__()
        self.addCleanup(self._project.__exit__, None, None, None)
        repository = CapabilityPolicyRepository(self.root)
        repository.resolve("raw_board")
        repository.commit(
            "lite_board",
            Tier.SETUP_LITE,
            map_snapshot=confirmed_lite_policy("lite_board"),
        )
        full_profile, full_map = known_good_full_policy()
        repository.commit(
            "legacy_full",
            Tier.SETUP_FULL,
            profile_snapshot=full_profile,
            map_snapshot=full_map,
        )
        self.backend = _RawBackend()
        self.global_stops = 0

        def global_stop() -> None:
            self.global_stops += 1

        self.handlers = build_raw_handlers(
            RawToolServices(
                router=TierRouter(repository),
                global_stop=global_stop,
                handle_for=self.backend.handle,
                read_memory=self.backend.read_memory,
                read_block=self.backend.read_block,
                write_memory=self.backend.write_memory,
                write_register=self.backend.write_register,
                set_breakpoint=self.backend.breakpoint,
                reset=self.backend.reset,
                flash=self.backend.flash,
                recover=self.backend.recover,
                capture_uart=lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError("not used")
                ),
                write_uart=lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError("not used")
                ),
                exchange_uart=lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError("not used")
                ),
            )
        )

    def test_public_raw_inventory_is_complete(self) -> None:
        """AT05: every frozen raw tool is registered by the raw route builder."""

        self.assertEqual(
            set(self.handlers),
            {
                "read_memory_raw",
                "write_memory_raw",
                "register_write_raw",
                "write_cpu_register_raw",
                "set_execution_state_raw",
                "set_breakpoint_raw",
                "reset_and_halt_raw",
                "flash_raw",
                "read_serial_raw",
                "write_serial_raw",
                "serial_exchange_raw",
                "target_unlock_raw",
            },
        )

    def test_no_setup_raw_read_needs_no_map_plan_or_identity_and_returns_no_lite_warning(
        self,
    ) -> None:
        """AT03: a named fresh board reaches an actual raw backend primitive."""

        payload = json.loads(self.handlers["read_memory_raw"]("raw_board", "0x20000000"))
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["tier"], "no-setup")
        self.assertEqual(payload["result"], "0x12345678")
        self.assertNotIn("warning", payload)
        self.assertEqual(
            self.backend.calls,
            [("handle", "raw_board"), ("read_memory", "handle:raw_board", 0x20000000, 32)],
        )
        self.assertEqual(self.global_stops, 1)

    def test_lite_raw_success_and_failure_both_carry_the_human_display_warning(self) -> None:
        """AT05/AT08: raw lite bypass instruction survives a backend/argument error."""

        success = json.loads(self.handlers["read_memory_raw"]("lite_board", 0x20000000))
        assert_lite_raw_warning(self, success, "read_memory_raw")
        self.assertEqual(success["status"], "ok")

        before = list(self.backend.calls)
        failed = json.loads(self.handlers["read_memory_raw"]("lite_board", 0x20000000, length=4097))
        self.assertEqual(failed["status"], "error")
        assert_lite_raw_warning(self, failed, "read_memory_raw")
        self.assertEqual(
            self.backend.calls, before, "invalid raw request must not reach backend I/O"
        )

    def test_full_raw_refusal_happens_before_global_stop_or_backend(self) -> None:
        """AT05: full safe policy cannot be bypassed by any direct raw handler."""

        with self.assertRaises(TierRouteRefusal) as raised:
            self.handlers["write_memory_raw"]("legacy_full", 0x20000000, 1)
        self.assertEqual(raised.exception.code, "tier/wrong-route")
        self.assertEqual(self.global_stops, 0)
        self.assertEqual(self.backend.calls, [])

    def test_raw_read_cap_is_exactly_4096_bytes(self) -> None:
        """AT05: raw remains bounded even where containment deliberately does not apply."""

        accepted = json.loads(self.handlers["read_memory_raw"]("raw_board", 0, length=4096))
        self.assertEqual(accepted["status"], "ok")
        before = list(self.backend.calls)
        refused = json.loads(self.handlers["read_memory_raw"]("raw_board", 0, length=4097))
        self.assertEqual(refused["status"], "error")
        self.assertEqual(self.backend.calls, before)

    def test_raw_flash_reaches_the_backend_without_a_safety_map_but_keeps_the_file_limit(
        self,
    ) -> None:
        """AT05/C04: raw flash has backend geometry limits, not map containment."""

        artifact = Path(__file__).resolve()
        payload = json.loads(self.handlers["flash_raw"]("raw_board", str(artifact)))
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["tier"], "no-setup")
        self.assertEqual(payload["result"]["artifact"], str(artifact))
        self.assertIn(("flash", "handle:raw_board", artifact), self.backend.calls)

    def test_public_no_profile_connect_auto_resolution_then_raw_read_uses_the_real_mcp_dispatch(
        self,
    ) -> None:
        """AT03: discovery assignment/connection/raw operation is not only a private helper test."""

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(
                backend=backend,
                auto_target_resolver=lambda board_id: (
                    "nrf52840" if board_id == "fresh_board" else None
                ),
            )
            # ``connect`` retains its historical text response, while the new
            # raw tool below has the frozen JSON envelope.
            import asyncio

            connection = asyncio.run(server.mcp.call_tool("connect", {"board_id": "fresh_board"}))
            self.assertTrue(connection)
            payload = public_call(
                server,
                "read_memory_raw",
                {"board_id": "fresh_board", "address": "0x20000000"},
            )
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["tier"], "no-setup")
            self.assertNotIn("warning", payload)
            opened = next(call for call in backend.calls if call[0] == "open")
            self.assertEqual(opened[1]["target"], "nrf52840")
            self.assertTrue(any(call[0] == "read_memory" for call in backend.calls))

    def test_public_connect_uses_explicit_target_fallback_when_auto_resolution_is_unavailable(
        self,
    ) -> None:
        """AT03: caller-supplied target fallback is accepted only for no-setup."""

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(
                backend=backend, auto_target_resolver=lambda _board: None
            )
            import asyncio

            asyncio.run(
                server.mcp.call_tool(
                    "connect",
                    {
                        "board_id": "fallback_board",
                        "probe_uid": "fake-probe",
                        "target": "stm32l476rg",
                    },
                )
            )
            opened = next(call for call in backend.calls if call[0] == "open")
            self.assertEqual(opened[1]["unique_id"], "fake-probe")
            self.assertEqual(opened[1]["target"], "stm32l476rg")
            payload = public_call(
                server,
                "read_memory_raw",
                {"board_id": "fallback_board", "address": 0x20000000},
            )
            self.assertEqual(payload["status"], "ok")

    def test_public_no_profile_connect_reports_inaccessible_and_unsupported_target_failures(
        self,
    ) -> None:
        """AT03: raw eligibility cannot hide an unreachable probe or bad target."""

        import asyncio

        cases = (
            (
                "inaccessible_board",
                DeterministicTargetBackend(open_error=OSError("fixture probe is inaccessible")),
                lambda _board: "nrf52840",
                "inaccessible",
            ),
            (
                "unsupported_board",
                DeterministicTargetBackend(supported_targets=frozenset({"nrf52840"})),
                lambda _board: "not-a-pyocd-target",
                "unsupported target",
            ),
        )
        for board, backend, resolver, message in cases:
            with (
                self.subTest(board=board),
                isolated_project() as root,
                tiered_test_server(root) as server,
            ):
                server.configure_tiered_test_seams(backend=backend, auto_target_resolver=resolver)
                with self.assertRaisesRegex(Exception, message):
                    asyncio.run(server.mcp.call_tool("connect", {"board_id": board}))
                self.assertEqual(backend.calls[0][0], "open")
                raw = public_call(
                    server,
                    "read_memory_raw",
                    {"board_id": board, "address": 0x20000000},
                )
                self.assertNotEqual(raw["status"], "ok", raw)
                self.assertFalse(
                    any(call[0] == "read_memory" for call in backend.calls),
                    "a failed public connection must not expose raw backend I/O",
                )

    def test_public_global_stop_refuses_raw_before_the_connected_backend_is_touched(self) -> None:
        """AT03/AT08: monitor stop remains authoritative at public raw dispatch."""

        class _StoppedMonitor:
            def check_block(self) -> None:
                raise RuntimeError("fixture global hardware stop")

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(
                backend=backend, auto_target_resolver=lambda _board: "nrf52840"
            )
            import asyncio

            asyncio.run(server.mcp.call_tool("connect", {"board_id": "raw_board"}))
            before = list(backend.calls)
            with patch.object(server, "_monitor", _StoppedMonitor()):
                payload = public_call(
                    server,
                    "read_memory_raw",
                    {"board_id": "raw_board", "address": 0x20000000},
                )
            self.assertNotEqual(payload["status"], "ok", payload)
            self.assertIn("fixture global hardware stop", payload["message"])
            self.assertEqual(
                backend.calls,
                before,
                "the monitor refusal must happen before public raw backend I/O",
            )

    def test_tiered_server_context_restores_the_exact_backend_after_fixture_failure(self) -> None:
        """TEST isolation: a failed tiered fixture cannot poison later baseline workers."""

        from pyocd_debug_mcp.services import target_control

        original_backend = target_control._BACKEND  # noqa: SLF001 - isolation oracle
        fake_backend = DeterministicTargetBackend()
        with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
            with isolated_project() as root, tiered_test_server(root) as server:
                server.configure_tiered_test_seams(backend=fake_backend)
                self.assertIs(target_control._BACKEND, fake_backend)  # noqa: SLF001
                raise RuntimeError("fixture interruption")

        self.assertIs(target_control._BACKEND, original_backend)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
