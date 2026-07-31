"""Decide what an outcome *was*, before anything decides whether to report it.

This server refuses on purpose, constantly, and correctly. Locked handlers,
all-NULL plan guidance, closed validation gates, containment rejections, digest
drift, exhausted budgets, and the ``no board`` sentinel are all the product
working as designed. A monitor built on "error or refusal implies issue" would
file hundreds of correct refusals on the first real session and bury the signal.

So the central rule is: **a refusal that names a remedy is not an issue. An issue
is when the remedy is absent, wrong, unreachable, or when following it does not
converge.** Everything below is written against that rule, as explicit tables
rather than heuristics.
"""

from __future__ import annotations

import json
import re
from enum import Enum

from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.monitor.redaction import normalize_signature

# Held locally rather than imported from ``server``: server imports this module,
# so importing back would be circular. A test asserts this stays a substring of
# ``server.NO_BOARD_CONFIG_MESSAGE`` so the two cannot drift apart silently.
NO_BOARD_FRAGMENT = "No project board profile is loaded"

_REFUSAL_PREFIX = "Refused ["
_REFUSAL_CODE = re.compile(r"^Refused \[([^\]]+)\]:\s*(.*)", re.DOTALL)
_CALL_FIRST = re.compile(r"Call '([^']+)' first", re.IGNORECASE)
_LOCKED_TOOL = re.compile(r"^Tool '.+' is locked")
_UNKNOWN_TOOL = re.compile(r"^Unknown tool: ")
_NEEDS_BOARD = re.compile(r"^Guarded tool '.+' requires a non-empty board_id")
_NO_FINALIZER = re.compile(r"^Tool '.+' cannot accept an on_exit finalizer")
# The framework validates a tool's arguments before the handler runs, so a badly
# formed call arrives as a validation failure rather than a handler refusal.
_BAD_ARGUMENTS = re.compile(r"validation error[s]? for ", re.IGNORECASE)


class Outcome(str, Enum):
    SUCCESS = "success"
    POLICY_REFUSAL = "policy_refusal"
    UNEXPECTED_ERROR = "unexpected_error"


class TriageClass(str, Enum):
    """Without this separation the team chases code changes for USB faults."""

    SERVER_DEFECT = "server_defect"
    PRODUCT_FEEDBACK = "product_feedback"
    ENVIRONMENT_FAULT = "environment_fault"
    AGENT_BEHAVIOR = "agent_behavior"
    SOFT_SIGNAL = "soft_signal"
    NONE = "none"


class Signal(str, Enum):
    RUNTIME_ERROR = "S-1"
    THRASHING = "S-2"
    ENVIRONMENT_FAULT = "S-3"
    PLAN_PROTOCOL = "S-4"
    DISCOVERY_BINDING = "S-5"
    GUIDANCE = "S-6"
    REMEDY_DEAD_END = "S-7"
    COVERAGE_GAP = "S-8"
    UNUSABLE_OUTPUT = "S-9"
    SAFETY_SURPRISE = "S-10"
    ABANDONMENT = "S-11"
    RELAY_BOUNDARY = "S-12"
    FRUSTRATION = "S-13"
    EXPLICIT_REPORT = "S-14"


MODEL_SIGNALS = frozenset(
    {
        Signal.PLAN_PROTOCOL,
        Signal.DISCOVERY_BINDING,
        Signal.GUIDANCE,
        Signal.REMEDY_DEAD_END,
        Signal.COVERAGE_GAP,
        Signal.UNUSABLE_OUTPUT,
        Signal.SAFETY_SURPRISE,
        Signal.ABANDONMENT,
        Signal.RELAY_BOUNDARY,
        Signal.FRUSTRATION,
        Signal.EXPLICIT_REPORT,
    }
)

SUBCASE_REQUIRED: dict[Signal, tuple[str, ...]] = {
    Signal.GUIDANCE: ("ignored_usable_guidance", "guidance_was_unusable"),
    Signal.REMEDY_DEAD_END: (
        "no_remedy",
        "remedy_repeated_same_refusal",
        "cycle_never_converged",
    ),
}

TRIAGE_FOR_SIGNAL: dict[Signal, TriageClass] = {
    Signal.RUNTIME_ERROR: TriageClass.SERVER_DEFECT,
    Signal.THRASHING: TriageClass.AGENT_BEHAVIOR,
    Signal.ENVIRONMENT_FAULT: TriageClass.ENVIRONMENT_FAULT,
    Signal.PLAN_PROTOCOL: TriageClass.AGENT_BEHAVIOR,
    Signal.DISCOVERY_BINDING: TriageClass.AGENT_BEHAVIOR,
    Signal.GUIDANCE: TriageClass.AGENT_BEHAVIOR,
    Signal.REMEDY_DEAD_END: TriageClass.SERVER_DEFECT,
    Signal.COVERAGE_GAP: TriageClass.PRODUCT_FEEDBACK,
    Signal.UNUSABLE_OUTPUT: TriageClass.SERVER_DEFECT,
    Signal.SAFETY_SURPRISE: TriageClass.PRODUCT_FEEDBACK,
    Signal.ABANDONMENT: TriageClass.AGENT_BEHAVIOR,
    Signal.RELAY_BOUNDARY: TriageClass.AGENT_BEHAVIOR,
    Signal.FRUSTRATION: TriageClass.SOFT_SIGNAL,
    Signal.EXPLICIT_REPORT: TriageClass.SOFT_SIGNAL,
}

SEVERITY_FOR_SIGNAL: dict[Signal, str] = {
    Signal.RUNTIME_ERROR: "error",
    Signal.THRASHING: "warning",
    Signal.ENVIRONMENT_FAULT: "warning",
    Signal.PLAN_PROTOCOL: "warning",
    Signal.DISCOVERY_BINDING: "warning",
    Signal.GUIDANCE: "warning",
    Signal.REMEDY_DEAD_END: "error",
    Signal.COVERAGE_GAP: "info",
    Signal.UNUSABLE_OUTPUT: "warning",
    Signal.SAFETY_SURPRISE: "info",
    Signal.ABANDONMENT: "error",
    Signal.RELAY_BOUNDARY: "warning",
    Signal.FRUSTRATION: "info",
    Signal.EXPLICIT_REPORT: "info",
}


def _policy_refusal_types() -> tuple[type[BaseException], ...]:
    """Collect refusal types lazily to keep import order free of cycles."""

    types: list[type[BaseException]] = []
    from pyocd_debug_mcp.services.connections import BoardNotConnectedError
    from pyocd_debug_mcp.services.session_runtime import PolicyRefusal

    types.append(PolicyRefusal)  # covers PlanRefusal
    types.append(BoardNotConnectedError)  # names its remedy: connect first
    try:
        from pyocd_debug_mcp.kernel.operations import BoardBusyError

        types.append(BoardBusyError)
    except ImportError:  # pragma: no cover - kernel is always importable in practice
        pass
    try:
        from pyocd_debug_mcp.setup_flow.setup import SetupWorkflowError

        types.append(SetupWorkflowError)
    except ImportError:  # pragma: no cover
        pass
    try:
        from pyocd_debug_mcp.tools.batch import BatchValidationError

        types.append(BatchValidationError)
    except ImportError:  # pragma: no cover
        pass
    try:
        from pyocd_debug_mcp.tools.registers import RegisterPreconditionError

        types.append(RegisterPreconditionError)
    except ImportError:  # pragma: no cover
        pass
    return tuple(types)


def _environment_types() -> tuple[type[BaseException], ...]:
    """Failures whose evidence points at the host or the board, not the code.

    ``BoardNotConnectedError`` is deliberately absent: its message names its own
    remedy ("call connect first"), so it is a correct precondition refusal rather
    than a hardware fault. Treating it as one would file an environment report
    every time an agent touches a tool before connecting.
    """

    types: list[type[BaseException]] = []
    from pyocd_debug_mcp.target_errors import (
        LockedTargetError,
        ProbeNotFoundError,
        TargetConnectionError,
    )

    types.extend([ProbeNotFoundError, LockedTargetError, TargetConnectionError])
    try:
        from serial import SerialException  # type: ignore[import-untyped]

        types.append(SerialException)
    except ImportError:  # pragma: no cover
        pass
    return tuple(types)


_policy_cache: tuple[type[BaseException], ...] | None = None
_environment_cache: tuple[type[BaseException], ...] | None = None


def _policy_types() -> tuple[type[BaseException], ...]:
    global _policy_cache
    if _policy_cache is None:
        _policy_cache = _policy_refusal_types()
    return _policy_cache


def _env_types() -> tuple[type[BaseException], ...]:
    global _environment_cache
    if _environment_cache is None:
        _environment_cache = _environment_types()
    return _environment_cache


def error_code(exc: BaseException) -> str:
    """Return the stable error code the server records in its event log.

    These strings are written into durable evidence, so they must stay
    byte-identical to what the server produced before this module owned them.
    """

    from pyocd_debug_mcp.services.connections import BoardNotConnectedError
    from pyocd_debug_mcp.target_errors import (
        LockedTargetError,
        ProbeNotFoundError,
        ReferenceArtifactError,
        SymbolLookupError,
        TargetConnectionError,
        UnsupportedArtifactError,
    )

    if isinstance(exc, ProbeNotFoundError):
        return "probe/not-found"
    if isinstance(exc, LockedTargetError):
        return "target/locked"
    if isinstance(exc, TargetConnectionError):
        return "target/connection-failure"
    if isinstance(exc, UnsupportedArtifactError):
        return "flash/unsupported-artifact"
    if isinstance(exc, ReferenceArtifactError):
        return "flash/reference-artifact"
    if isinstance(exc, SymbolLookupError):
        return "symbols/lookup-failure"
    if isinstance(exc, BoardNotConnectedError):
        return "server/not-connected"
    return f"runtime/{type(exc).__name__}"


def error_signature(exc: BaseException) -> str:
    """Return a normalized signature stable enough to group across runs."""

    kind = f"{type(exc).__module__}.{type(exc).__name__}"
    return f"{kind}: {normalize_signature(str(exc))}"


def _tool_error_classification(
    message: str,
) -> tuple[Outcome, TriageClass, str] | None:
    """Classify the ToolErrors that dispatch itself raises.

    All of these reach the classifier because observation begins before the
    locked-handler check. Any that fell through to the default branch would file
    as a server defect -- false entries straight through the criterion that a full
    session of correct guarded behavior must produce zero server-defect reports.
    """

    if _LOCKED_TOOL.match(message):
        return Outcome.POLICY_REFUSAL, TriageClass.NONE, "handler/locked"
    if _UNKNOWN_TOOL.match(message):
        # The agent called something unlisted: discovery/binding, not a defect.
        return Outcome.POLICY_REFUSAL, TriageClass.AGENT_BEHAVIOR, "handler/unknown-tool"
    if _NEEDS_BOARD.match(message):
        return Outcome.POLICY_REFUSAL, TriageClass.NONE, "handler/board-required"
    if _NO_FINALIZER.match(message):
        return Outcome.POLICY_REFUSAL, TriageClass.NONE, "handler/no-finalizer"
    if message.startswith(_REFUSAL_PREFIX) or NO_BOARD_FRAGMENT in message:
        return Outcome.POLICY_REFUSAL, TriageClass.NONE, "handler/refused"
    return None


def _is_argument_validation(exc: BaseException) -> bool:
    """Return whether this is the framework rejecting a call's arguments."""

    if type(exc).__name__ == "ValidationError":
        return True
    return isinstance(exc, ToolError) and bool(_BAD_ARGUMENTS.search(str(exc)))


def _causes(exc: BaseException, limit: int = 8) -> list[BaseException]:
    """Return the exception and its ``__cause__`` chain, outermost first."""

    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(chain) < limit and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__
    return chain


def classify_exception(exc: BaseException) -> tuple[Outcome, TriageClass, str]:
    """Return the outcome, triage class, and error class for a raised exception.

    The whole chain is examined, not just the outermost exception. Layer-2 tools
    re-raise their failures as ``ToolError`` (and the framework wraps once more
    on the way out), so by the time dispatch sees it the real failure is two
    ``__cause__`` levels down. Classifying only the outer wrapper would read every
    provider fault, deadline termination, and unplugged probe in a hardware tool
    as a correct refusal, and none of them would ever be reported.
    """

    chain = _causes(exc)
    policy_types = _policy_types()
    env_types = _env_types()
    from pyocd_debug_mcp.kernel.operations import (
        OperationCleanupError,
        OperationTimeoutError,
    )

    # A refusal anywhere in the chain is a refusal: the wrapping is transport,
    # not meaning.
    for item in chain:
        if isinstance(item, policy_types):
            code = getattr(item, "code", None)
            return (
                Outcome.POLICY_REFUSAL,
                TriageClass.NONE,
                str(code or error_code(item)),
            )
    # A malformed call is the agent getting the arguments wrong, not the server
    # breaking. Classifying it as a defect would file a false report for every
    # ordinary argument mistake an agent makes, on every tool.
    for item in chain:
        if _is_argument_validation(item):
            return (
                Outcome.POLICY_REFUSAL,
                TriageClass.AGENT_BEHAVIOR,
                "handler/invalid-arguments",
            )
    for item in chain:
        if isinstance(item, (OperationTimeoutError, OperationCleanupError)):
            # Deadline termination of an unreturned worker, or a cleanup failure
            # that could not confirm it released what it owned.
            return Outcome.UNEXPECTED_ERROR, TriageClass.SERVER_DEFECT, error_code(item)
    for item in chain:
        if isinstance(item, env_types):
            return (
                Outcome.UNEXPECTED_ERROR,
                TriageClass.ENVIRONMENT_FAULT,
                error_code(item),
            )
    # Only dispatch-raised ToolErrors remain ambiguous; classify by their shape.
    for item in chain:
        if isinstance(item, ToolError):
            classified = _tool_error_classification(str(item))
            if classified is not None:
                return classified
    root = chain[-1]
    if isinstance(root, ToolError):
        # A bare ToolError with no cause is an argument or shape rejection from
        # the handler surface, not a defect.
        return Outcome.POLICY_REFUSAL, TriageClass.NONE, "handler/rejected"
    return Outcome.UNEXPECTED_ERROR, TriageClass.SERVER_DEFECT, error_code(root)


def classify_result(text: str) -> tuple[Outcome, str | None, str | None]:
    """Classify a non-error return value.

    Refusal is a first-class normal output here and arrives as a structured
    payload as often as an exception, so a monitor that only watches exceptions
    misses most of what this server says.

    Returns ``(outcome, error_class, remedy)``.
    """

    if not text:
        return Outcome.SUCCESS, None, None
    stripped = text.lstrip()
    if stripped.startswith(_REFUSAL_PREFIX):
        match = _REFUSAL_CODE.match(stripped)
        code = match.group(1) if match else "policy/refused"
        return Outcome.POLICY_REFUSAL, code, _named_remedy(text)
    if NO_BOARD_FRAGMENT in text:
        return Outcome.POLICY_REFUSAL, "server/no-board", _named_remedy(text)
    payload = _maybe_json(stripped)
    if isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, str) and (
            status.endswith("_refused") or status.endswith("_rejected")
        ):
            remedy = payload.get("remedy") or payload.get("next_step")
            return (
                Outcome.POLICY_REFUSAL,
                status,
                remedy if isinstance(remedy, str) else _named_remedy(text),
            )
    return Outcome.SUCCESS, None, None


def _maybe_json(text: str) -> object:
    if not text.startswith("{"):
        return None
    for candidate in (text, text.split("\n", 1)[0]):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _named_remedy(text: str) -> str | None:
    """Extract the remedy a refusal names, if it names one.

    Whether a remedy is present is the whole classification rule, so this is
    recorded on every refusal rather than only on reported ones.
    """

    match = _CALL_FIRST.search(text)
    if match:
        return f"call {match.group(1)}"
    for marker in ("remedy:", "Next step:", "next_step"):
        index = text.find(marker)
        if index >= 0:
            return text[index + len(marker) : index + len(marker) + 200].strip() or None
    return None


__all__ = [
    "MODEL_SIGNALS",
    "NO_BOARD_FRAGMENT",
    "SEVERITY_FOR_SIGNAL",
    "SUBCASE_REQUIRED",
    "TRIAGE_FOR_SIGNAL",
    "Outcome",
    "Signal",
    "TriageClass",
    "classify_exception",
    "classify_result",
    "error_code",
    "error_signature",
]
