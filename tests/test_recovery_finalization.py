from __future__ import annotations

import asyncio
from contextlib import nullcontext
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError

from firmware_mcp import server
from firmware_mcp.adapters.debug_interface import (
    RecoveryCapability,
    RecoveryResult,
    TargetSessionHandle,
)
from firmware_mcp.services import target_control
from firmware_mcp.services.connections import ConnectionManager
from firmware_mcp.services.session_runtime import SessionRecord, ToolOutcome
from firmware_mcp.target_errors import (
    RecoveryPostDispatchError,
    RecoverySessionFinalizationError,
    TargetStateError,
)


def _capability(*, postcondition: str = "unknown") -> RecoveryCapability:
    return RecoveryCapability(
        "bank_erase",
        "erase",
        {"kind": "all_matching", "physical_kinds": ["physical_flash"]},
        "unavailable",
        postcondition,
    )


class RecoveryDispatchTests(unittest.TestCase):
    def test_returned_mechanism_mismatch_is_post_dispatch_error(self) -> None:
        handle = TargetSessionHandle(None, None, "probe", "worker", None)
        with (
            patch.object(target_control, "recovery_capabilities", return_value=(_capability(),)),
            patch.object(
                target_control._BACKEND,
                "recover",
                return_value=RecoveryResult("mass_erase", True, "unavailable", "unknown"),
            ),
        ):
            with self.assertRaises(RecoveryPostDispatchError) as raised:
                target_control.recover_target(handle, mechanism="bank_erase")

        self.assertIsNotNone(raised.exception.selected_capability)
        assert raised.exception.selected_capability is not None
        self.assertEqual(raised.exception.selected_capability.mechanism, "bank_erase")
        self.assertIsNotNone(raised.exception.result)
        assert raised.exception.result is not None
        self.assertEqual(raised.exception.result.mechanism, "mass_erase")

    def test_decode_failure_after_dispatch_retains_selected_descriptor(self) -> None:
        handle = TargetSessionHandle(None, None, "probe", "worker", None)
        protocol_failure = ValueError("malformed recovery worker result")
        with (
            patch.object(target_control, "recovery_capabilities", return_value=(_capability(),)),
            patch.object(target_control._BACKEND, "recover", side_effect=protocol_failure),
        ):
            with self.assertRaises(RecoveryPostDispatchError) as raised:
                target_control.recover_target(handle, mechanism="bank_erase")

        self.assertIs(raised.exception.cause, protocol_failure)
        self.assertIsNotNone(raised.exception.selected_capability)
        assert raised.exception.selected_capability is not None
        self.assertEqual(raised.exception.selected_capability.mechanism, "bank_erase")
        self.assertIsNone(raised.exception.result)


class RecoveryFinalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConnectionManager()
        self.old_handle = TargetSessionHandle(None, None, "probe-one", "worker-one", None)
        self.old_runtime = SimpleNamespace(session_id="old-runtime")
        self.old = self.manager.assign(
            "board", self.old_handle, cast(SessionRecord, self.old_runtime)
        )
        self.other_handle = TargetSessionHandle(None, None, "probe-two", "worker-two", None)
        self.other = self.manager.assign(
            "other", self.other_handle, cast(SessionRecord, SimpleNamespace(session_id="other"))
        )
        self.events: list[dict[str, object]] = []
        self.close = Mock(return_value={"termination": "proven"})
        self.runtime_close = Mock()
        self.invalidate = Mock()

    def _run(
        self,
        dispatch: object | None = None,
        *,
        error: BaseException | None = None,
        bind: object | None = None,
        bind_error: BaseException | None = None,
    ) -> object:
        recover = Mock(return_value=dispatch)
        if error is not None:
            recover.side_effect = error
        binding = (
            Mock(side_effect=bind_error)
            if bind_error is not None
            else Mock(return_value=bind or {"fresh": True})
        )
        with (
            patch.object(server, "connection_manager", self.manager),
            patch.object(server.target_control, "recover_target", recover),
            patch.object(server.target_control, "close_session", self.close),
            patch.object(server._session_store, "close_session", self.runtime_close),
            patch.object(server._guard_core, "invalidate_board", self.invalidate),
            patch.object(server.assignment_store, "clear_board"),
            patch.object(
                server,
                "_record_event",
                side_effect=lambda *_args, **kwargs: self.events.append(kwargs),
            ),
            patch.object(server._safety_authority, "binding", binding),
        ):
            return server._recover_target("board", "bank_erase")

    def test_descriptor_invalidated_overrides_preserved_result_and_evicts_exact_old_connection(
        self,
    ) -> None:
        dispatch = target_control.RecoveryDispatch(
            _capability(postcondition="invalidated"),
            RecoveryResult("bank_erase", True, "unavailable", "preserved"),
        )
        with self.assertRaises(RecoverySessionFinalizationError) as raised:
            self._run(dispatch)

        self.assertIn("declares the session invalidated", str(raised.exception))
        self.assertIsNone(self.manager.maybe_connection("board"))
        self.assertIs(self.manager.maybe_connection("other"), self.other)
        self.close.assert_called_once_with(self.old_handle)
        details = cast(dict[str, object], self.events[-1]["details"])
        selected = cast(dict[str, object], details["selected_capability"])
        cleanup = cast(dict[str, object], details["cleanup"])
        self.assertEqual(selected["session_postcondition"], "invalidated")
        self.assertEqual(cleanup["routing_removal"], "removed")

    def test_unknown_accepted_recovery_keeps_acceptance_visible_when_authority_or_close_is_uncertain(
        self,
    ) -> None:
        dispatch = target_control.RecoveryDispatch(
            _capability(), RecoveryResult("bank_erase", True, "unavailable", "unknown")
        )
        self.invalidate.side_effect = RuntimeError("permission publication failed")
        self.close.side_effect = OSError("worker close failed")
        with self.assertRaises(RecoverySessionFinalizationError) as raised:
            self._run(dispatch)

        evidence = cast(dict[str, object], raised.exception.evidence)
        cleanup = cast(dict[str, object], evidence["cleanup"])
        self.assertTrue(evidence["accepted"])
        self.assertEqual(cleanup["routing_removal"], "removed")
        self.assertEqual(cleanup["authority_invalidation"], "uncertain")
        self.assertEqual(cleanup["provider_close"], "uncertain")
        self.assertIsNone(self.manager.maybe_connection("board"))
        self.assertIs(self.manager.maybe_connection("other"), self.other)
        outcome_kind = cast(ToolOutcome, self.events[-1]["outcome_kind"])
        self.assertEqual(outcome_kind.value, "failed")

    def test_registered_handler_serializes_provider_and_cleanup_evidence(self) -> None:
        dispatch = target_control.RecoveryDispatch(
            _capability(), RecoveryResult("bank_erase", True, "unavailable", "unknown")
        )
        self.invalidate.side_effect = RuntimeError("permission publication failed")
        self.close.side_effect = OSError("worker close failed")
        tool = server.mcp._tool_manager.get_tool("recover_target")
        assert tool is not None
        with (
            patch.object(server, "connection_manager", self.manager),
            patch.object(server.target_control, "recover_target", return_value=dispatch),
            patch.object(server.target_control, "close_session", self.close),
            patch.object(server._session_store, "close_session", self.runtime_close),
            patch.object(server._guard_core, "execute"),
            patch.object(server._guard_core, "clear_execution_files"),
            patch.object(server._guard_core, "invalidate_board", self.invalidate),
            patch.object(server.assignment_store, "clear_board"),
            patch.object(server, "_record_event"),
            patch.object(server, "safety_publication_lock", return_value=nullcontext()),
        ):
            with self.assertRaises(ToolError) as raised:
                asyncio.run(
                    tool.run({"board_id": "board", "mechanism": "bank_erase", "plan_id": "plan"})
                )

        message = str(raised.exception)
        for expected in (
            "mechanism='bank_erase'",
            "effect='erase'",
            "provider_accepted=True",
            "effect_verification='unavailable'",
            "observed_session_postcondition='unknown'",
            "primary_finalization=none",
            "routing_removal='removed'",
            "authority_invalidation='uncertain'",
            "provider_close='uncertain'",
            "Reconnect, inspect the provider outcome",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, message)
        self.assertIsNone(self.manager.maybe_connection("board"))
        self.assertIs(self.manager.maybe_connection("other"), self.other)

    def test_post_dispatch_protocol_failure_evicts_old_connection_and_preserves_primary(
        self,
    ) -> None:
        protocol = ValueError("bad worker recovery schema")
        error = RecoveryPostDispatchError(_capability(), None, protocol)
        self.close.side_effect = OSError("provider close failed")
        with self.assertRaises(RecoverySessionFinalizationError) as raised:
            self._run(error=error)

        self.assertIs(raised.exception.primary, error)
        self.assertIsNone(self.manager.maybe_connection("board"))
        self.assertIs(self.manager.maybe_connection("other"), self.other)
        details = cast(dict[str, object], self.events[-1]["details"])
        post_dispatch = cast(dict[str, object], details["post_dispatch_error"])
        self.assertEqual(post_dispatch["type"], "RecoveryPostDispatchError")
        cleanup = cast(dict[str, object], raised.exception.evidence["cleanup"])
        self.assertEqual(cleanup["provider_close"], "uncertain")

    def test_returned_different_mechanism_finalizes_exact_old_connection(self) -> None:
        returned = RecoveryResult("mass_erase", True, "unavailable", "preserved")
        error = RecoveryPostDispatchError(
            _capability(postcondition="invalidated"),
            returned,
            TargetStateError(
                "recovery provider returned mechanism 'mass_erase' after 'bank_erase' dispatch"
            ),
        )

        with self.assertRaises(RecoverySessionFinalizationError) as raised:
            self._run(error=error)

        self.assertIs(raised.exception.primary, error)
        self.assertIsNone(self.manager.maybe_connection("board"))
        self.assertIs(self.manager.maybe_connection("other"), self.other)
        details = cast(dict[str, object], self.events[-1]["details"])
        selected = cast(dict[str, object], details["selected_capability"])
        provider_result = cast(dict[str, object], details["provider_result"])
        self.assertEqual(selected["mechanism"], "bank_erase")
        self.assertEqual(provider_result["mechanism"], "mass_erase")
        self.assertIn("primary_finalization=RecoveryPostDispatchError", str(raised.exception))

    def test_fresh_observed_preserved_session_retains_the_exact_connection(self) -> None:
        dispatch = target_control.RecoveryDispatch(
            _capability(postcondition="preserved"),
            RecoveryResult("bank_erase", True, "matched", "preserved"),
        )
        result = self._run(dispatch)

        self.assertIn("freshly re-observed", str(result))
        self.assertIs(self.manager.maybe_connection("board"), self.old)
        self.close.assert_not_called()
        self.invalidate.assert_not_called()

    def test_unknown_descriptor_with_failed_freshness_proof_evicts(self) -> None:
        dispatch = target_control.RecoveryDispatch(
            _capability(), RecoveryResult("bank_erase", True, "matched", "preserved")
        )
        with self.assertRaises(RecoverySessionFinalizationError) as raised:
            self._run(dispatch, bind_error=RuntimeError("stale map"))

        self.assertIn("could not be re-observed", str(raised.exception))
        self.assertIsNone(self.manager.maybe_connection("board"))
        self.assertIs(self.manager.maybe_connection("other"), self.other)
        self.close.assert_called_once_with(self.old_handle)

    def test_unknown_descriptor_with_observed_preserved_session_retains_after_fresh_binding(
        self,
    ) -> None:
        dispatch = target_control.RecoveryDispatch(
            _capability(), RecoveryResult("bank_erase", True, "matched", "preserved")
        )

        result = self._run(dispatch, bind={"fresh": True})

        self.assertIn("freshly re-observed", str(result))
        self.assertIs(self.manager.maybe_connection("board"), self.old)
        self.assertIs(self.manager.maybe_connection("other"), self.other)
        self.close.assert_not_called()
        self.invalidate.assert_not_called()

    def test_pre_dispatch_unsupported_capability_keeps_connection_routable(self) -> None:
        unsupported = RuntimeError("mechanism unavailable")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self._run(error=unsupported)

        self.assertIs(self.manager.maybe_connection("board"), self.old)
        self.close.assert_not_called()
        self.invalidate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
