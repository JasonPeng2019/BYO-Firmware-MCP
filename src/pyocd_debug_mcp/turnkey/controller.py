"""Turnkey middleman state machine owned by Server A."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, cast

from pyocd_debug_mcp.turnkey.contracts import (
    MiddlemanDecision,
    TurnkeyContext,
    TurnkeyContractError,
)
from pyocd_debug_mcp.turnkey.green_check import GreenCheckError, GreenCheckResult, GreenCheckRunner
from pyocd_debug_mcp.turnkey.prompts import delta_prompt, init_prompt, schema_rejection
from pyocd_debug_mcp.turnkey.provider import (
    MiddlemanFactory,
    MiddlemanSession,
    ProviderError,
    ProviderTerminationError,
)

FIXED_WORKFLOWS: Final[dict[str, tuple[str, ...]]] = {
    "bug_fix": (
        "Diagnose and reproduce the reported failure with concrete evidence.",
        "Locate and prove the root cause in the workspace or live target.",
        "Patch the smallest correct implementation.",
        "Rebuild with the provided native build context.",
        "Flash through guarded Server B after its plan and permission requirements.",
    ),
    "complex_implementation": (
        "Understand the requirement and acceptance conditions in the workspace.",
        "Implement the smallest complete feature.",
        "Rebuild with the provided native build context.",
        "Flash through guarded Server B after its plan and permission requirements.",
    ),
}


class CallArtifactCleanupError(RuntimeError):
    """Server A could not uphold its per-call document-deletion guarantee."""


def _remove_call_artifacts(artifact_root: Path) -> None:
    try:
        shutil.rmtree(artifact_root)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CallArtifactCleanupError(
            f"could not delete call-owned artifacts at {artifact_root}: {exc}"
        ) from exc
    if artifact_root.exists():  # pragma: no cover - defensive check for unusual filesystems
        raise CallArtifactCleanupError(
            f"call-owned artifacts still exist after cleanup: {artifact_root}"
        )


@dataclass(frozen=True, slots=True)
class TurnkeyResult:
    status: str
    message: str
    iterations_used: int
    step_index: int
    workflow_complete: bool
    green_check: GreenCheckResult | None = None
    user_text: str | None = None
    last_result: str = ""
    failed_strategies: tuple[str, ...] = ()
    carry_forward_warnings: tuple[str, ...] = ()

    def to_document(self) -> dict[str, object]:
        return {
            "status": self.status,
            "message": self.message,
            "iterations_used": self.iterations_used,
            "step_index": self.step_index,
            "workflow_complete": self.workflow_complete,
            "green_check": asdict(self.green_check) if self.green_check is not None else None,
            "user_text": self.user_text,
            "last_result": self.last_result,
            "failed_strategies": list(self.failed_strategies),
            "carry_forward_warnings": list(self.carry_forward_warnings),
        }


class TurnkeyController:
    """Run one fresh, bounded middleman session for one agentic tool call."""

    def __init__(
        self,
        provider_factory: MiddlemanFactory,
        *,
        green_checks: GreenCheckRunner | None = None,
        response_timeout_seconds: float = 120.0,
        green_check_timeout_seconds: float = 300.0,
    ) -> None:
        if response_timeout_seconds <= 0 or green_check_timeout_seconds <= 0:
            raise ValueError("turnkey timeouts must be positive")
        self.provider_factory = provider_factory
        self.green_checks = green_checks or GreenCheckRunner()
        self.response_timeout_seconds = response_timeout_seconds
        self.green_check_timeout_seconds = green_check_timeout_seconds

    def run(
        self,
        *,
        tool_name: str,
        context: TurnkeyContext,
        workspace: Path,
        server_b_url: str,
        steps: tuple[str, ...] | None = None,
        start_step_index: int = 0,
        continue_instruction: str | None = None,
        prior_last_result: str | None = None,
        prior_failed_strategies: tuple[str, ...] = (),
        prior_carry_forward_warnings: tuple[str, ...] = (),
        prior_workflow_complete: bool = False,
        relay_text: Callable[[str], bool] | None = None,
        session_observer: Callable[[MiddlemanSession | None], None] | None = None,
    ) -> TurnkeyResult:
        workflow = steps or FIXED_WORKFLOWS.get(tool_name)
        if not workflow:
            raise TurnkeyContractError(f"no workflow is defined for {tool_name}")
        if not 0 <= start_step_index < len(workflow):
            raise TurnkeyContractError("continuation step is outside the workflow")
        remaining_steps = 0 if prior_workflow_complete else len(workflow) - start_step_index
        minimum_iterations = remaining_steps + 3
        if context.iteration_max < minimum_iterations:
            raise TurnkeyContractError(
                f"iteration_max={context.iteration_max} cannot complete the remaining workflow; "
                f"at least {minimum_iterations} decisions are required"
            )
        session = None
        artifact_root = Path(tempfile.mkdtemp(prefix=".turnkey-call-", dir=workspace))
        try:
            trusted_script_root = Path(tempfile.mkdtemp(prefix="byo-green-check-"))
        except BaseException:
            _remove_call_artifacts(artifact_root)
            raise
        green_script = trusted_script_root / context.green_check_script.filename
        green_script_bytes = context.green_check_script.content.encode("utf-8")
        expected_script_sha256 = hashlib.sha256(green_script_bytes).hexdigest()
        try:
            green_script.write_bytes(green_script_bytes)
        except BaseException:
            _remove_call_artifacts(artifact_root)
            _remove_call_artifacts(trusted_script_root)
            raise
        step_index = start_step_index
        workflow_complete = prior_workflow_complete
        green_result: GreenCheckResult | None = None
        green_requested = False
        failed_strategies = prior_failed_strategies
        carry_forward_warnings = prior_carry_forward_warnings
        last_result = prior_last_result or (
            f"Client A continuation instruction: {continue_instruction}"
            if continue_instruction
            else "Initialization complete. Work the current step."
        )
        prompt = init_prompt(
            tool_name,
            context,
            workflow,
            artifact_root,
            start_step_index=step_index,
            continuation=continue_instruction,
            prior_last_result=prior_last_result,
            failed_strategies=failed_strategies,
            carry_forward_warnings=carry_forward_warnings,
        )

        def outcome(
            status: str,
            message: str,
            iterations_used: int,
            *,
            user_text: str | None = None,
        ) -> TurnkeyResult:
            return TurnkeyResult(
                status=status,
                message=message,
                iterations_used=iterations_used,
                step_index=step_index,
                workflow_complete=workflow_complete,
                green_check=green_result,
                user_text=user_text,
                last_result=last_result,
                failed_strategies=failed_strategies,
                carry_forward_warnings=carry_forward_warnings,
            )
        try:
            session = self.provider_factory.open(
                workspace=workspace,
                server_b_url=server_b_url,
                artifact_root=artifact_root,
            )
            if session_observer is not None:
                session_observer(session)
            for iteration in range(1, context.iteration_max + 1):
                try:
                    raw = session.exchange(prompt, timeout_seconds=self.response_timeout_seconds)
                    decision = MiddlemanDecision.parse(raw)
                    if decision.failed_strategies[: len(failed_strategies)] != failed_strategies:
                        raise TurnkeyContractError(
                            "failed_strategies must carry the complete prior ordered list forward"
                        )
                    if (
                        decision.carry_forward_warnings[: len(carry_forward_warnings)]
                        != carry_forward_warnings
                    ):
                        raise TurnkeyContractError(
                            "carry_forward_warnings must carry the complete prior ordered list "
                            "forward"
                        )
                except TurnkeyContractError as exc:
                    last_result = f"Middleman response was rejected: {exc}"
                    prompt = schema_rejection(str(exc), context.iteration_max - iteration)
                    continue

                failed_strategies = decision.failed_strategies
                carry_forward_warnings = decision.carry_forward_warnings

                action = decision.action
                if action == "next_step":
                    green_result = None
                    green_requested = False
                    if workflow_complete:
                        last_result = (
                            "The workflow is already complete. Request and validate the green "
                            "check before finish_task."
                        )
                    elif step_index >= len(workflow) - 1:
                        workflow_complete = True
                        last_result = (
                            "The final workflow step is complete. Concrete evidence: "
                            + decision.observation_summary
                            + ". Request and validate the green check before finish_task."
                        )
                    else:
                        step_index += 1
                        last_result = (
                            "next_step accepted. Concrete evidence: "
                            + decision.observation_summary
                        )
                elif action == "continue_step":
                    green_result = None
                    green_requested = False
                    if workflow_complete:
                        last_result = (
                            "continue_step rejected: the workflow is complete; request the green "
                            "check or report a terminal failure."
                        )
                    else:
                        last_result = "continue_step accepted: " + decision.observation_summary
                elif action == "return_text_to_user":
                    text = str(decision.action_params["text"])
                    if relay_text is not None:
                        try:
                            relayed = relay_text(text)
                        except Exception:
                            relayed = False
                        if relayed:
                            last_result = (
                                "return_text_to_user was surfaced to Client A; continue this step."
                            )
                        else:
                            last_result = "return_text_to_user requested this exact relay: " + text
                            return outcome(
                                "user_text_required",
                                "Relay user_text to the user exactly, then resume this same tool "
                                "and task with continue_instruction after the user responds.",
                                iteration,
                                user_text=text,
                            )
                    else:
                        last_result = "return_text_to_user requested this exact relay: " + text
                        return outcome(
                            "user_text_required",
                            "Relay user_text to the user exactly, then resume this same tool and "
                            "task with continue_instruction after the user responds.",
                            iteration,
                            user_text=text,
                        )
                elif action == "request_green_check":
                    if not workflow_complete:
                        last_result = (
                            "request_green_check rejected: complete the current and remaining "
                            "workflow steps with next_step first."
                        )
                    else:
                        green_requested = True
                        last_result = (
                            "Green-check instructions: "
                            + context.green_check_guide
                            + ". Prepare exactly that evidence, then call validate_green_check."
                        )
                elif action == "validate_green_check":
                    if not workflow_complete or not green_requested:
                        last_result = (
                            "validate_green_check rejected: complete every workflow step and call "
                            "request_green_check first."
                        )
                        prompt = delta_prompt(
                            tool_name,
                            context,
                            workflow,
                            step_index,
                            last_result,
                            context.iteration_max - iteration,
                        )
                        continue
                    raw_args = cast(Sequence[object], decision.action_params["script_args"])
                    args = tuple(str(item) for item in raw_args)
                    try:
                        green_result = self.green_checks.run(
                            script_path=green_script,
                            script_args=args,
                            expected_outputs=context.green_check_expected_outputs,
                            command_template=context.green_check_script.command,
                            workspace=workspace,
                            artifact_root=artifact_root,
                            timeout_seconds=self.green_check_timeout_seconds,
                            trusted_script_root=trusted_script_root,
                            expected_script_sha256=expected_script_sha256,
                        )
                    except GreenCheckError as exc:
                        last_result = f"Green check was not runnable: {exc}"
                    else:
                        preparation = str(decision.action_params["preparation_summary"])
                        last_result = (
                            "Green check passed with all expected outputs. Preparation: "
                            + preparation
                            if green_result.passed
                            else "Green check failed: exit "
                            f"{green_result.exit_code}; missing={list(green_result.missing_outputs)}; "
                            f"output={green_result.output[:2048]!r}"
                        )
                elif action == "finish_task":
                    if (
                        not workflow_complete
                        or green_result is None
                        or not green_result.passed
                    ):
                        last_result = (
                            "finish_task rejected: validate_green_check has not passed in this call."
                        )
                    else:
                        return outcome(
                            "completed", str(decision.action_params["task_result"]), iteration
                        )
                elif action == "fail_task":
                    reason = str(decision.action_params["failure_reason"])
                    last_result = "fail_task reported: " + reason
                    return outcome(
                        "failed",
                        f"agentic tool did not finish: {reason}; diagnose the issue and try again.",
                        iteration,
                    )
                elif action == "finalize_needs_user_permission":
                    permission_request = str(decision.action_params["permission_request"])
                    last_result = "User permission is still required: " + permission_request
                    return outcome(
                        "needs_user_permission",
                        "agentic tool did not finish: user permission required; get user "
                        "permission and try again.",
                        iteration,
                        user_text=permission_request,
                    )

                prompt = delta_prompt(
                    tool_name,
                    context,
                    workflow,
                    step_index,
                    last_result,
                    context.iteration_max - iteration,
                )
            return outcome(
                "iteration_limit",
                f"agentic tool did not finish: {last_result}; iteration_max reached; "
                "diagnose the issue and try again.",
                context.iteration_max,
            )
        except ProviderError as exc:
            return outcome(
                "provider_error",
                f"agentic tool did not finish: {exc}; diagnose the issue and try again.",
                0,
            )
        finally:
            termination_error: ProviderTerminationError | None = None
            try:
                if session is not None:
                    session.close()
            except ProviderTerminationError as exc:
                # A still-live middleman violates the per-call lifecycle contract. Its
                # marker remains intact so bounded startup cleanup can identify it.
                termination_error = exc
            except Exception:
                # Provider teardown is best-effort and must never replace the primary
                # tool outcome. Process-owning sessions already attempt termination in
                # their own finally path; the call-owned filesystem still must be removed.
                pass
            finally:
                if session_observer is not None:
                    session_observer(None)
            cleanup_error: CallArtifactCleanupError | None = None
            for call_root in (artifact_root, trusted_script_root):
                try:
                    _remove_call_artifacts(call_root)
                except CallArtifactCleanupError as exc:
                    cleanup_error = cleanup_error or exc
            if cleanup_error is not None:
                raise cleanup_error
            if termination_error is not None:
                raise termination_error
