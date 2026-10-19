"""Independent M05 client: consume public forms, never manufacture setup authority."""

from __future__ import annotations

import copy
import json

from tests.tiered_acceptance_support import public_text


NULL_PLAN = dict.fromkeys(
    (
        "board_id",
        "hypothesis",
        "strategy",
        "hypothesis_made",
        "strategy_evaluated",
        "expected_fail_return",
        "expected_success_return",
        "max_calls",
        "max_calls_buffer",
        "action_parameters",
        "user_permission",
    )
)


def body(text):
    return json.loads(text.split("\n", 1)[0])


def child(batch):
    item = batch["completed"][0] if batch["status"] == "batch_completed" else batch["failure"]
    result = item["result"]
    if isinstance(result, list):
        result = result[0]["text"]
    return body(result)


class PublicSetup:
    def __init__(self, server, *, call=None):
        self.server = server
        self.call = call or (lambda name, args: public_text(server, name, args))

    def json(self, name, args):
        return body(self.call(name, args))

    def overview(self, name):
        return self.json("setup_overview", {"board_names": [name]})

    def accept(self, route, pdf, part, *, tier="setup-lite"):
        board = route["board_id"]
        self.json("load_setup_tool", {"board_id": board, "tool_name": "board_setup-plan"})
        self.call("board_setup-plan", NULL_PLAN)
        action = {
            **route["plan_action_parameters_template"],
            "mcu_part_number": part,
            "target_tier": tier,
            "requires_uart": False,
            "serial_baudrate": None,
            "serial_id": None,
            "datasheet_path": str(pdf),
        }
        plan = self.plan("board_setup", board, action, permission="one-time")
        assert plan["status"] == "plan_accepted", plan
        return action, plan

    def plan(self, action, board, parameters, *, permission=None):
        args = {
            "board_id": board,
            "hypothesis": "The stated bounded operation matches the supplied local evidence.",
            "strategy": "Execute the returned public fallback once and inspect its result.",
            "hypothesis_made": True,
            "strategy_evaluated": True,
            "expected_fail_return": "A truthful refusal with no unauthorized target effect.",
            "expected_success_return": "The requested bounded operation completes.",
            "max_calls": 1,
            "max_calls_buffer": 0,
            "action_parameters": parameters,
        }
        if permission is not None:
            args["user_permission"] = permission
        return self.json(action + "-plan", args)

    def execute(self, call):
        return self.json(call["tool_name"], call["arguments"])

    def start(self, plan):
        return child(self.execute(plan["stable_client_fallback"]))

    def fix(self, plan):
        return child(self.execute(plan["paired_action_fallbacks"][0]["call"]))

    def answer(self, board, pending, response):
        return self.json(
            "continue_setup",
            {
                "board_id": board,
                "continuation_id": pending["continuation_id"],
                "response": response,
            },
        )

    @staticmethod
    def target_answer(pending, target):
        form = next(
            a
            for a in pending["accepted_response"]["alternatives"]
            if target in a["eligible_targets"]
        )
        response = copy.deepcopy(form["response"])
        response.update(
            pyocd_target=target,
            evidence=[
                {
                    "source": "Installed pyOCD device class and vendor part declaration",
                    "claim": "The target supports the supplied device; CPUID is core evidence only.",
                }
            ],
            reasoning_summary="Preserve the supplied order code; compatible target metadata is not live package proof.",
        )
        return response

    @staticmethod
    def ram_answer(
        pending, *, start=0x20000000, end=0x20001000, pages=(1,), note="Synthetic RAM evidence"
    ):
        form = pending["accepted_response"]
        response = copy.deepcopy(form["response_example"])
        assert set(response) == set(form["response_schema"]["required"])
        region = response["regions"][0]
        assert set(region) == set(
            form["response_schema"]["properties"]["regions"]["items"]["required"]
        )
        region.update(
            name="Confirmed scratch RAM",
            kind="ram",
            start=start,
            end=end,
            readable=True,
            writable=True,
            executable=False,
            source_pages=list(pages),
            source_note=note,
        )
        response["flash"] = {"backend_target": None, "erase_sectors": []}
        response["recovery"] = None
        return response
