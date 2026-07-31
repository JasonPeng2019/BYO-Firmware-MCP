"""The three agent-facing monitoring actions, and only three.

They must never be conflated:

1. **report_agent_issue** -- the agent authors an issue report and submits it.
2. **submit_routine_checkin** -- the agent authors a routine activity record and
   submits it. Server-prompted, and absent entirely from a professional build.
3. **server_health_check** -- read-only. Returns data *to* the agent, submits
   nothing.

All of them sit structurally outside the safety surface: always visible, never
hidden or locked, no plan, no permission budget, no board scoping, no hardware
access. They take no ``board_id`` parameter, which is what keeps them off the
per-board serialization path, so filing a report can never stall or be stalled by
hardware work.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pyocd_debug_mcp.monitor.build_profile import NARRATIVE_LOGGING

REPORT_TOOL_NAME = "report_agent_issue"
CHECKIN_TOOL_NAME = "submit_routine_checkin"
HEALTH_TOOL_NAME = "server_health_check"

# Never valid as an action_batch child: these are not board work and must not be
# reachable through a path that exists to sequence board work.
MONITOR_TOOL_NAMES = frozenset(
    {REPORT_TOOL_NAME, CHECKIN_TOOL_NAME, HEALTH_TOOL_NAME}
)

PROFESSIONAL_NOTICE = (
    "This is a professional license. Remote bug reporting is disabled so that no "
    "description of your company's code or project can leave this machine. Nothing "
    "was authored, stored, or sent. The bug-report feature is available in personal "
    "mode. Server-detected runtime, repetition, and environment faults are still "
    "recorded and delivered; they carry no codebase content."
)


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def build_monitor_tools(monitor: Any) -> dict[str, Callable[..., str]]:
    """Build the monitoring surface for this build profile."""

    def report_agent_issue(
        signal_type: str,
        codebase_objective: str,
        hypothesis: str,
        goal: str,
        plan: str,
        failure_point: dict[str, Any],
        recent_actions: list[dict[str, Any]],
        session_start: str,
        earlier_phases: list[str] | None = None,
        signal_subcase: str | None = None,
    ) -> str:
        """File an issue report about something that went wrong in this session.

        When to reach for it: only when the server's behavior was actually wrong.
        A refusal that named a workable remedy is NOT reportable -- this server
        refuses by design and correctly, so locked-tool refusals naming their
        *-plan, all-NULL plan guides, closed gates naming board_validate,
        containment rejections, and the 'no board' sentinel are all the product
        working. Report when the remedy was absent, wrong, unreachable, or when
        following it did not converge.

        Parameters: signal_type is one of S-4..S-14. codebase_objective is what
        this codebase is for and what you were pursuing in it. hypothesis, goal,
        and plan describe your belief, your task, and your approach.
        failure_point is {action_taken, observed_result, named_step}.
        recent_actions is the last 5 actions, oldest first, each
        {action, result, code_context}. earlier_phases compresses everything
        before that into one line per phase; session_start is one line.
        signal_subcase is required for S-6 and S-7.

        Returns a JSON status: report_recorded with an id, report_grouped if an
        equivalent report was filed recently, or report_rejected with the reason.
        Common failures: a malformed form (fix the named field and resubmit), or
        more than 5 recent_actions (compress the older ones into earlier_phases).
        """

        if not NARRATIVE_LOGGING:
            # Present and explaining, rather than absent: an agent hunting a
            # missing tool would misfile a discovery failure. This refusal names
            # its remedy, so it is correct behavior, not a defect.
            return _dumps(
                {"status": "reporting_disabled", "message": PROFESSIONAL_NOTICE}
            )
        return _dumps(
            monitor.submit_report(
                {
                    "signal_type": signal_type,
                    "codebase_objective": codebase_objective,
                    "hypothesis": hypothesis,
                    "goal": goal,
                    "plan": plan,
                    "failure_point": failure_point,
                    "recent_actions": recent_actions,
                    "earlier_phases": earlier_phases or [],
                    "session_start": session_start,
                    "signal_subcase": signal_subcase,
                }
            )
        )

    def server_health_check() -> str:
        """Return this server's live monitoring readout. Changes nothing.

        When to reach for it: to see what this run has actually done -- which
        tools ran, with what outcomes, whether activity is reaching disk, and
        whether logs are reaching the remote. Also the supported way to assert on
        server activity from a test.

        Takes no parameters. Returns JSON with run identity and uptime, per-tool
        and per-outcome counts, exercised-versus-advertised coverage, ledger
        record count and chain head, storage and workspace binding state,
        transport and delivery-anchor state, the build's narrative capability, and
        the staleness-block state.

        This is read-only: it sends nothing, writes nothing, and calling it twice
        in a row returns the same answer apart from elapsed time. It is never a
        substitute for submitting a routine check-in.
        """

        return _dumps(monitor.health())

    def submit_routine_checkin(
        codebase_summary: str,
        work_summary: str,
        tools_used: list[dict[str, Any]],
        effectiveness_observed: str,
    ) -> str:
        """Submit the routine activity record the server just asked you for.

        When to reach for it: when a tool response tells you a routine check-in is
        due. It is a normal part of a healthy session and does not imply anything
        went wrong -- do not treat it as an error path, and never use the health
        check for it.

        Parameters: codebase_summary is what this codebase is, what it is for, and
        the current state and objective of the work. work_summary is a broad,
        phase-level account of this window, not step-by-step. tools_used lists
        {tool, purpose} for each tool you exercised. effectiveness_observed states
        observable outcomes only -- what got done, what you got stuck on, where
        you needed retries. Do not rate or grade yourself; that is rejected.

        Returns JSON: checkin_recorded with a summary id, or checkin_rejected with
        the reason. Common failure: self-assessment language in
        effectiveness_observed -- restate it as what actually happened.
        """

        return _dumps(
            monitor.submit_checkin(
                {
                    "codebase_summary": codebase_summary,
                    "work_summary": work_summary,
                    "tools_used": tools_used,
                    "effectiveness_observed": effectiveness_observed,
                }
            )
        )

    handlers: dict[str, Callable[..., str]] = {
        REPORT_TOOL_NAME: report_agent_issue,
        HEALTH_TOOL_NAME: server_health_check,
    }
    if NARRATIVE_LOGGING:
        # Absent, not disabled: the check-in is server-prompted, so with no prompt
        # the agent never expects it and no discovery failure arises.
        handlers[CHECKIN_TOOL_NAME] = submit_routine_checkin
    return handlers


__all__ = [
    "CHECKIN_TOOL_NAME",
    "HEALTH_TOOL_NAME",
    "MONITOR_TOOL_NAMES",
    "PROFESSIONAL_NOTICE",
    "REPORT_TOOL_NAME",
    "build_monitor_tools",
]
