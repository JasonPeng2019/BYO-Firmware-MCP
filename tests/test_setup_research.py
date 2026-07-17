from __future__ import annotations

import pytest

from pyocd_debug_mcp.setup_flow.preflight import NO_INTERNALS_RELAY_INSTRUCTION
from pyocd_debug_mcp.setup_flow.research import (
    ResearchError,
    ResearchTracker,
    ValidationOutcome,
    classify_research_condition,
    make_research_request,
)


def request():
    return make_research_request(
        fact_id="target",
        continuation_token="continue-1",
        board_id="bench_board",
        mcu_part_number="STM32L476RGT6-Exact",
        unresolved_fact="The exact pyOCD target is unknown.",
        requested_fields=("pyocd_target",),
        authoritative_facts={"probe_family": "stlink"},
        observed_output=({"detected": []},),
        acceptable_sources=("official vendor CMSIS-Pack",),
        validation_plan=("enumerate target", "live connect"),
    )


def test_research_prompt_is_self_contained_and_has_no_authority() -> None:
    tracker = ResearchTracker()
    prompt = tracker.prompt(request())

    assert prompt["status"] == "setup_research_required"
    assert prompt["continuation_token"] == "continue-1"
    assert prompt["authoritative_facts"]["mcu_part_number"] == "STM32L476RGT6-Exact"
    assert prompt["exact_response_fields"] == ["pyocd_target"]
    assert prompt["fields_that_must_not_change"] == ["mcu_part_number"]
    assert prompt["user_approval_default"] == "no"
    assert prompt["observed_output"] == [{"detected": []}]
    assert prompt["acceptable_sources"]
    assert prompt["validation_plan"]
    assert NO_INTERNALS_RELAY_INSTRUCTION in prompt["agent_prompt"]
    assert not {"gate", "permission", "plan_grant"}.intersection(prompt)


def test_reply_rejects_unrequested_fields_and_attempted_part_change() -> None:
    tracker = ResearchTracker()

    def validator(_candidate):
        return ValidationOutcome(True)

    with pytest.raises(ResearchError) as extra:
        tracker.validate_reply(
            request(), {"pyocd_target": "stm32l476rgtx", "source": "blog"}, validator
        )
    assert extra.value.code == "research/field-set-mismatch"

    with pytest.raises(ResearchError) as changed:
        tracker.validate_reply(
            request(),
            {
                "pyocd_target": "stm32l476rgtx",
                "mcu_part_number": "STM32L476RGT6",
            },
            validator,
        )
    assert changed.value.code == "research/immutable-field"


def test_duplicate_candidate_is_not_revalidated_and_rejection_is_replayed() -> None:
    tracker = ResearchTracker()
    calls = 0

    def reject(_candidate):
        nonlocal calls
        calls += 1
        return ValidationOutcome(False, "not supported", {"targets": []})

    first = tracker.validate_reply(request(), {"pyocd_target": "made_up"}, reject)
    duplicate = tracker.validate_reply(request(), {"pyocd_target": "made_up"}, reject)

    assert calls == 1
    assert duplicate.duplicate
    assert duplicate.failure == first.failure
    assert tracker.prompt(request())["rejected_candidates"][0]["reason"] == "not supported"


def test_third_distinct_failure_exhausts_fact_budget() -> None:
    tracker = ResearchTracker()
    statuses = [
        tracker.validate_reply(
            request(),
            {"pyocd_target": f"candidate_{index}"},
            lambda _candidate: ValidationOutcome(False, "failed"),
        ).status
        for index in range(3)
    ]
    assert statuses == [
        "setup_research_required",
        "setup_research_required",
        "setup_unresolved",
    ]


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("missing_probe", "setup_blocked"),
        ("locked_target", "setup_blocked"),
        ("probe_disconnected", "setup_blocked"),
        ("unknown_target", "research"),
    ],
)
def test_blocked_conditions_are_not_misclassified_as_research(condition: str, expected: str) -> None:
    assert classify_research_condition(condition) == expected
