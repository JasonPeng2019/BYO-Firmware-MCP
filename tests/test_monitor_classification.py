"""Classification: the gate the whole feature stands on.

The primary criterion is that a full session of correct guarded behaviour produces
zero server-defect reports. This server refuses constantly and correctly, so a
classifier that reads refusals as defects would bury the real signal on day one.
"""

from __future__ import annotations

import unittest

from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.kernel.operations import (
    BoardBusyError,
    OperationCleanupError,
    OperationTimeoutError,
)
from pyocd_debug_mcp.monitor.classify import (
    NO_BOARD_FRAGMENT,
    Outcome,
    TriageClass,
    classify_exception,
    classify_result,
    error_code,
    error_signature,
)
from pyocd_debug_mcp.services.connections import BoardNotConnectedError
from pyocd_debug_mcp.services.session_runtime import PolicyRefusal
from pyocd_debug_mcp.target_errors import (
    LockedTargetError,
    ProbeNotFoundError,
    ReferenceArtifactError,
    SymbolLookupError,
    TargetConnectionError,
    UnsupportedArtifactError,
)


def wrap_layer2(inner: BaseException) -> BaseException:
    """Reproduce how a layer-2 tool failure reaches the dispatch funnel.

    The handler failure is wrapped by the framework and then again by the
    layer-2 response wrapper, so the real exception sits two causes deep.
    """

    try:
        try:
            try:
                raise inner
            except BaseException as first:
                raise ToolError(f"Error executing tool x: {first}") from first
        except BaseException as second:
            raise ToolError(f"{second}\nSafe exit: ...") from second
    except BaseException as top:
        return top


class RefusalsAreNotDefects(unittest.TestCase):
    """A refusal that names a remedy is not an issue."""

    def test_policy_refusal_is_never_a_defect(self) -> None:
        outcome, triage, code = classify_exception(
            PolicyRefusal("plan/gate-closed", "Run board_validate first.")
        )
        self.assertIs(outcome, Outcome.POLICY_REFUSAL)
        self.assertIs(triage, TriageClass.NONE)
        self.assertEqual(code, "plan/gate-closed")

    def test_board_busy_is_a_refusal_not_a_defect(self) -> None:
        outcome, triage, _ = classify_exception(BoardBusyError("read_memory", "b1", 5.0))
        self.assertIs(outcome, Outcome.POLICY_REFUSAL)
        self.assertIs(triage, TriageClass.NONE)

    def test_board_not_connected_names_its_remedy_so_is_a_refusal(self) -> None:
        # "Call connect(...) first" is the server telling the agent what to do.
        # Treating it as a hardware fault would fire a false environment report
        # every time an agent touches a tool before connecting.
        outcome, triage, code = classify_exception(
            BoardNotConnectedError("Board 'b' is not connected. Call connect first.")
        )
        self.assertIs(outcome, Outcome.POLICY_REFUSAL)
        self.assertIs(triage, TriageClass.NONE)
        self.assertEqual(code, "server/not-connected")

    def test_locked_handler_refusal(self) -> None:
        outcome, triage, code = classify_exception(
            ToolError("Tool 'flash_application' is locked for board 'b1'. Call 'x' first.")
        )
        self.assertIs(outcome, Outcome.POLICY_REFUSAL)
        self.assertIs(triage, TriageClass.NONE)
        self.assertEqual(code, "handler/locked")

    def test_unknown_tool_is_agent_behaviour_not_a_server_defect(self) -> None:
        outcome, triage, code = classify_exception(ToolError("Unknown tool: made_up"))
        self.assertIs(outcome, Outcome.POLICY_REFUSAL)
        self.assertIs(triage, TriageClass.AGENT_BEHAVIOR)
        self.assertEqual(code, "handler/unknown-tool")

    def test_dispatch_raised_tool_errors_are_all_refusals(self) -> None:
        for message in (
            "Guarded tool 'flash_application' requires a non-empty board_id.",
            "Tool 'read_serial' cannot accept an on_exit finalizer.",
            "on_exit finalizer is malformed",
        ):
            with self.subTest(message=message):
                outcome, triage, _ = classify_exception(ToolError(message))
                self.assertIs(outcome, Outcome.POLICY_REFUSAL)
                self.assertIsNot(triage, TriageClass.SERVER_DEFECT)


class MalformedCallsAreAgentErrorsNotDefects(unittest.TestCase):
    """The framework validates arguments before the handler runs.

    Every ordinary argument mistake an agent makes -- on any tool -- arrives as a
    validation failure. Reading those as server defects would flood the sink with
    false reports and bury the real signal.
    """

    def test_pydantic_validation_error_is_an_agent_error(self) -> None:
        from pydantic import BaseModel, ValidationError

        class Model(BaseModel):
            required: int

        try:
            Model()  # type: ignore[call-arg]
        except ValidationError as exc:
            outcome, triage, code = classify_exception(exc)
        self.assertIs(outcome, Outcome.POLICY_REFUSAL)
        self.assertIs(triage, TriageClass.AGENT_BEHAVIOR)
        self.assertEqual(code, "handler/invalid-arguments")

    def test_framework_argument_error_is_an_agent_error(self) -> None:
        outcome, triage, _ = classify_exception(
            ToolError(
                "Error executing tool report_agent_issue: 6 validation errors "
                "for report_agent_issueArguments\ngoal\n  Field required"
            )
        )
        self.assertIs(outcome, Outcome.POLICY_REFUSAL)
        self.assertIs(triage, TriageClass.AGENT_BEHAVIOR)

    def test_wrapped_validation_error_is_still_an_agent_error(self) -> None:
        from pydantic import BaseModel, ValidationError

        class Model(BaseModel):
            required: int

        try:
            Model()  # type: ignore[call-arg]
        except ValidationError as exc:
            wrapped = wrap_layer2(exc)
        outcome, triage, _ = classify_exception(wrapped)
        self.assertIs(outcome, Outcome.POLICY_REFUSAL)
        self.assertIs(triage, TriageClass.AGENT_BEHAVIOR)

    def test_a_genuine_runtime_error_is_still_a_defect(self) -> None:
        # The fix must not swallow real failures.
        outcome, triage, _ = classify_exception(RuntimeError("validation errors for x"))
        self.assertIs(outcome, Outcome.UNEXPECTED_ERROR)
        self.assertIs(triage, TriageClass.SERVER_DEFECT)


class UnexpectedFailuresAreReported(unittest.TestCase):
    def test_plain_exception_is_a_server_defect(self) -> None:
        outcome, triage, code = classify_exception(RuntimeError("boom"))
        self.assertIs(outcome, Outcome.UNEXPECTED_ERROR)
        self.assertIs(triage, TriageClass.SERVER_DEFECT)
        self.assertEqual(code, "runtime/RuntimeError")

    def test_deadline_termination_is_a_server_defect(self) -> None:
        outcome, triage, _ = classify_exception(
            OperationTimeoutError("flash_application", 120.0, board_id="b1")
        )
        self.assertIs(outcome, Outcome.UNEXPECTED_ERROR)
        self.assertIs(triage, TriageClass.SERVER_DEFECT)

    def test_cleanup_failure_is_a_server_defect(self) -> None:
        outcome, triage, _ = classify_exception(OperationCleanupError("worker not confirmed"))
        self.assertIs(outcome, Outcome.UNEXPECTED_ERROR)
        self.assertIs(triage, TriageClass.SERVER_DEFECT)

    def test_hardware_faults_are_environment_not_code(self) -> None:
        for exc in (
            ProbeNotFoundError("no probe"),
            LockedTargetError("target locked"),
            TargetConnectionError("usb reset"),
        ):
            with self.subTest(exc=type(exc).__name__):
                outcome, triage, _ = classify_exception(exc)
                self.assertIs(outcome, Outcome.UNEXPECTED_ERROR)
                self.assertIs(triage, TriageClass.ENVIRONMENT_FAULT)


class WrappedFailuresSurviveTheLayerTwoWrapper(unittest.TestCase):
    """Layer-2 tools re-raise as ToolError, burying the real failure two deep.

    Classifying only the outermost wrapper would read every provider fault,
    deadline termination, and unplugged probe in a hardware tool as a correct
    refusal, so none of them would ever be reported.
    """

    def test_wrapped_hardware_fault_is_still_environment(self) -> None:
        outcome, triage, code = classify_exception(wrap_layer2(ProbeNotFoundError("gone")))
        self.assertIs(outcome, Outcome.UNEXPECTED_ERROR)
        self.assertIs(triage, TriageClass.ENVIRONMENT_FAULT)
        self.assertEqual(code, "probe/not-found")

    def test_wrapped_runtime_error_is_still_a_defect(self) -> None:
        outcome, triage, _ = classify_exception(wrap_layer2(ValueError("bad state")))
        self.assertIs(outcome, Outcome.UNEXPECTED_ERROR)
        self.assertIs(triage, TriageClass.SERVER_DEFECT)

    def test_wrapped_timeout_is_still_a_defect(self) -> None:
        outcome, triage, _ = classify_exception(
            wrap_layer2(OperationTimeoutError("flash_application", 120.0))
        )
        self.assertIs(outcome, Outcome.UNEXPECTED_ERROR)
        self.assertIs(triage, TriageClass.SERVER_DEFECT)

    def test_wrapped_policy_refusal_is_still_a_refusal(self) -> None:
        outcome, triage, _ = classify_exception(
            wrap_layer2(PolicyRefusal("plan/expired", "Resubmit the plan."))
        )
        self.assertIs(outcome, Outcome.POLICY_REFUSAL)
        self.assertIs(triage, TriageClass.NONE)

    def test_cause_cycle_does_not_hang(self) -> None:
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a
        outcome, _, _ = classify_exception(a)
        self.assertIs(outcome, Outcome.UNEXPECTED_ERROR)


class NonErrorRefusalsAreRecognised(unittest.TestCase):
    """Refusal arrives as a structured payload as often as an exception."""

    def test_formatted_refusal_string(self) -> None:
        outcome, code, remedy = classify_result(
            "Refused [plan/gate-closed]: Validation gate is closed. "
            "Call 'board_validate' first. session_id=abc"
        )
        self.assertIs(outcome, Outcome.POLICY_REFUSAL)
        self.assertEqual(code, "plan/gate-closed")
        self.assertEqual(remedy, "call board_validate")

    def test_json_refusal_status(self) -> None:
        outcome, code, remedy = classify_result(
            '{"status": "artifact_collection_refused", "remedy": "Use explicit outputs."}'
        )
        self.assertIs(outcome, Outcome.POLICY_REFUSAL)
        self.assertEqual(code, "artifact_collection_refused")
        self.assertEqual(remedy, "Use explicit outputs.")

    def test_no_board_sentinel(self) -> None:
        outcome, code, _ = classify_result(f"{NO_BOARD_FRAGMENT} for this session.")
        self.assertIs(outcome, Outcome.POLICY_REFUSAL)
        self.assertEqual(code, "server/no-board")

    def test_ordinary_success_is_success(self) -> None:
        outcome, code, remedy = classify_result("Core state: HALTED")
        self.assertIs(outcome, Outcome.SUCCESS)
        self.assertIsNone(code)
        self.assertIsNone(remedy)

    def test_empty_result_is_success(self) -> None:
        self.assertIs(classify_result("")[0], Outcome.SUCCESS)


class SentinelStaysInSyncWithTheServer(unittest.TestCase):
    def test_no_board_fragment_matches_the_server_message(self) -> None:
        # The classifier holds its own copy to avoid a circular import, so this
        # asserts the two cannot drift apart silently.
        from pyocd_debug_mcp import server

        self.assertIn(NO_BOARD_FRAGMENT, server.NO_BOARD_CONFIG_MESSAGE)


class ErrorCodesAreUnchanged(unittest.TestCase):
    """These strings are written into durable evidence; they must not drift."""

    def test_error_code_mapping(self) -> None:
        cases = [
            (ProbeNotFoundError("x"), "probe/not-found"),
            (LockedTargetError("x"), "target/locked"),
            (TargetConnectionError("x"), "target/connection-failure"),
            (UnsupportedArtifactError("x"), "flash/unsupported-artifact"),
            (ReferenceArtifactError("x"), "flash/reference-artifact"),
            (SymbolLookupError("x"), "symbols/lookup-failure"),
            (BoardNotConnectedError("x"), "server/not-connected"),
            (KeyError("x"), "runtime/KeyError"),
        ]
        for exc, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(error_code(exc), expected)

    def test_server_delegates_to_the_shared_taxonomy(self) -> None:
        from pyocd_debug_mcp import server

        self.assertEqual(server._error_code(ProbeNotFoundError("x")), "probe/not-found")


class SignaturesGroupAcrossRuns(unittest.TestCase):
    def test_signature_normalises_volatile_detail(self) -> None:
        first = error_signature(RuntimeError("failed at 0x20001234 after 17 tries"))
        second = error_signature(RuntimeError("failed at 0xDEADBEEF after 4 tries"))
        self.assertEqual(first, second)

    def test_signature_keeps_the_exception_identity(self) -> None:
        self.assertNotEqual(
            error_signature(RuntimeError("x")), error_signature(ValueError("x"))
        )


if __name__ == "__main__":
    unittest.main()
